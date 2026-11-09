"""Run a generated bundle: on this machine, or on a remote server over SSH."""
from __future__ import annotations

import contextlib
import os
import shlex
import shutil
import tarfile
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Iterator

from . import deployments, journal, ops, providers, runner, transport
from .core import paths
from .core.errors import DeployError



def home_dir() -> Path:
    """Kept for compatibility; the location logic lives in core.paths."""
    return paths.home()


def _stream(cmd: list[str], cwd: str | None = None, env: dict | None = None, *, stop: threading.Event | None = None,
            redact: Callable[[str], str] | None = None, deadline: float | None | str = "default") -> Iterator[str]:
    """Run a command through the central runner: bounded, cancellable, whole process group killed, secrets masked."""
    yield from runner.stream(cmd, cwd=cwd, env=env, stop=stop, redact=redact, error=DeployError,
                             deadline=runner.default_deadline() if deadline == "default" else deadline,  # type: ignore[arg-type]
                             idle=runner.default_idle())


def preflight(target: str, mode: str) -> list[str]:
    """Return a list of blocking problems (empty = OK)."""
    problems: list[str] = []
    if mode == "local":
        if target == "native" and os.geteuid() != 0:
            problems.append("native ინსტალაციას root სჭირდება (გაუშვი sudo-ით)")
        if target == "k8s" and not shutil.which("kubectl"):
            problems.append("kubectl ვერ მოიძებნა")
        if target == "compose" and not (shutil.which("docker") or shutil.which("podman")):
            problems.append("docker ან podman ვერ მოიძებნა")
    else:
        if not shutil.which("ssh") or not shutil.which("tar"):
            problems.append("ssh და tar საჭიროა")
    return problems


_ssh_base = transport.ssh_base          # compatibility alias; new code uses transport.ssh_base


# Components the cleanup can target (catalog ids); every native/compose/k8s name derives from these.
CLEAN_COMPONENTS = list(providers.common.ORDER)
_SHORT = {"qgis_server": "qgis"}   # name used by services / units / k8s objects


def _provider(target: str) -> providers.Provider:
    return providers.get(target)


def clean_args(target: str, components: list[str] | None, purge: bool = False,
               remove_postgres: bool = False, force_postgres: bool = False) -> list[str]:
    """Arguments for the bundle's cleanup entry point (uninstall.sh / up.sh clean / apply.sh clean).

    components=None means the whole stack."""
    prov = _provider(target)
    comps = ["qgis_server" if c == "qgis" else c for c in (components or [])]   # accept the short name too
    for c in comps:
        if c not in CLEAN_COMPONENTS:
            raise DeployError(f"უცნობი კომპონენტი: {c}")
    short = [_SHORT.get(c, c) for c in comps]
    return prov.clean_args(short, purge, remove_postgres, force_postgres)


REMOTE_LOCK = "flock -n -E 75 /tmp/gisstack-op.lock"      # exit 75 = somebody else is running an operation on that server
LOCK_EXIT = 75


def _remote_lock_prefix() -> str:
    """Shell fragment that serialises operations on one server; empty where flock is missing (the run is then unlocked)."""
    return f"$(command -v flock >/dev/null 2>&1 && echo '{REMOTE_LOCK}') "


def _run_entry(cfg: dict[str, Any], bundle: Path, target: str, mode: str, ssh: dict[str, Any] | None,
               entry: str, args: list[str], op: str = "run") -> Iterator[str]:
    """Run <bundle>/<target>/<entry> <args> here (mode=local) or on a server (mode=ssh).

    The operation is journalled, serialised (local flock / remote flock), bounded and its output is scrubbed of secrets."""
    mask = runner.redactor(runner.secrets_of(cfg))
    host = "localhost" if mode == "local" else str((ssh or {}).get("host", ""))
    with journal.Operation(op, redact=mask, target=target, mode=mode, host=host, project=cfg.get("project", ""), entry=entry):
        if mode == "local":
            yield f"== ლოკალური გაშვება: {target} ({entry})"
            env = dict(os.environ)
            if cfg.get("runtime"):
                env["RUNTIME"] = cfg["runtime"]
            try:
                with journal.exclusive():
                    yield from _stream(["bash", entry, *args], cwd=str(bundle / target), env=env, redact=mask)
            except journal.Busy as e:
                raise DeployError(str(e)) from e
            yield "== დასრულდა"
            return

        if mode != "ssh":
            raise DeployError("უცნობი რეჟიმი")
        ssh = ssh or {}
        base = _ssh_base(ssh)
        project = cfg["project"]
        remote_dir = f"gisstack-{project}"
        yield f"== SSH: {ssh['user']}@{ssh['host']} -> ~/{remote_dir}/{target}"

        # 1. upload (tar over ssh: no scp/rsync dependency)
        with tempfile.NamedTemporaryFile(suffix=".tgz", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            with tarfile.open(tmp_path, "w:gz") as tf:
                tf.add(bundle / target, arcname=target)   # webmap etc. are already inside <target>/
            yield "ბანდლის ატვირთვა…"
            with open(tmp_path, "rb") as fh:
                up = f"umask 077 && mkdir -p {remote_dir} && tar xzf - -C {remote_dir}"
                p = runner.run(base + [up], timeout=runner.default_deadline() or 3600, stdin=fh, error=DeployError)
            if p.returncode != 0:
                raise DeployError("ატვირთვა ვერ მოხერხდა: " + mask((p.stderr or p.stdout).strip()))
        finally:
            os.unlink(tmp_path)

        # 2. run (under the server-side lock)
        sudo = "" if ssh.get("user", "root") == "root" else "sudo -n "
        argv = " ".join(shlex.quote(a) for a in args)
        lock = _remote_lock_prefix()
        if target in ("compose", "k8s") and sudo:
            sudo = ""
        run = f"cd {remote_dir}/{target} && chmod +x {entry} && RUNTIME={cfg['runtime']} {sudo}{lock}./{entry} {argv}".rstrip()
        try:
            yield from _stream(base + [run], redact=mask)
        except DeployError as e:
            if f"exit {LOCK_EXIT}" in str(e):
                raise DeployError("სხვა gis-stack ოპერაცია უკვე მიმდინარეობს ამ სერვერზე") from e
            raise
        finally:
            if target == "native":
                # the native installer has copied everything it needs; do not leave stack.env / passwords in the home directory
                with contextlib.suppress(Exception):
                    runner.run(base + [f"rm -rf -- {remote_dir}/{target}"], timeout=60, error=DeployError)
        yield "== დასრულდა"


def _check_bundle(bundle: Path, target: str, mode: str) -> None:
    _provider(target)
    if not (bundle / target).is_dir():
        raise DeployError(f"bundle-ში არ არის {target}/")
    problems = preflight(target, mode)
    if problems:
        raise DeployError("; ".join(problems))


def deploy(cfg: dict[str, Any], bundle_dir: str | Path, target: str, mode: str,
           ssh: dict[str, Any] | None = None, extra_args: list[str] | None = None, *, kind: str = "deploy", rollback_of: str = "") -> Iterator[str]:
    """Yield log lines. Raises DeployError on failure.  extra_args go to the entry script (native: install.sh).

    The run is recorded (`deployments`): the state before, the outcome, and - unless GISSTACK_VERIFY=0 - the health of every program
    afterwards.  A deployment that ran but is not healthy is *reported* (a warning line, outcome `unhealthy`), not turned into an exception:
    the script succeeded, and the record is what a rollback decision is based on."""
    bundle = Path(bundle_dir)
    _check_bundle(bundle, target, mode)
    verify = os.environ.get("GISSTACK_VERIFY") != "0"
    host = transport.host_label(mode, ssh)

    def rows() -> list[dict[str, Any]]:
        return list(ops.status(cfg, mode, ssh, target)["rows"])
    mask = runner.redactor(runner.secrets_of(cfg))
    try:
        rec = deployments.begin(cfg, target, mode, host, rows if verify else None, kind=kind, rollback_of=rollback_of)
    except OSError as e:                                  # the history is unwritable: deploy anyway, without a record, and say so
        yield f"!! განთავსების ჩანაწერი ვერ შეიქმნა: {e.strerror or type(e).__name__}"
        yield from _run_entry(cfg, bundle, target, mode, ssh, _provider(target).entry, list(extra_args or []), op="rollback" if kind == "rollback" else "deploy")
        return
    try:
        yield from _run_entry(cfg, bundle, target, mode, ssh, _provider(target).entry, list(extra_args or []), op="rollback" if kind == "rollback" else "deploy")
    except GeneratorExit:
        deployments.finish(rec, "aborted")
        raise
    except BaseException as e:
        deployments.finish(rec, "failed", mask(f"{type(e).__name__}: {e}"))
        raise
    if not verify:
        deployments.finish(rec, "unverified")
        return
    yield "== შემოწმება: პროგრამები გაშვებულია?"
    h = deployments.verify(cfg, rows)
    deployments.finish(rec, {"healthy": "ok", "unhealthy": "unhealthy"}.get(h["state"], "unverified"), h.get("error", ""), h["rows"])
    if h["state"] == "healthy":
        yield "== ყველა პროგრამა მუშაობს და შემოწმებები გავიდა"
    elif h["state"] == "unhealthy":
        for r in h["rows"]:
            if not r["ok"]:
                yield f"!! {r['id']}: {r['state']}" + (f" ({', '.join(r['failing'])})" if r["failing"] else "")
        yield "!! განთავსება დასრულდა, მაგრამ ყველაფერი ჯანმრთელი არ არის"
    else:
        yield "!! ჯანმრთელობა ვერ შემოწმდა: " + (h.get("error") or "პასუხი არ არის")


def clean(cfg: dict[str, Any], bundle_dir: str | Path, target: str, mode: str,
          ssh: dict[str, Any] | None = None, components: list[str] | None = None,
          purge: bool = False, remove_postgres: bool = False,
          force_postgres: bool = False) -> Iterator[str]:
    """Remove the whole stack (components=None) or just the listed components.

    purge=False keeps data (databases, GeoServer data dir, caches, volumes);
    purge=True deletes it too.  Yields log lines; raises DeployError on failure."""
    bundle = Path(bundle_dir)
    args = clean_args(target, components, purge, remove_postgres, force_postgres)
    _check_bundle(bundle, target, mode)
    what = ", ".join(components) if components else "მთელი სტეკი"
    yield f"== გასუფთავება: {what}{' + მონაცემები' if purge else ''}"
    yield from _run_entry(cfg, bundle, target, mode, ssh, _provider(target).clean_entry, args, op="clean")


def apply_args(target: str, component: str) -> list[str]:
    """Arguments that (re)install / restart just one component through the bundle's entry point."""
    prov = _provider(target)
    if component not in CLEAN_COMPONENTS:
        raise DeployError(f"უცნობი კომპონენტი: {component}")
    return prov.apply_args(component)


def apply_component(cfg: dict[str, Any], bundle_dir: str | Path, target: str, mode: str,
                    ssh: dict[str, Any] | None, component: str) -> Iterator[str]:
    """Roll out only one program with the current settings (the others are left alone)."""
    bundle = Path(bundle_dir)
    args = apply_args(target, component)
    _check_bundle(bundle, target, mode)
    yield f"== გამოყენება: {component}"
    yield from _run_entry(cfg, bundle, target, mode, ssh, _provider(target).entry, args, op=f"apply:{component}")
