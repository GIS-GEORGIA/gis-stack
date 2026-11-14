"""Collectors (Phase 8).  One read-only shell script is run on the target (here or over SSH); its sections are parsed by pure
functions into a *sample* and then flattened into metrics.  Every number comes from a real source:

  host     /proc/loadavg, /proc/meminfo, /proc/stat (two readings one second apart), /proc/uptime, df
  services systemd cgroup members' /proc/<pid>/stat + status (CPU, RSS), unit state from `systemctl show`
  postgres pg_stat_activity, pg_stat_database, pg_settings, pg_locks
  probes   curl timings against the public / local endpoints, TCP connect for the database
  backup   `gisstack-backup list`
  compose  `docker stats --no-stream`

If a source is unavailable the metric is simply absent and the failure is reported in `errors` - nothing is ever made up."""
from __future__ import annotations

import calendar
import contextlib
import re
import shlex
import time
from typing import Any, Optional

from .. import generate, providers, transport
from ..core import log
from ..core.errors import DeployError
from ..providers import common

_log = log.get("monitoring")
SAMPLE_SECONDS = 1.0

# --------------------------------------------------------------------------- the script
_HOST = r"""
echo "@@meta ts=$(date +%s)"
echo "@@host"
read -r l1 l5 l15 _ < /proc/loadavg; echo "load $l1 $l5 $l15"
echo "uptime $(cut -d. -f1 /proc/uptime)"
echo "cpus $(nproc 2>/dev/null || echo 1)"
echo "clk_tck $(getconf CLK_TCK 2>/dev/null || echo 100)"
awk '/^(MemTotal|MemAvailable|SwapTotal|SwapFree):/ {print "mem " $1 " " $2}' /proc/meminfo
echo "cpu0 $(head -1 /proc/stat | sed 's/^cpu *//')"
"""

_AFTER = r"""
echo "cpu1 $(head -1 /proc/stat | sed 's/^cpu *//')"
echo "@@disk"
df -Pk / /var /var/lib/gisstack /var/lib/postgresql /var/lib/pgsql /var/backups/gisstack 2>/dev/null | awk 'NR>1 {print "disk " $6 " " $2 " " $3 " " $4}' | sort -u
"""

_PROCS = r"""
cg_pids() {   # pids of a systemd unit (all processes of its cgroup)
  local cg; cg="$(systemctl show -p ControlGroup --value "$1" 2>/dev/null)"
  [ -n "$cg" ] || return 0
  cat "/sys/fs/cgroup${cg}/cgroup.procs" "/sys/fs/cgroup/systemd${cg}/cgroup.procs" 2>/dev/null
}
ticks() { local t=0 x; for p in $1; do x="$(sed 's/^.*) //' /proc/$p/stat 2>/dev/null | awk '{print $12+$13}')"; t=$((t + ${x:-0})); done; echo "$t"; }
rss() { local t=0 x; for p in $1; do x="$(awk '/^VmRSS:/ {print $2}' /proc/$p/status 2>/dev/null)"; t=$((t + ${x:-0})); done; echo "$t"; }
echo "@@procs"
"""


def _unit_list(cfg: dict[str, Any]) -> list[tuple[str, str]]:
    """(service id, systemd unit) pairs for the selected native components."""
    from ..providers import native
    on = generate.effective_components(cfg, "native")
    units = native.units(cfg)
    out: list[tuple[str, str]] = []
    for comp in common.ORDER:
        if not on.get(comp) or comp == "webmap" or not units.get(comp):
            continue
        u = units[comp][-1] if comp != "qgis_server" else "gisstack-qgis.service"
        out.append((comp, "$PGU" if comp == "postgis" else u))
    return out


def _probe_lines(cfg: dict[str, Any], target: str, ids: list[str]) -> str:
    lines = [
        "probe_http() { out=\"$(curl -sk -m 5 -o /dev/null -w '%{http_code} %{time_total}' \"${@:3}\" 2>/dev/null)\"; echo \"probe $1 ${out:-000 0} $2\"; }",
        # probe_httpm <name> <kind> <marker> <curl args>: the code counts only when the body contains <marker> (QGIS: an OGC exception document, not Apache's own 500 page)
        "probe_httpm() { o=\"$(curl -sk -m 5 -w '\\n%{http_code} %{time_total}' \"${@:4}\" 2>/dev/null)\"; r=\"${o##*$'\\n'}\"; case \"${o%$'\\n'*}\" in *\"$3\"*) ;; *) r=\"000 ${r#* }\" ;; esac; echo \"probe $1 ${r:-000 0} $2\"; }",
        "probe_tcp() { s=$(date +%s%N); if (exec 3<>/dev/tcp/127.0.0.1/$2) 2>/dev/null; then e=$(date +%s%N); echo \"probe $1 open $(awk -v a=$s -v b=$e 'BEGIN {printf \"%.4f\", (b-a)/1e9}') tcp\"; else echo \"probe $1 closed 0 tcp\"; fi; }",
        common.PG_READY_FN,
        "probe_pgready() { s=$(date +%s%N); r=$(pg_ready \"$2\"); e=$(date +%s%N); echo \"probe $1 $r $(awk -v a=$s -v b=$e 'BEGIN {printf \"%.4f\", (b-a)/1e9}') tcp\"; }",
        'echo "@@probes"',
    ]
    for c in common.checks(cfg, target, ids):
        name = f"{c['comp']}:{re.sub(r'[^a-z0-9]+', '_', c['label'].lower()).strip('_')}"
        if c["kind"] == "tcp":
            lines.append(f"probe_tcp {shlex.quote(name)} {int(c['port'])}")
        elif c["kind"] == "pgready":
            lines.append(f"probe_pgready {shlex.quote(name)} {int(c['port'])}")
        else:
            res = f"--resolve {shlex.quote(c['resolve'])} " if c.get("resolve") else ""
            fn = f"probe_httpm {shlex.quote(name)} {c['kind']} {shlex.quote(c['match'])}" if c.get("match") else f"probe_http {shlex.quote(name)} {c['kind']}"
            lines.append(f"{fn} {res}{shlex.quote(c['url'])}")
    return "\n".join(lines) + "\n"


_PG = r"""
echo "@@postgres"
if command -v psql >/dev/null 2>&1; then
  (cd /tmp && runuser -u postgres -- psql -X -Atq -F '|' 2>/dev/null <<'SQL'
select 'up', 1;
select 'version', current_setting('server_version');
select 'maxconn', setting from pg_settings where name = 'max_connections';
select 'conn', coalesce(state, 'none'), count(*) from pg_stat_activity where datname is not null group by 2;
select 'db', datname, numbackends, xact_commit, xact_rollback, blks_read, blks_hit, deadlocks, pg_database_size(datname)
  from pg_stat_database where datname is not null and datname not like 'template%' order by datname;
select 'longest', coalesce(extract(epoch from max(now() - query_start)), 0)::bigint
  from pg_stat_activity where state = 'active' and pid <> pg_backend_pid();
select 'waiting_locks', count(*) from pg_locks where not granted;
SQL
  ) || true
fi
"""

_CACHE = r"""
echo "@@cache"
d=/var/lib/gisstack/mapproxy/cache_data
[ -d "$d" ] && echo "cache $(timeout 15 du -sb "$d" 2>/dev/null | cut -f1)"
"""

_BACKUP = r"""
echo "@@backup"
[ -x /usr/local/sbin/gisstack-backup ] && /usr/local/sbin/gisstack-backup list 2>/dev/null | sed 's/^/backup /' || true
"""

_COMPOSE_STATS = r"""
echo "@@stats"
@RT@ stats --no-stream --format '{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}' 2>/dev/null | sed 's/^/stat /' || true
"""


def script(cfg: dict[str, Any], target: str, ids: Optional[list[str]] = None) -> str:
    """The read-only collection script for a target."""
    ids = ids if ids is not None else common.components(cfg, target, None)
    if target == "k8s":
        return 'echo "@@meta ts=$(date +%s)"\n'            # cluster state comes from the provider rows; no host to read
    s = _HOST
    if target == "native":
        units = _unit_list(cfg)
        pre = ['. /etc/os-release 2>/dev/null || true',
               'case " ${ID_LIKE:-} ${ID:-} " in *" debian "*|*" ubuntu "*) PGU="postgresql@' + str(cfg["postgres"]["version"]) + '-main.service" ;; '
               '*) PGU="postgresql-' + str(cfg["postgres"]["version"]) + '.service" ;; esac']
        s += "\n".join(pre) + "\n" + _PROCS
        s += 'SVCS="' + " ".join(f"{comp}:{unit}" for comp, unit in units) + '"\n'
        s += ('declare -A T0\n'
              'for s in $SVCS; do n=${s%%:*}; P="$(cg_pids "${s#*:}")"; T0[$n]="$(echo $P | wc -w) $(ticks "$P") $(rss "$P")"; done\n'
              'sleep 1\n'
              'for s in $SVCS; do n=${s%%:*}; P="$(cg_pids "${s#*:}")"; echo "svc $n ${T0[$n]}"; echo "svc1 $n $(ticks "$P")"; done\n') + _AFTER
        if generate.effective_components(cfg, "native").get("postgis"):
            s += _PG
        if generate.effective_components(cfg, "native").get("mapproxy"):
            s += _CACHE
        s += _probe_lines(cfg, target, ids) + _BACKUP
    else:
        rt = "podman" if cfg.get("runtime") == "podman" else "docker"
        s += "sleep 1\n" + _AFTER + _COMPOSE_STATS.replace("@RT@", rt) + _probe_lines(cfg, target, ids)
    return s + 'echo "@@end"\n'


# --------------------------------------------------------------------------- parsing (pure)
def _num(v: str, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def parse(text: str) -> dict[str, Any]:
    """Split the script output into sections of whitespace-separated fields."""
    sections: dict[str, list[list[str]]] = {}
    cur = "_"
    for line in text.splitlines():
        line = line.rstrip()
        if line.startswith("@@"):
            name, _, rest = line[2:].partition(" ")
            cur = name
            sections.setdefault(cur, [])
            if rest:
                sections[cur].append(rest.split())
            continue
        if line:
            sections.setdefault(cur, []).append(line.split("|") if cur == "postgres" else line.split())
    # the per-service lines are emitted before @@disk; they sit in the preceding section - collect them by keyword
    svc: dict[str, dict[str, float]] = {}
    for rows in sections.values():
        for r in rows:
            if r and r[0] == "svc" and len(r) >= 5:
                svc.setdefault(r[1], {}).update(pids=_num(r[2]), ticks0=_num(r[3]), rss_kb=_num(r[4]))
            elif r and r[0] == "svc1" and len(r) >= 3:
                svc.setdefault(r[1], {})["ticks1"] = _num(r[2])
    return {"sections": sections, "svc": svc}


def _cpu_percent(a: list[str], b: list[str]) -> Optional[float]:
    try:
        x, y = [int(v) for v in a], [int(v) for v in b]
    except ValueError:
        return None
    if len(x) < 4 or len(x) != len(y):
        return None
    total = sum(y) - sum(x)
    idle = (y[3] + (y[4] if len(y) > 4 else 0)) - (x[3] + (x[4] if len(x) > 4 else 0))
    if total <= 0:
        return None
    return round(max(0.0, min(100.0, 100.0 * (total - idle) / total)), 2)


def host_metrics(rows: list[list[str]], interval: float = SAMPLE_SECONDS) -> dict[str, float]:
    m: dict[str, float] = {}
    mem: dict[str, float] = {}
    cpu0 = cpu1 = None
    for r in rows:
        if not r:
            continue
        k = r[0]
        if k == "load" and len(r) >= 4:
            m["load1"], m["load5"], m["load15"] = _num(r[1]), _num(r[2]), _num(r[3])
        elif k == "uptime" and len(r) > 1:
            m["uptime_seconds"] = _num(r[1])
        elif k == "cpus" and len(r) > 1:
            m["cpu_count"] = _num(r[1], 1)
        elif k == "mem" and len(r) >= 3:
            mem[r[1].rstrip(":")] = _num(r[2]) * 1024
        elif k == "cpu0":
            cpu0 = r[1:]
        elif k == "cpu1":
            cpu1 = r[1:]
    if cpu0 and cpu1:
        pct = _cpu_percent(cpu0, cpu1)
        if pct is not None:
            m["cpu_percent"] = pct
    if mem.get("MemTotal"):
        m["mem_total_bytes"] = mem["MemTotal"]
        if "MemAvailable" in mem:
            m["mem_available_bytes"] = mem["MemAvailable"]
            m["mem_used_percent"] = round(100.0 * (mem["MemTotal"] - mem["MemAvailable"]) / mem["MemTotal"], 2)
    if mem.get("SwapTotal"):
        m["swap_used_percent"] = round(100.0 * (mem["SwapTotal"] - mem.get("SwapFree", 0)) / mem["SwapTotal"], 2)
    return m


def disk_metrics(rows: list[list[str]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        if len(r) >= 5 and r[0] == "disk":
            total, used, free = _num(r[2]) * 1024, _num(r[3]) * 1024, _num(r[4]) * 1024
            if total > 0:
                out.append({"mount": r[1], "total_bytes": total, "used_bytes": used, "free_bytes": free,
                            "used_percent": round(100.0 * used / (used + free), 2) if used + free else 0.0})
    return out


def service_usage(svc: dict[str, dict[str, float]], clk_tck: float, cpus: float, interval: float = SAMPLE_SECONDS) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for name, v in svc.items():
        if "ticks0" not in v:
            continue
        item = {"rss_bytes": v.get("rss_kb", 0.0) * 1024, "processes": v.get("pids", 0.0)}
        if "ticks1" in v and clk_tck > 0 and interval > 0:
            item["cpu_percent"] = round(max(0.0, (v["ticks1"] - v["ticks0"]) / clk_tck / interval * 100.0), 2)      # of one core
        out[name] = item
    return out


def postgres_metrics(rows: list[list[str]]) -> dict[str, Any]:
    pg: dict[str, Any] = {"connections": {}, "databases": []}
    for r in rows:
        k = r[0] if r else ""
        if k == "up":
            pg["up"] = 1.0
        elif k == "version" and len(r) > 1:
            pg["version"] = r[1]
        elif k == "maxconn" and len(r) > 1:
            pg["max_connections"] = _num(r[1])
        elif k == "conn" and len(r) >= 3:
            pg["connections"][r[1]] = pg["connections"].get(r[1], 0.0) + _num(r[2])
        elif k == "db" and len(r) >= 9:
            read, hit = _num(r[5]), _num(r[6])
            pg["databases"].append({"name": r[1], "backends": _num(r[2]), "xact_commit": _num(r[3]), "xact_rollback": _num(r[4]),
                                    "blks_read": read, "blks_hit": hit, "deadlocks": _num(r[7]), "size_bytes": _num(r[8]),
                                    "cache_hit_ratio": round(hit / (hit + read), 4) if hit + read else None})
        elif k == "longest" and len(r) > 1:
            pg["longest_query_seconds"] = _num(r[1])
        elif k == "waiting_locks" and len(r) > 1:
            pg["waiting_locks"] = _num(r[1])
    if "up" not in pg:
        return {}
    total = sum(pg["connections"].values())
    pg["connections_total"] = total
    if pg.get("max_connections"):
        pg["connections_percent"] = round(100.0 * total / pg["max_connections"], 2)
    return pg


def probe_metrics(rows: list[list[str]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        if len(r) >= 4 and r[0] == "probe":
            name, code, secs, kind = r[1], r[2], _num(r[3]), (r[4] if len(r) > 4 else "http")
            n = int(code) if code.isdigit() else 0
            alive = code == "open" or (n > 0 and n not in (502, 503, 504))
            # kind "alive" (e.g. Tomcat's root, QGIS without a project): any real answer counts; "http": 2xx/3xx; "tcp": connect
            up = (code == "open") if kind == "tcp" else (alive if kind == "alive" else 200 <= n < 400)
            out.append({"name": name, "kind": kind, "code": code, "seconds": secs, "up": bool(up)})
    return out


def _stamp(backup_id: str) -> str:
    """The time part of a backup id (`[pre-]YYYYMMDDTHHMMSSZ-xxxx`, second resolution).  The random suffix says nothing about order, so ids are compared by it alone:
    a failed attempt in the same second as the newest good backup counts as newer (an alert that is one backup too eager beats a failure nobody hears about)."""
    return backup_id.removeprefix("pre-")[:16]


def backup_metrics(rows: list[list[str]], now: float) -> dict[str, Any]:
    """Age of the newest complete manual/scheduled backup and the number of FAILED attempts since it, from `gisstack-backup list` lines
    (`backup <id> <status> <kind> <created> <bytes> <components>`; status complete / partial / failed / damaged)."""
    best: Optional[float] = None
    count = 0
    newest_id = ""
    for r in rows:
        if len(r) >= 6 and r[0] == "backup" and r[2] == "complete" and r[3] in ("manual", "scheduled"):
            count += 1
            newest_id = max(newest_id, r[1])
            try:
                t = calendar.timegm(time.strptime(r[4], "%Y-%m-%dT%H:%M:%SZ"))
            except ValueError:
                continue
            best = t if best is None or t > best else best
    failed = sum(1 for r in rows if len(r) >= 3 and r[0] == "backup" and r[2] in ("partial", "failed", "damaged") and (not newest_id or _stamp(r[1]) >= _stamp(newest_id)))
    if not count and not failed:
        return {"count": 0}
    return {"count": count, "failed": failed, **({"last_age_seconds": max(0.0, now - best)} if best is not None else {})}


def _bytes(text: str) -> float:
    m = re.match(r"^\s*([0-9.]+)\s*([KMGT]?i?B)?", text)
    if not m:
        return 0.0
    mult = {"B": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12, "KIB": 1024, "MIB": 1024**2, "GIB": 1024**3, "TIB": 1024**4}
    return float(m.group(1)) * mult.get((m.group(2) or "B").upper(), 1)


def compose_usage(rows: list[list[str]], project: str) -> dict[str, dict[str, float]]:
    """`docker stats` lines -> usage per compose service (container names are <project>-<service>-<n> or <project>_<service>_<n>)."""
    out: dict[str, dict[str, float]] = {}
    pat = re.compile(r"^" + re.escape(project) + r"[-_](.+?)[-_]\d+$")
    for r in rows:
        if r and r[0] == "stat" and len(r) >= 2:
            name, _, rest = " ".join(r[1:]).partition("|")
            cpu, _, mem = rest.partition("|")
            m = pat.match(name.strip())
            if m:
                svc = {"qgis": "qgis_server"}.get(m.group(1), m.group(1))
                out[svc] = {"cpu_percent": _num(cpu.strip().rstrip("%")), "rss_bytes": _bytes(mem.split("/")[0]), "processes": 1.0}
    return out


# --------------------------------------------------------------------------- the sample
def build_sample(cfg: dict[str, Any], target: str, text: str, rows: list[dict[str, Any]], took: float) -> dict[str, Any]:
    """Combine one script output and the provider's status rows into a sample.  Missing sources are listed in `errors`."""
    parsed = parse(text)
    sec = parsed["sections"]
    errors: list[str] = []
    ts = 0.0
    for r in sec.get("meta", []):
        for kv in r:
            if kv.startswith("ts="):
                ts = _num(kv[3:])
    sample: dict[str, Any] = {"ts": ts or time.time(), "target": target, "project": cfg["project"], "services": [], "errors": errors, "took": round(took, 3)}
    if target != "k8s":
        # cpu1 is printed after the per-service sampling, i.e. outside the @@host section
        hm = host_metrics(sec.get("host", []) + [r for rows in sec.values() for r in rows if r and r[0] == "cpu1"])
        if hm.get("cpu_percent") is None:
            errors.append("host: cpu sample missing")
        if "mem_total_bytes" not in hm:
            errors.append("host: /proc/meminfo unreadable")
        sample["host"] = hm
        sample["disks"] = disk_metrics(sec.get("disk", []))
        if not sample["disks"]:
            errors.append("disk: df returned nothing")
    clk = next((_num(r[1], 100) for r in sec.get("host", []) if r and r[0] == "clk_tck" and len(r) > 1), 100.0)
    cpus = sample.get("host", {}).get("cpu_count", 1.0)
    usage = service_usage(parsed["svc"], clk, cpus) if target == "native" else (compose_usage(sec.get("stats", []), cfg["project"]) if target == "compose" else {})
    for r in rows:
        u = usage.get(r["id"], {})
        sample["services"].append({"id": r["id"], "state": r["state"], "up": r["state"] == "running", "restarts": r.get("restarts"),
                                   "enabled": r.get("enabled"), **u})
    if target == "native" and generate.effective_components(cfg, "native").get("postgis"):
        pg = postgres_metrics(sec.get("postgres", []))
        if pg:
            sample["postgres"] = pg
        else:
            errors.append("postgres: no answer from psql")
    cache = next((_num(r[1], -1) for r in sec.get("cache", []) if r and r[0] == "cache" and len(r) > 1), -1)
    if cache >= 0:
        sample["mapproxy"] = {"cache_bytes": cache}
    elif target == "native" and generate.effective_components(cfg, "native").get("mapproxy"):
        errors.append("mapproxy: cache size unavailable")
    if target != "k8s":
        sample["probes"] = probe_metrics(sec.get("probes", []))
    if target == "native":
        b = backup_metrics(sec.get("backup", []), sample["ts"])
        if b.get("count") or "backup" in sec:
            sample["backup"] = b
    return sample


def collect(cfg: dict[str, Any], mode: str, ssh: Optional[dict[str, Any]], target: Optional[str] = None) -> dict[str, Any]:
    """Take one sample.  Raises DeployError when the machine cannot be reached at all."""
    t = common.resolve_target(cfg, target)
    ids = common.components(cfg, t, None)
    t0 = time.time()
    prov = providers.get(t)
    rows = prov.status_rows(cfg, ids, lambda s: transport.run(mode, ssh, s))
    out = transport.run(mode, ssh, script(cfg, t, ids), root=prov.needs_root or t == "native", timeout=90)
    if not out.strip():
        raise DeployError("monitoring: empty answer from the collector")
    return build_sample(cfg, t, out, rows, time.time() - t0)


# --------------------------------------------------------------------------- container targets: database health and backups
_PG_SQL = ("select count(*), current_setting('max_connections')::int, "
           "coalesce(max(extract(epoch from now() - query_start)) filter (where state = 'active' and pid <> pg_backend_pid()), 0), "
           "(select count(*) from pg_locks where not granted) from pg_stat_activity where datname is not null")


def stack_backup_stats(store_rows: list[dict[str, Any]], now: float) -> dict[str, float]:
    """From the stack-backup list (newest first): how many intact backups, the age of the newest one and how many attempts FAILED since it
    (a partial / failed / damaged entry newer than the newest intact backup - a later good backup clears the alarm)."""
    ok = [r for r in store_rows if r.get("status") == "ok" and r.get("kind") in ("manual", "scheduled")]
    out: dict[str, float] = {"count": float(len(ok)), "failed": 0.0}
    newest = ok[0]["id"] if ok else ""
    out["failed"] = float(sum(1 for r in store_rows if r.get("status") in ("partial", "failed", "damaged") and (not newest or _stamp(r["id"]) >= _stamp(newest))))
    for r in ok:
        with contextlib.suppress(ValueError):
            out["last_age_seconds"] = max(0.0, now - calendar.timegm(time.strptime(r["created"], "%Y-%m-%dT%H:%M:%SZ")))
            break
    return out


def container_extras(cfg: dict[str, Any], mode: str, ssh: Optional[dict[str, Any]], now: float) -> tuple[list[tuple[str, dict[str, str], float]], list[str]]:
    """Metrics the host script cannot give on Compose / Podman / Kubernetes: PostgreSQL health (asked through the same engine access the backups use)
    and the state of the stack backups (read from the local backup store).  Returns (flat metrics, errors); never raises."""
    from ..backups import provider as bprov
    flat: list[tuple[str, dict[str, str], float]] = []
    errors: list[str] = []
    try:
        prov = bprov.provider_for(cfg, mode, ssh)
    except Exception as e:  # noqa: BLE001 - e.g. native target: nothing to add
        return flat, [f"stack metrics: {type(e).__name__}"] if "native" not in str(e) else []
    try:
        st = stack_backup_stats(prov.store.list(), now)
        flat += [("backup_count", {}, st["count"]), ("backup_failed_count", {}, st["failed"])]
        if "last_age_seconds" in st:
            flat.append(("backup_last_age_seconds", {}, st["last_age_seconds"]))
    except Exception as e:  # noqa: BLE001 - an unreadable store is reported, not fatal
        errors.append(f"backup store: {type(e).__name__}: {e}"[:200])
    if generate.effective_components(cfg, cfg.get("deploy_target", "compose")).get("postgis"):
        try:
            row = prov.psql(_PG_SQL, db=prov.db, timeout=30).split("|")
            total, mx, longest, waiting = (_num(x) for x in row[:4])
            flat += [("pg_up", {}, 1.0), ("pg_connections_total", {}, total), ("pg_max_connections", {}, mx),
                     ("pg_connections_percent", {}, round(100.0 * total / mx, 1) if mx else 0.0), ("pg_longest_query_seconds", {}, longest),
                     ("pg_waiting_locks", {}, waiting)]
        except Exception as e:  # noqa: BLE001 - PostgreSQL not answering IS the finding
            flat.append(("pg_up", {}, 0.0))
            errors.append(f"postgres: no answer ({type(e).__name__})")
    return flat, errors


# --------------------------------------------------------------------------- flattening
def flatten(sample: dict[str, Any]) -> list[tuple[str, dict[str, str], float]]:
    """(metric name, labels, value) triples - the shape stored in SQLite and exported as Prometheus text."""
    out: list[tuple[str, dict[str, str], float]] = []

    def add(name: str, value: Any, **labels: str) -> None:
        if value is not None:
            out.append((name, {k: str(v) for k, v in labels.items()}, float(value)))
    for k, v in (sample.get("host") or {}).items():
        add(f"host_{k}", v)
    for d in sample.get("disks") or []:
        add("disk_used_percent", d["used_percent"], mount=d["mount"])
        add("disk_free_bytes", d["free_bytes"], mount=d["mount"])
        add("disk_total_bytes", d["total_bytes"], mount=d["mount"])
    for s in sample.get("services") or []:
        add("service_up", 1.0 if s["up"] else 0.0, service=s["id"])
        add("service_restarts", s.get("restarts"), service=s["id"])
        add("service_cpu_percent", s.get("cpu_percent"), service=s["id"])
        add("service_rss_bytes", s.get("rss_bytes"), service=s["id"])
    pg = sample.get("postgres")
    if pg:
        add("pg_up", 1.0)
        add("pg_max_connections", pg.get("max_connections"))
        add("pg_connections_total", pg.get("connections_total"))
        add("pg_connections_percent", pg.get("connections_percent"))
        add("pg_longest_query_seconds", pg.get("longest_query_seconds"))
        add("pg_waiting_locks", pg.get("waiting_locks"))
        for state, n in pg.get("connections", {}).items():
            add("pg_connections", n, state=state)
        for d in pg.get("databases", []):
            add("pg_database_size_bytes", d["size_bytes"], database=d["name"])
            add("pg_xact_commit_total", d["xact_commit"], database=d["name"])
            add("pg_xact_rollback_total", d["xact_rollback"], database=d["name"])
            add("pg_deadlocks_total", d["deadlocks"], database=d["name"])
            add("pg_cache_hit_ratio", d["cache_hit_ratio"], database=d["name"])
    elif "postgres" in " ".join(sample.get("errors", [])):
        add("pg_up", 0.0)
    for p in sample.get("probes") or []:
        add("probe_up", 1.0 if p["up"] else 0.0, probe=p["name"])
        add("probe_seconds", p["seconds"], probe=p["name"])
    if sample.get("mapproxy"):
        add("mapproxy_cache_bytes", sample["mapproxy"]["cache_bytes"])
    b = sample.get("backup")
    if b:
        add("backup_count", b.get("count"))
        add("backup_failed_count", b.get("failed", 0))
        add("backup_last_age_seconds", b.get("last_age_seconds"))
    add("monitor_reachable", 1.0)                       # a sample exists, so the machine answered (Monitor.tick records 0 when it did not)
    add("collect_seconds", sample.get("took"))
    add("collect_errors", float(len(sample.get("errors", []))))
    return out
