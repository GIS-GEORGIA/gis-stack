"""Logging with secret redaction (audit A-06).  Library code uses `get(__name__)`; entry points call `configure()`.
Nothing is logged unless the user asks (`--log-level` / GISSTACK_LOG), and the log file is owner-only."""
from __future__ import annotations

import logging
import os
from typing import Iterable

from . import paths

_ROOT = "gisstack"
_secrets: set[str] = set()


class _Redact(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for s in sorted(_secrets, key=len, reverse=True):
            msg = msg.replace(s, "••••••••")
        record.msg, record.args = msg, ()
        return True


def get(name: str) -> logging.Logger:
    return logging.getLogger(name if name.startswith(_ROOT) else f"{_ROOT}.{name}")


def add_secrets(values: Iterable[str]) -> None:
    _secrets.update(v for v in values if v and len(v) >= 4)


def configure(level: str | None = None, to_file: bool = False) -> None:
    """level: debug|info|warning|error (default: $GISSTACK_LOG, else nothing is emitted)."""
    level = level or os.environ.get("GISSTACK_LOG")
    root = logging.getLogger(_ROOT)
    for h in [h for h in root.handlers if getattr(h, "_gisstack", False)]:
        root.removeHandler(h)
        h.close()
    if not level:
        root.addHandler(logging.NullHandler())
        return
    lv = getattr(logging, level.upper(), None)
    if not isinstance(lv, int):
        raise ValueError(f"unknown log level: {level}")
    handler: logging.Handler
    if to_file:
        p = paths.log_file()
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.close(fd)
        handler = logging.FileHandler(p, encoding="utf-8")
    else:
        handler = logging.StreamHandler()
    setattr(handler, "_gisstack", True)  # noqa: B010
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(_Redact())
    root.addHandler(handler)
    root.setLevel(lv)
