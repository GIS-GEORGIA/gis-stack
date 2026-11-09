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
from .core.errors import DeployError

_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-]*[A-Za-z0-9])?$|^\[[0-9a-fA-F:]+\]$")
_USER_RE = re.compile(r"^[a-z_][a-z0-9_\-]{0,31}$")


def ssh_base(ssh: dict[str, Any]) -> list[str]:
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
             else ["-o", "StrictHostKeyChecking=accept-new"])
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
    if p.returncode != 0 and not p.stdout.strip():
        raise DeployError((p.stderr or "command failed").strip().splitlines()[-1])
    return p.stdout


def stream(mode: str, ssh: Optional[dict[str, Any]], script: str, *, root: bool = False,
           stop: Optional[threading.Event] = None, deadline: Optional[float] = None) -> Iterator[str]:
    """Stream a script's output.  `stop` ends it (not an error); `deadline` bounds it (None: log following has no limit)."""
    yield from runner.stream(argv(mode, ssh, script, root=root), stop=stop, error=DeployError, echo=False, deadline=deadline, idle=None)


def host_label(mode: str, ssh: Optional[dict[str, Any]]) -> str:
    return "localhost" if mode == "local" else str((ssh or {}).get("host", "server"))
