"""Native provider: systemd units on a Debian/Ubuntu or RHEL-family host."""
from __future__ import annotations

import contextlib
from typing import Any, Callable

from . import common
from .common import MAPPROXY_PORT, QGIS_PORT, normalize_addr

ENTRY, CLEAN_ENTRY = "install.sh", "uninstall.sh"
ROOT = True

_PG_HEAD = '''. /etc/os-release 2>/dev/null || true
case " ${ID_LIKE:-} ${ID:-} " in
  *" debian "*|*" ubuntu "*) PGU="postgresql@@V@-main.service"; PGE="postgresql.service" ;;
  *) PGU="postgresql-@V@.service"; PGE="$PGU" ;;
esac
'''


def units(cfg: dict[str, Any]) -> dict[str, list[str]]:
    return {"postgis": ["$PGU"], "tomcat": ["gisstack-tomcat.service"], "geoserver": ["gisstack-tomcat.service"],
            "qgis_server": ["gisstack-qgis.socket", "gisstack-qgis.service"],
            "mapproxy": ["gisstack-mapproxy.service"], "caddy": ["caddy.service"], "webmap": []}


def status_script(cfg: dict[str, Any], checks: list[dict[str, Any]]) -> str:
    head = _PG_HEAD.replace("@V@", str(cfg["postgres"]["version"]))
    shows = "\n".join([
        'show() { echo "@@unit $1 $2"; systemctl show -p LoadState,ActiveState,SubState,UnitFileState,MainPID,'
        'ActiveEnterTimestamp,NRestarts,Listen "$2" 2>&1; }',
        'show postgis "$PGU"', "show tomcat gisstack-tomcat.service", "show qgis_server gisstack-qgis.service",
        "show qgis_socket gisstack-qgis.socket", "show mapproxy gisstack-mapproxy.service", "show caddy caddy.service",
    ])
    return (head + shows + '\necho "@@ss"; ss -Hltn 2>&1\n'
            'echo "@@file webmap $([ -f /var/www/gisstack/webmap/index.html ] && echo present || echo absent)"\n'
            + common.probe_script(checks))


def parse(out: str) -> dict[str, Any]:
    """Split the status script's output into units / listening sockets / probe results."""
    units: dict[str, dict[str, str]] = {}
    ss: list[str] = []
    http: dict[int, str] = {}
    files: dict[str, str] = {}
    cur: dict[str, str] | None = None
    mode = ""
    for line in out.splitlines():
        if line.startswith("@@unit "):
            _, name, unit = line.split(None, 2)
            cur = units.setdefault(name, {"_unit": unit})
            mode = "unit"
        elif line.startswith("@@ss"):
            mode, cur = "ss", None
        elif line.startswith("@@http ") or line.startswith("@@tcp "):
            parts = line.split()
            with contextlib.suppress(ValueError, IndexError):
                http[int(parts[1])] = parts[2] if len(parts) > 2 else ""
            mode, cur = "", None
        elif line.startswith("@@file "):
            parts = line.split()
            files[parts[1]] = parts[2] if len(parts) > 2 else ""
            mode, cur = "", None
        elif mode == "unit" and cur is not None and "=" in line:
            k, v = line.split("=", 1)
            cur[k] = v
        elif mode == "ss" and line.strip():
            f = line.split()
            if len(f) >= 4:
                ss.append(normalize_addr(f[3]))
    return {"units": units, "ss": ss, "http": http, "files": files}


def _state_of(show: dict[str, str]) -> tuple[str, str]:
    if not show or show.get("LoadState") == "not-found" or "ActiveState" not in show:
        return "missing", "unit not found"
    a, sub = show.get("ActiveState", ""), show.get("SubState", "")
    state = {"active": "running", "inactive": "stopped", "failed": "failed", "activating": "starting",
             "deactivating": "stopping", "reloading": "running"}.get(a, "unknown")
    if a == "active" and sub == "exited":
        state = "running"          # oneshot wrappers such as postgresql.service
    return state, f"{a} ({sub})"


def _listening(ss: list[str], ports: list[int]) -> list[str]:
    out = []
    for a in ss:
        try:
            if int(a.rsplit(":", 1)[1]) in ports:
                out.append(a)
        except (ValueError, IndexError):
            continue
    return sorted(set(out))


def rows(cfg: dict[str, Any], ids: list[str], parsed: dict[str, Any], checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ports = {"postgis": [int(cfg["postgres"]["port"])], "tomcat": [int(cfg["tomcat"]["port"])],
             "geoserver": [int(cfg["tomcat"]["port"])], "qgis_server": [QGIS_PORT],
             "mapproxy": [MAPPROXY_PORT], "caddy": [80, 443], "webmap": []}
    unit_key = {"postgis": "postgis", "tomcat": "tomcat", "geoserver": "tomcat", "qgis_server": "qgis_server",
                "mapproxy": "mapproxy", "caddy": "caddy"}
    rows = []
    for comp in ids:
        r = common.row(cfg, comp)
        if comp == "webmap":
            r["state"] = "running" if parsed["files"].get("webmap") == "present" else "missing"
            r["detail"] = "static files served by Caddy" if r["state"] == "running" else "files not installed"
            r["unit"] = "/var/www/gisstack/webmap"
            r["listen"] = _listening(parsed["ss"], [80, 443])
        else:
            show = parsed["units"].get(unit_key[comp], {})
            r["state"], r["detail"] = _state_of(show)
            r["unit"] = show.get("_unit", "")
            ufs = show.get("UnitFileState", "")
            r["enabled"] = True if ufs in ("enabled", "enabled-runtime") else (False if ufs in ("disabled", "masked") else None)
            pid = show.get("MainPID", "0")
            r["pid"] = int(pid) if pid.isdigit() and int(pid) > 0 else None
            r["since"] = show.get("ActiveEnterTimestamp", "")
            r["restarts"] = int(show["NRestarts"]) if show.get("NRestarts", "").isdigit() else None
            r["listen"] = _listening(parsed["ss"], ports[comp])
            if comp == "qgis_server":
                sock = parsed["units"].get("qgis_socket", {})
                if sock.get("Listen"):
                    r["detail"] += f"; socket {sock.get('ActiveState', '?')}"
        rows.append(r)
    common.finish(rows, checks, parsed["http"])
    return rows


def fetch_status(cfg: dict[str, Any], ids: list[str], run: Callable[[str], str]) -> list[dict[str, Any]]:
    checks = common.checks(cfg, "native", ids)
    return rows(cfg, ids, parse(run(status_script(cfg, checks))), checks)


def control_script(cfg: dict[str, Any], action: str, order: list[str]) -> str:
    lines = ["set -u", _PG_HEAD.replace("@V@", str(cfg["postgres"]["version"]))]
    unit_map = units(cfg)
    seen: list[str] = []
    for comp in order:
        if comp == "webmap":
            lines.append('echo "webmap: static files served by Caddy - nothing to ' + action + '"')
            continue
        us = unit_map[comp]
        if comp == "postgis" and action in ("enable", "disable"):
            us = ["$PGE"]
        if action == "stop":
            us = list(reversed(us))            # service first, then its socket
        us = [u for u in us if u not in seen]
        seen += us
        if not us:
            continue
        lines.append(f'echo "== {action} {comp}: {" ".join(us)}"')
        lines.append(f'systemctl {action} {" ".join(us)} && echo ok || {{ echo "FAILED: {comp}"; rc=1; }}')
    lines.append("exit ${rc:-0}")
    return "\n".join(lines) + "\n"


def logs_script(cfg: dict[str, Any], comp: str, n: int, follow: bool) -> str:
    f = " -f" if follow else ""
    if comp == "webmap":
        comp = "caddy"
    unit = {"postgis": "$PGU", "tomcat": "gisstack-tomcat", "geoserver": "gisstack-tomcat",
            "qgis_server": "gisstack-qgis", "mapproxy": "gisstack-mapproxy", "caddy": "caddy"}[comp]
    s = _PG_HEAD.replace("@V@", str(cfg["postgres"]["version"]))
    extra = ""
    if comp == "postgis":
        extra = f'L=/var/log/postgresql/postgresql-{cfg["postgres"]["version"]}-main.log\n[ -r "$L" ] && tail -n {n}{" -F" if follow else ""} "$L" &\n'
    if comp == "geoserver":
        extra = f'L=/var/lib/gisstack/geoserver_data/logs/geoserver.log\n[ -r "$L" ] && tail -n {n}{" -F" if follow else ""} "$L" &\n'
    return s + extra + f"journalctl --no-pager -n {n}{f} -u {unit}\nwait\n"


def clean_args(short: list[str], purge: bool, remove_postgres: bool, force_postgres: bool) -> list[str]:
    args: list[str] = []
    if short:
        args += ["--only", ",".join(short)]
    if purge:
        args.append("--purge")
    if remove_postgres:
        args += ["--remove-postgres"] + (["--force-postgres"] if force_postgres else [])
    return args


def apply_args(component: str) -> list[str]:
    return ["--only", {"qgis_server": "qgis"}.get(component, component)]
