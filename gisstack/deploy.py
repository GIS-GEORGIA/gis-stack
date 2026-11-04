"""Run a generated bundle: on this machine, or on a remote server over SSH."""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Iterator

_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-]*[A-Za-z0-9])?$|^\[[0-9a-fA-F:]+\]$")
_USER_RE = re.compile(r"^[a-z_][a-z0-9_\-]{0,31}$")
_ENTRY = {"native": "install.sh", "compose": "up.sh", "k8s": "apply.sh"}


class DeployError(RuntimeError):
    pass


def home_dir() -> Path:
    p = Path(os.environ.get("GISSTACK_HOME") or (Path.home() / ".gisstack"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def _stream(cmd: list[str], cwd: str | None = None, env: dict | None = None) -> Iterator[str]:
    yield "$ " + " ".join(shlex.quote(c) for c in cmd)
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1, errors="replace")
    except FileNotFoundError as e:
        raise DeployError(f"command not found: {cmd[0]}") from e
    assert proc.stdout is not None
    for line in proc.stdout:
        yield line.rstrip("\n")
    rc = proc.wait()
    if rc != 0:
        raise DeployError(f"command failed (exit {rc}): {cmd[0]}")


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
    port = int(ssh.get("port") or 22)
    if not _HOST_RE.match(host):
        raise DeployError("SSH host არასწორია")
    if not _USER_RE.match(user):
        raise DeployError("SSH user არასწორია")
    if not 1 <= port <= 65535:
        raise DeployError("SSH port არასწორია")
    cmd = ["ssh", "-p", str(port), "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
           "-o", "ConnectTimeout=15"]
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


def _run_entry(cfg: dict[str, Any], bundle: Path, target: str, mode: str, ssh: dict[str, Any] | None,
               entry: str, args: list[str]) -> Iterator[str]:
    """Run <bundle>/<target>/<entry> <args> here (mode=local) or on a server (mode=ssh)."""
    if mode == "local":
        yield f"== ლოკალური გაშვება: {target} ({entry})"
        env = dict(os.environ)
        if cfg.get("runtime"):
            env["RUNTIME"] = cfg["runtime"]
        yield from _stream(["bash", entry, *args], cwd=str(bundle / target), env=env)
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
            p = subprocess.run(base + [f"mkdir -p {remote_dir} && tar xzf - -C {remote_dir}"],
                               stdin=fh, capture_output=True, text=True)
        if p.returncode != 0:
            raise DeployError("ატვირთვა ვერ მოხერხდა: " + (p.stderr or p.stdout).strip())
    finally:
        os.unlink(tmp_path)

    # 2. run
    sudo = "" if ssh.get("user", "root") == "root" else "sudo -n "
    argv = " ".join(shlex.quote(a) for a in args)
    run = f"cd {remote_dir}/{target} && chmod +x {entry} && RUNTIME={cfg['runtime']} {sudo}./{entry} {argv}".rstrip()
    if target in ("compose", "k8s") and sudo:
        run = f"cd {remote_dir}/{target} && chmod +x {entry} && RUNTIME={cfg['runtime']} ./{entry} {argv}".rstrip()
    yield from _stream(base + [run])
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
    yield from _run_entry(cfg, bundle, target, mode, ssh, _ENTRY[target], list(extra_args or []))


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
    yield from _run_entry(cfg, bundle, target, mode, ssh, _entry_cmd(target) if target == "native" else _ENTRY[target], args)


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
    yield from _run_entry(cfg, bundle, target, mode, ssh, entry, args)
