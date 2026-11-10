"""Server profiles: `~/.gisstack/servers.json` (owner-only) and one pinned `known_hosts` file per server.

What is stored: name, environment tag, host/port/user, the PATH of an SSH key (or nothing: the ssh agent is used) and the pinned host
key.  What is never stored: key material, passwords, passphrases.  Anything that looks like a private key is rejected, so a profile
file can be copied or backed up without leaking a credential.  Every operation on a server uses its own pinned key file with
`StrictHostKeyChecking=yes`: an unpinned or changed host key stops the connection instead of being accepted silently."""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .. import fsutil, transport
from ..core import paths
from ..core.errors import GisStackError

ENVS = ("production", "development", "testing")
ID_RE = re.compile(r"^[a-z][a-z0-9_\-]{0,30}$")
_FP_RE = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")
_lock = threading.Lock()


class ServerError(GisStackError):
    code = "config"


class ServerNotFound(ServerError):
    code = "not_found"


def file() -> Path:
    return paths.home() / "servers.json"


def known_hosts_file(sid: str) -> Path:
    if not ID_RE.match(sid):
        raise ServerError("სერვერის id არასწორია")
    return fsutil.private_dir(paths.home() / "known_hosts") / sid


def _read() -> dict[str, Any]:
    p = file()
    if not p.is_file():
        return {"servers": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ServerError(f"servers.json დაზიანებულია: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("servers"), list):
        raise ServerError("servers.json დაზიანებულია")
    return data


def _write(data: dict[str, Any]) -> None:
    fsutil.write_private(file(), json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True))


def check_key_path(key: str) -> str:
    """A key is referenced by path only.  Pasted key material (or a path with odd characters) is refused."""
    key = (key or "").strip()
    if not key:
        return ""
    if "\n" in key or "\r" in key or "PRIVATE KEY" in key or "\0" in key or len(key) > 400:
        raise ServerError("key: მხოლოდ ფაილის გზა შეიძლება; გასაღების შიგთავსი არ ინახება")
    if not key.startswith("/"):
        raise ServerError("key: აბსოლუტური გზა სჭირდება (ან ცარიელი: ssh-agent)")
    return key


def validate(p: dict[str, Any]) -> dict[str, Any]:
    sid = str(p.get("id", "")).strip()
    if not ID_RE.match(sid):
        raise ServerError("სერვერის id: a-z, 0-9, _ და - (პატარა ასოთი იწყება)")
    name = str(p.get("name") or sid).strip()[:80]
    env = str(p.get("env", "development"))
    if env not in ENVS:
        raise ServerError(f"env: {', '.join(ENVS)}")
    ssh = {"host": str(p.get("host", "")).strip(), "port": 22 if p.get("port") in (None, "") else p["port"], "user": str(p.get("user") or "root").strip()}
    transport.ssh_base(ssh, check_trust=False)                                # same host / user / port rules as every connection
    try:
        ssh["port"] = int(ssh["port"])
    except (TypeError, ValueError):
        raise ServerError("SSH port არასწორია") from None
    return {"id": sid, "name": name, "env": env, "host": ssh["host"], "port": ssh["port"], "user": ssh["user"],
            "key": check_key_path(str(p.get("key") or "")), "notes": str(p.get("notes") or "")[:500]}


def _public(s: dict[str, Any]) -> dict[str, Any]:
    pin = s.get("hostkey") or {}
    return {**{k: s.get(k) for k in ("id", "name", "env", "host", "port", "user", "key", "notes", "created", "updated")},
            "pinned": bool(pin.get("fingerprints")), "fingerprints": list(pin.get("fingerprints") or [])}


def list_servers() -> list[dict[str, Any]]:
    return [_public(s) for s in sorted(_read()["servers"], key=lambda s: (ENVS.index(s["env"]) if s.get("env") in ENVS else 9, s["id"]))]


def _find(data: dict[str, Any], sid: str) -> Optional[dict[str, Any]]:
    return next((s for s in data["servers"] if s.get("id") == sid), None)


def get(sid: str) -> dict[str, Any]:
    s = _find(_read(), sid)
    if s is None:
        raise ServerNotFound(f"სერვერი ვერ მოიძებნა: {sid}")
    return _public(s)


def save(p: dict[str, Any]) -> dict[str, Any]:
    """Create or update a profile.  Changing host / port / user drops the pinned host key (it belongs to that address)."""
    new = validate(p)
    with _lock:
        data = _read()
        old = _find(data, new["id"])
        now = int(time.time())
        if old is None:
            data["servers"].append({**new, "created": now, "updated": now})
        else:
            if (old["host"], old["port"]) != (new["host"], new["port"]):
                old.pop("hostkey", None)
                known_hosts_file(new["id"]).unlink(missing_ok=True)
            old.update(new, updated=now)
        _write(data)
        return _public(_find(data, new["id"]) or new)


def delete(sid: str) -> None:
    with _lock:
        data = _read()
        if _find(data, sid) is None:
            raise ServerNotFound(f"სერვერი ვერ მოიძებნა: {sid}")
        data["servers"] = [s for s in data["servers"] if s.get("id") != sid]
        _write(data)
        known_hosts_file(sid).unlink(missing_ok=True)


def set_pin(sid: str, lines: list[str], fingerprints: list[str]) -> dict[str, Any]:
    if not lines or any(not _FP_RE.match(f) for f in fingerprints):
        raise ServerError("host key არასწორია")
    with _lock:
        data = _read()
        s = _find(data, sid)
        if s is None:
            raise ServerNotFound(f"სერვერი ვერ მოიძებნა: {sid}")
        fsutil.write_private(known_hosts_file(sid), "\n".join(lines) + "\n")
        s["hostkey"] = {"fingerprints": sorted(fingerprints), "pinned": int(time.time())}
        s["updated"] = int(time.time())
        _write(data)
        return _public(s)


def ssh_for(sid: str) -> dict[str, Any]:
    """The `ssh` settings every transport call needs for this server, with its pinned `known_hosts` file.  Unpinned servers are refused."""
    s = get(sid)
    if not s["pinned"]:
        raise ServerError(f"{sid}: host key არ არის დაფიქსირებული (Servers → Pin host key)")
    kh = known_hosts_file(sid)
    if not kh.is_file():
        raise ServerError(f"{sid}: pinned known_hosts ფაილი აკლია; ხელახლა დააფიქსირეთ")
    return {"host": s["host"], "port": s["port"], "user": s["user"], "key": s["key"], "known_hosts": str(kh)}
