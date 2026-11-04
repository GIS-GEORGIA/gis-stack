"""Web GUI hardening (audit S-06, S-10): expiring sessions, brute-force limiting, Host allow-list, audit log.

The static access token printed by `gisstack web` is only exchanged for a random, server-side session; the session id (not the
token) lives in the browser cookie, expires when idle and in absolute terms, and can be revoked (logout).  Nothing here stores or
logs a secret."""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .. import fsutil

IDLE_SECONDS = 2 * 3600
ABSOLUTE_SECONDS = 12 * 3600
MAX_FAILURES, FAILURE_WINDOW = 10, 60
AUDIT_MAX_BYTES = 5 * 1024 * 1024
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "[::1]", "::1")


def _env_int(name: str, default: int) -> int:
    try:
        v = int(os.environ.get(name, ""))
        return v if v > 0 else default
    except ValueError:
        return default


class Sessions:
    def __init__(self, idle: Optional[int] = None, absolute: Optional[int] = None, clock: Callable[[], float] = time.time) -> None:
        self.idle = idle or _env_int("GISSTACK_SESSION_IDLE", IDLE_SECONDS)
        self.absolute = absolute or _env_int("GISSTACK_SESSION_TTL", ABSOLUTE_SECONDS)
        self.clock = clock
        self._s: dict[str, tuple[float, float]] = {}      # sid -> (created, last_seen)
        self._lock = threading.Lock()

    def create(self) -> str:
        sid = secrets.token_urlsafe(32)
        now = self.clock()
        with self._lock:
            self._purge(now)
            self._s[sid] = (now, now)
        return sid

    def valid(self, sid: str) -> bool:
        now = self.clock()
        with self._lock:
            item = self._s.get(sid or "")
            if not item:
                return False
            created, last = item
            if now - created > self.absolute or now - last > self.idle:
                del self._s[sid]
                return False
            self._s[sid] = (created, now)
            return True

    def revoke(self, sid: str) -> None:
        with self._lock:
            self._s.pop(sid or "", None)

    def revoke_all(self) -> None:
        with self._lock:
            self._s.clear()

    def _purge(self, now: float) -> None:
        for k in [k for k, (c, last) in self._s.items() if now - c > self.absolute or now - last > self.idle]:
            del self._s[k]

    def __len__(self) -> int:
        return len(self._s)


class RateLimiter:
    """Blocks a client address after MAX_FAILURES failed authentications within FAILURE_WINDOW seconds."""

    def __init__(self, max_failures: int = MAX_FAILURES, window: int = FAILURE_WINDOW, clock: Callable[[], float] = time.time) -> None:
        self.max, self.window, self.clock = max_failures, window, clock
        self._f: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, ip: str, now: float) -> list[float]:
        fresh = [t for t in self._f.get(ip, []) if now - t < self.window]
        if fresh:
            self._f[ip] = fresh
        else:
            self._f.pop(ip, None)
        return fresh

    def blocked(self, ip: str) -> bool:
        with self._lock:
            return len(self._recent(ip, self.clock())) >= self.max

    def fail(self, ip: str) -> None:
        with self._lock:
            now = self.clock()
            self._recent(ip, now)
            self._f.setdefault(ip, []).append(now)
            if len(self._f) > 10_000:                      # bounded memory under a spray from many addresses
                self._f.pop(next(iter(self._f)))

    def reset(self, ip: str) -> None:
        with self._lock:
            self._f.pop(ip, None)


def host_allowed(host_header: str, extra: tuple[str, ...] = ()) -> bool:
    """Host header must name this machine as the operator intended (blocks DNS-rebinding), with or without a port."""
    h = (host_header or "").strip().lower()
    if not h:
        return False
    name = h.rsplit(":", 1)[0] if (h.startswith("[") and "]:" in h) or (not h.startswith("[") and h.count(":") == 1) else h
    allowed = {x.lower() for x in (*LOOPBACK_HOSTS, *extra)}
    return "*" in allowed or name in allowed or h in allowed


def allowed_hosts_from_env() -> tuple[str, ...]:
    return tuple(x.strip() for x in os.environ.get("GISSTACK_ALLOWED_HOSTS", "").split(",") if x.strip())


SAFE_BODY_KEYS = ("mode", "component", "components", "action", "purge", "remove_postgres", "with_data", "skip_preflight", "lines", "follow",
                  "install", "confirm_data_loss", "save", "id", "only", "dry_run", "staging", "confirm", "keep", "days", "with_cache")


def summarize(body: Any) -> dict[str, Any]:
    """The few non-secret facts about a request that belong in the audit log (no passwords, keys, plans or file contents)."""
    if not isinstance(body, dict):
        return {}
    out: dict[str, Any] = {k: body[k] for k in SAFE_BODY_KEYS if k in body and isinstance(body[k], (bool, int, str, list))}
    for k, v in list(out.items()):
        if isinstance(v, list):
            out[k] = [x for x in v if isinstance(x, str)][:20]
        elif isinstance(v, str):
            out[k] = v[:80]
    cfg = body.get("config")
    if isinstance(cfg, dict):
        for k in ("project", "deploy_target", "domain"):
            if isinstance(cfg.get(k), str):
                out[f"config.{k}"] = cfg[k][:80]
    ssh = body.get("ssh")
    if isinstance(ssh, dict):
        for k in ("host", "user", "port"):
            if isinstance(ssh.get(k), (str, int)):
                out[f"ssh.{k}"] = str(ssh[k])[:80]
    return out


class AuditLog:
    def __init__(self, path_fn: Callable[[], Path]) -> None:
        self._path_fn = path_fn
        self._lock = threading.Lock()

    def write(self, **event: Any) -> None:
        event = {"ts": int(time.time()), **event}
        with self._lock:
            try:
                p = self._path_fn()
                if p.exists() and p.stat().st_size > AUDIT_MAX_BYTES:
                    os.replace(p, p.with_suffix(".log.1"))
                fsutil.private_dir(p.parent)
                fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    os.write(fd, (json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
                finally:
                    os.close(fd)
            except OSError:
                pass                                       # auditing must never take the request down
