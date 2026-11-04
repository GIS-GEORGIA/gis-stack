"""Conformance kit for plugin authors.

    from gisstack.plugins.kit import ConformanceTests
    class TestMine(ConformanceTests):
        kind = "backup_provider"
        plugin = staticmethod(lambda: MyBackup())

or without unittest:  problems = kit.verify("backup_provider", MyBackup())  ->  [] when the plugin honours the contract."""
from __future__ import annotations

import functools
import inspect
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable

from . import loader
from .contract import KINDS, Plugin


def verify(kind: str, obj: Any) -> list[str]:
    """Every way `obj` (a class or instance) breaks the contract for `kind`; an empty list means it conforms."""
    if kind not in KINDS:
        return [f"unknown kind {kind!r} (known: {', '.join(KINDS)})"]
    try:
        p = loader.check(obj, kind)
    except Exception as e:  # noqa: BLE001
        return [str(e)]
    out: list[str] = []
    out += _check_kind(kind, p)
    return out


def _guarded(label: str, fn: Callable[[], Any], out: list[str]) -> Any:
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        out.append(f"{label} raised {type(e).__name__}: {e}")
        return None


def _check_kind(kind: str, p: Plugin) -> list[str]:
    out: list[str] = []
    if kind == "service_provider":
        prov = _guarded("provider()", p.provider, out)  # type: ignore[attr-defined]
        if prov is not None:
            for attr in ("entry", "clean_entry", "needs_root", "status_rows", "control_script", "logs_script", "clean_args", "apply_args"):
                if not hasattr(prov, attr):
                    out.append(f"provider() result lacks {attr}")
    elif kind == "monitoring_provider":
        notifiers = _guarded("notifiers()", p.notifiers, out)  # type: ignore[attr-defined]
        if notifiers is not None:
            if not isinstance(notifiers, list):
                out.append("notifiers() must return a list")
            else:
                for n in notifiers:
                    if not isinstance(getattr(n, "name", None), str) or not callable(getattr(n, "send", None)):
                        out.append(f"notifier {n!r} needs a str `name` and a send(note) method")
    elif kind == "backup_provider":
        sig = inspect.signature(p.store)  # type: ignore[attr-defined]
        if len(sig.parameters) < 2:
            out.append("store(archive, backup_id) must take two arguments")
        else:
            with tempfile.TemporaryDirectory() as td:
                f = Path(td) / "20260101-000000-aaaaaaaa.tar.gz"
                f.write_bytes(b"gisstack conformance archive")
                where = _guarded("store()", lambda: p.store(f, "20260101-000000-aaaaaaaa"), out)  # type: ignore[attr-defined]
                if where is not None and not (isinstance(where, str) and where):
                    out.append("store() must return a non-empty location string")
                if not f.exists() or f.read_bytes() != b"gisstack conformance archive":
                    out.append("store() must not alter or delete the archive")
    elif kind == "auth_provider":
        for bad in ("", "x", "Bearer", "\x00" * 4, "a" * 5000, "gsk_00000000_" + "z" * 32):
            r = _guarded(f"authenticate({bad[:12]!r})", functools.partial(p.authenticate, bad), out)  # type: ignore[attr-defined]
            if r is not None and not (isinstance(r, dict) and isinstance(r.get("name"), str) and r.get("scope") in ("read", "operate", "admin")):
                out.append(f"authenticate({bad[:12]!r}) must return None or {{'name', 'scope'}} with scope read/operate/admin, not {r!r}")
    elif kind == "data_source":
        layers = _guarded("layers()", p.layers, out)  # type: ignore[attr-defined]
        if layers is not None:
            if not isinstance(layers, list):
                out.append("layers() must return a list")
            else:
                for lay in layers:
                    if not (isinstance(lay, dict) and all(isinstance(lay.get(k), str) and lay[k] for k in ("id", "title", "kind"))):
                        out.append(f"layer {lay!r} needs non-empty str id, title and kind")
    return out


class ConformanceTests(unittest.TestCase):
    """Subclass, set `kind` and `plugin` (a zero-argument factory) and the contract is tested for you."""
    kind: str = ""
    plugin: Callable[[], Any]

    @classmethod
    def setUpClass(cls) -> None:
        if cls is ConformanceTests:
            raise unittest.SkipTest("abstract")

    def test_conforms(self) -> None:
        self.assertEqual(verify(self.kind, type(self).plugin()), [])
