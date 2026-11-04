"""SQLite ring-buffer history (Phase 8): no external dependency, one file per monitored machine, owner-only.

Tables: metrics (time series), events (health / alert history), alerts (current state of each rule+subject).  `prune` keeps the
newest `days` of data and at most `max_rows` metric rows - oldest first - so the file cannot grow without bound."""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Optional

from ..core.errors import GisStackError

SCHEMA_VERSION = 1
DEFAULT_DAYS = 14
DEFAULT_MAX_ROWS = 2_000_000
Labels = dict[str, str]


class StoreError(GisStackError):
    code = "store"


def _labels_key(labels: Optional[Labels]) -> str:
    return json.dumps(labels or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


_MIGRATIONS = {
    0: """
        CREATE TABLE metrics (ts REAL NOT NULL, name TEXT NOT NULL, labels TEXT NOT NULL, value REAL NOT NULL);
        CREATE INDEX metrics_series ON metrics (name, labels, ts);
        CREATE INDEX metrics_ts ON metrics (ts);
        CREATE TABLE events (id INTEGER PRIMARY KEY, ts REAL NOT NULL, level TEXT NOT NULL, kind TEXT NOT NULL, subject TEXT NOT NULL, message TEXT NOT NULL);
        CREATE INDEX events_ts ON events (ts);
        CREATE TABLE alerts (rule TEXT NOT NULL, subject TEXT NOT NULL, state TEXT NOT NULL, since REAL NOT NULL, value REAL, message TEXT NOT NULL,
                             streak INTEGER NOT NULL DEFAULT 0, notified REAL, PRIMARY KEY (rule, subject));
        CREATE TABLE samples (ts REAL NOT NULL, errors TEXT NOT NULL);
        CREATE INDEX samples_ts ON samples (ts);
    """,
}


class Store:
    def __init__(self, path: str | Path, days: Optional[int] = None, max_rows: Optional[int] = None) -> None:
        self.path = Path(path)
        self.days = int(os.environ.get("GISSTACK_MONITOR_RETENTION_DAYS", days or DEFAULT_DAYS))
        self.max_rows = int(os.environ.get("GISSTACK_MONITOR_MAX_ROWS", max_rows or DEFAULT_MAX_ROWS))
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))      # owner-only from the first byte
        try:
            self._db = sqlite3.connect(self.path, timeout=30, check_same_thread=False, isolation_level=None)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._migrate()
        except sqlite3.DatabaseError as e:
            raise StoreError(f"monitoring history is unreadable ({self.path}): {e}") from e

    def _migrate(self) -> None:
        ver = self._db.execute("PRAGMA user_version").fetchone()[0]
        if ver > SCHEMA_VERSION:
            raise StoreError(f"history was written by a newer gisstack (schema {ver} > {SCHEMA_VERSION})")
        while ver < SCHEMA_VERSION:
            with self._tx():
                for stmt in _MIGRATIONS[ver].split(";"):
                    if stmt.strip():
                        self._db.execute(stmt)
                ver += 1
                self._db.execute(f"PRAGMA user_version={ver}")

    @contextlib.contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            else:
                self._db.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ writing
    def add_sample(self, ts: float, metrics: list[tuple[str, Labels, float]], errors: Optional[list[str]] = None) -> None:
        rows = [(ts, n, _labels_key(l), v) for n, l, v in metrics if v == v and abs(v) != float("inf")]     # never store NaN / inf
        with self._tx() as db:
            db.executemany("INSERT INTO metrics (ts, name, labels, value) VALUES (?, ?, ?, ?)", rows)
            db.execute("INSERT INTO samples (ts, errors) VALUES (?, ?)", (ts, json.dumps(errors or [])))

    def add_event(self, ts: float, level: str, kind: str, subject: str, message: str) -> None:
        with self._tx() as db:
            db.execute("INSERT INTO events (ts, level, kind, subject, message) VALUES (?, ?, ?, ?, ?)", (ts, level, kind, subject, message[:500]))

    def prune(self, now: Optional[float] = None) -> int:
        """Delete metrics older than `days`, then the oldest metrics beyond `max_rows`; the newest sample always survives."""
        now = now or time.time()
        cutoff = now - self.days * 86400
        with self._tx() as db:
            newest = db.execute("SELECT MAX(ts) FROM samples").fetchone()[0]
            keep_after = min(cutoff, newest - 1e-6) if newest is not None else cutoff
            n = db.execute("DELETE FROM metrics WHERE ts < ?", (keep_after,)).rowcount
            db.execute("DELETE FROM samples WHERE ts < ?", (keep_after,))
            db.execute("DELETE FROM events WHERE ts < ?", (cutoff - 30 * 86400,))                     # health history is kept longer
            total = db.execute("SELECT COUNT(*) FROM metrics").fetchone()[0]
            if total > self.max_rows:
                limit_ts = db.execute("SELECT ts FROM metrics ORDER BY ts DESC LIMIT 1 OFFSET ?", (self.max_rows - 1,)).fetchone()
                if limit_ts:
                    n += db.execute("DELETE FROM metrics WHERE ts < ?", (limit_ts[0],)).rowcount
                    db.execute("DELETE FROM samples WHERE ts < ?", (limit_ts[0],))
        return n

    # ------------------------------------------------------------------ reading
    def latest_ts(self) -> Optional[float]:
        with self._lock:
            r = self._db.execute("SELECT MAX(ts) FROM samples").fetchone()
        return r[0]

    def latest(self) -> dict[str, list[dict[str, Any]]]:
        """The newest value of every series: {name: [{labels, value, ts}]}."""
        ts = self.latest_ts()
        out: dict[str, list[dict[str, Any]]] = {}
        if ts is None:
            return out
        with self._lock:
            rows = self._db.execute("SELECT name, labels, value FROM metrics WHERE ts = ? ORDER BY name, labels", (ts,)).fetchall()
        for name, labels, value in rows:
            out.setdefault(name, []).append({"labels": json.loads(labels), "value": value, "ts": ts})
        return out

    def series(self, name: str, labels: Optional[Labels] = None, since: Optional[float] = None, until: Optional[float] = None,
               limit: int = 5000, step: Optional[float] = None) -> list[dict[str, Any]]:
        """Points of one metric (all label sets unless `labels` filters).  `step` averages into buckets of that many seconds."""
        q, args = "SELECT ts, labels, value FROM metrics WHERE name = ?", [name]
        if labels is not None:
            q += " AND labels = ?"
            args.append(_labels_key(labels))
        if since is not None:
            q += " AND ts >= ?"
            args.append(since)
        if until is not None:
            q += " AND ts <= ?"
            args.append(until)
        q += " ORDER BY ts ASC"
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        if step and step > 0:
            buckets: dict[tuple[float, str], list[float]] = {}
            for ts, lab, v in rows:
                buckets.setdefault((ts - ts % step, lab), []).append(v)
            rows = [(b, lab, sum(vs) / len(vs)) for (b, lab), vs in sorted(buckets.items())]
        return [{"ts": ts, "labels": json.loads(lab), "value": v} for ts, lab, v in rows[-max(1, int(limit)):]]

    def events(self, since: Optional[float] = None, level: Optional[str] = None, limit: int = 200) -> list[dict[str, Any]]:
        q, args = "SELECT ts, level, kind, subject, message FROM events WHERE 1=1", []
        if since is not None:
            q += " AND ts >= ?"
            args.append(since)
        if level:
            q += " AND level = ?"
            args.append(level)
        q += " ORDER BY ts DESC, id DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        return [{"ts": a, "level": b, "kind": c, "subject": d, "message": e} for a, b, c, d, e in rows]

    def availability(self, name: str = "service_up", since: Optional[float] = None) -> dict[str, float]:
        """Share of samples in which each subject was up (0..100) - real history, not an estimate."""
        out: dict[str, float] = {}
        by: dict[str, list[float]] = {}
        for p in self.series(name, since=since, limit=1_000_000):
            by.setdefault(next(iter(p["labels"].values()), ""), []).append(p["value"])
        for k, vs in by.items():
            out[k] = round(100.0 * sum(1 for v in vs if v >= 1) / len(vs), 2)
        return out

    def count(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM metrics").fetchone()[0])

    # ------------------------------------------------------------------ alert state
    def alert_state(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT rule, subject, state, since, value, message, streak, notified FROM alerts ORDER BY rule, subject").fetchall()
        return [dict(zip(("rule", "subject", "state", "since", "value", "message", "streak", "notified"), r)) for r in rows]

    def set_alert(self, rule: str, subject: str, state: str, since: float, value: Optional[float], message: str, streak: int,
                  notified: Optional[float]) -> None:
        with self._tx() as db:
            db.execute("INSERT OR REPLACE INTO alerts (rule, subject, state, since, value, message, streak, notified) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (rule, subject, state, since, value, message[:500], streak, notified))
