"""Security overview of the machine that runs gis-stack: file permissions of everything that holds secrets, signature policy and
the administrative audit trail.  Read-only; reports only what it can actually observe and never prints secret contents."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, Mapping, Optional

from .core import paths

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"
TAIL_BYTES = 512 * 1024                    # the audit log rotates itself; the newest part is all a page ever needs
MAX_EVENTS = 500


def _mode_check(cid: str, label: str, path: Path, want_dir: bool) -> Optional[dict[str, Any]]:
    """Owner-only means no group/other bits at all.  A missing path is simply not reported (nothing to protect yet)."""
    try:
        st = path.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(st.st_mode):
        return {"id": cid, "label": label, "status": WARN, "detail": "symbolic link - permissions of the target are not inspected", "path": str(path)}
    if want_dir != stat.S_ISDIR(st.st_mode):
        return {"id": cid, "label": label, "status": WARN, "detail": "unexpected file type", "path": str(path)}
    mode = stat.S_IMODE(st.st_mode)
    owner_ok = not hasattr(os, "getuid") or st.st_uid == os.getuid()
    bad = mode & 0o077
    if bad:
        return {"id": cid, "label": label, "status": FAIL, "detail": f"mode {mode:04o}: readable or writable by others (should be {'0700' if want_dir else '0600'})", "path": str(path)}
    if not owner_ok:
        return {"id": cid, "label": label, "status": WARN, "detail": "owned by another user", "path": str(path)}
    return {"id": cid, "label": label, "status": OK, "detail": f"mode {mode:04o}", "path": str(path)}


def posture(env: Optional[Mapping[str, str]] = None) -> list[dict[str, Any]]:
    """One entry per check: {id, label, status ok|warn|fail|info, detail[, path]}."""
    env = os.environ if env is None else env
    home = paths.home()
    out: list[dict[str, Any]] = []
    candidates: list[tuple[str, str, Path, bool]] = [
        ("home", "gis-stack data directory", home, True),
        ("token", "web access token", paths.token_file(), False),
        ("api_tokens", "API tokens", paths.tokens_file(), False),
        ("audit", "audit log", paths.audit_log(), False),
        ("servers", "server profiles", home / "servers.json", False),
        ("known_hosts", "pinned SSH host keys", home / "known_hosts", True),
        ("secrets_dir", "generated passwords directory", home / "secrets", True),
    ]
    for cid, label, p, want_dir in candidates:
        c = _mode_check(cid, label, p, want_dir)
        if c:
            out.append(c)
    sec = home / "secrets"
    if sec.is_dir():
        loose = [f.name for f in sorted(sec.iterdir()) if f.is_file() and stat.S_IMODE(f.lstat().st_mode) & 0o077]
        out.append({"id": "secret_files", "label": "generated password files", "status": FAIL if loose else OK,
                    "detail": f"{len(loose)} file(s) with loose permissions: {', '.join(loose[:5])}" if loose else "all owner-only", "path": str(sec)})
    sig = env.get("GISSTACK_REQUIRE_SIGNATURE") == "1"
    out.append({"id": "signature", "label": "update signature policy", "status": OK if sig else INFO,
                "detail": "updates without a valid signature are refused" if sig else "a missing signature only warns (set GISSTACK_REQUIRE_SIGNATURE=1 to refuse)"})
    out.append({"id": "token_env", "label": "access token source", "status": INFO,
                "detail": "taken from GISSTACK_TOKEN" if env.get("GISSTACK_TOKEN") else "stored in the data directory"})
    return out


def audit_events(limit: int = 100, event: str = "", log: Optional[Path] = None) -> list[dict[str, Any]]:
    """Newest-first administrative events.  `event` is a prefix filter (e.g. "postgis."); damaged lines are skipped, never raised."""
    limit = max(1, min(int(limit), MAX_EVENTS))
    p = log or paths.audit_log()
    try:
        with open(p, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - TAIL_BYTES))
            raw = f.read()
    except OSError:
        return []
    lines = raw.decode("utf-8", "replace").splitlines()
    if size > TAIL_BYTES and lines:
        lines = lines[1:]                  # the first line of a tail read is cut in the middle
    events: list[dict[str, Any]] = []
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        if event and not str(e.get("event", "")).startswith(event):
            continue
        events.append(e)
        if len(events) >= limit:
            break
    return events
