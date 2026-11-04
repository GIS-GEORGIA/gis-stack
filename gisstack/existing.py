"""What to do with programs that are already installed: back up, remove, build on top, or re-use their settings.

A *plan* maps a program to an action:

    ignore  leave it alone (only allowed when it does not block a port we need)
    adopt   build on top of it  (PostgreSQL: use the existing server, add our database/extensions)
    remove  back up its configuration, then uninstall it  (purge=True also deletes its data)
    reuse   remove + after our install, import what can be carried over
            (PostgreSQL databases, GeoServer workspaces/styles/layers, QGIS projects, Caddy site blocks)
    disable (other web servers on port 80/443) stop it and switch off autostart

Backups stay on the server in /var/backups/gisstack/<timestamp>/ and a copy is downloaded to this machine."""
from __future__ import annotations

import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from . import deploy, discover, ops

ACTIONS = ("ignore", "adopt", "remove", "reuse", "disable")
REMOTE_BACKUPS = "/var/backups/gisstack"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+._:-]*$")
_UNIT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@._:-]*$")
_DB = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.$-]*$")

# configuration worth keeping, per program (constants: nothing from the server ever reaches a shell unquoted)
BACKUP_PATHS: dict[str, list[str]] = {
    "postgis": ["/etc/postgresql", "/var/lib/pgsql/*/data/postgresql.conf", "/var/lib/pgsql/*/data/pg_hba.conf",
                "/var/lib/pgsql/*/data/pg_ident.conf", "/var/lib/pgsql/*/data/postgresql.auto.conf"],
    "tomcat": ["/etc/tomcat*", "/opt/tomcat*/conf", "/opt/apache-tomcat*/conf", "/usr/share/tomcat*/conf"],
    "geoserver": ["/var/lib/geoserver*", "/opt/geoserver*/data_dir", "/usr/share/geoserver/data_dir", "/etc/geoserver*"],
    "qgis_server": ["/srv/qgis", "/etc/qgis*"],
    "mapproxy": ["/etc/mapproxy", "/opt/mapproxy*"],
    "caddy": ["/etc/caddy"],
}
# software removed together with the program (never data); data paths are only removed on purge
DATA_PATHS: dict[str, list[str]] = {
    "postgis": ["/var/lib/postgresql", "/etc/postgresql", "/var/lib/pgsql"],
    "tomcat": ["/var/lib/tomcat*", "/etc/tomcat*"],
    "geoserver": ["/var/lib/geoserver*", "/opt/geoserver*/data_dir", "/usr/share/geoserver/data_dir"],
    "qgis_server": ["/srv/qgis"],
    "mapproxy": ["/etc/mapproxy"],
    "caddy": ["/var/lib/caddy"],
}


class PlanError(deploy.DeployError):
    pass


def parse_plan_spec(spec: str) -> dict[str, dict[str, Any]]:
    """'postgis=adopt,tomcat=remove,caddy=reuse,nginx=disable'  ->  plan dict ('remove+purge' also works)."""
    plan: dict[str, dict[str, Any]] = {}
    for part in [p.strip() for p in spec.split(",") if p.strip()]:
        comp, _, act = part.partition("=")
        purge = act.endswith("+purge")
        act = act.removesuffix("+purge")
        if comp == "qgis":
            comp = "qgis_server"
        if act not in ACTIONS:
            raise PlanError(f"unknown action {act!r} for {comp}: {', '.join(ACTIONS)}")
        plan[comp] = {"action": act, "purge": purge}
    return plan


def _norm(plan: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for comp, v in (plan or {}).items():
        v = {"action": v} if isinstance(v, str) else dict(v)
        if v.get("action") not in ACTIONS:
            raise PlanError(f"unknown action for {comp}: {v.get('action')!r}")
        known = comp in discover.COMPONENTS or comp in discover.WEB_SERVERS
        if not known:
            raise PlanError(f"unknown program: {comp}")
        out[comp] = {"action": v["action"], "purge": bool(v.get("purge"))}
    return out


def validate(plan: dict[str, dict[str, Any]], rep: dict[str, Any], cfg: dict[str, Any]) -> list[str]:
    """Problems that must be fixed before anything is touched (empty = plan is fine)."""
    problems: list[str] = []
    comps = rep["components"]
    removed = set()
    for comp, p in plan.items():
        act = p["action"]
        if comp in discover.WEB_SERVERS:
            if act not in ("disable", "ignore"):
                problems.append(f"{comp}: მხოლოდ disable ან ignore")
            if act == "disable":
                removed.add(comp)
            continue
        c = comps[comp]
        if act in ("remove", "reuse", "adopt") and not c["foreign"]:
            problems.append(f"{comp}: უცხო ინსტალაცია არ ნაპოვნია — {act} საჭირო არ არის")
        if act == "adopt" and comp != "postgis":
            problems.append(f"{comp}: 'adopt' მხოლოდ PostgreSQL-ისთვისაა (დანარჩენისთვის remove ან reuse)")
        if act == "adopt" and comp == "postgis" and not c["major"]:
            problems.append("postgis: არსებული PostgreSQL გაშვებული არ არის (ვერსია უცნობია) — adopt შეუძლებელია")
        if act in ("remove", "reuse"):
            removed.add(comp)
        if act == "adopt":
            removed.add(comp)          # no longer a conflict: we use it instead of starting our own
        if act == "reuse" and comp not in ("postgis", "geoserver", "qgis_server", "caddy", "tomcat", "mapproxy"):
            problems.append(f"{comp}: reuse არ არის მხარდაჭერილი")
    # whatever still holds a port we need and is not dealt with in the plan
    for x in rep["conflicts"]:
        who = x["component"]
        owner_is_web = x["process"] in ("nginx", "apache2", "httpd")
        if owner_is_web:
            name = "apache2" if x["process"] in ("apache2",) else x["process"]
            if name not in removed and plan.get(name, {}).get("action") != "disable":
                problems.append(f"პორტი {x['port']} დაკავებულია: {x['process']} — დაამატე {name}=disable")
        elif who not in removed:
            problems.append(f"პორტი {x['port']} ({who}) დაკავებულია: {x['process']} — აირჩიე remove / reuse"
                            + (" / adopt" if who == "postgis" else ""))
    return problems


# --------------------------------------------------------------------------- scripts
def _q(s: str) -> str:
    return shlex.quote(s)


def backup_script(rep: dict[str, Any], comps: list[str], ts: str, with_data: bool) -> str:
    d = f"{REMOTE_BACKUPS}/{ts}"
    out = ["set -u", f"D={_q(d)}", 'umask 077', 'mkdir -p "$D"', f'echo "gisstack backup {ts}" > "$D/manifest.txt"']
    for comp in comps:
        paths = BACKUP_PATHS.get(comp, [])
        out.append(f'echo "== backup {comp}"')
        out.append("L=''")
        for pat in paths:
            out.append(f'for p in {pat}; do [ -e "$p" ] && L="$L ${{p#/}}"; done')
        if comp == "geoserver" and not with_data:
            # data dirs can be huge (rasters): skip above 500 MB unless the user asked for data too
            out.append('for p in $L; do s=$(du -sm "/$p" 2>/dev/null | cut -f1); '
                       '[ "${s:-0}" -gt 500 ] && { echo "skipped (too big: ${s} MB, use --with-data): /$p"; '
                       'L="$(echo " $L " | sed "s# $p # #")"; }; done')
        out.append(f'[ -n "$L" ] && tar czf "$D/{comp}.tar.gz" --ignore-failed-read -C / $L 2>/dev/null && echo "  {comp}.tar.gz ok" || echo "  (no files)"')
        if comp == "tomcat":
            out.append('ls -1d /var/lib/tomcat*/webapps/* /opt/tomcat*/webapps/* /opt/apache-tomcat*/webapps/* /usr/share/tomcat*/webapps/* 2>/dev/null > "$D/tomcat-webapps.txt"')
        if comp == "postgis":
            dbs = [x for x in rep["components"]["postgis"].get("databases", []) if _DB.match(x)]
            out.append('(cd /tmp && runuser -u postgres -- pg_dumpall --globals-only > "$D/pg-globals.sql" 2>/dev/null) && echo "  pg-globals.sql ok"')
            if with_data:
                for db in dbs:
                    out.append(f'(cd /tmp && runuser -u postgres -- pg_dump -Fc {_q(db)} > "$D/pg-db-{db}.dump") && echo "  pg-db-{db}.dump ok" || echo "  FAILED dump {db}"')
    out.append('echo "BACKUP_DIR=$D"')
    return "\n".join(out) + "\n"


def _pkgs(c: dict[str, Any], pattern: str | None = None) -> list[str]:
    out = []
    for p in c["packages"]:
        if _NAME.match(p["name"]) and (pattern is None or re.search(pattern, p["name"])):
            out.append(p["name"])
    return out


def remove_script(rep: dict[str, Any], comp: str, purge: bool) -> str:
    fam = rep["family"]
    c = rep["components"][comp]
    units = [u["name"] for u in c["services"] if _UNIT.match(u["name"])]
    pk = _pkgs(c)
    out = ["set -u", f'echo "== remove {comp}"']
    if units:
        out.append(f'systemctl disable --now {" ".join(_q(u) for u in units)} 2>&1 | tail -2; true')
    if comp == "postgis":
        # every PostgreSQL / PostGIS package, not only the server (PostGIS packages depend on the server)
        pk = sorted({p["name"] for p in rep["pkgs_all"] if re.match(r"^(postgresql|postgis)", p["name"]) and _NAME.match(p["name"])
                     and not p["name"].startswith(("postgresql-common", "postgresql-client-common"))})
    if comp == "tomcat":
        pk = sorted({p["name"] for p in rep["pkgs_all"] if re.match(r"^tomcat", p["name"]) and _NAME.match(p["name"])})
    if comp == "qgis_server":
        pk = sorted({p["name"] for p in rep["pkgs_all"] if re.match(r"^qgis-server", p["name"]) and _NAME.match(p["name"])})
    if comp == "caddy":
        pk = ["caddy"] if any(p["name"] == "caddy" for p in rep["pkgs_all"]) else []
    if comp == "geoserver":
        out.append('for w in /var/lib/tomcat*/webapps/geoserver /opt/tomcat*/webapps/geoserver /opt/apache-tomcat*/webapps/geoserver '
                   '/usr/share/tomcat*/webapps/geoserver /var/lib/tomcat*/webapps/geoserver.war; do '
                   '[ -e "$w" ] && rm -rf "$w" && echo "  removed $w"; done; true')
    if pk:
        names = " ".join(_q(p) for p in pk)
        if fam == "deb":
            verb = "purge" if purge else "remove"    # 'purge' would also drop PostgreSQL clusters
            out.append(f"DEBIAN_FRONTEND=noninteractive apt-get {verb} -y {names} 2>&1 | tail -3")
        else:
            out.append(f"dnf remove -y {names} 2>&1 | tail -3")
    if comp == "postgis" and not purge and fam == "deb":
        # the data stays, but Debian would still count the old cluster's config when it picks a port for ours
        out.append('[ -d /etc/postgresql ] && mv /etc/postgresql "/etc/postgresql.old-$(date +%s)" && echo "  /etc/postgresql moved aside (data in /var/lib/postgresql is untouched)"; true')
    if comp == "caddy":
        out.append('rm -f /usr/local/bin/caddy.foreign 2>/dev/null; '
                   '[ -f /usr/local/bin/caddy ] && ! grep -q "(gisstack)" /etc/systemd/system/caddy.service 2>/dev/null && '
                   'rm -f /usr/local/bin/caddy && echo "  removed /usr/local/bin/caddy"; true')
        if not purge:
            # our Caddyfile would overwrite theirs - move it aside (the backup has a copy too)
            out.append('[ -d /etc/caddy ] && mv /etc/caddy "/etc/caddy.old-$(date +%s)" && echo "  /etc/caddy moved aside"; true')
    if comp == "tomcat":
        out.append('for t in /opt/tomcat* /opt/apache-tomcat*; do [ -e "$t" ] && ! [ -f /etc/systemd/system/gisstack-tomcat.service ] && rm -rf "$t" && echo "  removed $t"; done; true')
    if purge:
        for pat in DATA_PATHS.get(comp, []):
            out.append(f'for p in {pat}; do [ -e "$p" ] && rm -rf "$p" && echo "  purged $p"; done; true')
    if units:
        # Debian leaves the units of a removed package masked (a symlink to /dev/null) - that would swallow our own unit file
        out.append(f'systemctl unmask {" ".join(_q(u) for u in units)} 2>/dev/null; '
                   f'for u in {" ".join(_q(u) for u in units)}; do [ -L "/etc/systemd/system/$u" ] && [ "$(readlink "/etc/systemd/system/$u")" = /dev/null ] && rm -f "/etc/systemd/system/$u"; done; true')
    out.append('systemctl daemon-reload 2>/dev/null; true')
    return "\n".join(out) + "\n"


def disable_script(name: str) -> str:
    if not _UNIT.match(name):
        raise PlanError("bad unit name")
    return f'echo "== disable {name}"; systemctl disable --now {_q(name)} 2>&1 | tail -2; systemctl is-active {_q(name)} || true\n'


def import_script(rep: dict[str, Any], comps: list[str], ts: str) -> str:
    d = f"{REMOTE_BACKUPS}/{ts}"
    out = ["set -u", f"D={_q(d)}", "[ -f /etc/gisstack/stack.env ] && . /etc/gisstack/stack.env",
           'GS="${GS_DATA_DIR:-/var/lib/gisstack/geoserver_data}"; QP="${QGIS_PROJECTS_DIR:-/srv/qgis/projects}"',
           'DBU="${DB_USER:-gis}"', "T=$(mktemp -d); trap 'rm -rf \"$T\"' EXIT"]
    for comp in comps:
        out.append(f'echo "== import {comp}"')
        if comp == "postgis":
            out.append(r'''for f in "$D"/pg-db-*.dump; do
  [ -e "$f" ] || { echo "  (no database dumps in the backup)"; break; }
  n="${f##*/pg-db-}"; n="${n%.dump}"
  if (cd /tmp && runuser -u postgres -- psql -Atqc "select 1 from pg_database where datname='$n'" | grep -q 1); then echo "  $n: already exists - skipped"; continue; fi
  (cd /tmp && runuser -u postgres -- createdb -O "$DBU" "$n") || { echo "  FAILED createdb $n"; continue; }
  # the dump is root-only: open it here (as root) and feed it to pg_restore through stdin
  if (cd /tmp && runuser -u postgres -- pg_restore --no-owner --no-acl --exit-on-error -d "$n" < "$f" 2>&1 | tail -3; exit ${PIPESTATUS[0]}); then
    (cd /tmp && runuser -u postgres -- psql -qd "$n" -c "ALTER SCHEMA public OWNER TO \"$DBU\"" 2>/dev/null)
    (cd /tmp && runuser -u postgres -- psql -qd "$n" -Atc "select format('ALTER TABLE %I.%I OWNER TO %I', schemaname, tablename, '$DBU') from pg_tables where schemaname not in ('pg_catalog','information_schema') and tableowner<>'$DBU'" | while read -r q; do (cd /tmp && runuser -u postgres -- psql -qd "$n" -c "$q" >/dev/null 2>&1); done)
    echo "  restored database $n (owner $DBU)"
  else
    (cd /tmp && runuser -u postgres -- dropdb "$n" 2>/dev/null)      # do not leave an empty database behind
    echo "  FAILED to restore $n (the dump is still in $D)"
  fi
done''')
        elif comp == "geoserver":
            out.append(r'''if [ -f "$D/geoserver.tar.gz" ]; then
  mkdir -p "$T/gs" && tar xzf "$D/geoserver.tar.gz" -C "$T/gs"
  src="$(find "$T/gs" -type d -name workspaces -prune -printf '%h\n' | head -1)"
  if [ -n "$src" ]; then
    systemctl stop gisstack-tomcat 2>/dev/null
    for x in "$src"/*; do b="$(basename "$x")"; [ "$b" = security ] && continue; rm -rf "$GS/$b"; cp -a "$x" "$GS/$b"; done
    chown -R tomcat:tomcat "$GS"
    systemctl start gisstack-tomcat 2>/dev/null
    echo "  GeoServer data copied from $src (security/ not copied: our admin password stays; users/roles and encrypted store passwords need to be re-entered)"
  else echo "  no GeoServer data directory in the backup"; fi
else echo "  (nothing to import)"; fi''')
        elif comp == "qgis_server":
            out.append(r'''if [ -f "$D/qgis_server.tar.gz" ]; then
  mkdir -p "$T/q" && tar xzf "$D/qgis_server.tar.gz" -C "$T/q"
  if [ -d "$T/q/srv/qgis" ]; then
    mkdir -p "$QP"; cp -an "$T/q/srv/qgis/." "$QP/" 2>/dev/null; chown -R qgis:qgis "$QP" 2>/dev/null
    echo "  QGIS projects copied into $QP"
  fi
else echo "  (nothing to import)"; fi''')
        elif comp == "caddy":
            out.append(r'''if [ -f "$D/caddy.tar.gz" ]; then
  mkdir -p "$T/c" && tar xzf "$D/caddy.tar.gz" -C "$T/c"
  F="$T/c/etc/caddy/Caddyfile"
  if [ -f "$F" ]; then
    mkdir -p /etc/gisstack/caddy.d /etc/gisstack/imported
    cp "$F" /etc/gisstack/imported/Caddyfile.foreign
    cp "$F" /etc/gisstack/caddy.d/imported.caddy
    if ! /usr/local/bin/caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1; then
      # most common clash: their global options block - try without it
      awk 'BEGIN{skip=0;done=0} !done && /^\{[[:space:]]*$/ {skip=1} skip { if ($0 ~ /^\}[[:space:]]*$/) {skip=0;done=1}; next } {print}' "$F" > /etc/gisstack/caddy.d/imported.caddy
    fi
    if /usr/local/bin/caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1; then
      systemctl reload caddy 2>/dev/null || systemctl restart caddy
      echo "  Caddy: your site blocks are now included via /etc/gisstack/caddy.d/imported.caddy"
    else
      rm -f /etc/gisstack/caddy.d/imported.caddy
      echo "  Caddy: could not merge your Caddyfile (conflicts with ours). It is saved in /etc/gisstack/imported/Caddyfile.foreign"
    fi
  else echo "  no Caddyfile in the backup"; fi
else echo "  (nothing to import)"; fi''')
        else:
            out.append('echo "  (settings are kept in the backup; nothing is carried over automatically)"')
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- backups on this machine
def fetch_backup(mode: str, ssh: dict[str, Any] | None, ts: str, dest_dir: str | Path | None, host_label: str) -> Path:
    """Download /var/backups/gisstack/<ts> as one tar.gz into dest_dir/<host>/."""
    dest = (Path(dest_dir).expanduser() if dest_dir else deploy.home_dir() / "backups") / re.sub(r"[^A-Za-z0-9._-]", "_", host_label)
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / f"{ts}.tar.gz"
    script = f"tar czf - -C {_q(REMOTE_BACKUPS)} {_q(ts)}"
    with open(out, "wb") as fh:
        p = subprocess.run(ops._argv(mode, ssh, script, root=True), stdin=subprocess.DEVNULL, stdout=fh,
                           stderr=subprocess.PIPE, timeout=900)
    if p.returncode != 0 or out.stat().st_size == 0:
        out.unlink(missing_ok=True)
        raise deploy.DeployError("ბექაფის ჩამოტვირთვა ვერ მოხერხდა: " + p.stderr.decode(errors="replace").strip()[-200:])
    out.chmod(0o600)
    return out


def host_label(mode: str, ssh: dict[str, Any] | None) -> str:
    return "localhost" if mode == "local" else str((ssh or {}).get("host", "server"))


# --------------------------------------------------------------------------- orchestration
def backup(cfg: dict[str, Any], target: str, mode: str, ssh: dict[str, Any] | None, comps: list[str], *,
           with_data: bool = False, backup_dir: str | None = None) -> Iterator[str]:
    """Back up the settings (and with_data: databases / big data dirs) of programs that are already installed."""
    if target != "native":
        raise PlanError("არსებული პროგრამების მართვა ახლა მხოლოდ native სამიზნეზეა")
    rep = discover.scan(cfg, mode, ssh, target)
    comps = [c for c in comps if c in discover.COMPONENTS]
    if not comps:
        raise PlanError("კომპონენტი არ არის არჩეული")
    ts = time.strftime("%Y%m%d-%H%M%S")
    yield f"== ბექაფი: {', '.join(comps)}  ({REMOTE_BACKUPS}/{ts})"
    out = ops._run(mode, ssh, backup_script(rep, comps, ts, with_data), root=True, timeout=900)
    yield from _tail(out)
    if "BACKUP_DIR=" not in out:
        raise deploy.DeployError("ბექაფი ვერ შეიქმნა")
    yield f"ბექაფის ასლი ამ მანქანაზე: {fetch_backup(mode, ssh, ts, backup_dir, host_label(mode, ssh))}"
    yield "== დასრულდა"


def _tail(text: str) -> Iterator[str]:
    for line in text.splitlines():
        if line.strip():
            yield line


def run(cfg: dict[str, Any], target: str, mode: str, ssh: dict[str, Any] | None, plan: dict[str, Any] | None,
        make_bundle: Callable[[dict[str, Any]], Path], *, with_data: bool = False, backup_dir: str | None = None,
        install: bool = True) -> Iterator[str]:
    """scan -> check plan -> backup -> remove -> install (build on top where asked) -> import.  Yields log lines.

    make_bundle(cfg) must render the bundle for the (possibly adjusted) cfg and return its folder."""
    if target != "native":
        raise PlanError("არსებული პროგრამების მართვა ახლა მხოლოდ native სამიზნეზეა")
    plan = _norm(plan)
    yield "== არსებული პროგრამების შემოწმება"
    rep = discover.scan(cfg, mode, ssh, target)
    problems = validate(plan, rep, cfg)
    if problems:
        yield from discover.format_report(rep).splitlines()
        raise PlanError("გეგმა არასრულია:\n  " + "\n  ".join(problems))
    cfg = dict(cfg)
    flags: list[str] = []
    todo_backup = [c for c, p in plan.items() if c in discover.COMPONENTS and p["action"] in ("remove", "reuse")]
    ts = time.strftime("%Y%m%d-%H%M%S")
    if todo_backup:
        # PostgreSQL databases are only dumped when they will be re-used (or when asked for)
        yield f"== ბექაფი: {', '.join(todo_backup)}  ({REMOTE_BACKUPS}/{ts})"
        dump_pg = with_data or plan.get("postgis", {}).get("action") == "reuse"
        out = ops._run(mode, ssh, backup_script(rep, todo_backup, ts, with_data or dump_pg), root=True, timeout=900)
        yield from _tail(out)
        if "BACKUP_DIR=" not in out:
            raise deploy.DeployError("ბექაფი ვერ შეიქმნა — არაფერი წაშლილა")
        local = fetch_backup(mode, ssh, ts, backup_dir, host_label(mode, ssh))
        yield f"ბექაფის ასლი ამ მანქანაზე: {local}"
    for comp, p in plan.items():
        if comp in discover.WEB_SERVERS and p["action"] == "disable":
            yield from _tail(ops._run(mode, ssh, disable_script(comp + ".service"), root=True))
    for comp in discover.COMPONENTS:
        p = plan.get(comp)
        if p and p["action"] in ("remove", "reuse"):
            yield from _tail(ops._run(mode, ssh, remove_script(rep, comp, p["purge"]), root=True, timeout=600))
    pg = plan.get("postgis")
    if pg and pg["action"] == "adopt":
        c = rep["components"]["postgis"]
        cfg["postgres"] = dict(cfg["postgres"], version=c["major"])
        flags.append("--adopt-postgres")
        yield f"== PostgreSQL {c['major']}: ვიყენებთ არსებულს (ახალ სერვერს არ ვაყენებთ, პარამეტრებს არ ვცვლით)"
    if install:
        bundle = make_bundle(cfg)
        yield from deploy.deploy(cfg, bundle, target, mode, ssh, extra_args=flags)
    reuse = [c for c, p in plan.items() if c in discover.COMPONENTS and p["action"] == "reuse"]
    if reuse and install:
        yield f"== იმპორტი ბექაფიდან: {', '.join(reuse)}"
        yield from _tail(ops._run(mode, ssh, import_script(rep, reuse, ts), root=True, timeout=900))
    yield "== დასრულდა"
