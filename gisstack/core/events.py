"""Tiny synchronous event bus (audit A-06): operations announce what they do, anything may listen (journal, GUI, tests, the
future monitoring dashboard).  A failing listener never breaks the operation that emitted the event."""
from __future__ import annotations

import contextlib
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

Listener = Callable[["Event"], None]


@dataclass(frozen=True)
class Event:
    kind: str                       # e.g. "operation.start", "operation.end"
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)


_listeners: list[Listener] = []
_lock = threading.Lock()


def subscribe(fn: Listener) -> Callable[[], None]:
    """Register a listener; returns a function that unregisters it."""
    with _lock:
        _listeners.append(fn)

    def unsubscribe() -> None:
        with _lock:
            if fn in _listeners:
                _listeners.remove(fn)
    return unsubscribe


def emit(kind: str, message: str = "", **data: Any) -> Event:
    ev = Event(kind, message, data)
    with _lock:
        targets = list(_listeners)
    for fn in targets:
        with contextlib.suppress(Exception):          # observers must not break the emitter
            fn(ev)
    return ev
