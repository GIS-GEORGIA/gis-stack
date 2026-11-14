"""What every provider shares: component order, public URLs, health-probe checks, status rows (audit A-02/A-05)."""
from __future__ import annotations

import re
import shlex
from typing import Any

from .. import catalog, generate
from ..core.errors import DeployError

ACTIONS = ("start", "stop", "restart", "enable", "disable")
ORDER = ["postgis", "tomcat", "geoserver", "qgis_server", "mapproxy", "caddy", "webmap"]   # start order
QGIS_PORT, MAPPROXY_PORT = 9993, 8081
TARGETS = ("native", "compose", "k8s")
NAMES = {c["id"]: c["ka"] for c in catalog.COMPONENTS}


def resolve_target(cfg: dict[str, Any], target: str | None) -> str:
    t = target or cfg["deploy_target"]
    if t not in TARGETS:
        raise DeployError("უცნობი სამიზნე")
    return t


def components(cfg: dict[str, Any], target: str, wanted: list[str] | None) -> list[str]:
    on = generate.effective_components(cfg, target)
    ids = [c for c in ORDER if on.get(c)]
    if wanted is None:
        return ids
    out = []
    for w in wanted:
        w = "qgis_server" if w == "qgis" else w
        if w not in ORDER:
            raise DeployError(f"უცნობი კომპონენტი: {w}")
        out.append(w)
    return [c for c in ORDER if c in out]


def public(cfg: dict[str, Any]) -> tuple[str, str, int]:
    """(base url, domain, port) of the Caddy entry point as seen from the server itself."""
    scheme = "http" if cfg["tls"] == "none" else "https"
    return f"{scheme}://{cfg['domain']}", cfg["domain"].split(":")[0], 80 if scheme == "http" else 443


# Native PostgreSQL readiness: `pg_isready` asks the server itself (a server that is still starting, in recovery or hung accepts the TCP connection but does not
# answer), so a bare port check says "healthy" too early.  The binary is not on PATH on every distribution (RPM: /usr/pgsql-*/bin, Debian: /usr/lib/postgresql/*/bin);
# when it is nowhere to be found the check degrades to the port check instead of failing.  Prints "open" / "closed".
PG_READY_FN = ("pg_ready() { b=\"$(command -v pg_isready || ls /usr/lib/postgresql/*/bin/pg_isready /usr/pgsql-*/bin/pg_isready 2>/dev/null | tail -n1)\"; "
               "if [ -n \"$b\" ]; then if \"$b\" -q -h 127.0.0.1 -p \"$1\" -t 5 >/dev/null 2>&1; then echo open; else echo closed; fi; "
               "elif (exec 3<>/dev/tcp/127.0.0.1/$1) 2>/dev/null; then echo open; else echo closed; fi; }")

PUBLIC_PATH = {"geoserver": "/geoserver/web/", "mapproxy": "/mapproxy/demo/",
                "qgis_server": "/qgis/?SERVICE=WMS&REQUEST=GetCapabilities", "caddy": "/", "webmap": "/"}


# A real answer from QGIS Server without a project is HTTP 500 carrying an OGC exception document ("ServerException ... Project file error").  A status code
# alone cannot tell that from Apache's own 500 page when the FastCGI process behind it is dead (S-14: mod_fcgid could not bind its socket), so the
# public probe also requires the body to contain the marker.
BODY_MARKER = {"qgis_server": "Exception"}


def urls(cfg: dict[str, Any], comp: str) -> list[str]:
    if comp not in PUBLIC_PATH or not cfg["components"].get("caddy"):
        return []
    base = public(cfg)[0]
    return [base + (PUBLIC_PATH[comp] if comp != "qgis_server" else "/qgis/")]


def checks(cfg: dict[str, Any], target: str, ids: list[str]) -> list[dict[str, Any]]:
    """What to probe from the server itself.  kind: http (needs 2xx/3xx), alive (any real answer), tcp, pgready (pg_isready)."""
    out: list[dict[str, Any]] = []
    base, domain, port = public(cfg)
    edge = bool(generate.effective_components(cfg, target).get("caddy"))
    if target == "native":
        tp = int(cfg["tomcat"]["port"])
        if "postgis" in ids:
            out.append({"comp": "postgis", "label": f"ready {cfg['postgres']['port']}", "kind": "pgready", "port": int(cfg["postgres"]["port"])})
        if "tomcat" in ids:
            out.append({"comp": "tomcat", "label": f"http :{tp}", "kind": "alive", "url": f"http://127.0.0.1:{tp}/"})
        if "geoserver" in ids:
            out.append({"comp": "geoserver", "label": f"WMS :{tp}", "kind": "http",
                        "url": f"http://127.0.0.1:{tp}/geoserver/ows?service=wms&request=GetCapabilities"})
        if "qgis_server" in ids:
            out.append({"comp": "qgis_server", "label": f"fcgi :{QGIS_PORT}", "kind": "tcp", "port": QGIS_PORT})
        if "mapproxy" in ids:
            out.append({"comp": "mapproxy", "label": f"http :{MAPPROXY_PORT}", "kind": "http",
                        "url": f"http://127.0.0.1:{MAPPROXY_PORT}/demo/"})
    if edge:
        for comp in ids:
            if comp in PUBLIC_PATH:
                if target == "compose":
                    # Compose: the host port of Caddy is whatever `.env` (HTTP_PORT / HTTPS_PORT) published - ask the engine, do not assume 80/443 (S-3)
                    url = f"{base}:@PORT@{PUBLIC_PATH[comp]}"
                    resolve = f"{domain}:@PORT@:127.0.0.1"
                else:
                    url, resolve = base + PUBLIC_PATH[comp], f"{domain}:{port}:127.0.0.1"
                chk = {"comp": comp, "label": "public", "kind": "alive" if comp == "qgis_server" else "http", "url": url, "resolve": resolve}
                if comp in BODY_MARKER:
                    chk["match"] = BODY_MARKER[comp]
                out.append(chk)
    return out


def check_ok(kind: str, code: str) -> bool:
    if kind in ("tcp", "pgready"):
        return code == "open"
    try:
        n = int(code)
    except ValueError:
        return False
    if kind == "alive":
        return n > 0 and n not in (502, 503, 504)
    return 200 <= n < 400


def public_port_prelude(runtime: str, project_filter: str, tls: bool) -> str:
    """Shell lines that set GS_PUBLIC_PORT to the host port the running Caddy container really publishes (443/tcp, or 80/tcp without TLS); standard port when
    it cannot be read.  `project_filter` is the `--filter label=com.docker.compose.project=...` argument."""
    inner = "443/tcp" if tls else "80/tcp"
    std = 443 if tls else 80
    return (f'GS_CADDY="$({runtime} ps -q {project_filter} --filter label=com.docker.compose.service=caddy 2>/dev/null | head -n1)"\n'
            f'GS_PUBLIC_PORT=""\n'
            f'[ -z "$GS_CADDY" ] || GS_PUBLIC_PORT="$({runtime} port "$GS_CADDY" {inner} 2>/dev/null | head -n1 | sed \'s/.*://\')"\n'
            f'case "$GS_PUBLIC_PORT" in ""|*[!0-9]*) GS_PUBLIC_PORT={std} ;; esac\n')


def probe_script(checks: list[dict[str, Any]]) -> str:
    lines = [
        "http() { echo \"@@http $1 $(curl -sk -m 5 -o /dev/null -w '%{http_code}' \"${@:2}\" 2>/dev/null)\"; }",
        # httpm <id> <marker> <curl args>: the status code counts only when the body contains <marker> (otherwise 000 = no real answer)
        "httpm() { o=\"$(curl -sk -m 5 -w '\\n%{http_code}' \"${@:3}\" 2>/dev/null)\"; c=\"${o##*$'\\n'}\"; case \"${o%$'\\n'*}\" in *\"$2\"*) ;; *) c=000 ;; esac; echo \"@@http $1 ${c:-000}\"; }",
        "tcp() { if (exec 3<>/dev/tcp/127.0.0.1/$2) 2>/dev/null; then echo \"@@tcp $1 open\"; else echo \"@@tcp $1 closed\"; fi; }",
        PG_READY_FN,
        "pgready() { echo \"@@tcp $1 $(pg_ready \"$2\")\"; }",
    ]
    for i, c in enumerate(checks):
        if c["kind"] == "tcp":
            lines.append(f"tcp {i} {int(c['port'])}")
        elif c["kind"] == "pgready":
            lines.append(f"pgready {i} {int(c['port'])}")
        else:
            fn = f"httpm {i} {shlex.quote(c['match'])}" if c.get("match") else f"http {i}"
            if "@PORT@" in c["url"] or "@PORT@" in (c.get("resolve") or ""):         # ports read from the engine: variable expansion, so double quotes (domain / path are validated)
                res = f'--resolve "{c["resolve"].replace("@PORT@", "${GS_PUBLIC_PORT}")}" ' if c.get("resolve") else ""
                lines.append(f'{fn} {res}"{c["url"].replace("@PORT@", "${GS_PUBLIC_PORT}")}"')
            else:
                res = f"--resolve {c['resolve']} " if c.get("resolve") else ""
                lines.append(f"{fn} {res}'{c['url']}'")
    return "\n".join(lines) + "\n"


def row(cfg: dict[str, Any], comp: str) -> dict[str, Any]:
    return {"id": comp, "name": NAMES.get(comp, comp), "state": "unknown", "detail": "", "enabled": None,
            "pid": None, "since": "", "restarts": None, "listen": [], "urls": urls(cfg, comp), "checks": [],
            "ok": False, "unit": ""}


def finish(rows: list[dict[str, Any]], checks: list[dict[str, Any]], results: dict[int, str]) -> None:
    for i, c in enumerate(checks):
        code = results.get(i, "")
        for r in rows:
            if r["id"] == c["comp"]:
                r["checks"].append({"label": c["label"], "code": code, "ok": check_ok(c["kind"], code),
                                    "url": c.get("url", "")})
    for r in rows:
        r["ok"] = r["state"] == "running" and all(c["ok"] for c in r["checks"])


def normalize_addr(a: str) -> str:
    if a.startswith("*:"):
        return "0.0.0.0:" + a[2:]
    m = re.match(r"^\[::ffff:(\d+\.\d+\.\d+\.\d+)\]:(\d+)$", a)      # Java's dual-stack sockets
    return f"{m.group(1)}:{m.group(2)}" if m else a
