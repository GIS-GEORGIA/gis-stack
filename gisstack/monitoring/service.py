"""The monitoring loop (Phase 8): sample -> store -> evaluate alerts -> notify -> prune.  Also read-side helpers for the CLI and web API
(snapshot, Prometheus text)."""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .. import transport
from ..core import events, log, paths
from ..core.errors import DeployError
from . import alerts, collect, notify
from .store import Store

_log = log.get("monitor")
MIN_INTERVAL = 10.0
PRUNE_EVERY = 3600.0


def rules_path() -> Path:
    return paths.home() / "monitor" / "rules.json"


def notify_path() -> Path:
    return paths.home() / "monitor" / "notify.json"


class Monitor:
    def __init__(self, cfg: dict[str, Any], mode: str, ssh: Optional[dict[str, Any]] = None, target: Optional[str] = None,
                 store: Optional[Store] = None, rules: Optional[dict[str, dict[str, Any]]] = None, providers: Optional[list[Any]] = None,
                 clock: Any = time.time) -> None:
        self.cfg, self.mode, self.ssh, self.target = cfg, mode, ssh, target
        self.host = transport.host_label(mode, ssh)
        self.store = store or Store(paths.monitor_db(cfg["project"], self.host))
        self.rules = rules if rules is not None else alerts.load_rules(rules_path())
        self.providers = providers if providers is not None else notify.load_providers(notify_path()) + notify.plugin_notifiers()
        self.clock = clock
        self._last_prune = 0.0
        self.outbox = notify.Outbox(paths.monitor_db(cfg["project"], self.host).with_suffix(".outbox.json"))

    def tick(self) -> dict[str, Any]:
        """One round.  An unreachable machine is itself a recorded fact (`monitor_reachable` = 0), never a gap that hides an outage."""
        ctx = {"project": self.cfg["project"], "host": self.host}
        try:
            sample = collect.collect(self.cfg, self.mode, self.ssh, self.target)
            flat = collect.flatten(sample)
            ts, errors = sample["ts"], sample["errors"]
            if collect.common.resolve_target(self.cfg, self.target) != "native":
                extra, extra_errors = collect.container_extras(self.cfg, self.mode, self.ssh, ts)
                have = {n for n, _, _ in flat}
                flat += [m for m in extra if m[0] not in have]
                errors = list(errors) + extra_errors
                flat = [m for m in flat if m[0] != "collect_errors"] + [("collect_errors", {}, float(len(errors)))]
        except DeployError as e:
            ts, sample, errors = self.clock(), None, [f"unreachable: {e}"]
            flat = [("monitor_reachable", {}, 0.0)]
            self.store.add_event(ts, "warn", "collect.failed", self.host, str(e))
        self.store.add_sample(ts, flat, errors)
        notes = alerts.evaluate(self.store, self.rules, flat, ts, ctx)
        results = self._deliver(notes)
        for n in notes:
            events.emit("monitor.alert", n["message"], **{k: n[k] for k in ("rule", "subject", "level", "state")})
        if self.clock() - self._last_prune >= PRUNE_EVERY:
            self.store.prune(self.clock())
            self._last_prune = self.clock()
        return {"sample": sample, "metrics": flat, "notifications": notes, "delivery": results, "errors": errors}

    def _deliver(self, notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Retry what earlier rounds could not deliver, then send the new notifications; whatever fails is queued (never lost, never reordered)."""
        now = self.clock()
        event = lambda level, who, msg: self.store.add_event(self.clock(), level, "notify.retried" if level == "info" else "notify.failed", who, msg)  # noqa: E731
        for old in self.outbox.expire(now):
            self.store.add_event(now, "warn", "notify.dropped", old["provider"], f"undeliverable for over a day, dropped: {old['note'].get('rule')} {old['note'].get('state')}")
        self.outbox.flush(now, self.providers, event)
        immediate: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        queued_for = {p.name for p in self.providers if self.outbox.pending(p.name)}
        for p in self.providers:
            if p.name in queued_for:                                                  # an older notice is still waiting: keep the order
                for n in notes:
                    self.outbox.add(now, p.name, n)
                    results.append({"provider": p.name, "rule": n["rule"], "subject": n["subject"], "ok": False, "error": "queued behind an undelivered notification"})
            else:
                immediate.append(p)
        failed: list[tuple[str, dict[str, Any]]] = []
        results += notify.dispatch(notes, immediate, on_failure=lambda name, msg: self.store.add_event(self.clock(), "warn", "notify.failed", name, msg), failed=failed)
        for name, n in failed:
            self.outbox.add(now, name, n)
        self.outbox.save()
        return results

    def watch(self, interval: float = 30.0, stop: Optional[threading.Event] = None, max_ticks: Optional[int] = None, on_tick: Any = None) -> int:
        """Run `tick` every `interval` seconds until `stop` is set.  Never dies of a single failure."""
        stop = stop or threading.Event()
        interval = max(MIN_INTERVAL, float(interval))
        n = 0
        while not stop.is_set():
            t0 = time.monotonic()
            try:
                res = self.tick()
                if on_tick:
                    on_tick(res)
            except Exception as e:  # noqa: BLE001 - keep watching; the next round may work
                _log.error("monitor round failed: %s", e)
                self.store.add_event(self.clock(), "warn", "monitor.error", self.host, f"{type(e).__name__}: {e}")
            n += 1
            if max_ticks is not None and n >= max_ticks:
                break
            stop.wait(max(0.0, interval - (time.monotonic() - t0)))
        return n


# --------------------------------------------------------------------------- read side
def snapshot(store: Store, now: Optional[float] = None) -> dict[str, Any]:
    """Newest values + active alerts + availability, straight from the history."""
    ts = store.latest_ts()
    if ts is None:
        return {"ts": None, "age_seconds": None, "metrics": {}, "alerts": [], "availability": {}}
    now = now or time.time()
    return {"ts": ts, "age_seconds": round(max(0.0, now - ts), 1), "metrics": store.latest(),
            "alerts": [a for a in store.alert_state() if a["state"] == "firing"],
            "availability": store.availability(since=ts - 86400)}


_LABEL_NAME = re.compile(r"[^a-zA-Z0-9_]")


def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def prometheus_text(store: Store, project: str = "") -> str:
    """OpenMetrics-compatible text for the newest sample.  Every series is a real stored value."""
    latest = store.latest()
    out: list[str] = []
    for name in sorted(latest):
        metric = "gisstack_" + _LABEL_NAME.sub("_", name)
        out.append(f"# TYPE {metric} {'counter' if name.endswith('_total') else 'gauge'}")
        for s in latest[name]:
            labels = {**s["labels"], **({"project": project} if project else {})}
            lab = ",".join(f'{_LABEL_NAME.sub("_", k)}="{_esc(str(v))}"' for k, v in sorted(labels.items()))
            out.append(f"{metric}{{{lab}}} {s['value']:g}" if lab else f"{metric} {s['value']:g}")
    return "\n".join(out) + ("\n" if out else "")
