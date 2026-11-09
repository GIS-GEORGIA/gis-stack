"""Editing of alert rules (the dashboard / API side of `alerts.py`).

One JSON file, `~/.gisstack/monitor/rules.json`: {"rules": {built-in id: overrides}, "custom": {id: full rule}}.  Built-in rules always
exist and can be tuned, disabled or reset but never deleted; custom rules are created, changed and deleted freely.

Every change is a read-modify-write under an exclusive file lock, guarded by a *revision* (SHA-256 of the file) the caller read earlier:
a stale revision is a conflict, so two administrators can never silently overwrite each other.  The new content is validated by the same
strict loader the monitor uses before it replaces the file atomically (0600).  Values equal to a built-in default are not stored."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from .. import fsutil
from ..core import paths
from ..core.errors import GisStackError
from . import alerts
from .alerts import RulesError

EDITABLE = ("threshold", "for", "level", "enabled", "renotify")       # what may change on a built-in rule (`op` stays a hand edit)
NO_FILE = "0"


class RulesConflict(GisStackError):
    code = "conflict"


class RulesNotFound(GisStackError):
    code = "not_found"


def rules_path() -> Path:
    return paths.home() / "monitor" / "rules.json"


def _read(p: Path) -> tuple[str, dict[str, Any]]:
    """(revision, parsed content).  A damaged file is an error and is never overwritten."""
    if not p.is_file():
        return NO_FILE, {"rules": {}, "custom": {}}
    raw = p.read_bytes()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise RulesError(f"{p.name}: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("rules", {}), dict) or not isinstance(data.get("custom", {}), dict):
        raise RulesError(f'{p.name}: expected {{"rules": {{...}}, "custom": {{...}}}}')
    return hashlib.sha256(raw).hexdigest()[:16], {"rules": dict(data.get("rules", {})), "custom": dict(data.get("custom", {}))}


@contextlib.contextmanager
def _locked(p: Path) -> Iterator[None]:
    fsutil.private_dir(p.parent)
    fd = os.open(p.parent / ".rules.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)                                       # closing releases the lock


def _row(rid: str, r: dict[str, Any], builtin: bool) -> dict[str, Any]:
    out: dict[str, Any] = {"id": rid, "builtin": builtin, "metric": r["metric"], "op": r["op"], "threshold": r["threshold"], "for": r["for"], "level": r["level"],
                           "enabled": r["enabled"], "text": r["text"], "description": r.get("description", ""),
                           "renotify": r.get("renotify", alerts.DEFAULT_RENOTIFY)}
    if builtin:
        d = dict(alerts.DEFAULT_RULES[rid], enabled=True, renotify=alerts.DEFAULT_RENOTIFY)
        out["default"] = {k: d[k] for k in EDITABLE}
        out["modified"] = any(out[k] != d[k] for k in EDITABLE)
    else:
        out["modified"] = True
    return out


def view(path: Optional[Path] = None) -> dict[str, Any]:
    """Revision, last modification time (None while no file exists) and every effective rule.  Never creates the file."""
    p = path or rules_path()
    rev, data = _read(p)
    eff = alerts.apply_overrides(data["rules"], p.name, data["custom"])
    rows = [_row(rid, r, rid in alerts.DEFAULT_RULES) for rid, r in eff.items()]
    return {"revision": rev, "modified": p.stat().st_mtime if p.is_file() else None, "metrics": list(alerts.KNOWN_METRICS), "rules": rows}


def mutate(change: Callable[[dict[str, Any]], None], revision: str, path: Optional[Path] = None) -> dict[str, Any]:
    p = path or rules_path()
    with _locked(p):
        rev, data = _read(p)
        if rev != revision:
            raise RulesConflict("the rules were changed by someone else - reload and try again")
        change(data)
        alerts.apply_overrides(data["rules"], p.name, data["custom"])          # validate everything before writing anything
        fsutil.write_private(p, json.dumps({"rules": data["rules"], "custom": data["custom"]}, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    return view(p)


def create(rid: str, rule: dict[str, Any], revision: str, path: Optional[Path] = None) -> dict[str, Any]:
    def go(d: dict[str, Any]) -> None:
        if rid in alerts.DEFAULT_RULES or rid in d["custom"]:
            raise RulesConflict(f"a rule named {rid!r} already exists")
        d["custom"][rid] = rule
    return mutate(go, revision, path)


def update(rid: str, fields: dict[str, Any], revision: str, path: Optional[Path] = None) -> dict[str, Any]:
    def go(d: dict[str, Any]) -> None:
        if rid in d["custom"]:
            d["custom"][rid] = {**d["custom"][rid], **fields}
        elif rid in alerts.DEFAULT_RULES:
            bad = set(fields) - set(EDITABLE)
            if bad:
                raise RulesError(f"built-in rule {rid}: only {', '.join(EDITABLE)} can be changed (not {', '.join(sorted(bad))})")
            base = dict(alerts.DEFAULT_RULES[rid], enabled=True, renotify=alerts.DEFAULT_RENOTIFY)
            cur = dict(d["rules"].get(rid) or {})
            for k, v in fields.items():
                if v == base[k]:
                    cur.pop(k, None)
                else:
                    cur[k] = v
            if cur:
                d["rules"][rid] = cur
            else:
                d["rules"].pop(rid, None)
        else:
            raise RulesNotFound(f"no rule named {rid!r}")
    return mutate(go, revision, path)


def reset(rid: str, revision: str, path: Optional[Path] = None) -> dict[str, Any]:
    def go(d: dict[str, Any]) -> None:
        if rid not in alerts.DEFAULT_RULES:
            raise RulesError(f"{rid!r} is not a built-in rule")
        keep = {k: v for k, v in (d["rules"].get(rid) or {}).items() if k not in EDITABLE}
        if keep:
            d["rules"][rid] = keep
        else:
            d["rules"].pop(rid, None)
    return mutate(go, revision, path)


def delete(rid: str, revision: str, path: Optional[Path] = None) -> dict[str, Any]:
    def go(d: dict[str, Any]) -> None:
        if rid in alerts.DEFAULT_RULES:
            raise RulesError(f"{rid!r} is a built-in rule: disable it instead of deleting it")
        if rid not in d["custom"]:
            raise RulesNotFound(f"no rule named {rid!r}")
        del d["custom"][rid]
    return mutate(go, revision, path)
