"""Day-to-day operations on a running stack: live status, start / stop / restart, logs.

Works for the three targets (native systemd units, Compose containers, Kubernetes workloads), on this
machine or over SSH.  Everything is read with ordinary tools (systemctl, ss, curl, docker, kubectl), so no
agent has to be installed on the server.  What differs per target lives in `gisstack.providers`; this module is the
target-independent front: validation, journalling, orchestration and console formatting."""
from __future__ import annotations

import threading
import time
from typing import Any, Iterator

from . import journal, providers, transport
from .core.errors import DeployError
from .providers import common
from .providers import compose as _compose
from .providers import kubernetes as _kubernetes
from .providers import native as _native

ACTIONS = common.ACTIONS
ORDER = common.ORDER
QGIS_PORT, MAPPROXY_PORT = common.QGIS_PORT, common.MAPPROXY_PORT
parse_native, parse_compose, parse_k8s = _native.parse, _compose.parse, _kubernetes.parse
normalize_addr = common.normalize_addr


# --------------------------------------------------------------------------- public API
def status(cfg: dict[str, Any], mode: str, ssh: dict[str, Any] | None = None, target: str | None = None,
           components: list[str] | None = None) -> dict[str, Any]:
    """Current state of every selected program.  Raises DeployError if the host cannot be queried."""
    t = common.resolve_target(cfg, target)
    ids = common.components(cfg, t, components)
    t0 = time.time()
    rows = providers.get(t).status_rows(cfg, ids, lambda script: transport.run(mode, ssh, script))
    return {"target": t, "project": cfg["project"], "domain": cfg["domain"], "time": time.strftime("%H:%M:%S"),
            "took": round(time.time() - t0, 2), "rows": rows}


def control_script(cfg: dict[str, Any], target: str, action: str, ids: list[str]) -> str:
    """Shell script that applies start / stop / restart / enable / disable to the given programs."""
    if action not in ACTIONS:
        raise DeployError(f"უცნობი ქმედება: {action}")
    if not ids:
        raise DeployError("კომპონენტი არ არის არჩეული")
    order = ids if action != "stop" else list(reversed(ids))
    return providers.get(target).control_script(cfg, action, order)


def control(cfg: dict[str, Any], mode: str, ssh: dict[str, Any] | None, action: str,
            components: list[str] | None = None, target: str | None = None) -> Iterator[str]:
    """Apply an action to some programs (components=None: the whole stack).  Yields log lines."""
    t = common.resolve_target(cfg, target)
    ids = common.components(cfg, t, components)
    script = control_script(cfg, t, action, ids)
    yield f"== {action}: {', '.join(ids)}"
    host = transport.host_label(mode, ssh)
    with journal.Operation(f"control:{action}", target=t, mode=mode, host=host, project=cfg.get("project", ""), components=ids):
        yield from transport.stream(mode, ssh, script, root=providers.get(t).needs_root, deadline=900)
    yield "== დასრულდა"


def logs_script(cfg: dict[str, Any], target: str, comp: str, lines: int = 100, follow: bool = False) -> str:
    n = max(1, min(int(lines), 5000))
    if comp not in ORDER:
        raise DeployError(f"უცნობი კომპონენტი: {comp}")
    return providers.get(target).logs_script(cfg, comp, n, follow)


def logs(cfg: dict[str, Any], mode: str, ssh: dict[str, Any] | None, component: str, lines: int = 100,
         follow: bool = False, stop: threading.Event | None = None, target: str | None = None) -> Iterator[str]:
    t = common.resolve_target(cfg, target)
    comp = "qgis_server" if component == "qgis" else component
    script = logs_script(cfg, t, comp, lines, follow)
    yield from transport.stream(mode, ssh, script, root=providers.get(t).needs_root, stop=stop)


# --------------------------------------------------------------------------- console formatting
_COLORS = {"running": "32", "stopped": "33", "starting": "36", "stopping": "36", "failed": "31", "missing": "90", "unknown": "90"}


def format_table(result: dict[str, Any], color: bool = False) -> str:
    """Plain-text status table (one program per block of lines)."""
    rows = result["rows"]
    head = f"{result['project']} · {result['target']} · {result['domain']} · {result['time']}"
    cols = ("PROGRAM", "STATE", "AUTOSTART", "PID", "LISTEN (IP:PORT)", "CHECKS")
    table = []
    for r in rows:
        listen = r["listen"] or ["-"]
        checks = [f"{'✔' if c['ok'] else '✘'} {c['label']} {c['code']}" for c in r["checks"]] or ["-"]
        auto = {True: "yes", False: "no", None: "-"}[r["enabled"]]
        n = max(len(listen), len(checks))
        for i in range(n):
            table.append((r["id"] if i == 0 else "", r["state"] if i == 0 else "", auto if i == 0 else "",
                          str(r["pid"] or "-") if i == 0 else "", listen[i] if i < len(listen) else "",
                          checks[i] if i < len(checks) else ""))
    widths = [max(len(str(x[i])) for x in [cols] + table) for i in range(len(cols))]
    out = [head, ""]
    out.append("  ".join(c.ljust(widths[i]) for i, c in enumerate(cols)).rstrip())
    for row in table:
        cells = [str(c).ljust(widths[i]) for i, c in enumerate(row)]
        if color and row[1] in _COLORS:
            cells[1] = f"\033[{_COLORS[row[1]]}m{cells[1]}\033[0m"
        out.append("  ".join(cells).rstrip())
    urls = [(r["id"], u) for r in rows for u in r["urls"]]
    if urls:
        out.append("")
        out += [f"{k}: {u}" for k, u in urls]
    return "\n".join(out)
