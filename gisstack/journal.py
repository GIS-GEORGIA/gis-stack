"""Operation journal and operation lock (audit R-02, R-05).

Every deployment-style operation appends two JSON lines to `~/.gisstack/journal.jsonl` (start and end, with the outcome and
duration).  Nothing secret is stored: commands are not recorded, error text is passed through the runner's redaction.
`exclusive()` is a lock on this machine that stops two gis-stack processes (web GUI + CLI) from working at once."""
from __future__ import annotations

import contextlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from . import fsutil

MAX_BYTES = 5 * 1024 * 1024


class Busy(RuntimeError):
    """Another operation holds the lock."""


def _dir() -> Path:
    p = Path(os.environ.get("GISSTACK_HOME") or (Path.home() / ".gisstack"))
    return fsutil.private_dir(p)


def path() -> Path:
    return _dir() / "journal.jsonl"


def record(event: dict[str, Any]) -> None:
    p = path()
    with contextlib.suppress(OSError):
        if p.exists() and p.stat().st_size > MAX_BYTES:
            os.replace(p, p.with_suffix(".jsonl.1"))
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def tail(n: int = 50) -> list[dict[str, Any]]:
    p = path()
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines()[-max(1, n):]:
        with contextlib.suppress(ValueError):
            out.append(json.loads(line))
    return out


class Operation:
    """`with Operation("deploy", target=..., mode=..., host=..., project=...):` journals start and outcome."""

    def __init__(self, op: str, redact: Optional[Callable[[str], str]] = None, **meta: Any) -> None:
        self.id = uuid.uuid4().hex[:10]
        self.op, self.meta, self.redact = op, meta, redact or (lambda s: s)
        self.t0 = 0.0

    def __enter__(self) -> Operation:
        self.t0 = time.time()
        record({"id": self.id, "ts": int(self.t0), "op": self.op, "event": "start", **self.meta})
        return self

    def __exit__(self, et: Any, ev: Any, tb: Any) -> None:
        if ev is None:
            status, err = "ok", ""
        elif isinstance(ev, GeneratorExit):
            status, err = "aborted", ""            # the consumer stopped reading (cancelled)
        else:
            status, err = "error", self.redact(f"{type(ev).__name__}: {ev}")[:500]
        record({"id": self.id, "ts": int(time.time()), "op": self.op, "event": "end", "status": status,
                "seconds": round(time.time() - self.t0, 1), "error": err, **self.meta})


@contextlib.contextmanager
def exclusive(name: str = "operation") -> Iterator[None]:
    """Exclusive lock on this machine (flock); raises Busy at once when somebody else holds it."""
    try:
        import fcntl
    except ImportError:                              # Windows: no lock available, the in-process lock still applies
        yield
        return
    lock_dir = fsutil.private_dir(_dir() / "locks")
    fd = os.open(lock_dir / f"{name}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise Busy("სხვა gis-stack ოპერაცია უკვე მიმდინარეობს ამ კომპიუტერზე") from e
        yield
    finally:
        os.close(fd)             # closing releases the lock
