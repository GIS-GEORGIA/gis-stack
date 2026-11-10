"""Compose provider: docker / podman compose project on this host or a server."""
from __future__ import annotations

import contextlib
import json
import re
from typing import Any, Callable

from ..core.errors import DeployError
from . import common, native

ENTRY, CLEAN_ENTRY = "up.sh", "up.sh"
ROOT = False

def runtime(cfg: dict[str, Any]) -> str:
    return "podman" if cfg.get("runtime") == "podman" else "docker"


_COMPOSE_SVC = {"qgis_server": "qgis", "webmap": "caddy"}


def service(comp: str) -> str:
    return _COMPOSE_SVC.get(comp, comp)


def _project_filter(cfg: dict[str, Any]) -> str:
    return f"--filter label=com.docker.compose.project={cfg['project']}"


def status_script(cfg: dict[str, Any], checks: list[dict[str, Any]]) -> str:
    rt, p = runtime(cfg), cfg["project"]
    if rt == "podman":
        # `podman compose ps` is whatever compose provider is installed: podman-compose rejects `-a` and prints no JSON.  The engine itself answers the same
        # question with every provider (both set the com.docker.compose.* labels), so ask it.
        flt = _project_filter(cfg)
        return (f"podman ps -a {flt} --format json 2>&1\n"
                "echo '@@inspect'\n"
                f"for id in $(podman ps -a -q {flt} 2>/dev/null); do "
                "podman inspect -f '{{.Config.Labels}}|{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' $id 2>/dev/null; done\n"
                + common.probe_script(checks))
    return (f"{rt} compose -p {p} ps -a --format json 2>&1\n"
            "echo '@@inspect'\n"
            f"for id in $({rt} compose -p {p} ps -a -q 2>/dev/null); do "
            f"{rt} inspect -f '{{{{.Config.Labels}}}}|{{{{range .NetworkSettings.Networks}}}}{{{{.IPAddress}}}} {{{{end}}}}' $id 2>/dev/null; done\n"
            + common.probe_script(checks))


def _json_items(text: str) -> list[dict[str, Any]]:
    text = text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else [data]
    except ValueError:
        items = []
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("{"):
                with contextlib.suppress(ValueError):
                    items.append(json.loads(line))
        return items


def _from_podman(it: dict[str, Any]) -> dict[str, Any]:
    """One `podman ps --format json` object in the shape `docker compose ps --format json` has (Service / State / Status / Publishers)."""
    labels = it.get("Labels") or {}
    pubs = [{"URL": p.get("host_ip", ""), "TargetPort": p.get("container_port"), "PublishedPort": p.get("host_port"), "Protocol": p.get("protocol", "tcp")}
            for p in (it.get("Ports") or []) if isinstance(p, dict)]
    status = str(it.get("Status", ""))
    return {"Service": labels.get("com.docker.compose.service") or labels.get("io.podman.compose.service", ""), "State": str(it.get("State", "")).lower(),
            "Status": status, "RunningFor": status, "Publishers": pubs, "Health": ""}


def parse(out: str) -> dict[str, Any]:
    head, _, rest = out.partition("@@inspect")
    ips: dict[str, str] = {}
    probe_lines = []
    for line in rest.splitlines():
        if line.startswith("@@"):
            probe_lines.append(line)
        elif "|" in line:
            labels, _, ip = line.rpartition("|")
            m = re.search(r"com\.docker\.compose\.service:([^\s\]]+)", labels)
            if m and ip.strip():
                ips[m.group(1)] = ip.split()[0]
    http = native.parse("\n".join(probe_lines))["http"]
    items = [_from_podman(i) if "Service" not in i and "Labels" in i else i for i in _json_items(head)]
    return {"items": items, "ips": ips, "http": http}


def rows(cfg: dict[str, Any], ids: list[str], parsed: dict[str, Any], checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_svc = {i.get("Service"): i for i in parsed["items"]}
    rows = []
    for comp in ids:
        r = common.row(cfg, comp)
        it = by_svc.get(service(comp))
        r["unit"] = service(comp)
        if not it:
            r["state"], r["detail"] = "missing", "container not created"
        else:
            st, health = str(it.get("State", "")), str(it.get("Health", ""))
            text = str(it.get("Status", ""))
            if not health:                                  # right after `up` the Health field is empty but Status already says "(health: starting)"
                m = re.search(r"\((?:health: )?(starting|healthy|unhealthy)\)", text)        # Docker: "(health: starting)"; Podman: "(starting)"
                health = m.group(1) if m else ""
            r["state"] = {"running": "running", "exited": "stopped", "created": "stopped", "dead": "failed",
                          "restarting": "starting", "paused": "stopped"}.get(st, "unknown")
            if st == "running" and health == "starting":
                r["state"] = "starting"
            if st == "running" and health == "unhealthy":
                r["state"] = "failed"
            r["detail"] = str(it.get("Status", st))
            r["since"] = str(it.get("RunningFor", "") or it.get("Status", ""))
            ip = parsed["ips"].get(service(comp), "") if st == "running" else ""
            for pub in it.get("Publishers") or []:
                tgt, port = pub.get("TargetPort"), pub.get("PublishedPort")
                proto = pub.get("Protocol", "tcp")
                if port:
                    r["listen"].append(f"{pub.get('URL') or '0.0.0.0'}:{port} -> {tgt}/{proto}")  # nosec B104  # noqa: S104 - display text only
                elif ip:
                    r["listen"].append(f"{ip}:{tgt}/{proto} (container)")
            if ip and not r["listen"]:
                r["listen"].append(f"{ip} (container)")
            r["listen"] = sorted(set(r["listen"]))
            r["enabled"] = None
        rows.append(r)
    common.finish(rows, checks, parsed["http"])
    return rows


def fetch_status(cfg: dict[str, Any], ids: list[str], run: Callable[[str], str]) -> list[dict[str, Any]]:
    checks = common.checks(cfg, "compose", ids)
    out = run(status_script(cfg, checks))
    if out.lstrip().startswith(("Error", "error", "Cannot", "permission")) and "@@inspect" not in out:
        raise DeployError(out.strip().splitlines()[0])
    return rows(cfg, ids, parse(out), checks)


def control_script(cfg: dict[str, Any], action: str, order: list[str]) -> str:
    lines = ["set -u"]
    rt, p = runtime(cfg), cfg["project"]
    svcs: list[str] = []
    for comp in order:
        s = service(comp)
        if s not in svcs:
            svcs.append(s)
    names = " ".join(svcs)
    if action in ("start", "stop", "restart"):
        lines.append(f"{rt} compose -p {p} {action} {names}")
    else:
        pol = "unless-stopped" if action == "enable" else "no"
        ids = (f'{rt} ps -a -q {_project_filter(cfg)} --filter "label=com.docker.compose.service=$s"' if rt == "podman"
               else f'{rt} compose -p {p} ps -a -q "$s"')                       # podman-compose has no `ps -a -q`
        lines.append(f'for s in {names}; do for id in $({ids}); do '
                     f'{rt} update --restart {pol} "$id" >/dev/null && echo "$s: restart policy = {pol}"; done; done')
    return "\n".join(lines) + "\n"


def logs_script(cfg: dict[str, Any], comp: str, n: int, follow: bool) -> str:
    f = " -f" if follow else ""
    if runtime(cfg) == "podman":                                        # podman-compose has no --no-color; the engine's own `logs` works with every provider
        return (f'id=$(podman ps -a -q {_project_filter(cfg)} --filter label=com.docker.compose.service={service(comp)} | head -n1)\n'
                f'[ -n "$id" ] || {{ echo "{service(comp)}: container not found" >&2; exit 1; }}\n'
                f'podman logs --tail {n}{f} "$id"\n')
    return f"{runtime(cfg)} compose -p {cfg['project']} logs --no-color --tail {n}{f} {service(comp)}\n"


def clean_args(short: list[str], purge: bool, remove_postgres: bool, force_postgres: bool) -> list[str]:
    if remove_postgres:
        raise DeployError("PostgreSQL პაკეტების წაშლა მხოლოდ native სამიზნეზეა")
    return ["clean"] + (["--purge"] if purge else []) + short


def apply_args(component: str) -> list[str]:
    return ["only", {"qgis_server": "qgis", "webmap": "caddy"}.get(component, component)]       # the web map is served by Caddy
