"""Plain-text rendering of a monitoring snapshot for the CLI (no colour codes: output is piped and logged)."""
from __future__ import annotations

import time
from typing import Any


def _g(m: dict[str, list[dict[str, Any]]], name: str, **labels: str) -> Any:
    for s in m.get(name, []):
        if all(s["labels"].get(k) == v for k, v in labels.items()):
            return s["value"]
    return None


def _gb(v: Any) -> str:
    return "-" if v is None else f"{v / 1024 ** 3:.1f} GB" if v >= 1024 ** 3 else f"{v / 1024 ** 2:.0f} MB"


def _pct(v: Any) -> str:
    return "-" if v is None else f"{v:.0f} %"


def format_snapshot(snap: dict[str, Any]) -> str:
    if snap.get("ts") is None:
        return "(no data yet - run: gisstack monitor collect)"
    m = snap["metrics"]
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(snap["ts"])) + " UTC"
    lines = [f"sample {when}  ({snap['age_seconds']:.0f} s ago)", ""]
    if _g(m, "host_load1") is not None:
        lines.append(f"HOST   cpu {_pct(_g(m, 'host_cpu_percent'))}  mem {_pct(_g(m, 'host_mem_used_percent'))} of {_gb(_g(m, 'host_mem_total_bytes'))}  "
                     f"load {_g(m, 'host_load1'):.2f} {_g(m, 'host_load5'):.2f} {_g(m, 'host_load15'):.2f}  up {(_g(m, 'host_uptime_seconds') or 0) / 3600:.1f} h")
    for s in m.get("disk_used_percent", []):
        lines.append(f"DISK   {s['labels']['mount']:<24} {_pct(s['value'])} used, {_gb(_g(m, 'disk_free_bytes', mount=s['labels']['mount']))} free")
    for s in m.get("service_up", []):
        svc = s["labels"]["service"]
        cpu, rss, rst = _g(m, "service_cpu_percent", service=svc), _g(m, "service_rss_bytes", service=svc), _g(m, "service_restarts", service=svc)
        lines.append(f"SVC    {svc:<12} {'up' if s['value'] >= 1 else 'DOWN':<5} cpu {_pct(cpu):>6}  mem {_gb(rss):>8}" + (f"  restarts {rst:.0f}" if rst else ""))
    if _g(m, "pg_up") is not None:
        lines.append(f"PG     {'up' if _g(m, 'pg_up') >= 1 else 'DOWN'}  connections {_g(m, 'pg_connections_total') or 0:.0f}/{_g(m, 'pg_max_connections') or 0:.0f} "
                     f"({_pct(_g(m, 'pg_connections_percent'))})  longest query {_g(m, 'pg_longest_query_seconds') or 0:.0f} s  waiting locks {_g(m, 'pg_waiting_locks') or 0:.0f}")
        for s in m.get("pg_database_size_bytes", []):
            hit = _g(m, "pg_cache_hit_ratio", database=s["labels"]["database"])
            lines.append(f"       db {s['labels']['database']:<28} {_gb(s['value']):>8}" + (f"  cache hit {hit * 100:.1f} %" if hit is not None else ""))
    for s in m.get("probe_up", []):
        p = s["labels"]["probe"]
        lines.append(f"PROBE  {p:<26} {'ok' if s['value'] >= 1 else 'FAIL':<5} {_g(m, 'probe_seconds', probe=p) or 0:.3f} s")
    age = _g(m, "backup_last_age_seconds")
    if age is not None or _g(m, "backup_count") is not None:
        lines.append(f"BACKUP {_g(m, 'backup_count') or 0:.0f} backup(s)" + (f", newest {age / 3600:.1f} h old" if age is not None else ""))
    if _g(m, "collect_errors"):
        lines.append(f"NOTE   {_g(m, 'collect_errors'):.0f} metric source(s) were unavailable in this sample")
    lines.append("")
    firing = snap.get("alerts", [])
    lines.append("ALERTS " + ("none firing" if not firing else ""))
    for a in firing:
        lines.append(f"  ! {a['rule']} [{a['subject']}] {a['message']}")
    return "\n".join(lines)
