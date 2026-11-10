"""Threshold alerts (Phase 8).  A rule watches one metric; every series of that metric (one per label set - a service, a disk, a
probe) has its own state: ok -> firing after `for` consecutive samples over the threshold, firing -> ok (resolved) on the first
sample back inside it.  Transitions become events and notifications.  A series that is absent from a sample (collector failed) is
"no data": it neither fires nor resolves anything, so a broken collector can never produce a false alarm or hide a real one.

Rules are plain data; overrides live in ~/.gisstack/monitor/rules.json and are validated strictly."""
from __future__ import annotations

import json
import operator
import re
import string
from pathlib import Path
from typing import Any, Callable, Optional

from ..core.errors import GisStackError
from .store import Labels, Store

OPS: dict[str, Callable[[float, float], bool]] = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le, "==": operator.eq}
LEVELS = ("info", "warn", "crit")

DEFAULT_RULES: dict[str, dict[str, Any]] = {
    "cpu_high": {"metric": "host_cpu_percent", "op": ">", "threshold": 90.0, "for": 5, "level": "warn", "text": "CPU is at {value:.0f} %"},
    "memory_high": {"metric": "host_mem_used_percent", "op": ">", "threshold": 90.0, "for": 3, "level": "warn", "text": "memory is {value:.0f} % used"},
    "memory_critical": {"metric": "host_mem_used_percent", "op": ">", "threshold": 97.0, "for": 2, "level": "crit", "text": "memory is {value:.0f} % used"},
    "swap_high": {"metric": "host_swap_used_percent", "op": ">", "threshold": 50.0, "for": 5, "level": "warn", "text": "swap is {value:.0f} % used"},
    "disk_high": {"metric": "disk_used_percent", "op": ">", "threshold": 85.0, "for": 1, "level": "warn", "text": "disk {subject} is {value:.0f} % full"},
    "disk_critical": {"metric": "disk_used_percent", "op": ">", "threshold": 95.0, "for": 1, "level": "crit", "text": "disk {subject} is {value:.0f} % full"},
    "service_down": {"metric": "service_up", "op": "<", "threshold": 1.0, "for": 2, "level": "crit", "text": "service {subject} is not running"},
    "probe_failing": {"metric": "probe_up", "op": "<", "threshold": 1.0, "for": 3, "level": "warn", "text": "health check {subject} fails"},
    "probe_slow": {"metric": "probe_seconds", "op": ">", "threshold": 5.0, "for": 3, "level": "warn", "text": "health check {subject} answers in {value:.1f} s"},
    "postgres_down": {"metric": "pg_up", "op": "<", "threshold": 1.0, "for": 2, "level": "crit", "text": "PostgreSQL does not answer"},
    "postgres_connections": {"metric": "pg_connections_percent", "op": ">", "threshold": 80.0, "for": 2, "level": "warn", "text": "PostgreSQL connections at {value:.0f} % of max_connections"},
    "postgres_long_query": {"metric": "pg_longest_query_seconds", "op": ">", "threshold": 300.0, "for": 1, "level": "warn", "text": "a query has been running for {value:.0f} s"},
    "postgres_locks": {"metric": "pg_waiting_locks", "op": ">", "threshold": 5.0, "for": 3, "level": "warn", "text": "{value:.0f} sessions are waiting for locks"},
    "backup_old": {"metric": "backup_last_age_seconds", "op": ">", "threshold": 36 * 3600.0, "for": 1, "level": "warn", "text": "the newest backup is {value:.0f} s old"},
    "backup_failed": {"metric": "backup_failed_count", "op": ">", "threshold": 0.0, "for": 1, "level": "crit", "text": "{value:.0f} backup attempt(s) failed since the newest good backup"},
    "host_unreachable": {"metric": "monitor_reachable", "op": "<", "threshold": 1.0, "for": 3, "level": "crit", "text": "the machine cannot be reached"},
    "collector_errors": {"metric": "collect_errors", "op": ">", "threshold": 0.0, "for": 3, "level": "warn", "text": "{value:.0f} metric source(s) unavailable"},
}
KNOWN_METRICS = ("host_cpu_percent", "host_mem_used_percent", "host_swap_used_percent", "host_load1", "disk_used_percent", "service_up", "service_cpu_percent",
                 "service_rss_bytes", "probe_up", "probe_seconds", "pg_up", "pg_connections_percent", "pg_longest_query_seconds", "pg_waiting_locks",
                 "pg_database_size_bytes", "pg_cache_hit_ratio", "mapproxy_cache_bytes", "backup_last_age_seconds", "backup_failed_count", "monitor_reachable", "collect_errors")
CUSTOM_ID = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
CUSTOM_KEYS = {"metric", "op", "threshold", "for", "level", "text", "description", "enabled", "renotify"}
MAX_RENOTIFY = 30 * 86400.0
_SPEC = re.compile(r"^\.?[0-9]{0,2}[fg%]?$")
_ALLOWED_OVERRIDE = {"threshold", "for", "level", "enabled", "op", "renotify"}
DEFAULT_RENOTIFY = 6 * 3600.0


class RulesError(GisStackError):
    code = "rules"


def read_overrides(path: Optional[Path]) -> dict[str, dict[str, Any]]:
    """The user's raw overrides from the rules file (no file = none).  Structure errors are RulesError."""
    if path is None or not Path(path).is_file():
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise RulesError(f"{path}: {e}") from e
    over = data.get("rules") if isinstance(data, dict) else None
    if not isinstance(over, dict):
        raise RulesError(f'{path}: expected {{"rules": {{...}}}}')
    return over


def check_text(rid: str, text: Any, where: str) -> str:
    """Alert text is a str.format template with only {value} and {subject} (plain format specs): no attribute access, no conversions."""
    if not isinstance(text, str) or not 1 <= len(text) <= 200 or any(ord(c) < 32 for c in text):
        raise RulesError(f"{where}: {rid}.text must be 1-200 characters without control characters")
    try:
        for _lit, name, spec, conv in string.Formatter().parse(text):
            if name is None:
                continue
            if name not in ("value", "subject") or conv or (spec and not _SPEC.match(spec)):
                raise ValueError(name)
        text.format(value=1.5, subject="x")
    except (ValueError, KeyError, IndexError):
        raise RulesError(f"{where}: {rid}.text may use only {{value}} (e.g. {{value:.0f}}) and {{subject}}") from None
    return text


def custom_rule(rid: str, o: Any, where: str = "rules") -> dict[str, Any]:
    """Validate one user-defined rule (all of metric/op/threshold/for/level/text are required) and return it with defaults filled in."""
    if not CUSTOM_ID.match(rid or ""):
        raise RulesError(f"{where}: rule id must be 3-41 characters: a-z, 0-9 and _, starting with a letter")
    if rid in DEFAULT_RULES:
        raise RulesError(f"{where}: {rid!r} is a built-in rule id")
    if not isinstance(o, dict) or set(o) - CUSTOM_KEYS:
        raise RulesError(f"{where}: rule {rid}: allowed keys are {sorted(CUSTOM_KEYS)}")
    missing = [k for k in ("metric", "op", "threshold", "for", "level", "text") if k not in o]
    if missing:
        raise RulesError(f"{where}: rule {rid}: missing {', '.join(missing)}")
    if o["metric"] not in KNOWN_METRICS:
        raise RulesError(f"{where}: {rid}.metric must be one of {list(KNOWN_METRICS)}")
    r = dict(o, enabled=o.get("enabled", True))
    r.update({k: v for k, v in apply_overrides({}, where, scratch=(rid, {k: o[k] for k in ("threshold", "for", "level", "op", "enabled", "renotify") if k in o}))[rid].items()
              if k in ("threshold", "for", "level", "op", "enabled", "renotify")})
    r["text"] = check_text(rid, o["text"], where)
    desc = o.get("description", "")
    if not isinstance(desc, str) or len(desc) > 200 or any(ord(c) < 32 for c in desc):
        raise RulesError(f"{where}: {rid}.description must be at most 200 characters without control characters")
    r["description"] = desc
    if "renotify" in r and r["renotify"] > MAX_RENOTIFY:
        raise RulesError(f"{where}: {rid}.renotify must be at most {int(MAX_RENOTIFY)} seconds")
    return r


def apply_overrides(over: dict[str, Any], where: str = "rules", custom: Optional[dict[str, Any]] = None,
                    scratch: Optional[tuple[str, dict[str, Any]]] = None) -> dict[str, dict[str, Any]]:
    """Default rules with `over` applied.  Unknown rule ids, unknown keys and out-of-range values are errors."""
    rules = {k: dict(v, enabled=True) for k, v in DEFAULT_RULES.items()}
    if scratch is not None:                                  # validate a custom rule's overridable fields with the very same code
        rules = {scratch[0]: dict(DEFAULT_RULES["cpu_high"], enabled=True)}
        over = {scratch[0]: scratch[1]}
    path = where
    for rid, o in over.items():
        if rid not in rules:
            raise RulesError(f"{path}: unknown rule {rid!r} (known: {', '.join(sorted(rules))})")
        if not isinstance(o, dict) or set(o) - _ALLOWED_OVERRIDE:
            raise RulesError(f"{path}: rule {rid}: allowed keys are {sorted(_ALLOWED_OVERRIDE)}")
        r = rules[rid]
        if "threshold" in o:
            if isinstance(o["threshold"], bool) or not isinstance(o["threshold"], (int, float)):
                raise RulesError(f"{path}: {rid}.threshold must be a number")
            r["threshold"] = float(o["threshold"])
        if "for" in o:
            if isinstance(o["for"], bool) or not isinstance(o["for"], int) or not 1 <= o["for"] <= 1000:
                raise RulesError(f"{path}: {rid}.for must be an integer 1..1000")
            r["for"] = o["for"]
        if "level" in o:
            if o["level"] not in LEVELS[1:]:
                raise RulesError(f"{path}: {rid}.level must be warn or crit")
            r["level"] = o["level"]
        if "op" in o:
            if o["op"] not in OPS:
                raise RulesError(f"{path}: {rid}.op must be one of {list(OPS)}")
            r["op"] = o["op"]
        if "enabled" in o:
            if not isinstance(o["enabled"], bool):
                raise RulesError(f"{path}: {rid}.enabled must be true or false")
            r["enabled"] = o["enabled"]
        if "renotify" in o:
            if isinstance(o["renotify"], bool) or not isinstance(o["renotify"], (int, float)) or o["renotify"] < 0:
                raise RulesError(f"{path}: {rid}.renotify must be seconds >= 0")
            r["renotify"] = float(o["renotify"])
    for cid, c in (custom or {}).items():
        rules[cid] = custom_rule(cid, c, where)
    return rules


def read_custom(path: Optional[Path]) -> dict[str, Any]:
    if path is None or not Path(path).is_file():
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}                                            # read_overrides reports the damage
    c = data.get("custom", {}) if isinstance(data, dict) else {}
    if not isinstance(c, dict):
        raise RulesError(f'{path}: "custom" must be an object')
    return c


def load_rules(path: Optional[Path] = None) -> dict[str, dict[str, Any]]:
    """Default rules with the user's overrides applied, plus user-defined ("custom") rules."""
    return apply_overrides(read_overrides(path), str(path), read_custom(path))


def subject_of(labels: Labels) -> str:
    return ",".join(f"{v}" for _, v in sorted(labels.items())) or "-"


SUPPRESSED = -1.0          # `notified` marker: firing, but held back because its cause is already being reported


def cause_of(rule: str, subject: str) -> Optional[tuple[str, str]]:
    """The alert that already explains this one, so one outage produces one page instead of five.  A failing health check of a
    component is explained by that component's service being down (every ':public' check goes through Caddy); PostgreSQL not
    answering is explained by its service being down."""
    if rule in ("probe_failing", "probe_slow") and ":" in subject:
        comp, _, what = subject.partition(":")
        return ("service_down", "caddy" if what == "public" else comp)
    if rule in ("postgres_down", "postgres_connections", "postgres_long_query", "postgres_locks"):
        return ("service_down", "postgis")
    return None


def evaluate(store: Store, rules: dict[str, dict[str, Any]], metrics: list[tuple[str, Labels, float]], ts: float, context: Optional[dict[str, str]] = None
             ) -> list[dict[str, Any]]:
    """Advance every rule's state machine with one sample.  Returns the notifications to send (firing / resolved / reminders);
    state changes and events are already written to the store."""
    context = context or {}
    prior = {(a["rule"], a["subject"]): a for a in store.alert_state()}
    firing_now = {k for k, a in prior.items() if a["state"] == "firing"}
    out: list[dict[str, Any]] = []
    for rid, rule in rules.items():
        if not rule.get("enabled", True):
            continue
        cmp = OPS[rule["op"]]
        for name, labels, value in metrics:
            if name != rule["metric"]:
                continue
            subject = subject_of(labels)
            cur = prior.get((rid, subject))
            state = cur["state"] if cur else "ok"
            streak = cur["streak"] if cur else 0
            since = cur["since"] if cur else ts
            notified = cur["notified"] if cur else None
            text = rule["text"].format(value=value, subject=subject)
            note = {"rule": rid, "subject": subject, "level": rule["level"], "value": value, "ts": ts, "message": text, "metric": name,
                    "threshold": rule["threshold"], **context}
            cause = cause_of(rid, subject)
            explained = cause is not None and cause in firing_now and cause != (rid, subject)
            if cmp(value, rule["threshold"]):
                streak += 1
                if state != "firing" and streak >= rule["for"]:
                    state, since = "firing", ts
                    if explained:
                        notified = SUPPRESSED
                        store.add_event(ts, "info", "alert.suppressed", f"{rid}:{subject}", f"{text} (explained by {cause[0]}:{cause[1]})")      # type: ignore[index]
                    else:
                        notified = ts
                        store.add_event(ts, rule["level"], "alert.firing", f"{rid}:{subject}", text)
                        out.append({**note, "state": "firing"})
                elif state == "firing" and notified == SUPPRESSED and not explained:
                    notified = ts                                   # its cause is gone but it is still broken: report it now
                    store.add_event(ts, rule["level"], "alert.firing", f"{rid}:{subject}", text)
                    out.append({**note, "state": "firing"})
                elif state == "firing" and notified not in (None, SUPPRESSED) and rule.get("renotify", DEFAULT_RENOTIFY) \
                        and ts - (notified or since) >= rule.get("renotify", DEFAULT_RENOTIFY):
                    notified = ts
                    out.append({**note, "state": "firing", "reminder": True})
                if state == "firing":
                    firing_now.add((rid, subject))
            else:
                if state == "firing":
                    store.add_event(ts, "info", "alert.resolved", f"{rid}:{subject}", f"resolved: {text}")
                    if notified != SUPPRESSED:                        # nobody was told it was broken, so nobody is told it is fixed
                        out.append({**note, "state": "resolved", "level": "info", "message": f"resolved: {text}"})
                    state, since, notified = "ok", ts, None
                    firing_now.discard((rid, subject))
                streak = 0
            store.set_alert(rid, subject, state, since, value, text, streak, notified)
    return out
