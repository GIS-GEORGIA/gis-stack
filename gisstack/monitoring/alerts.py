"""Threshold alerts (Phase 8).  A rule watches one metric; every series of that metric (one per label set - a service, a disk, a
probe) has its own state: ok -> firing after `for` consecutive samples over the threshold, firing -> ok (resolved) on the first
sample back inside it.  Transitions become events and notifications.  A series that is absent from a sample (collector failed) is
"no data": it neither fires nor resolves anything, so a broken collector can never produce a false alarm or hide a real one.

Rules are plain data; overrides live in ~/.gisstack/monitor/rules.json and are validated strictly."""
from __future__ import annotations

import json
import operator
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
    "collector_errors": {"metric": "collect_errors", "op": ">", "threshold": 0.0, "for": 3, "level": "warn", "text": "{value:.0f} metric source(s) unavailable"},
}
_ALLOWED_OVERRIDE = {"threshold", "for", "level", "enabled", "op", "renotify"}
DEFAULT_RENOTIFY = 6 * 3600.0


class RulesError(GisStackError):
    code = "rules"


def load_rules(path: Optional[Path] = None) -> dict[str, dict[str, Any]]:
    """Default rules with the user's overrides applied.  Unknown rule ids, unknown keys and out-of-range values are errors."""
    rules = {k: dict(v, enabled=True) for k, v in DEFAULT_RULES.items()}
    if path is None or not Path(path).is_file():
        return rules
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise RulesError(f"{path}: {e}") from e
    over = data.get("rules") if isinstance(data, dict) else None
    if not isinstance(over, dict):
        raise RulesError(f'{path}: expected {{"rules": {{...}}}}')
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
    return rules


def subject_of(labels: Labels) -> str:
    return ",".join(f"{v}" for _, v in sorted(labels.items())) or "-"


def evaluate(store: Store, rules: dict[str, dict[str, Any]], metrics: list[tuple[str, Labels, float]], ts: float, context: Optional[dict[str, str]] = None
             ) -> list[dict[str, Any]]:
    """Advance every rule's state machine with one sample.  Returns the notifications to send (firing / resolved / reminders);
    state changes and events are already written to the store."""
    context = context or {}
    prior = {(a["rule"], a["subject"]): a for a in store.alert_state()}
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
            if cmp(value, rule["threshold"]):
                streak += 1
                if state != "firing" and streak >= rule["for"]:
                    state, since, notified = "firing", ts, ts
                    store.add_event(ts, rule["level"], "alert.firing", f"{rid}:{subject}", text)
                    out.append({**note, "state": "firing"})
                elif state == "firing" and rule.get("renotify", DEFAULT_RENOTIFY) and ts - (notified or since) >= rule.get("renotify", DEFAULT_RENOTIFY):
                    notified = ts
                    out.append({**note, "state": "firing", "reminder": True})
            else:
                if state == "firing":
                    store.add_event(ts, "info", "alert.resolved", f"{rid}:{subject}", f"resolved: {text}")
                    out.append({**note, "state": "resolved", "level": "info", "message": f"resolved: {text}"})
                    state, since, notified = "ok", ts, None
                streak = 0
            store.set_alert(rid, subject, state, since, value, text, streak, notified)
    return out
