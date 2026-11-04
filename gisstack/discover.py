"""Look for programs that are already on the server before we install ours.

`scan()` reports, for each program of the stack, what is installed / running / listening and which port conflicts
an installation would run into.  Everything is read with ordinary tools (dpkg/rpm, systemctl, ss, ...) through the
same local / SSH transport the other operations use."""
from __future__ import annotations

import re
from typing import Any

from . import catalog, ops

_NAMES = {c["id"]: c["ka"] for c in catalog.COMPONENTS}
COMPONENTS = ["postgis", "tomcat", "geoserver", "qgis_server", "mapproxy", "caddy"]
WEB_SERVERS = ["nginx", "apache2", "httpd"]

SCAN_SCRIPT = r'''
. /etc/os-release 2>/dev/null || true
FAM=rpm; case " ${ID_LIKE:-} ${ID:-} " in *" debian "*|*" ubuntu "*) FAM=deb ;; esac
echo "@@os $FAM ${PRETTY_NAME:-unknown}"
echo "@@root $(id -u)"
echo "@@ours $([ -f /etc/gisstack/stack.env ] && echo yes || echo no)"
echo "@@pkgs"
if [ "$FAM" = deb ]; then dpkg-query -W -f='${db:Status-Abbrev} ${Package} ${Version}\n' 2>/dev/null | awk '$1 ~ /^ii/ {print $2" "$3}'
else rpm -qa --qf '%{NAME} %{VERSION}-%{RELEASE}\n' 2>/dev/null; fi \
  | grep -E '^(postgresql|postgis|tomcat|geoserver|qgis|python3-mapproxy|mapproxy|caddy|nginx|apache2|httpd)' | sort
echo "@@units"
systemctl list-units --all --no-legend --plain --type=service,socket 2>/dev/null \
  | awk '{print $1" "$3" "$4}' | grep -E '^(postgresql|tomcat|geoserver|qgis|mapproxy|gisstack|caddy|nginx|apache2|httpd|spawn-fcgi)'
echo "@@unitfiles"
systemctl list-unit-files --no-legend --plain --type=service,socket 2>/dev/null \
  | awk '{print $1" "$2}' | grep -E '^(postgresql|tomcat|geoserver|qgis|mapproxy|gisstack|caddy|nginx|apache2|httpd|spawn-fcgi)'
echo "@@ss"
ss -Hltnp 2>/dev/null
echo "@@paths"
for p in /opt/tomcat* /usr/share/tomcat* /var/lib/tomcat* /etc/tomcat* /opt/geoserver* /usr/share/geoserver* \
         /var/lib/geoserver* /etc/geoserver* /etc/caddy /usr/bin/caddy /usr/local/bin/caddy /etc/postgresql \
         /var/lib/postgresql /var/lib/pgsql /etc/mapproxy /opt/mapproxy* /srv/qgis /var/www/gisstack \
         /etc/gisstack /etc/systemd/system/caddy.service; do
  [ -e "$p" ] && echo "$p"
done
echo "@@webapps"
ls -d /var/lib/tomcat*/webapps/* /opt/tomcat*/webapps/* /opt/apache-tomcat*/webapps/* /usr/share/tomcat*/webapps/* 2>/dev/null
echo "@@caddy"
CB="$(command -v caddy 2>/dev/null || true)"; echo "bin=$CB"
[ -n "$CB" ] && echo "version=$("$CB" version 2>/dev/null | head -1)"
grep -q '(gisstack)' /etc/systemd/system/caddy.service 2>/dev/null && echo "ours=yes" || echo "ours=no"
echo "@@mapproxy"
echo "bin=$(command -v mapproxy-util 2>/dev/null || true)"
echo "@@qgis"
echo "fcgi=$(find /usr/lib /usr/lib64 /usr/libexec -name qgis_mapserv.fcgi 2>/dev/null | head -1)"
echo "@@pg"
if command -v psql >/dev/null 2>&1 && (cd /tmp && runuser -u postgres -- pg_isready -q) 2>/dev/null; then
  # one statement per line on stdin: psql before v15 prints only the last result of a multi-statement -c string
  (cd /tmp && runuser -u postgres -- psql -X -Atq -F '|' 2>&1 <<'SQL'
select 'ver|'||current_setting('server_version');
select 'port|'||current_setting('port');
select 'datadir|'||current_setting('data_directory');
select 'db|'||datname||'|'||pg_database_size(datname) from pg_database where not datistemplate and datname<>'postgres' order by 1;
SQL
  )
fi
echo "@@end"
'''


def _sections(out: str) -> dict[str, list[str]]:
    secs: dict[str, list[str]] = {}
    cur = "_"
    for line in out.splitlines():
        if line.startswith("@@"):
            parts = line[2:].split(None, 1)
            cur = parts[0]
            secs[cur] = [parts[1]] if len(parts) > 1 else []
        elif line.strip():
            secs.setdefault(cur, []).append(line.rstrip())
    return secs


def _kv(lines: list[str]) -> dict[str, str]:
    out = {}
    for l in lines:
        if "=" in l:
            k, v = l.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def parse(out: str) -> dict[str, Any]:
    """Turn the scan script's output into plain data (pure, unit-tested)."""
    s = _sections(out)
    pkgs = []
    for l in s.get("pkgs", []):
        p = l.split(None, 1)
        pkgs.append({"name": p[0], "version": p[1] if len(p) > 1 else ""})
    units: dict[str, dict[str, Any]] = {}
    for l in s.get("units", []):
        f = l.split()
        if len(f) >= 3:
            units[f[0]] = {"name": f[0], "active": f[1], "sub": f[2], "enabled": None}
    for l in s.get("unitfiles", []):
        f = l.split()
        if len(f) >= 2:
            u = units.setdefault(f[0], {"name": f[0], "active": "inactive", "sub": "dead", "enabled": None})
            u["enabled"] = f[1] in ("enabled", "enabled-runtime")
    listeners = []
    for l in s.get("ss", []):
        f = l.split()
        if len(f) < 4:
            continue
        addr = ops._normalize_addr(f[3])
        m = re.search(r'users:\(\("([^"]+)",pid=(\d+)', l)
        try:
            port = int(addr.rsplit(":", 1)[1])
        except (ValueError, IndexError):
            continue
        listeners.append({"addr": addr, "port": port, "proc": m.group(1) if m else "", "pid": int(m.group(2)) if m else None})
    pg: dict[str, Any] = {"dbs": []}
    for l in s.get("pg", []):
        p = l.split("|")
        if p[0] == "ver" and len(p) > 1:
            pg["version"] = p[1]
        elif p[0] == "port" and len(p) > 1:
            pg["port"] = int(p[1]) if p[1].isdigit() else None
        elif p[0] == "datadir" and len(p) > 1:
            pg["datadir"] = p[1]
        elif p[0] == "db" and len(p) > 2:
            pg["dbs"].append({"name": p[1], "bytes": int(p[2]) if p[2].isdigit() else 0})
    os_line = (s.get("os") or [""])[0]
    return {
        "family": os_line.split()[0] if os_line else "", "os": " ".join(os_line.split()[1:]),
        "root": (s.get("root") or ["1"])[0] == "0", "ours_marker": (s.get("ours") or ["no"])[0] == "yes",
        "pkgs": pkgs, "units": units, "listeners": listeners, "paths": s.get("paths", []),
        "webapps": s.get("webapps", []), "caddy": _kv(s.get("caddy", [])), "mapproxy": _kv(s.get("mapproxy", [])),
        "qgis": _kv(s.get("qgis", [])), "pg": pg,
    }


def _pkgs(raw: dict[str, Any], pattern: str) -> list[dict[str, str]]:
    return [p for p in raw["pkgs"] if re.search(pattern, p["name"])]


def _units(raw: dict[str, Any], pattern: str, ours: bool) -> list[dict[str, Any]]:
    out = []
    for name, u in sorted(raw["units"].items()):
        if name.endswith("@.service"):
            continue                      # systemd templates are not services
        if re.search(pattern, name) and name.startswith("gisstack-") == ours:
            out.append(u)
    return out


def _paths(raw: dict[str, Any], pattern: str) -> list[str]:
    return [p for p in raw["paths"] if re.search(pattern, p)]


def _empty(comp: str) -> dict[str, Any]:
    return {"id": comp, "name": _NAMES.get(comp, comp), "found": False, "ours": False, "foreign": False,
            "version": "", "packages": [], "services": [], "paths": [], "listen": [], "details": [], "data": []}


def analyse(raw: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """Classify what was found into the stack's programs, mark ours vs. foreign and list port conflicts."""
    comps = {c: _empty(c) for c in COMPONENTS}
    ours_marker = raw["ours_marker"]
    listeners = raw["listeners"]

    def mine(unit: str) -> bool:
        return unit in raw["units"] and unit.startswith("gisstack-")

    # --- PostgreSQL
    c = comps["postgis"]
    c["packages"] = _pkgs(raw, r"^(postgresql-\d+|postgresql\d+-server|postgresql-server)$")
    c["services"] = _units(raw, r"^postgresql", False)
    c["paths"] = _paths(raw, r"^/(etc/postgresql|var/lib/postgresql|var/lib/pgsql)$")
    pg = raw["pg"]
    c["version"] = pg.get("version", "") or (re.sub(r"\D", "", c["packages"][0]["name"]) if c["packages"] else "")
    c["listen"] = [f"{l['addr']} ({l['proc'] or 'postgres'})" for l in listeners if l["proc"] in ("postgres", "postmaster") or l["port"] == pg.get("port")]
    c["data"] = [f"{d['name']} ({d['bytes'] // (1024 * 1024)} MB)" for d in pg["dbs"]]
    c["databases"] = [d["name"] for d in pg["dbs"]]
    major = re.match(r"\d+", c["version"])
    c["major"] = major.group(0) if major else ""
    c["found"] = bool(c["packages"] or c["services"] or pg.get("version"))
    c["ours"] = c["found"] and ours_marker
    c["foreign"] = c["found"] and not ours_marker
    ourdb = cfg["postgres"]["db"]
    if c["ours"]:
        c["details"].append(f"gisstack-ის ბაზა: {ourdb}" if ourdb in c["databases"] else "gisstack-ის მარკერი არის")
    others = [d for d in c["databases"] if d != ourdb]
    if c["found"] and others:
        c["details"].append("სხვა ბაზები: " + ", ".join(others))

    # --- Tomcat (the apt/dnf one or an unpacked one; ours lives in /opt/tomcat + gisstack-tomcat)
    c = comps["tomcat"]
    c["packages"] = _pkgs(raw, r"^tomcat\d*$")
    c["services"] = _units(raw, r"^tomcat", False)
    c["paths"] = [p for p in _paths(raw, r"^/(opt/(apache-)?tomcat|usr/share/tomcat|var/lib/tomcat|etc/tomcat)") if not (mine("gisstack-tomcat.service") and p.startswith("/opt/"))]
    c["found"] = bool(c["packages"] or c["services"] or c["paths"] or mine("gisstack-tomcat.service"))
    c["ours"] = mine("gisstack-tomcat.service")
    c["foreign"] = bool(c["packages"] or c["services"])          # leftover files alone are not a running program
    if c["paths"] and not c["foreign"]:
        c["details"].append("დარჩენილი ფაილები: " + ", ".join(c["paths"][:3]))
    c["version"] = c["packages"][0]["version"] if c["packages"] else ""
    c["listen"] = [f"{l['addr']} ({l['proc']})" for l in listeners if l["proc"] == "java" and l["port"] in (8080, int(cfg["tomcat"]["port"]))]

    # --- GeoServer (a webapp inside some Tomcat, or a package)
    c = comps["geoserver"]
    c["packages"] = _pkgs(raw, r"^geoserver")
    c["services"] = _units(raw, r"^geoserver", False)
    c["paths"] = _paths(raw, r"geoserver")
    gs_apps = [w for w in raw["webapps"] if w.rstrip("/").endswith("/geoserver") or w.endswith("geoserver.war")]
    c["paths"] += [w for w in gs_apps if w not in c["paths"]]
    def ours_app(w: str) -> bool:       # /opt/tomcat is a symlink to /opt/apache-tomcat-<ver>: both spellings are ours
        return mine("gisstack-tomcat.service") and re.match(r"^/opt/(tomcat|apache-tomcat[^/]*)/", w) is not None
    ours_gs = any(ours_app(w) for w in gs_apps)
    foreign_gs = [w for w in gs_apps if not ours_app(w)]
    c["found"] = bool(c["packages"] or c["services"] or gs_apps)
    c["ours"] = ours_gs
    c["foreign"] = bool(c["packages"] or c["services"] or foreign_gs)
    c["data"] = [p for p in c["paths"] if "data" in p]

    # --- QGIS Server
    c = comps["qgis_server"]
    c["packages"] = _pkgs(raw, r"^qgis-server$|^qgis-server-")
    c["services"] = _units(raw, r"^(qgis|spawn-fcgi)", False)
    c["paths"] = _paths(raw, r"^/srv/qgis")
    c["found"] = bool(c["packages"] or raw["qgis"].get("fcgi") or mine("gisstack-qgis.service") or c["services"])
    c["ours"] = mine("gisstack-qgis.service")
    c["foreign"] = bool(c["packages"] or c["services"]) and not c["ours"]
    c["version"] = c["packages"][0]["version"] if c["packages"] else ""
    if raw["qgis"].get("fcgi"):
        c["details"].append(raw["qgis"]["fcgi"])

    # --- MapProxy
    c = comps["mapproxy"]
    c["packages"] = _pkgs(raw, r"mapproxy")
    c["services"] = _units(raw, r"mapproxy", False)
    c["paths"] = _paths(raw, r"mapproxy")
    c["found"] = bool(c["packages"] or c["services"] or c["paths"] or raw["mapproxy"].get("bin") or mine("gisstack-mapproxy.service"))
    c["ours"] = mine("gisstack-mapproxy.service")
    c["foreign"] = bool(c["packages"] or c["services"] or raw["mapproxy"].get("bin")) and not c["ours"]
    c["version"] = c["packages"][0]["version"] if c["packages"] else ""
    if raw["mapproxy"].get("bin"):
        c["details"].append(raw["mapproxy"]["bin"])

    # --- Caddy
    c = comps["caddy"]
    cd = raw["caddy"]
    c["packages"] = _pkgs(raw, r"^caddy$")
    c["services"] = [u for n, u in sorted(raw["units"].items()) if n.startswith("caddy")]
    c["paths"] = _paths(raw, r"caddy")
    c["found"] = bool(c["packages"] or c["services"] or cd.get("bin") or c["paths"])
    c["ours"] = cd.get("ours") == "yes"
    c["foreign"] = c["found"] and not c["ours"]
    c["version"] = cd.get("version", "")
    c["listen"] = [f"{l['addr']} ({l['proc']})" for l in listeners if l["proc"] == "caddy"]

    # --- other web servers (only relevant as port conflicts)
    webs = []
    for w in WEB_SERVERS:
        pk = _pkgs(raw, rf"^{w}$")
        svc = [u for n, u in raw["units"].items() if n.startswith(w + ".service")]
        if pk or svc:
            names = {"nginx": {"nginx"}, "apache2": {"apache2"}, "httpd": {"httpd"}}[w]
            webs.append({"name": w, "packages": pk, "services": svc,
                         "listen": sorted({l["addr"] for l in listeners if l["proc"] in names})})
    return {"components": comps, "web_servers": webs, "os": raw["os"], "family": raw["family"], "root": raw["root"],
            "listeners": listeners, "ours_marker": ours_marker, "pkgs_all": raw["pkgs"]}


def needed_ports(cfg: dict[str, Any], target: str = "native") -> dict[str, list[int]]:
    on = {c for c, v in cfg["components"].items() if v}
    if cfg["components"].get("geoserver") and target == "native":
        on.add("tomcat")
    want = {"postgis": [int(cfg["postgres"]["port"])], "tomcat": [int(cfg["tomcat"]["port"])], "geoserver": [],
            "qgis_server": [ops.QGIS_PORT], "mapproxy": [ops.MAPPROXY_PORT], "caddy": [80, 443], "webmap": []}
    return {c: want[c] for c in want if c in on}


# processes that belong to a component, for telling "ours" from "somebody else's" on a port
_PROC = {"postgis": {"postgres", "postmaster"}, "tomcat": {"java"}, "qgis_server": {"qgis_mapserv", "spawn-fcgi", "systemd", "init"},
         "mapproxy": {"gunicorn", "python3", "python"}, "caddy": {"caddy"}}


def _is_ours_proc(comp: str, name: str) -> bool:
    """Process names in `ss` are cut to 15 characters, so compare by prefix (qgis_mapserv.fc)."""
    return any(name == p or name.startswith(p) for p in _PROC.get(comp, set()))


def conflicts(report: dict[str, Any], cfg: dict[str, Any], target: str = "native") -> list[dict[str, Any]]:
    """Ports we need that something other than gisstack already listens on."""
    out: list[dict[str, Any]] = []
    seen: dict[tuple[str, int], dict[str, Any]] = {}
    comps = report["components"]
    for comp, ports in needed_ports(cfg, target).items():
        c = comps.get(comp)
        for l in report["listeners"]:
            if l["port"] not in ports:
                continue
            who = l["proc"] or "?"
            if c is not None and c["ours"] and not c["foreign"] and (not l["proc"] or _is_ours_proc(comp, who)):
                continue                      # our own, already running (a different process on our port is still a conflict)
            key = (comp, l["port"])
            if key in seen:                       # same listener on IPv4 and IPv6
                seen[key]["addr"] += ", " + l["addr"]
                continue
            seen[key] = {"component": comp, "port": l["port"], "addr": l["addr"], "process": who, "pid": l["pid"],
                         "owner": comp if (c and c["foreign"] and _is_ours_proc(comp, who)) else "other"}
            out.append(seen[key])
    return out


def scan(cfg: dict[str, Any], mode: str, ssh: dict[str, Any] | None = None, target: str | None = None) -> dict[str, Any]:
    """Inspect the host and return {components, web_servers, conflicts, ...}.  Needs root for process names."""
    t = target or cfg["deploy_target"]
    raw = parse(ops._run(mode, ssh, SCAN_SCRIPT, root=True, timeout=60))
    rep = analyse(raw, cfg)
    rep["target"] = t
    rep["conflicts"] = conflicts(rep, cfg, t)
    rep["wanted"] = list(needed_ports(cfg, t))
    rep["any_foreign"] = any(c["foreign"] for c in rep["components"].values())
    return rep


def format_report(rep: dict[str, Any]) -> str:
    lines = [f"{rep['os']} · {rep['target']}", ""]
    for c in rep["components"].values():
        if not c["found"]:
            lines.append(f"  {c['id']:<12} —")
            continue
        tag = "ჩვენია" if c["ours"] and not c["foreign"] else ("უცხო" if c["foreign"] and not c["ours"] else "ორივე")
        v = f" {c['version']}" if c["version"] else ""
        lines.append(f"  {c['id']:<12} [{tag}]{v}")
        for s in c["services"]:
            lines.append(f"      service {s['name']}: {s['active']}{'' if s['enabled'] is None else (', enabled' if s['enabled'] else ', disabled')}")
        for p in c["packages"][:4]:
            lines.append(f"      package {p['name']} {p['version']}")
        for x in c["paths"][:5]:
            lines.append(f"      path    {x}")
        for x in c["listen"]:
            lines.append(f"      listen  {x}")
        for x in c["data"]:
            lines.append(f"      data    {x}")
        for x in c["details"]:
            lines.append(f"      note    {x}")
    for w in rep["web_servers"]:
        lines.append(f"  {w['name']:<12} [სხვა web server] " + ", ".join(f"{s['name']}:{s['active']}" for s in w["services"]))
    if rep["conflicts"]:
        lines += ["", "პორტების კონფლიქტი:"]
        for x in rep["conflicts"]:
            lines.append(f"  {x['component']}: პორტი {x['port']} უკვე დაკავებულია — {x['process']} ({x['addr']})")
    return "\n".join(lines)
