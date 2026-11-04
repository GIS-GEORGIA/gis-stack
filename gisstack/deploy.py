"""Run a generated bundle: on this machine, or on a remote server over SSH."""
from __future__ import annotations

import contextlib
import os
import re
import shlex
import shutil
import tarfile
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Iterator

from . import journal, runner
from .core import paths
from .core.errors import DeployError

_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-]*[A-Za-z0-9])?$|^\[[0-9a-fA-F:]+\]$")
_USER_RE = re.compile(r"^[a-z_][a-z0-9_\-]{0,31}$")
_ENTRY = {"native": "install.sh", "compose": "up.sh", "k8s": "apply.sh"}


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


def _ssh_base(ssh: dict[str, Any]) -> list[str]:
    host, user = str(ssh.get("host", "")), str(ssh.get("user", "root"))
    raw_port = ssh.get("port")
    try:
        port = 22 if raw_port in (None, "") else int(raw_port)      # port 0 is invalid, not "default"
    except (TypeError, ValueError):
        raise DeployError("SSH port არასწორია") from None
    if not _HOST_RE.match(host):
        raise DeployError("SSH host არასწორია")
    if not _USER_RE.match(user):
        raise DeployError("SSH user არასწორია")
    if not 1 <= port <= 65535:
        raise DeployError("SSH port არასწორია")
    cmd = ["ssh", "-p", str(port), "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
           "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4"]   # a dead link ends in ~1 min, not never
    key = ssh.get("key")
    if key:
        if not Path(key).is_file():
            raise DeployError(f"SSH key ვერ მოიძებნა: {key}")
        cmd += ["-i", key]
    return cmd + [f"{user}@{host}"]


# Components the cleanup can target (catalog ids); every native/compose/k8s name derives from these.
CLEAN_COMPONENTS = ["postgis", "tomcat", "geoserver", "qgis_server", "mapproxy", "caddy", "webmap"]
_SHORT = {"qgis_server": "qgis"}   # name used by services / units / k8s objects


def clean_args(target: str, components: list[str] | None, purge: bool = False,
               remove_postgres: bool = False, force_postgres: bool = False) -> list[str]:
    """Arguments for the bundle's cleanup entry point (uninstall.sh / up.sh clean / apply.sh clean).

    components=None means the whole stack."""
    if target not in _ENTRY:
        raise DeployError("უცნობი სამიზნე")
    comps = ["qgis_server" if c == "qgis" else c for c in (components or [])]   # accept the short name too
    for c in comps:
        if c not in CLEAN_COMPONENTS:
            raise DeployError(f"უცნობი კომპონენტი: {c}")
    short = [_SHORT.get(c, c) for c in comps]
    if target == "native":
        args = []
        if short:
            args += ["--only", ",".join(short)]
        if purge:
            args.append("--purge")
        if remove_postgres:
            args += ["--remove-postgres"] + (["--force-postgres"] if force_postgres else [])
        return args
    if remove_postgres:
        raise DeployError("PostgreSQL პაკეტების წაშლა მხოლოდ native სამიზნეზეა")
    return ["clean"] + (["--purge"] if purge else []) + short


def _entry_cmd(target: str) -> str:
    return {"native": "uninstall.sh", "compose": "up.sh", "k8s": "apply.sh"}[target]


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
    if target not in _ENTRY:
        raise DeployError("უცნობი სამიზნე")
    if not (bundle / target).is_dir():
        raise DeployError(f"bundle-ში არ არის {target}/")
    problems = preflight(target, mode)
    if problems:
        raise DeployError("; ".join(problems))


def deploy(cfg: dict[str, Any], bundle_dir: str | Path, target: str, mode: str,
           ssh: dict[str, Any] | None = None, extra_args: list[str] | None = None) -> Iterator[str]:
    """Yield log lines. Raises DeployError on failure.  extra_args go to the entry script (native: install.sh)."""
    bundle = Path(bundle_dir)
    _check_bundle(bundle, target, mode)
    yield from _run_entry(cfg, bundle, target, mode, ssh, _ENTRY[target], list(extra_args or []), op="deploy")


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
    yield from _run_entry(cfg, bundle, target, mode, ssh, _entry_cmd(target) if target == "native" else _ENTRY[target], args, op="clean")


def apply_args(target: str, component: str) -> list[str]:
    """Arguments that (re)install / restart just one component through the bundle's entry point."""
    if target not in _ENTRY:
        raise DeployError("უცნობი სამიზნე")
    if component not in CLEAN_COMPONENTS:
        raise DeployError(f"უცნობი კომპონენტი: {component}")
    if target == "native":
        return ["--only", _SHORT.get(component, component)]
    name = {"qgis_server": "qgis", "webmap": "caddy"}.get(component, component)   # the web map is served by Caddy
    return ["only", name]


def apply_component(cfg: dict[str, Any], bundle_dir: str | Path, target: str, mode: str,
                    ssh: dict[str, Any] | None, component: str) -> Iterator[str]:
    """Roll out only one program with the current settings (the others are left alone)."""
    bundle = Path(bundle_dir)
    args = apply_args(target, component)
    _check_bundle(bundle, target, mode)
    yield f"== გამოყენება: {component}"
    entry = "install.sh" if target == "native" else _ENTRY[target]
    yield from _run_entry(cfg, bundle, target, mode, ssh, entry, args, op=f"apply:{component}")
