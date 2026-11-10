"""Explicit trust establishment for ad-hoc SSH connections (`gisstack ssh-trust`).

Nothing is trusted implicitly: `scan` only *reads* what the server presents (ssh-keyscan) and `trust` writes a key to gis-stack's own
`known_hosts_adhoc` only when its fingerprint equals the one the operator typed or passed after comparing it with the server console.
A host that already has a *different* trusted key is refused unless the operator says `replace` - a changed key is never accepted by accident.
Saved server profiles (`gisstack/servers`) pin keys the same way, per profile."""
from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404 - fixed argv (ssh-keyscan / ssh-keygen), no shell
from pathlib import Path
from typing import Any

from . import fsutil, transport
from .core.errors import DeployError, HostKeyChanged
from .servers import hostkey


def scan(host: str, port: int = 22, timeout: int = 20) -> list[dict[str, str]]:
    """What the server presents: [{type, fingerprint, line}] with `line` ready for a known_hosts file.  Trusts nothing."""
    transport.check_host(host, port)
    exe = shutil.which("ssh-keyscan")
    if not exe:
        raise DeployError("ssh-keyscan was not found (install the OpenSSH client)")
    r = subprocess.run([exe, "-T", str(timeout), "-p", str(port), host], capture_output=True, text=True, timeout=timeout + 10)  # nosec B603
    entries = hostkey.parse(r.stdout, host, port)
    if not entries:
        raise DeployError(f"{transport.host_pattern(host, port)} did not present a host key (unreachable, or not an SSH server)")
    return entries


def _keygen(*args: str) -> subprocess.CompletedProcess[str]:
    exe = shutil.which("ssh-keygen")
    if not exe:
        raise DeployError("ssh-keygen was not found (install the OpenSSH client)")
    return subprocess.run([exe, *args], capture_output=True, text=True, timeout=15)  # nosec B603


def trust(host: str, port: int = 22, *, expected: str, replace: bool = False) -> dict[str, Any]:
    """Trust the key whose fingerprint is exactly `expected` (SHA256:...).  Returns {path, fingerprint, type, replaced, already}."""
    if not expected.startswith("SHA256:"):
        raise DeployError("the fingerprint must be given as SHA256:... exactly as the server console shows it")
    entries = scan(host, port)
    match = next((e for e in entries if e["fingerprint"] == expected), None)
    if match is None:
        shown = ", ".join(f"{e['type']} {e['fingerprint']}" for e in entries)
        raise DeployError(f"{transport.host_pattern(host, port)} presents {shown} - none equals the fingerprint you gave: NOT trusted (wrong host, a typo, or a man in the middle)")
    path, pattern = transport.own_known_hosts(), transport.host_pattern(host, port)
    existing = os.path.isfile(path) and _keygen("-F", pattern, "-f", path).stdout.strip() != ""
    replaced = False
    if existing:
        current = _keygen("-F", pattern, "-f", path).stdout
        if match["line"].split()[-1] in current:
            return {"path": path, "fingerprint": expected, "type": match["type"], "replaced": False, "already": True}
        if not replace:
            raise HostKeyChanged(f"{pattern} already has a DIFFERENT trusted key; nothing was changed. If the server was reinstalled and you verified the new "
                                 "fingerprint on its console, run again with --replace.")
        _keygen("-R", pattern, "-f", path)
        replaced = True
    old = Path(path).read_text(encoding="utf-8") if os.path.isfile(path) else ""
    fsutil.write_private(path, old + ("" if not old or old.endswith("\n") else "\n") + match["line"] + "\n")
    return {"path": path, "fingerprint": expected, "type": match["type"], "replaced": replaced, "already": False}
