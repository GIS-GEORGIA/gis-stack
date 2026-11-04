"""API tokens with scopes (Phase 10).

A token looks like ``gsk_<id>_<secret>``.  Only the SHA-256 of the secret is stored (``~/.gisstack/tokens.json``, 0600), so a stolen
file does not reveal usable tokens; the secret is shown exactly once, when it is created.  Scopes: read < operate < admin.
Tokens only work on /api/v1; the legacy /api/* routes accept the web access token / browser session (= admin) only."""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

from .. import fsutil
from ..core import paths
from ..core.errors import GisStackError
from .registry import SCOPES

TOKEN_RE = re.compile(r"^gsk_([0-9a-f]{8})_([A-Za-z0-9_-]{32,64})$")
TOUCH_EVERY = 60.0          # last_used is written at most once a minute


class TokenError(GisStackError):
    code = "bad_request"


@dataclass(frozen=True)
class Principal:
    id: str
    name: str
    scope: str
    kind: str                   # token | session | static | plugin


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class TokenStore:
    def __init__(self, path: Any = None, clock: Any = time.time) -> None:
        self._path = path
        self.clock = clock
        self._lock = threading.Lock()
        self._touched: dict[str, float] = {}

    @property
    def path(self) -> Any:
        return self._path or paths.tokens_file()

    def _load(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [e for e in data.get("tokens", []) if isinstance(e, dict)] if isinstance(data, dict) else []

    def _save(self, entries: list[dict[str, Any]]) -> None:
        fsutil.write_private(self.path, json.dumps({"version": 1, "tokens": entries}, indent=1))

    def create(self, name: str, scope: str, ttl_days: Optional[float] = None) -> tuple[str, dict[str, Any]]:
        if scope not in SCOPES:
            raise TokenError(f"scope must be one of {', '.join(SCOPES)}")
        name = (name or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9 ._@-]{1,60}", name):
            raise TokenError("name: 1-60 characters (letters, digits, space . _ @ -)")
        if ttl_days is not None and not 0 < ttl_days <= 3650:
            raise TokenError("ttl must be between 0 and 3650 days")
        tid, secret = secrets.token_hex(4), secrets.token_urlsafe(24)
        now = self.clock()
        entry = {"id": tid, "name": name, "scope": scope, "hash": _digest(secret), "created": now,
                 "expires": (now + ttl_days * 86400) if ttl_days else None, "last_used": None}
        with self._lock:
            self._save(self._load() + [entry])
        return f"gsk_{tid}_{secret}", {k: v for k, v in entry.items() if k != "hash"}

    def verify(self, token: str) -> Optional[Principal]:
        m = TOKEN_RE.match(token or "")
        if not m:
            return None
        tid, secret = m.groups()
        with self._lock:
            entries = self._load()
        entry = next((e for e in entries if e.get("id") == tid), None)
        ok = entry is not None and hmac.compare_digest(str(entry.get("hash", "")), _digest(secret))
        if not ok or entry is None:
            return None
        now = self.clock()
        if entry.get("expires") and now > entry["expires"]:
            return None
        if entry.get("scope") not in SCOPES:
            return None
        if now - self._touched.get(tid, 0) >= TOUCH_EVERY:
            self._touched[tid] = now
            with contextlib.suppress(OSError), self._lock:
                fresh = self._load()
                for e in fresh:
                    if e.get("id") == tid:
                        e["last_used"] = now
                self._save(fresh)
        return Principal(tid, str(entry.get("name", tid)), str(entry["scope"]), "token")

    def list(self) -> list[dict[str, Any]]:
        return [{k: v for k, v in e.items() if k != "hash"} for e in self._load()]

    def revoke(self, tid: str) -> bool:
        with self._lock:
            entries = self._load()
            kept = [e for e in entries if e.get("id") != tid]
            if len(kept) == len(entries):
                return False
            self._save(kept)
        return True
