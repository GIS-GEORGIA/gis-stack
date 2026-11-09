"""Host key discovery and pinning.  `scan` only *reads* what the server presents (ssh-keyscan) and computes OpenSSH-style SHA256
fingerprints; `pin` stores a key only when its fingerprint equals the one the operator typed or confirmed.  Nothing is trusted
implicitly."""
from __future__ import annotations

import base64
import hashlib
import re
from typing import Any

from .. import runner
from ..core.errors import DeployError
from . import store

_KEY_TYPES = ("ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521", "ssh-rsa")
_LINE_RE = re.compile(r"^(\S+) (" + "|".join(re.escape(t) for t in _KEY_TYPES) + r") ([A-Za-z0-9+/=]{20,})$")


def fingerprint(b64: str) -> str:
    try:
        raw = base64.b64decode(b64, validate=True)
    except ValueError as e:
        raise store.ServerError("host key არასწორია") from e
    return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")


def parse(output: str, host: str, port: int) -> list[dict[str, str]]:
    """ssh-keyscan output -> [{type, fingerprint, line}] where `line` is a known_hosts line for exactly this host:port."""
    pattern = host if port == 22 else f"[{host}]:{port}"
    seen: dict[str, dict[str, str]] = {}
    for raw in output.splitlines():
        m = _LINE_RE.match(raw.strip())
        if not m:
            continue
        _, typ, key = m.groups()
        seen[typ] = {"type": typ, "fingerprint": fingerprint(key), "line": f"{pattern} {typ} {key}"}
    return list(seen.values())


def scan(sid: str, timeout: int = 20) -> list[dict[str, str]]:
    s = store.get(sid)
    p = runner.run(["ssh-keyscan", "-T", "10", "-p", str(s["port"]), "--", s["host"]], timeout=timeout, error=DeployError)
    keys = parse(p.stdout, s["host"], int(s["port"]))
    if not keys:
        raise DeployError(f"{s['host']}:{s['port']} host key ვერ მივიღეთ (მიუწვდომელია ან ssh-keyscan არ არის დაყენებული)")
    return keys


def pin(sid: str, expected: str, *, replace: bool = False) -> dict[str, Any]:
    """Pin the scanned key whose fingerprint equals `expected`.  Replacing an existing pin needs `replace=True` (a changed key is
    either a rebuilt server or an attack: the operator has to say which)."""
    expected = expected.strip()
    cur = store.get(sid)
    if cur["pinned"] and not replace:
        raise store.ServerError(f"{sid}: host key უკვე დაფიქსირებულია; შესაცვლელად replace=true")
    keys = scan(sid)
    hit = [k for k in keys if k["fingerprint"] == expected]
    if not hit:
        raise store.ServerError("თითის ანაბეჭდი (fingerprint) არ ემთხვევა: სერვერმა სხვა host key აჩვენა - არაფერი დაფიქსირდა")
    return store.set_pin(sid, [hit[0]["line"]], [hit[0]["fingerprint"]])
