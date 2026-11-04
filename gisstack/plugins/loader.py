"""Find, validate and cache plugins.  A broken plugin is reported, never fatal: the rest of the program runs as if it were absent.

Plugins are only loaded when the `plugins` feature flag is on (GISSTACK_FEATURES=plugins) because they run code from other packages."""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from importlib import metadata
from typing import Any, Callable, Iterable, Optional

from ..core import features, log
from .contract import API_VERSION, KINDS, Plugin

_log = log.get("plugins")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


@dataclass
class Failure:
    kind: str
    entry: str
    reason: str


_lock = threading.Lock()
_cache: Optional[tuple[dict[str, dict[str, Plugin]], list[Failure]]] = None
def _installed(group: str) -> Iterable[Any]:
    return metadata.entry_points(group=group)


# group -> iterable of entry points; replaced by tests and by the conformance kit
_source: Callable[[str], Iterable[Any]] = _installed


def set_source(fn: Optional[Callable[[str], Iterable[Any]]]) -> None:
    """Replace where entry points come from (tests); None restores the installed distributions.  Clears the cache."""
    global _source
    _source = fn or _installed
    reload()


def reload() -> None:
    global _cache
    with _lock:
        _cache = None


def check(obj: Any, kind: str) -> Plugin:
    """Turn what an entry point loaded into a validated plugin instance, or raise ValueError saying why not."""
    group, base = KINDS[kind]
    inst = obj() if isinstance(obj, type) else obj
    if not isinstance(inst, base):
        raise ValueError(f"must derive from gisstack.plugins.contract.{base.__name__}")
    if getattr(inst, "api_version", None) != API_VERSION:
        raise ValueError(f"written for plugin API {getattr(inst, 'api_version', '?')}, this gis-stack speaks {API_VERSION}")
    if not isinstance(inst.name, str) or not NAME_RE.match(inst.name):
        raise ValueError("name must match [a-z0-9][a-z0-9_-]{0,39}")
    return inst


def _load() -> tuple[dict[str, dict[str, Plugin]], list[Failure]]:
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        found: dict[str, dict[str, Plugin]] = {k: {} for k in KINDS}
        failed: list[Failure] = []
        for kind, (group, _base) in KINDS.items():
            try:
                eps = list(_source(group))
            except Exception as e:  # noqa: BLE001 - a broken metadata directory must not stop the program
                failed.append(Failure(kind, group, f"{type(e).__name__}: {e}"))
                continue
            for ep in eps:
                label = f"{ep.name} = {getattr(ep, 'value', '?')}"
                try:
                    plugin = check(ep.load(), kind)
                    if plugin.name in found[kind]:
                        raise ValueError(f"duplicate {kind} name {plugin.name!r}")
                    found[kind][plugin.name] = plugin
                except Exception as e:  # noqa: BLE001 - third-party code: anything can go wrong
                    _log.warning("plugin %s (%s) not loaded: %s", label, kind, e)
                    failed.append(Failure(kind, label, f"{type(e).__name__}: {e}"))
        _cache = (found, failed)
        return _cache


def active() -> bool:
    return features.enabled("plugins")


def get(kind: str) -> list[Plugin]:
    """Loaded plugins of one kind (empty while the feature flag is off)."""
    if kind not in KINDS:
        raise KeyError(kind)
    return list(_load()[0][kind].values()) if active() else []


def failures() -> list[Failure]:
    return list(_load()[1]) if active() else []


def describe() -> dict[str, Any]:
    """What /api/v1/plugins shows."""
    return {
        "enabled": active(), "api_version": API_VERSION,
        "plugins": [{"kind": k, "name": p.name, "description": p.description, "api_version": p.api_version} for k in KINDS for p in get(k)],
        "failures": [{"kind": f.kind, "entry": f.entry, "reason": f.reason} for f in failures()],
    }


def safe_call(plugin: Plugin, method: str, *args: Any, default: Any = None) -> Any:
    """Call a plugin method; any exception is logged and replaced by `default` (a plugin must never break core operations)."""
    try:
        return getattr(plugin, method)(*args)
    except Exception as e:  # noqa: BLE001
        _log.warning("plugin %s.%s failed: %s", plugin.name, method, type(e).__name__)
        return default
