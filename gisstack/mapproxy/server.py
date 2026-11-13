"""How the MapProxy manager reaches MapProxy: scripts run ON the server (locally or over SSH) through the shared transport, as root only where a file
under /etc or the cache needs it; MapProxy itself (validation, seeding) runs as the unprivileged `mapproxy` user.

Everything the web application sends is either validated names/numbers or file *content* (base64 on stdin / a quoted constant), never shell syntax.
`GS_MP_*` environment variables on the server side let tests (and unusual installs) relocate directories; they are never read from a request."""
from __future__ import annotations

import base64
import contextlib
import re
import shlex
import threading
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from .. import targets, transport
from ..core.errors import DeployError

MAX_FILE = 1024 * 1024
SEED_DEADLINE = 12 * 3600
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$")
BACKUP_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")

PRELUDE = r"""set -u
MP_DIR="${GS_MP_DIR:-/etc/gisstack/mapproxy}"
CACHE="${GS_MP_CACHE:-/var/lib/gisstack/mapproxy/cache_data}"
BK="${GS_MP_BACKUPS:-/var/lib/gisstack/mapproxy/config-backups}"
VENV="${GS_MP_VENV:-/opt/gisstack/mapproxy-venv}"
PORT="${GS_MP_PORT:-8081}"
RUNAS="${GS_MP_RUNAS-runuser -u mapproxy --}"
"""

VALIDATOR = r'''
import json, logging, sys
res = {"ok": True, "errors": [], "warnings": []}
seen = []
class H(logging.Handler):
    def emit(self, r):
        seen.append(r.getMessage()[:300])
logging.getLogger("mapproxy").addHandler(H())
logging.getLogger("mapproxy").setLevel(logging.WARNING)
try:
    from mapproxy.config.loader import load_configuration
    conf = load_configuration(sys.argv[1], ignore_warnings=False)    # strict: unknown keys are errors
    conf.configured_services()                                       # builds layers/caches/sources: bad references raise here
    if len(sys.argv) > 2 and sys.argv[2]:
        from mapproxy.seed.config import load_seed_tasks_conf
        sc = load_seed_tasks_conf(sys.argv[2], conf)
        sc.seeds()
        sc.cleanups()                                                # unknown caches/grids/coverages raise here
except Exception as e:
    res["ok"] = False
    res["errors"].append((type(e).__name__ + ": " + str(e))[:800])
    res["errors"] += [w for w in seen if "demo page enabled" not in w][:10]
else:
    res["warnings"] = [w for w in seen if "demo page enabled" not in w][:20]
print("RESULT " + json.dumps(res))
'''


@dataclass
class Check:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    backup: str = ""


@dataclass
class Tile:
    status: int
    content_type: str
    body: bytes


class Gateway:
    """The seam the manager is written against (tests plug in fakes)."""

    def read(self) -> dict[str, Optional[str]]:
        raise NotImplementedError

    def read_backup(self, backup_id: str) -> dict[str, Optional[str]]:
        raise NotImplementedError

    def backups(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def apply(self, config: str, seed: Optional[str], dry_run: bool) -> Check:
        raise NotImplementedError

    def stats(self) -> dict[str, Any]:
        raise NotImplementedError

    def cleanup(self, names: list[str], older_than_days: Optional[int], dry_run: bool) -> list[dict[str, Any]]:
        raise NotImplementedError

    def seed(self, seed: str, concurrency: int, dry_run: bool, stop: Optional[threading.Event]) -> Iterator[str]:
        raise NotImplementedError

    def tile(self, layer: str, grid: str, z: int, x: int, y: int, ext: str) -> Tile:
        raise NotImplementedError

    def probe(self) -> dict[str, Any]:
        raise NotImplementedError


def b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode()


def _name(label: str, value: str) -> str:
    if not isinstance(value, str) or not NAME_RE.match(value):
        raise DeployError(f"mapproxy: invalid {label} {value!r}")
    return value


class ScriptGateway(Gateway):
    def __init__(self, mode: str, ssh: Optional[dict[str, Any]]) -> None:
        self.mode, self.ssh = mode, ssh

    def _sh(self, script: str, stdin: str = "", timeout: int = 60) -> str:
        return transport.run(self.mode, self.ssh, PRELUDE + script, root=True, timeout=timeout, input_text=stdin or None)

    # ------------------------------------------------------------------ files
    def read(self) -> dict[str, Optional[str]]:
        out = self._sh(r"""for f in mapproxy.yaml seed.yaml; do
  echo "@@$f"
  if [ -f "$MP_DIR/$f" ]; then head -c {max} "$MP_DIR/$f" | base64 -w0; echo; else echo "-"; fi
done
""".replace("{max}", str(MAX_FILE)))
        return _files(out)

    def read_backup(self, backup_id: str) -> dict[str, Optional[str]]:
        if not BACKUP_RE.match(backup_id or ""):
            raise DeployError("mapproxy: invalid backup id")
        out = self._sh(rf"""B="$BK/{backup_id}"
[ -d "$B" ] || {{ echo "@@missing"; exit 0; }}
for f in mapproxy.yaml seed.yaml; do
  echo "@@$f"
  if [ -f "$B/$f" ]; then head -c {MAX_FILE} "$B/$f" | base64 -w0; echo; else echo "-"; fi
done
""")
        if "@@missing" in out:
            raise DeployError("mapproxy: no such backup")
        return _files(out)

    def backups(self) -> list[dict[str, Any]]:
        out = self._sh(r"""[ -d "$BK" ] || exit 0
for d in "$BK"/*/; do
  [ -d "$d" ] || continue
  id="$(basename "$d")"
  echo "B $id $(stat -c %Y "$d" 2>/dev/null || echo 0) $(du -sb "$d" 2>/dev/null | cut -f1)"
done
""")
        res = []
        for line in out.splitlines():
            p = line.split()
            if len(p) == 4 and p[0] == "B" and BACKUP_RE.match(p[1]):
                res.append({"id": p[1], "created": int(p[2]), "bytes": int(p[3] or 0)})
        return sorted(res, key=lambda b: str(b["id"]), reverse=True)

    def apply(self, config: str, seed: Optional[str], dry_run: bool) -> Check:
        """Validate the candidate files with MapProxy itself; unless dry_run, back the current ones up and install the new ones atomically."""
        if len(config) > MAX_FILE or (seed is not None and len(seed) > MAX_FILE):
            raise DeployError("mapproxy: configuration too large")
        stdin = b64(config) + "\n" + (b64(seed) if seed is not None else "-") + "\n"
        script = f"""DRY={'1' if dry_run else '0'}
IFS= read -r CB; IFS= read -r SB
fail() {{ echo "RESULT {{\\"ok\\": false, \\"errors\\": [\\"$1\\"], \\"warnings\\": []}}"; exit 0; }}
[ -x "$VENV/bin/python" ] || fail "MapProxy is not installed in the virtualenv"
t="$(mktemp -d)" || exit 1; trap 'rm -rf "$t"' EXIT; chmod 755 "$t"
printf %s "$CB" | base64 -d > "$t/mapproxy.yaml" || fail "bad payload"
SEEDARG=""
if [ "$SB" != "-" ]; then printf %s "$SB" | base64 -d > "$t/seed.yaml" || fail "bad payload"; SEEDARG="$t/seed.yaml"; fi
cat > "$t/validate.py" <<'PY'
{VALIDATOR}
PY
chmod 644 "$t"/*
cd /tmp
$RUNAS "$VENV/bin/python" "$t/validate.py" "$t/mapproxy.yaml" $SEEDARG 2>"$t/err" | grep '^RESULT ' > "$t/res"
[ -s "$t/res" ] || fail "validator crashed: $(head -c 200 "$t/err" | tr -d '"\\\\' | tr '\\n' ' ')"
cat "$t/res"
if [ "$DRY" = 1 ] || ! grep -q '"ok": true' "$t/res"; then exit 0; fi
id="$(date -u +%Y%m%d-%H%M%S)-$(head -c2 /dev/urandom | od -An -tx1 | tr -d ' \\n')"
install -d -m 700 "$BK/$id"
for f in mapproxy.yaml seed.yaml; do if [ -f "$MP_DIR/$f" ]; then cp -p "$MP_DIR/$f" "$BK/$id/$f"; fi; done
install -d "$MP_DIR"
install -m 644 "$t/mapproxy.yaml" "$MP_DIR/.mapproxy.yaml.new" && mv -f "$MP_DIR/.mapproxy.yaml.new" "$MP_DIR/mapproxy.yaml"
if [ -n "$SEEDARG" ]; then install -m 644 "$t/seed.yaml" "$MP_DIR/.seed.yaml.new" && mv -f "$MP_DIR/.seed.yaml.new" "$MP_DIR/seed.yaml"; fi
echo "BACKUP $id"
ls -1 "$BK" 2>/dev/null | sort | head -n -30 | while read -r old; do rm -rf -- "${{BK:?}}/$old"; done
"""
        return _check(self._sh(script, stdin=stdin, timeout=90))

    # ------------------------------------------------------------------ cache
    def stats(self) -> dict[str, Any]:
        out = self._sh(r"""cd /
[ -d "$CACHE" ] || { echo "NOCACHE"; exit 0; }
echo "D $(df -Pk "$CACHE" 2>/dev/null | awk 'NR==2 {print $2, $4}')"
for e in "$CACHE"/*; do
  [ -e "$e" ] || continue
  n="$(basename "$e")"
  case "$n" in locks|tile_locks) continue;; esac
  if [ -d "$e" ] && [ ! -L "$e" ]; then
    b="$(timeout 25 du -sb "$e" 2>/dev/null)"; rc=$?; b="${b%%[[:space:]]*}"
    t="$( set -o pipefail; timeout 25 find "$e" -type f 2>/dev/null | wc -l )"; rc2=$?
    p=0; [ "$rc" -ne 0 ] || [ "$rc2" -ne 0 ] && p=1
    echo "E $n dir ${b:-0} ${t:-0} $p"
    for z in "$e"/*/; do
      [ -d "$z" ] || continue
      zn="$(basename "$z")"; case "$zn" in ''|*[!0-9]*) continue;; esac
      [ "$SECONDS" -gt 40 ] && { echo "E_PARTIAL $n"; break; }
      c="$(timeout 10 find "$z" -type f 2>/dev/null | wc -l)"
      echo "L $n $zn $c"
    done
  elif [ -f "$e" ] && [ ! -L "$e" ]; then
    echo "E $n file $(stat -c %s "$e" 2>/dev/null || echo 0) - 0"
  fi
done
""", timeout=120)
        return parse_stats(out)

    def cleanup(self, names: list[str], older_than_days: Optional[int], dry_run: bool) -> list[dict[str, Any]]:
        for n in names:
            _name("cache entry", n)
            if n in ("locks", "tile_locks"):
                raise DeployError("mapproxy: refusing to touch lock directories")
        days = "" if older_than_days is None else str(int(older_than_days))
        if days and not (1 <= int(days) <= 36500):
            raise DeployError("mapproxy: days must be 1..36500")
        entries = " ".join(shlex.quote(n) for n in names)
        out = self._sh(f"""DRY={'1' if dry_run else '0'}; DAYS={shlex.quote(days)}
if ! command -v pgrep >/dev/null 2>&1; then
  [ "$DRY" = 1 ] || {{ echo "NOPGREP"; exit 0; }}
elif pgrep -f '[m]approxy-seed' >/dev/null 2>&1; then echo "BUSY"; exit 0; fi
real="$(readlink -f "$CACHE")"
for n in {entries}; do
  e="$CACHE/$n"
  if [ ! -e "$e" ] || [ -L "$e" ]; then echo "X $n missing"; continue; fi
  [ "$(dirname "$(readlink -f "$e")")" = "$real" ] || {{ echo "X $n outside"; continue; }}
  if [ -d "$e" ]; then
    if [ -n "$DAYS" ]; then
      f="$(find "$e" -type f -mtime +"$DAYS" 2>/dev/null | wc -l)"; b="$(find "$e" -type f -mtime +"$DAYS" -printf '%s\\n' 2>/dev/null | awk '{{s+=$1}} END {{print s+0}}')"
      [ "$DRY" = 0 ] && {{ find "$e" -type f -mtime +"$DAYS" -delete 2>/dev/null; find "$e" -mindepth 1 -type d -empty -delete 2>/dev/null; }}
    else
      f="$(find "$e" -type f 2>/dev/null | wc -l)"; b="$(du -sb "$e" 2>/dev/null | cut -f1)"
      [ "$DRY" = 0 ] && rm -rf --one-file-system -- "$e"
    fi
  else
    if [ -n "$DAYS" ]; then echo "X $n file-needs-full"; continue; fi
    f=1; b="$(stat -c %s "$e")"
    [ "$DRY" = 0 ] && rm -f -- "$e"
  fi
  echo "C $n ${{f:-0}} ${{b:-0}}"
done
""", timeout=600)
        if out.strip() == "NOPGREP":
            raise DeployError("mapproxy: pgrep (package procps) is required to check that no seeding is running; install it first")
        if out.strip() == "BUSY":
            raise DeployError("mapproxy: seeding is running - stop it before cleaning the cache")
        res = []
        for line in out.splitlines():
            p = line.split()
            if p[:1] == ["C"] and len(p) == 4:
                res.append({"entry": p[1], "files": int(p[2]), "bytes": int(p[3])})
            elif p[:1] == ["X"]:
                res.append({"entry": p[1], "skipped": p[2]})
        return res

    # ------------------------------------------------------------------ seeding
    def seed(self, seed: str, concurrency: int, dry_run: bool, stop: Optional[threading.Event]) -> Iterator[str]:
        if not 1 <= int(concurrency) <= 4:
            raise DeployError("mapproxy: concurrency must be 1..4")
        flags = "--dry-run" if dry_run else ""
        script = PRELUDE + f"""t="$(mktemp -d)" || exit 1; trap 'rm -rf "$t"' EXIT; chmod 755 "$t"
printf %s {shlex.quote(b64(seed))} | base64 -d > "$t/seed.yaml"; chmod 644 "$t/seed.yaml"
[ -x "$VENV/bin/mapproxy"-seed ] || {{ echo "MapProxy is not installed in the virtualenv" >&2; exit 4; }}
cd /tmp
$RUNAS nice -n 10 "$VENV/bin/mapproxy-seed" -f "$MP_DIR/mapproxy.yaml" -s "$t/seed.yaml" -c {int(concurrency)} {flags} 2>&1
"""
        yield from transport.stream(self.mode, self.ssh, script, root=True, stop=stop, deadline=SEED_DEADLINE)

    # ------------------------------------------------------------------ tiles and health
    def tile(self, layer: str, grid: str, z: int, x: int, y: int, ext: str) -> Tile:
        _name("layer", layer)
        _name("grid", grid)
        if ext not in ("png", "jpeg"):
            raise DeployError("mapproxy: tile format must be png or jpeg")
        z, x, y = int(z), int(x), int(y)
        out = self._sh(f"""t="$(mktemp -d)" || exit 1; trap 'rm -rf "$t"' EXIT
code="$(curl -sS -m 30 --max-filesize 2097152 -o "$t/b" -D "$t/h" -w '%{{http_code}}' "http://127.0.0.1:$PORT/wmts/{layer}/{grid}/{z}/{x}/{y}.{ext}" 2>"$t/e")"; rc=$?
echo "RC $rc"; echo "STATUS $code"
sed -n 's/\\r$//; s/^[Cc]ontent-[Tt]ype: \\(.*\\)$/CT \\1/p' "$t/h" | head -1
echo "BODY $(base64 -w0 "$t/b" 2>/dev/null)"
""", timeout=45)
        return parse_tile(out)

    def probe(self) -> dict[str, Any]:
        out = self._sh(r"""cd /tmp
u="${GS_MP_UNIT:-gisstack-mapproxy.service}"
if command -v systemctl >/dev/null 2>&1; then echo "UNIT $(systemctl is-active "$u" 2>/dev/null | head -1)"; else echo "UNIT unknown"; fi
t="$(mktemp -d)" || exit 1; trap 'rm -rf "$t"' EXIT
code="$(curl -sS -m 15 --max-filesize 2097152 -o "$t/b" -w '%{http_code}' "http://127.0.0.1:$PORT/wmts/1.0.0/WMTSCapabilities.xml" 2>/dev/null)"; echo "HTTP ${code:-000}"
echo "CAPS $(base64 -w0 "$t/b" 2>/dev/null)"
if command -v pgrep >/dev/null 2>&1; then echo "SEEDING $(pgrep -f '[m]approxy-seed' 2>/dev/null | wc -l)"; else echo "SEEDING unknown"; fi
if [ -d "$CACHE" ]; then
  if [ -z "$RUNAS" ]; then w=$([ -w "$CACHE" ] && echo yes || echo no); else w=$($RUNAS test -w "$CACHE" && echo yes || echo no); fi
  echo "WRITABLE $w"; echo "DISK $(df -Pk "$CACHE" 2>/dev/null | awk 'NR==2 {print $2, $4}')"
else echo "WRITABLE missing"; fi
[ -x "$VENV/bin/mapproxy"-seed ] && echo "INSTALLED yes" || echo "INSTALLED no"
""", timeout=60)
        return parse_probe(out)


# --------------------------------------------------------------------------- Compose / Kubernetes
# Inside the MapProxy container (python:3.12-slim image of the generated bundles): MapProxy and its tools are in /usr/local, the configuration is mounted at
# /etc/mapproxy and the tile cache lives on the volume at /var/lib/mapproxy; the container's own user (mapproxy) runs everything, curl and pgrep are not there.
INNER_PRELUDE = r"""export GS_MP_DIR=/etc/mapproxy GS_MP_CACHE=/var/lib/mapproxy/cache_data GS_MP_BACKUPS=/tmp/gisstack-unused GS_MP_VENV=/usr/local GS_MP_PORT=8081 GS_MP_RUNAS=
pgrep() {   # minimal pgrep -f for images without procps: matches the full command lines in /proc, never this shell
  local pat="${@: -1}" p rc=1
  for p in /proc/[0-9]*; do
    [ "${p#/proc/}" = "$$" ] || [ "${p#/proc/}" = "$BASHPID" ] && continue      # never this shell or its command-substitution subshell (same command line)
    if tr '\0' ' ' < "$p/cmdline" 2>/dev/null | grep -q -- "$pat"; then echo "${p#/proc/}"; rc=0; fi
  done
  return $rc
}
"""
HTTP_PY = r"""import base64, sys, urllib.request, urllib.error
url, maxb = sys.argv[1], int(sys.argv[2])
rc, status, ct, body = 0, 0, "", b""
try:
    with urllib.request.urlopen(url, timeout=30) as r:
        status, ct, body = r.status, r.headers.get("Content-Type", ""), r.read(maxb + 1)
except urllib.error.HTTPError as e:
    status, ct, body = e.code, e.headers.get("Content-Type", ""), e.read(maxb + 1)
except Exception as e:
    m = str(e).lower()
    rc = 7 if "refused" in m else 28 if "timed out" in m else 1
print("RC", rc)
print("STATUS", status)
print("CT", ct)
print("BODY", base64.b64encode(body[:maxb]).decode())
"""


class PlaceGateway(ScriptGateway):
    """MapProxy in a Compose container or a Kubernetes pod.

    Everything that touches MapProxy, its cache or its processes (validation, seeding, cache statistics and cleanup, tile preview, health) runs INSIDE the
    container through `gs_in` (see targets.Place).  The configuration is different: in Compose it is a read-only bind mount of `<project dir>/mapproxy`,
    so edits are written to that host directory (backups next to it) and MapProxy's own reloader picks them up; in Kubernetes it is a ConfigMap owned by the
    generated manifests, so it can be read but not edited from here."""

    def __init__(self, place: targets.Place, mode: str, ssh: Optional[dict[str, Any]]) -> None:
        super().__init__(mode, ssh)
        self.place = place

    # -- running things
    def _wrap(self, script: str) -> str:
        return self.place.prelude() + "gs_in '' bash -c " + shlex.quote(INNER_PRELUDE + PRELUDE + script)

    def _sh(self, script: str, stdin: str = "", timeout: int = 60) -> str:
        return transport.run(self.mode, self.ssh, self._wrap(script), root=False, timeout=timeout, input_text=stdin or None)

    def _host(self, script: str, stdin: str = "", timeout: int = 60) -> str:
        """A script on the machine that runs docker (Compose only): the project directory is in $WD."""
        pre = self.place.prelude() + 'WD="$(' + shlex.quote(self.place.runtime) + ' inspect -f \'{{ index .Config.Labels "com.docker.compose.project.working_dir" }}\' "$GS_CTR")"\n' \
              '[ -d "$WD/mapproxy" ] || { echo "the project directory $WD has no mapproxy/ folder" >&2; exit 97; }\nMP_DIR="$WD/mapproxy"; BK="$WD/mapproxy-backups"\n'
        return transport.run(self.mode, self.ssh, pre + script, root=False, timeout=timeout, input_text=stdin or None)

    # -- configuration
    def read_backup(self, backup_id: str) -> dict[str, Optional[str]]:
        if self.place.kind != "compose":
            raise DeployError("mapproxy: configuration backups exist for Compose only (Kubernetes keeps the configuration in a ConfigMap)")
        if not BACKUP_RE.match(backup_id or ""):
            raise DeployError("mapproxy: invalid backup id")
        out = self._host(rf"""B="$BK/{backup_id}"
[ -d "$B" ] || {{ echo "@@missing"; exit 0; }}
for f in mapproxy.yaml seed.yaml; do
  echo "@@$f"
  if [ -f "$B/$f" ]; then head -c {MAX_FILE} "$B/$f" | base64 -w0; echo; else echo "-"; fi
done
""")
        if "@@missing" in out:
            raise DeployError("mapproxy: no such backup")
        return _files(out)

    def backups(self) -> list[dict[str, Any]]:
        if self.place.kind != "compose":
            return []
        out = self._host(r"""[ -d "$BK" ] || exit 0
for d in "$BK"/*/; do
  [ -d "$d" ] || continue
  id="$(basename "$d")"
  echo "B $id $(stat -c %Y "$d" 2>/dev/null || echo 0) $(du -sb "$d" 2>/dev/null | cut -f1)"
done
""")
        res = []
        for line in out.splitlines():
            p = line.split()
            if len(p) == 4 and p[0] == "B" and BACKUP_RE.match(p[1]):
                res.append({"id": p[1], "created": int(p[2]), "bytes": int(p[3] or 0)})
        return sorted(res, key=lambda b: str(b["id"]), reverse=True)

    def apply(self, config: str, seed: Optional[str], dry_run: bool) -> Check:
        if len(config) > MAX_FILE or (seed is not None and len(seed) > MAX_FILE):
            raise DeployError("mapproxy: configuration too large")
        stdin = b64(config) + "\n" + (b64(seed) if seed is not None else "-") + "\n"
        checked = _check(self._sh(_validate_script(True), stdin=stdin, timeout=90))          # MapProxy itself, inside the container, strictly
        if dry_run or not checked.ok:
            return checked
        if self.place.kind != "compose":
            raise DeployError("mapproxy: on Kubernetes the configuration is a ConfigMap generated from the project - change the project configuration and re-apply it")
        out = self._host(r"""IFS= read -r CB; IFS= read -r SB
t="$(mktemp -d)" || exit 1; trap 'rm -rf "$t"' EXIT
printf %s "$CB" | base64 -d > "$t/mapproxy.yaml" || { echo "BAD"; exit 0; }
SEEDARG=""; [ "$SB" != "-" ] && { printf %s "$SB" | base64 -d > "$t/seed.yaml" || { echo "BAD"; exit 0; }; SEEDARG=1; }
id="$(date -u +%Y%m%d-%H%M%S)-$(head -c2 /dev/urandom | od -An -tx1 | tr -d ' \n')"
install -d -m 700 "$BK/$id"
for f in mapproxy.yaml seed.yaml; do if [ -f "$MP_DIR/$f" ]; then cp -p "$MP_DIR/$f" "$BK/$id/$f"; fi; done
install -m 644 "$t/mapproxy.yaml" "$MP_DIR/.mapproxy.yaml.new" && mv -f "$MP_DIR/.mapproxy.yaml.new" "$MP_DIR/mapproxy.yaml"
if [ -n "$SEEDARG" ]; then install -m 644 "$t/seed.yaml" "$MP_DIR/.seed.yaml.new" && mv -f "$MP_DIR/.seed.yaml.new" "$MP_DIR/seed.yaml"; fi
echo "BACKUP $id"
ls -1 "$BK" 2>/dev/null | sort | head -n -30 | while read -r old; do rm -rf -- "${BK:?}/$old"; done
""", stdin=stdin, timeout=60)
        if "BACKUP " not in out:
            raise DeployError("mapproxy: the configuration could not be written to the project directory")
        checked.backup = next((ln[7:].strip() for ln in out.splitlines() if ln.startswith("BACKUP ")), "")
        return checked

    # -- seeding: the seeder runs in the container, and a cancelled run must be killed THERE (killing the docker/kubectl client does not)
    def seed(self, seed: str, concurrency: int, dry_run: bool, stop: Optional[threading.Event]) -> Iterator[str]:
        if not 1 <= int(concurrency) <= 4:
            raise DeployError("mapproxy: concurrency must be 1..4")
        flags = "--dry-run" if dry_run else ""
        inner = INNER_PRELUDE + PRELUDE + f"""t="$(mktemp -d)" || exit 1; trap 'rm -rf "$t"' EXIT
printf %s {shlex.quote(b64(seed))} | base64 -d > "$t/seed.yaml"
[ -x "$VENV/bin/mapproxy"-seed ] || {{ echo "MapProxy is not installed in the container" >&2; exit 4; }}
cd /tmp
nice -n 10 "$VENV/bin/mapproxy-seed" -f "$MP_DIR/mapproxy.yaml" -s "$t/seed.yaml" -c {int(concurrency)} {flags} 2>&1
"""
        script = self.place.prelude() + "gs_in '' bash -c " + shlex.quote(inner)
        try:
            yield from transport.stream(self.mode, self.ssh, script, root=False, stop=stop, deadline=SEED_DEADLINE)
        finally:
            if stop is not None and stop.is_set():
                with contextlib.suppress(DeployError):
                    self._sh(r"""for p in $(pgrep -f '[m]approxy-seed -f' 2>/dev/null); do kill "$p" 2>/dev/null; done
for _ in 1 2 3 4 5; do pgrep -f '[m]approxy-seed -f' >/dev/null 2>&1 || exit 0; sleep 1; done
for p in $(pgrep -f '[m]approxy-seed -f' 2>/dev/null); do kill -9 "$p" 2>/dev/null; done; exit 0""", timeout=30)

    # -- HTTP checks without curl
    def tile(self, layer: str, grid: str, z: int, x: int, y: int, ext: str) -> Tile:
        _name("layer", layer)
        _name("grid", grid)
        if ext not in ("png", "jpeg"):
            raise DeployError("mapproxy: tile format must be png or jpeg")
        z, x, y = int(z), int(x), int(y)
        url = f"http://127.0.0.1:8081/wmts/{layer}/{grid}/{z}/{x}/{y}.{ext}"
        return parse_tile(self._sh(f"""python - {shlex.quote(url)} 2097152 <<'PY'\n{HTTP_PY}PY\n""", timeout=45))

    def probe(self) -> dict[str, Any]:
        out = self._sh(rf"""cd /tmp
echo "UNIT unknown"
python - "http://127.0.0.1:$PORT/wmts/1.0.0/WMTSCapabilities.xml" 2097152 > /tmp/.gs-probe 2>/dev/null <<'PY'
{HTTP_PY}PY
echo "HTTP $(sed -n 's/^STATUS //p' /tmp/.gs-probe)"; echo "CAPS $(sed -n 's/^BODY //p' /tmp/.gs-probe)"; rm -f /tmp/.gs-probe
echo "SEEDING $(pgrep -f '[m]approxy-seed' 2>/dev/null | wc -l)"
if [ -d "$CACHE" ]; then
  w=$([ -w "$CACHE" ] && echo yes || echo no); echo "WRITABLE $w"; echo "DISK $(df -Pk "$CACHE" 2>/dev/null | awk 'NR==2 {{print $2, $4}}')"
else echo "WRITABLE missing"; fi
[ -x "$VENV/bin/mapproxy"-seed ] && echo "INSTALLED yes" || echo "INSTALLED no"
""", timeout=60)
        return parse_probe(out)


def _validate_script(dry: bool) -> str:
    """The validation half of ScriptGateway.apply (reads two base64 lines on stdin; runs MapProxy's own strict loader)."""
    return f"""DRY={'1' if dry else '0'}
IFS= read -r CB; IFS= read -r SB
fail() {{ echo "RESULT {{\\"ok\\": false, \\"errors\\": [\\"$1\\"], \\"warnings\\": []}}"; exit 0; }}
[ -x "$VENV/bin/python" ] || fail "MapProxy is not installed in the container"
t="$(mktemp -d)" || exit 1; trap 'rm -rf "$t"' EXIT
printf %s "$CB" | base64 -d > "$t/mapproxy.yaml" || fail "bad payload"
SEEDARG=""
if [ "$SB" != "-" ]; then printf %s "$SB" | base64 -d > "$t/seed.yaml" || fail "bad payload"; SEEDARG="$t/seed.yaml"; fi
cat > "$t/validate.py" <<'PY'
{VALIDATOR}
PY
cd /tmp
"$VENV/bin/python" "$t/validate.py" "$t/mapproxy.yaml" $SEEDARG 2>"$t/err" | grep '^RESULT ' > "$t/res"
[ -s "$t/res" ] || fail "validator crashed: $(head -c 200 "$t/err" | tr -d '"\\\\' | tr '\\n' ' ')"
cat "$t/res"
"""


# --------------------------------------------------------------------------- parsers
def parse_tile(out: str) -> Tile:
    rc, status, ctype, body = 0, 0, "", b""
    for line in out.splitlines():
        if line.startswith("RC "):
            rc = int(line[3:] or 0)
        elif line.startswith("STATUS "):
            status = int(line[7:]) if line[7:].strip().isdigit() else 0
        elif line.startswith("CT "):
            ctype = line[3:].split(";")[0].strip().lower()
        elif line.startswith("BODY "):
            try:
                body = base64.b64decode(line[5:], validate=False)
            except ValueError:
                body = b""
    if rc != 0 or status == 0:
        raise DeployError({7: "mapproxy: connection refused (is the service running?)", 28: "mapproxy: tile request timed out"}.get(rc, f"mapproxy: tile request failed (curl {rc})"))
    return Tile(status, ctype, body)


def parse_probe(out: str) -> dict[str, Any]:
    res: dict[str, Any] = {"unit": "unknown", "http": 0, "caps": b"", "seeding": 0, "writable": "unknown", "disk_total_kb": None, "disk_free_kb": None, "installed": False, "seeding_known": True}
    for line in out.splitlines():
        k, _, v = line.partition(" ")
        if k == "UNIT":
            res["unit"] = v.strip() or "unknown"
        elif k == "HTTP":
            res["http"] = int(v) if v.strip().isdigit() else 0
        elif k == "CAPS":
            with contextlib.suppress(ValueError):
                res["caps"] = base64.b64decode(v, validate=False)
        elif k == "SEEDING":
            res["seeding"] = int(v) if v.strip().isdigit() else 0
            res["seeding_known"] = v.strip().isdigit()
        elif k == "WRITABLE":
            res["writable"] = v.strip()
        elif k == "DISK":
            p = v.split()
            if len(p) == 2 and all(x.isdigit() for x in p):
                res["disk_total_kb"], res["disk_free_kb"] = int(p[0]), int(p[1])
        elif k == "INSTALLED":
            res["installed"] = v.strip() == "yes"
    return res


def _files(out: str) -> dict[str, Optional[str]]:
    files: dict[str, Optional[str]] = {}
    name = None
    for line in out.splitlines():
        if line.startswith("@@"):
            name = line[2:].strip()
            files[name] = None
        elif name is not None and files.get(name) is None and line.strip() and line.strip() != "-":
            try:
                files[name] = base64.b64decode(line.strip(), validate=True).decode("utf-8")
            except (ValueError, UnicodeDecodeError) as e:
                raise DeployError(f"mapproxy: {name} is not readable text") from e
    return files


def _check(out: str) -> Check:
    import json
    res = Check(False, ["no answer from the validator"])
    for line in out.splitlines():
        if line.startswith("RESULT "):
            try:
                d = json.loads(line[7:])
                res = Check(bool(d.get("ok")), [str(x) for x in d.get("errors", [])], [str(x) for x in d.get("warnings", [])])
            except ValueError:
                pass
        elif line.startswith("BACKUP "):
            res.backup = line[7:].strip()
    return res


def parse_stats(out: str) -> dict[str, Any]:
    res: dict[str, Any] = {"available": True, "disk_total_kb": None, "disk_free_kb": None, "entries": {}}
    if out.strip().startswith("NOCACHE"):
        res["available"] = False
        return res
    for line in out.splitlines():
        p = line.split()
        if not p:
            continue
        if p[0] == "D" and len(p) == 3 and p[1].isdigit() and p[2].isdigit():
            res["disk_total_kb"], res["disk_free_kb"] = int(p[1]), int(p[2])
        elif p[0] == "E" and len(p) == 6:
            res["entries"][p[1]] = {"name": p[1], "kind": p[2], "bytes": int(p[3]) if p[3].isdigit() else 0,
                                    "tiles": int(p[4]) if p[4].isdigit() else None, "partial": p[5] == "1", "levels": {}}
        elif p[0] == "L" and len(p) == 4 and p[1] in res["entries"] and p[2].isdigit() and p[3].isdigit():
            res["entries"][p[1]]["levels"][int(p[2])] = int(p[3])
        elif p[0] == "E_PARTIAL" and len(p) == 2 and p[1] in res["entries"]:
            res["entries"][p[1]]["partial"] = True
    return res
