"""Per-server health, facts, comparison and history.  Everything runs through `transport` with the server's own credentials and pinned
host key; the scripts only read (no root needed, nothing is changed)."""
from __future__ import annotations

import re
import time
from typing import Any, Callable, Optional

from .. import journal, transport
from ..core.errors import DeployError
from . import store

MAX_COMPARE = 6
_KEY_RE = re.compile(r"^(os|kernel|arch|ver\.[a-z]+|svc\.[a-z_.@-]+|sha\.[a-z_.]+)$")

# files whose content should be identical on servers that are meant to be twins; a hash never reveals the content
HASHED = {"mapproxy.yaml": "/etc/gisstack/mapproxy/mapproxy.yaml", "seed.yaml": "/etc/gisstack/mapproxy/seed.yaml", "caddyfile": "/etc/caddy/Caddyfile"}
UNITS = ("gisstack-tomcat.service", "gisstack-mapproxy.service", "caddy.service", "gisstack-qgis.service")

PROBE = r"""
. /etc/os-release 2>/dev/null
echo "os=${PRETTY_NAME:-unknown}"
echo "kernel=$(uname -r 2>/dev/null)"
echo "arch=$(uname -m 2>/dev/null)"
if sudo -n true 2>/dev/null || [ "$(id -u)" = 0 ]; then echo "root=yes"; else echo "root=no"; fi
echo "load=$(cut -d' ' -f1 /proc/loadavg 2>/dev/null)"
echo "disk_free_kb=$(df -Pk / 2>/dev/null | awk 'NR==2{print $4}')"
"""

FACTS = PROBE + r"""
echo "ver.postgres=$(psql --version 2>/dev/null | head -1)"
echo "ver.caddy=$(caddy version 2>/dev/null | head -1 | cut -d' ' -f1)"
echo "ver.mapproxy=$(/opt/gisstack/mapproxy-venv/bin/python -c 'import mapproxy;print(mapproxy.__version__)' 2>/dev/null)"
echo "ver.geoserver=$(ls /opt/tomcat/webapps/geoserver/WEB-INF/lib/gs-main-*.jar 2>/dev/null | head -1 | sed 's/.*gs-main-//;s/\.jar$//')"
for u in __UNITS__; do echo "svc.$u=$(systemctl is-active "$u" 2>/dev/null || true)"; done
__HASHES__
"""


def _script() -> str:
    hashes = "\n".join(f'[ -r {p} ] && echo "sha.{n.replace(".", "_")}=$(sha256sum {p} | cut -c1-64)"' for n, p in HASHED.items())
    return FACTS.replace("__UNITS__", " ".join(UNITS)).replace("__HASHES__", hashes)


def parse(out: str) -> dict[str, str]:
    facts: dict[str, str] = {}
    for line in out.splitlines():
        k, sep, v = line.partition("=")
        k = k.strip()
        if sep and (_KEY_RE.match(k) or k in ("root", "load", "disk_free_kb")) and v.strip():
            facts[k] = v.strip()[:200]
    return facts


def probe(sid: str, run: Optional[Callable[..., str]] = None) -> dict[str, Any]:
    """Can we reach the server with this profile (key, pinned host key)?  Never raises: the answer is in the result."""
    ssh = store.ssh_for(sid)
    t0 = time.time()
    try:
        out = (run or transport.run)("ssh", ssh, PROBE, timeout=30)
    except DeployError as e:
        return {"id": sid, "reachable": False, "error": str(e)[:300], "seconds": round(time.time() - t0, 2)}
    return {"id": sid, "reachable": True, "seconds": round(time.time() - t0, 2), "facts": parse(out)}


def facts(sid: str, run: Optional[Callable[..., str]] = None) -> dict[str, Any]:
    ssh = store.ssh_for(sid)
    try:
        out = (run or transport.run)("ssh", ssh, _script(), timeout=40)
    except DeployError as e:
        return {"id": sid, "reachable": False, "error": str(e)[:300], "facts": {}}
    return {"id": sid, "reachable": True, "facts": parse(out)}


def compare(ids: list[str], run: Optional[Callable[..., str]] = None) -> dict[str, Any]:
    """Facts of several servers side by side: `rows` has one entry per fact name, `differs` says whether the servers disagree
    (only reachable servers count; an unreachable one is listed in `servers` with its error, never silently ignored)."""
    if not 2 <= len(ids) <= MAX_COMPARE or len(set(ids)) != len(ids):
        raise store.ServerError(f"შესადარებლად 2-{MAX_COMPARE} განსხვავებული სერვერია საჭირო")
    results = [facts(i, run) for i in ids]
    live = [r for r in results if r["reachable"]]
    names = sorted({k for r in live for k in r["facts"] if k not in ("load", "disk_free_kb")})
    rows = []
    for n in names:
        vals = {r["id"]: r["facts"].get(n, "") for r in live}
        rows.append({"fact": n, "values": vals, "differs": len(set(vals.values())) > 1})
    return {"servers": [{"id": r["id"], "reachable": r["reachable"], "error": r.get("error", "")} for r in results],
            "rows": rows, "drift": sum(1 for r in rows if r["differs"]), "complete": len(live) == len(results)}


def history(sid: str, limit: int = 50) -> list[dict[str, Any]]:
    """Journal rows (deployments, restarts, edits ...) that ran against this server's host."""
    s = store.get(sid)
    return [r for r in journal.operations(limit=500) if r.get("host") == s["host"]][: max(1, min(int(limit), 200))]
