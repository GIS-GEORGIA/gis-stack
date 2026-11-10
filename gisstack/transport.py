"""Where commands run: this machine or a server over SSH (audit A-01).

`Host` is just the pair (mode, ssh settings) that every operation already passed around; the functions here build the command
line (`argv`), run a script and return its output (`run`) or stream it (`stream`).  Everything goes through `gisstack.runner`, so
deadlines, cancellation, process-group kill and redaction apply uniformly.  Scripts are passed as an *argument* (`bash -c`), which
keeps stdin free for secrets; never put a secret in the script text or argv - use `input_text`."""
from __future__ import annotations

import os
import re
import shlex
import threading
from pathlib import Path
from typing import Any, Iterator, Optional

from . import runner
from .core import paths
from .core.errors import DeployError, HostKeyChanged, HostKeyUnknown

_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-]*[A-Za-z0-9])?$|^\[[0-9a-fA-F:]+\]$")
_USER_RE = re.compile(r"^[a-z_][a-z0-9_\-]{0,31}$")


def adhoc_policy() -> str:
    """`strict` (default): an ad-hoc SSH host must already be trusted - a new host is refused until the operator establishes trust explicitly
    (`gisstack ssh-trust`, or a saved server profile with a pinned key).  `tofu` (explicit opt-in, `GISSTACK_SSH_TOFU=1`): trust a new host on first
    use, as earlier versions did.  `GISSTACK_SSH_STRICT=1` always wins.  A host whose key CHANGED is refused in both modes."""
    if os.environ.get("GISSTACK_SSH_STRICT") == "1":
        return "strict"
    return "tofu" if os.environ.get("GISSTACK_SSH_TOFU") == "1" else "strict"


def own_known_hosts() -> str:
    return str(paths.home() / "known_hosts_adhoc")


def user_known_hosts() -> str:
    """The user's ~/.ssh/known_hosts, located the way ssh itself does (password database, not $HOME)."""
    try:
        import pwd
        return os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".ssh", "known_hosts")
    except (ImportError, KeyError):
        return os.path.join(os.path.expanduser("~"), ".ssh", "known_hosts")


def _quotable(p: str) -> bool:
    return not any(c in p for c in " \t\"',\n")                # ssh splits the option on spaces; such a path cannot be passed safely


def host_pattern(host: str, port: int) -> str:
    return host if port == 22 else f"[{host}]:{port}"


def is_known(host: str, port: int) -> Optional[bool]:
    """Is a key for host:port in gis-stack's own file or the user's known_hosts?  (hashed entries included)  None = cannot tell (no ssh-keygen)."""
    import shutil
    import subprocess  # nosec B404 - fixed argv, no shell
    kg = shutil.which("ssh-keygen")
    if not kg:
        return None
    for f in (own_known_hosts(), user_known_hosts()):
        if os.path.isfile(f):
            r = subprocess.run([kg, "-F", host_pattern(host, port), "-f", f], capture_output=True, text=True, timeout=10)  # nosec B603
            if r.returncode == 0 and r.stdout.strip():
                return True
    return False


def adhoc_trust() -> list[str]:
    """ssh options of an ad-hoc connection (no saved server profile, so nothing is pinned).  New hosts, when trust-on-first-use was chosen, are
    written to gis-stack's own `known_hosts_adhoc` (first in the list, so it is where ssh writes), while keys the user already trusts in
    ~/.ssh/known_hosts still count."""
    mode = ["-o", f"StrictHostKeyChecking={'yes' if adhoc_policy() == 'strict' else 'accept-new'}"]
    own, user = own_known_hosts(), user_known_hosts()
    if not _quotable(own):
        return mode
    return mode + ["-o", f"UserKnownHostsFile={own}" + (f" {user}" if _quotable(user) else "")]


def require_trusted(host: str, port: int) -> None:
    """Strict policy: refuse an unknown host *before* any connection, with the way out spelled out."""
    if adhoc_policy() == "strict" and is_known(host, port) is False:
        raise HostKeyUnknown(f"SSH host {host_pattern(host, port)} is not trusted yet, so nothing was sent to it. Establish trust explicitly: compare its fingerprint "
                             f"on the server console with `gisstack ssh-trust {host}{'' if port == 22 else f' --port {port}'}` (or save the server as a profile and pin its key "
                             "in the dashboard). Trust-on-first-use for this run: GISSTACK_SSH_TOFU=1.")


def diagnose(stderr: str, ssh: Optional[dict[str, Any]]) -> Optional[Exception]:
    """A clear error for the ssh failures that are about trust, from ssh's own message; None for anything else."""
    host, port = str((ssh or {}).get("host", "server")), int((ssh or {}).get("port") or 22)
    low = stderr.lower()
    if "remote host identification has changed" in low or "host key for" in low and "has changed" in low:
        return HostKeyChanged(f"The SSH host key of {host_pattern(host, port)} CHANGED since it was trusted: the connection was refused and nothing was sent. "
                              "This can mean a reinstalled server - or an attack. Check the new fingerprint on the server console, then replace the trusted key "
                              f"deliberately: `gisstack ssh-trust {host}{'' if port == 22 else f' --port {port}'} --replace`.")
    if "no " in low and "host key is known" in low or "host key verification failed" in low:
        return HostKeyUnknown(f"SSH host {host_pattern(host, port)} is not trusted (ssh: host key verification failed). Establish trust with `gisstack ssh-trust {host}"
                              f"{'' if port == 22 else f' --port {port}'}`.")
    return None


def check_host(host: str, port: int) -> None:
    """The host / port syntax ssh_base accepts (shared with the trust command)."""
    if not _HOST_RE.match(host):
        raise DeployError("SSH host არასწორია")
    if not 1 <= port <= 65535:
        raise DeployError("SSH port არასწორია")


def ssh_base(ssh: dict[str, Any], *, check_trust: bool = True) -> list[str]:
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
    # a server profile brings its own pinned known_hosts file: that key or nothing (no trust-on-first-use, no global known_hosts)
    kh = ssh.get("known_hosts")
    if kh and (not isinstance(kh, str) or not Path(kh).is_file() or any(c in kh for c in ",\n")):
        raise DeployError("known_hosts ფაილი ვერ მოიძებნა")
    trust = (["-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={kh}", "-o", "GlobalKnownHostsFile=/dev/null"] if kh
             else adhoc_trust())
    if not kh and check_trust:
        require_trusted(host, port)
    cmd = ["ssh", "-p", str(port), "-o", "BatchMode=yes", *trust,
           "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4"]   # a dead link ends in ~1 min, not never
    key = ssh.get("key")
    if key:
        if not Path(key).is_file():
            raise DeployError(f"SSH key ვერ მოიძებნა: {key}")
        cmd += ["-i", key]
    return cmd + [f"{user}@{host}"]


def argv(mode: str, ssh: Optional[dict[str, Any]], script: str, *, root: bool) -> list[str]:
    """Command that runs a bash script here or on the server (script is an argument, so stdin stays free)."""
    if mode == "local":
        pre = ["sudo", "-n"] if root and hasattr(os, "geteuid") and os.geteuid() != 0 else []
        return pre + ["bash", "-c", script]
    if mode != "ssh":
        raise DeployError("უცნობი რეჟიმი")
    ssh = ssh or {}
    sudo = "sudo -n " if root and ssh.get("user", "root") != "root" else ""
    return ssh_base(ssh) + [f"{sudo}bash -c {shlex.quote(script)}"]


def run(mode: str, ssh: Optional[dict[str, Any]], script: str, *, root: bool = False, timeout: int = 40,
        input_text: Optional[str] = None) -> str:
    """Run a script here / on the server and return stdout.  `input_text` goes to its stdin (use it for secrets, never argv)."""
    p = runner.run(argv(mode, ssh, script, root=root), timeout=timeout, error=DeployError, input_text=input_text)
    if p.returncode != 0 and mode == "ssh":
        why = diagnose(p.stderr or "", ssh)
        if why is not None:
            raise why
    if p.returncode != 0 and not p.stdout.strip():
        raise DeployError((p.stderr or "command failed").strip().splitlines()[-1])
    return p.stdout


def stream(mode: str, ssh: Optional[dict[str, Any]], script: str, *, root: bool = False,
           stop: Optional[threading.Event] = None, deadline: Optional[float] = None) -> Iterator[str]:
    """Stream a script's output.  `stop` ends it (not an error); `deadline` bounds it (None: log following has no limit)."""
    try:
        yield from runner.stream(argv(mode, ssh, script, root=root), stop=stop, error=DeployError, echo=False, deadline=deadline, idle=None)
    except DeployError as e:
        if mode == "ssh" and "exit 255" in str(e):                    # ssh's own failure code: connection or host-key trouble, not the remote command
            raise DeployError(f"{e} - ssh could not connect to {host_label(mode, ssh)} (unreachable, authentication, or an untrusted / changed host key: "
                              "see `gisstack ssh-trust`).") from e
        raise


def host_label(mode: str, ssh: Optional[dict[str, Any]]) -> str:
    return "localhost" if mode == "local" else str((ssh or {}).get("host", "server"))
