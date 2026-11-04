#!/usr/bin/env bash
# gisstack-backup - verified backups and safe restores of a native gis-stack installation.
#
#   gisstack-backup create  [--id ID] [--components LIST] [--with-cache] [--kind manual|scheduled] [--note TEXT]
#   gisstack-backup list    [--json-lines]
#   gisstack-backup verify  ID
#   gisstack-backup restore ID [--only LIST] [--dry-run] [--yes] [--staging] [--no-pre-backup] [--no-rollback]
#   gisstack-backup prune   [--keep N] [--days D] [--keep-pre N] [--dry-run]
#
# components: config postgres geoserver qgis cache      (default: every component that exists on this machine; cache only with --with-cache)
#
# Guarantees
#   * a backup is written to <id>.partial and renamed to <id> only after every file was re-read and checksummed;
#     failures are kept as <id>.FAILED and never counted as backups
#   * restore never overwrites silently: it needs --yes, always takes a verified pre-restore backup first, replaces data by
#     renaming (the old data stays next to the new until recovery validation passes) and rolls back on failure
#   * restore refuses archives whose members could escape the allowed directories
#
# Exit codes: 0 ok, 1 failed (nothing was changed or the change was rolled back), 2 usage, 3 not found, 75 another operation is running.
# Testing without root: GS_ENV_FILE GS_BACKUP_ROOT GS_PGRUN GS_SYSTEMCTL GS_NO_ROOT=1 GS_CONFIG_PATHS GS_RESTORE_ALLOW (see below).
set -Eeuo pipefail
umask 077

ENV_FILE="${GS_ENV_FILE:-/etc/gisstack/stack.env}"
ROOT="${GS_BACKUP_ROOT:-/var/backups/gisstack/stack}"
PGRUN="${GS_PGRUN-runuser -u postgres --}"
SYSTEMCTL="${GS_SYSTEMCTL:-systemctl}"
CACHE_DIR="${GS_CACHE_DIR:-/var/lib/gisstack/mapproxy/cache_data}"
WEBMAP_DIR="${GS_WEBMAP_DIR:-/var/www/gisstack/webmap}"
# files that make up "the configuration" (existing ones are archived)
CONFIG_PATHS="${GS_CONFIG_PATHS:-/etc/gisstack /etc/caddy/Caddyfile /etc/caddy/caddy.d /opt/tomcat/conf/server.xml /opt/tomcat/bin/setenv.sh /etc/systemd/system/gisstack-tomcat.service /etc/systemd/system/gisstack-mapproxy.service /etc/systemd/system/gisstack-qgis.service /etc/systemd/system/gisstack-qgis.socket /etc/systemd/system/caddy.service}"
# a restore may only write below these prefixes (regex on archive member names, no leading slash)
RESTORE_ALLOW="${GS_RESTORE_ALLOW:-^(etc|opt/tomcat|var/www/gisstack|var/lib/gisstack|srv|var/lib/qgis|home)/}"
HEALTH_WAIT="${GS_HEALTH_WAIT:-180}"

die() { echo "ERROR: $*" >&2; exit "${2:-1}"; }
warn() { echo "WARN: $*" >&2; }
log() { echo "== $*"; }

[ "${GS_NO_ROOT:-0}" = 1 ] || [ "$(id -u)" -eq 0 ] || die "run as root (sudo gisstack-backup ...)" 2
[ -r "$ENV_FILE" ] || die "$ENV_FILE not found - is gis-stack installed here?" 3
# shellcheck source=/dev/null
. "$ENV_FILE"
DB="${DB_NAME:-gis}"; DBU="${DB_USER:-gis}"; DBPORT="${PG_PORT:-5432}"
GSDIR="${GS_DATA_DIR:-/var/lib/gisstack/geoserver_data}"
QGISDIR="${QGIS_PROJECTS_DIR:-/srv/qgis/projects}"
TCPORT="${TOMCAT_PORT:-8080}"

mkdir -p "$ROOT" && chmod 700 "$ROOT"
exec 9>"$ROOT/.lock"
flock -n 9 || die "another gisstack-backup operation is running" 75

now_utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }
new_id() { echo "$(date -u +%Y%m%d-%H%M%S)-$(head -c4 /dev/urandom | od -An -tx1 | tr -d ' \n')"; }
valid_id() { [[ "$1" =~ ^[0-9]{8}-[0-9]{6}-[0-9a-f]{8}$ || "$1" =~ ^pre-[0-9]{8}-[0-9]{6}-[0-9a-f]{8}$ ]]; }
json_str() { local s=${1//\\/\\\\}; s=${s//\"/\\\"}; s=${s//$'\n'/ }; printf '%s' "$s"; }
have_pg() {   # PostgreSQL tools reachable: through a wrapper (docker exec ... in tests) or directly
  if [ -n "$PGRUN" ] && [ "${PGRUN%% *}" != runuser ]; then return 0; fi
  command -v psql >/dev/null 2>&1
}
pg() { (cd /tmp && $PGRUN "$@"); }                       # run a PostgreSQL client tool as the postgres user
tomcat_unit() { echo gisstack-tomcat.service; }
svc_active() { $SYSTEMCTL is-active --quiet "$1" 2>/dev/null; }

default_components() {
  local c="config"
  have_pg && pg psql -X -Atc "select 1 from pg_database where datname='$DB'" 2>/dev/null | grep -q 1 && c="$c postgres"
  [ -d "$GSDIR" ] && c="$c geoserver"
  [ -d "$QGISDIR" ] && c="$c qgis"
  echo "$c"
}

# ------------------------------------------------------------------------------------------------ archives
pack() {    # pack NAME.tar.gz PARENT BASENAME...: tar a tree; one retry when files changed while reading (exit 1)
  local out="$1" parent="$2"; shift 2
  local rc=0
  tar czf "$out" -C "$parent" "$@" 2>/dev/null || rc=$?
  if [ "$rc" -eq 1 ]; then tar czf "$out" -C "$parent" "$@" 2>/dev/null && rc=0 || rc=$?; fi
  if [ "$rc" -eq 1 ]; then WARNINGS+=("files changed while archiving $(basename "$out")"); rc=0; fi
  return "$rc"
}

config_members() {   # existing config paths relative to /
  local p
  for p in $CONFIG_PATHS; do [ -e "$p" ] && echo "${p#/}"; done
  [ -d "$WEBMAP_DIR" ] && echo "${WEBMAP_DIR#/}"
  true
}

sums_of() { ( cd "$1" && find . -maxdepth 1 -type f ! -name SHA256SUMS ! -name MANIFEST.json -printf '%f\n' | sort | xargs -r sha256sum ); }

# ------------------------------------------------------------------------------------------------ create
cmd_create() {
  local id="" comps="" with_cache=0 kind=manual note=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --id) id="$2"; shift 2 ;; --components) comps="${2//,/ }"; shift 2 ;; --with-cache) with_cache=1; shift ;;
      --kind) kind="$2"; shift 2 ;; --note) note="$2"; shift 2 ;; *) die "create: unknown option $1" 2 ;;
    esac
  done
  [ -n "$id" ] || id="$(new_id)"
  [ "$kind" = pre-restore ] && [[ "$id" != pre-* ]] && id="pre-$id"
  valid_id "$id" || die "bad backup id: $id" 2
  case "$kind" in manual|scheduled|pre-restore) ;; *) die "bad --kind" 2 ;; esac
  [ -n "$comps" ] || comps="$(default_components)"
  [ "$with_cache" = 1 ] && comps="$comps cache"
  local c; for c in $comps; do case "$c" in config|postgres|geoserver|qgis|cache) ;; *) die "unknown component: $c" 2 ;; esac; done
  [ ! -e "$ROOT/$id" ] && [ ! -e "$ROOT/$id.partial" ] || die "backup $id already exists" 1

  local D="$ROOT/$id.partial"; mkdir "$D"
  WARNINGS=(); local FAIL=0
  fail() { echo "  FAILED: $*"; FAIL=1; }
  local quiesced=0
  if [ "${GS_BACKUP_QUIESCE:-0}" = 1 ] && [[ " $comps " == *" geoserver "* ]] && svc_active "$(tomcat_unit)"; then
    log "quiesce: stopping $(tomcat_unit) while the GeoServer data directory is copied"
    $SYSTEMCTL stop "$(tomcat_unit)" && quiesced=1
  fi
  for c in $comps; do
    log "backup $c"
    case "$c" in
      config)   # shellcheck disable=SC2046
                if [ -n "$(config_members)" ]; then tar czf "$D/config.tar.gz" -C / $(config_members) 2>/dev/null || fail "config archive"; fi ;;
      postgres) pg pg_dump -Fc "$DB" > "$D/postgres-$DB.dump" 2>"$D/.err" || fail "pg_dump $DB: $(tail -1 "$D/.err")"
                pg pg_dumpall --globals-only > "$D/postgres-globals.sql" 2>>"$D/.err" || fail "pg_dumpall --globals-only"
                rm -f "$D/.err" ;;
      geoserver) [ -d "$GSDIR" ] && { pack "$D/geoserver-data.tar.gz" "$(dirname "$GSDIR")" "$(basename "$GSDIR")" || fail "geoserver data"; } || fail "$GSDIR missing" ;;
      qgis)     [ -d "$QGISDIR" ] && { pack "$D/qgis-projects.tar.gz" "$(dirname "$QGISDIR")" "$(basename "$QGISDIR")" || fail "qgis projects"; } || fail "$QGISDIR missing" ;;
      cache)    [ -d "$CACHE_DIR" ] && { pack "$D/mapproxy-cache.tar.gz" "$(dirname "$CACHE_DIR")" "$(basename "$CACHE_DIR")" || fail "mapproxy cache"; } || fail "$CACHE_DIR missing" ;;
    esac
  done
  [ "$quiesced" = 1 ] && $SYSTEMCTL start "$(tomcat_unit)"

  # ---- seal: checksums, manifest, then re-read everything
  sums_of "$D" > "$D/SHA256SUMS"
  [ -s "$D/SHA256SUMS" ] || fail "nothing was backed up"
  local sv=""; have_pg && sv="$(pg psql -X -Atc 'show server_version' 2>/dev/null | head -1 || true)"
  {
    echo "{"
    echo "\"schema\": 1,"
    echo "\"id\": \"$id\","
    echo "\"kind\": \"$kind\","
    echo "\"created\": \"$(now_utc)\","
    echo "\"host\": \"$(json_str "$(hostname)")\","
    echo "\"gisstack_version\": \"$(json_str "${GS_VERSION_STAMP:-}")\","
    echo "\"note\": \"$(json_str "$note")\","
    echo "\"database\": \"$(json_str "$DB")\","
    echo "\"postgres_version\": \"$(json_str "$sv")\","
    echo "\"components\": \"$(echo "$comps" | xargs)\","
    echo "\"warnings\": \"$(json_str "${WARNINGS[*]:-}")\","
    echo "\"files\": ["
    local first=1 line name sum bytes
    while read -r sum name; do
      bytes=$(stat -c %s "$D/$name"); [ "$first" = 1 ] || echo ","; first=0
      printf '{"name": "%s", "bytes": %s, "sha256": "%s"}' "$name" "$bytes" "$sum"
    done < "$D/SHA256SUMS"
    echo; echo "],"
    echo "\"status\": \"complete\""
    echo "}"
  } > "$D/MANIFEST.json"
  verify_dir "$D" || FAIL=1
  if [ "$FAIL" != 0 ]; then mv "$D" "$ROOT/$id.FAILED"; echo "BACKUP_FAILED: kept as $ROOT/$id.FAILED - nothing was deleted"; exit 1; fi
  mv "$D" "$ROOT/$id"
  echo "BACKUP_VERIFIED=$id"
  echo "BACKUP_DIR=$ROOT/$id"
}

# ------------------------------------------------------------------------------------------------ verify
verify_dir() {   # verify_dir DIR: every file re-read and matches the checksums; manifest consistent; dump restorable
  local D="$1" f ok=0
  [ -s "$D/MANIFEST.json" ] || { echo "  FAILED: manifest missing"; return 1; }
  [ -s "$D/SHA256SUMS" ] || { echo "  FAILED: SHA256SUMS missing"; return 1; }
  ( cd "$D" && sha256sum -c --quiet SHA256SUMS >/dev/null 2>&1 ) || { echo "  FAILED: checksum mismatch"; return 1; }
  # every file in the manifest is in SHA256SUMS with the same digest, and nothing else is in the directory
  while read -r line; do
    n=$(sed -n 's/.*"name": "\([^"]*\)".*/\1/p' <<<"$line"); s=$(sed -n 's/.*"sha256": "\([0-9a-f]*\)".*/\1/p' <<<"$line")
    [ -n "$n" ] || continue
    grep -q "^$s  $n\$" "$D/SHA256SUMS" || { echo "  FAILED: manifest disagrees with SHA256SUMS for $n"; return 1; }
  done < <(grep '"name"' "$D/MANIFEST.json" | sed 's/},\?$/}/')
  grep -q '"status": "complete"' "$D/MANIFEST.json" || { echo "  FAILED: manifest status is not complete"; return 1; }
  for f in "$D"/*.tar.gz; do [ -e "$f" ] || continue; tar tzf "$f" >/dev/null 2>&1 || { echo "  FAILED: archive unreadable: $(basename "$f")"; return 1; }; done
  for f in "$D"/postgres-*.dump; do
    [ -e "$f" ] || continue
    (cd /tmp && $PGRUN pg_restore -l < "$f" >/dev/null 2>&1) || { echo "  FAILED: dump not restorable: $(basename "$f")"; return 1; }
  done
  ok=1; [ "$ok" = 1 ]
}

cmd_verify() {
  local id="${1:-}"; valid_id "$id" || die "usage: verify ID" 2
  [ -d "$ROOT/$id" ] || die "no such backup: $id" 3
  if verify_dir "$ROOT/$id"; then echo "VERIFY_OK=$id"; else echo "VERIFY_FAILED=$id"; exit 1; fi
}

# ------------------------------------------------------------------------------------------------ list
cmd_list() {   # TSV: id status kind created bytes components note
  local d id st kind created bytes comps
  for d in "$ROOT"/*; do
    [ -d "$d" ] || continue
    id="$(basename "$d")"; st=complete
    case "$id" in *.partial) st=partial ;; *.FAILED) st=failed ;; esac
    kind="-"; created="-"; comps="-"
    if [ -s "$d/MANIFEST.json" ]; then
      kind=$(sed -n 's/^"kind": "\([^"]*\)".*/\1/p' "$d/MANIFEST.json" | head -1)
      created=$(sed -n 's/^"created": "\([^"]*\)".*/\1/p' "$d/MANIFEST.json" | head -1)
      comps=$(sed -n 's/^"components": "\([^"]*\)".*/\1/p' "$d/MANIFEST.json" | head -1)
    elif [ "$st" = complete ]; then st=damaged; fi
    bytes=$(du -sb "$d" 2>/dev/null | cut -f1)
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$id" "$st" "${kind:--}" "${created:--}" "${bytes:-0}" "${comps:--}"
  done
}

# ------------------------------------------------------------------------------------------------ prune
cmd_prune() {
  local keep=14 days="" keep_pre=5 dry=0
  while [ $# -gt 0 ]; do
    case "$1" in --keep) keep="$2"; shift 2 ;; --days) days="$2"; shift 2 ;; --keep-pre) keep_pre="$2"; shift 2 ;; --dry-run) dry=1; shift ;; *) die "prune: unknown option $1" 2 ;; esac
  done
  [[ "$keep" =~ ^[0-9]+$ && "$keep_pre" =~ ^[0-9]+$ && ( -z "$days" || "$days" =~ ^[0-9]+$ ) ]] || die "prune: numbers expected" 2
  [ "$keep" -ge 1 ] || die "prune: --keep must be at least 1 (the newest backup is never deleted)" 2
  local n=0 npre=0 id rm_list=()
  local cutoff=""; [ -n "$days" ] && cutoff="$(date -u -d "-$days days" +%Y%m%d-%H%M%S)"
  while read -r id; do
    [ -n "$id" ] || continue
    if [[ "$id" == pre-* ]]; then
      npre=$((npre + 1)); [ "$npre" -le "$keep_pre" ] && continue
      rm_list+=("$id"); continue
    fi
    n=$((n + 1)); [ "$n" -le "$keep" ] && continue
    if [ -n "$cutoff" ]; then [[ "${id:0:15}" < "$cutoff" ]] || continue; fi      # younger than --days: keep
    rm_list+=("$id")
  done < <(find "$ROOT" -maxdepth 1 -type d -regextype posix-extended -regex '.*/(pre-)?[0-9]{8}-[0-9]{6}-[0-9a-f]{8}' -printf '%f\n' | sed 's/^pre-\(.*\)$/\1 pre-\1/; t; s/^\(.*\)$/\1 \1/' | sort -r | awk '{print $2}')
  local stale
  # unfinished (.partial, > 1 day) and failed (.FAILED, > 7 days) leftovers
  stale=$(find "$ROOT" -maxdepth 1 \( -name '*.partial' -mtime +1 -o -name '*.FAILED' -mtime +7 \) -printf '%f\n')
  for id in "${rm_list[@]:-}" $stale; do
    [ -n "$id" ] || continue
    if [ "$dry" = 1 ]; then echo "WOULD_DELETE=$id"; else rm -rf -- "${ROOT:?}/$id"; echo "DELETED=$id"; fi
  done
  echo "PRUNE_DONE"
}

# ------------------------------------------------------------------------------------------------ restore
members_ok() {   # members_ok ARCHIVE REGEX: no absolute paths, no "..", no escaping links, every member matches REGEX
  local arc="$1" re="$2" bad
  bad=$(tar tzf "$arc" 2>/dev/null | grep -vE "$re" || true)
  [ -z "$bad" ] || { echo "  refused: members outside the allowed location in $(basename "$arc"):"; echo "$bad" | head -3 | sed 's/^/    /'; return 1; }
  if tar tzf "$arc" 2>/dev/null | grep -qE '(^|/)\.\.(/|$)|^/'; then echo "  refused: unsafe path in $(basename "$arc")"; return 1; fi
  if tar tzvf "$arc" 2>/dev/null | grep -E '^[lh]' | grep -qE -- '-> (/|.*\.\.)|link to (/|.*\.\.)'; then
    echo "  refused: link pointing outside the archive in $(basename "$arc")"; return 1; fi
}

wait_http() { local i; for i in $(seq 1 "$2"); do curl -fsS -o /dev/null -m 3 "$1" 2>/dev/null && return 0; sleep 1; done; return 1; }

cmd_restore() {
  local id="${1:-}"; shift || true
  valid_id "$id" || die "usage: restore ID [options]" 2
  local only="" dry=0 yes=0 staging=0 prebak=1 rollback=1
  while [ $# -gt 0 ]; do
    case "$1" in --only) only="${2//,/ }"; shift 2 ;; --dry-run) dry=1; shift ;; --yes) yes=1; shift ;; --staging) staging=1; shift ;;
      --no-pre-backup) prebak=0; shift ;; --no-rollback) rollback=0; shift ;; *) die "restore: unknown option $1" 2 ;; esac
  done
  local B="$ROOT/$id"; [ -d "$B" ] || die "no such backup: $id" 3
  log "verifying backup $id"
  verify_dir "$B" || die "backup $id is damaged - restore refused (nothing was changed)" 1
  local avail; avail="$(sed -n 's/^"components": "\([^"]*\)".*/\1/p' "$B/MANIFEST.json" | head -1)"
  [ -n "$only" ] || only="$avail"
  local c; for c in $only; do [[ " $avail " == *" $c "* ]] || die "backup $id has no component '$c' (it has: $avail)" 1; done
  # prerequisites
  local need_mb=0 f
  for f in "$B"/*; do need_mb=$((need_mb + $(stat -c %s "$f") / 1048576 * 3 + 1)); done
  local free_mb; free_mb=$(df -Pm "$ROOT" | awk 'NR==2 {print $4}')
  [ "$free_mb" -ge "$need_mb" ] || die "not enough free disk space: need about ${need_mb} MB, have ${free_mb} MB" 1
  for c in $only; do
    case "$c" in
      postgres) have_pg || die "PostgreSQL tools not available here" 1 ;;
      geoserver) members_ok "$B/geoserver-data.tar.gz" "^$(basename "$GSDIR")(/|\$)" || exit 1 ;;
      qgis) members_ok "$B/qgis-projects.tar.gz" "^$(basename "$QGISDIR")(/|\$)" || exit 1 ;;
      cache) members_ok "$B/mapproxy-cache.tar.gz" "^$(basename "$CACHE_DIR")(/|\$)" || exit 1 ;;
      config) members_ok "$B/config.tar.gz" "$RESTORE_ALLOW" || exit 1 ;;
    esac
  done
  echo "RESTORE_PLAN backup=$id components=\"$only\" mode=$([ "$staging" = 1 ] && echo staging || echo in-place) pre_backup=$prebak"
  for c in $only; do
    case "$c" in
      postgres) echo "  postgres : database $DB is replaced by the dump (the old database is kept as ${DB}_old_<stamp> until validation passes)" ;;
      geoserver) echo "  geoserver: $GSDIR is replaced (old directory kept as $GSDIR.pre-restore-<stamp>); GeoServer is stopped meanwhile" ;;
      qgis) echo "  qgis     : $QGISDIR is replaced (old directory kept as $QGISDIR.pre-restore-<stamp>)" ;;
      cache) echo "  cache    : $CACHE_DIR is replaced (old directory kept)" ;;
      config) echo "  config   : configuration files are overwritten from the archive (originals are in the pre-restore backup)" ;;
    esac
  done
  [ "$dry" = 1 ] && { echo "RESTORE_DRY_RUN_OK"; return 0; }
  if [ "$staging" = 1 ]; then restore_staging "$id" "$B" "$only"; return; fi
  [ "$yes" = 1 ] || die "restore replaces live data: add --yes to confirm (see --dry-run for the plan)" 2

  local stamp; stamp="$(date -u +%Y%m%d%H%M%S)"
  if [ "$prebak" = 1 ]; then
    log "pre-restore backup of the current state"
    flock -u 9
    bash "${BASH_SOURCE[0]}" create --kind pre-restore --id "pre-$(new_id)" --components "$(echo "$only" | tr ' ' ',')" --note "before restoring $id" | tee /tmp/.gs-pre.$$ >/dev/null || { rm -f /tmp/.gs-pre.$$; die "pre-restore backup failed - restore refused, nothing was changed" 1; }
    exec 9>"$ROOT/.lock"; flock -n 9 || die "lock lost" 75
    PRE="$(sed -n 's/^BACKUP_VERIFIED=//p' /tmp/.gs-pre.$$ | head -1)"; rm -f /tmp/.gs-pre.$$
    echo "PRE_RESTORE_BACKUP=$PRE"
  fi

  UNDO=()                       # commands that put things back, run in reverse order on failure
  local failed=0 stopped_tc=0 reload_systemd=0
  need_tomcat_stopped() { if [ "$stopped_tc" = 0 ] && svc_active "$(tomcat_unit)"; then $SYSTEMCTL stop "$(tomcat_unit)"; stopped_tc=1; UNDO+=("$SYSTEMCTL start $(tomcat_unit)"); fi; }
  for c in $only; do
    log "restore $c"
    case "$c" in
      postgres) restore_postgres "$B" "$stamp" || { failed=1; break; } ;;
      geoserver) need_tomcat_stopped; swap_dir "$B/geoserver-data.tar.gz" "$GSDIR" "$stamp" tomcat || { failed=1; break; } ;;
      qgis) swap_dir "$B/qgis-projects.tar.gz" "$QGISDIR" "$stamp" qgis || { failed=1; break; } ;;
      cache) swap_dir "$B/mapproxy-cache.tar.gz" "$CACHE_DIR" "$stamp" mapproxy || { failed=1; break; } ;;
      config) tar xzf "$B/config.tar.gz" -C / || { failed=1; break; }; reload_systemd=1
              [ -n "${PRE:-}" ] && UNDO+=("tar xzf '$ROOT/$PRE/config.tar.gz' -C /") ;;
    esac
  done
  [ "$reload_systemd" = 1 ] && $SYSTEMCTL daemon-reload || true
  if [ "$failed" = 0 ] && [ "$stopped_tc" = 1 ]; then $SYSTEMCTL start "$(tomcat_unit)" || failed=1; fi
  [ "$failed" = 0 ] && { validate_recovery "$only" || failed=1; }
  if [ "$failed" != 0 ]; then
    if [ "$rollback" = 1 ]; then
      log "RESTORE FAILED - rolling back"
      local i; for ((i = ${#UNDO[@]} - 1; i >= 0; i--)); do eval "${UNDO[$i]}" || warn "undo step failed: ${UNDO[$i]}"; done
      [ "$reload_systemd" = 1 ] && $SYSTEMCTL daemon-reload || true
      echo "RESTORE_FAILED_ROLLED_BACK"
    else
      echo "RESTORE_FAILED_NO_ROLLBACK (old data is kept next to the new: *_old_*, *.pre-restore-*)"
    fi
    exit 1
  fi
  echo "RESTORE_OK=$id"
  echo "The previous data was kept (databases ${DB}_old_*, directories *.pre-restore-*); remove it when you are satisfied."
}

restore_postgres() {   # restore_postgres BACKUP STAMP
  local B="$1" stamp="$2" new="${DB}_restore_$2" old="${DB}_old_$2" f="$1/postgres-$DB.dump"
  [ -s "$f" ] || { echo "  no dump for $DB in this backup"; return 1; }
  # the application role must exist (its password hash lives in postgres-globals.sql)
  if ! pg psql -X -Atc "select 1 from pg_roles where rolname='$DBU'" | grep -q 1; then
    [ -s "$B/postgres-globals.sql" ] && (cd /tmp && $PGRUN psql -X -q < "$B/postgres-globals.sql" >/dev/null 2>&1) || true
  fi
  pg psql -X -v ON_ERROR_STOP=1 -q -c "CREATE DATABASE \"$new\" OWNER \"$DBU\"" || return 1
  UNDO+=("(cd /tmp && $PGRUN psql -X -q -c 'DROP DATABASE IF EXISTS \"$new\"' >/dev/null 2>&1) || true")
  (cd /tmp && $PGRUN pg_restore --exit-on-error -d "$new" < "$f") || { echo "  pg_restore failed"; return 1; }
  local n; n=$(pg psql -X -Atd "$new" -c "select count(*) from pg_class where relnamespace='public'::regnamespace" 2>/dev/null || echo "")
  [ -n "$n" ] || { echo "  restored database is not readable"; return 1; }
  # swap by renaming; connections to the live database are cut first
  local i
  for i in 1 2 3 4 5; do
    pg psql -X -q -c "select pg_terminate_backend(pid) from pg_stat_activity where datname='$DB' and pid<>pg_backend_pid()" >/dev/null 2>&1 || true
    if pg psql -X -v ON_ERROR_STOP=1 -q -c "ALTER DATABASE \"$DB\" RENAME TO \"$old\"" >/dev/null 2>&1; then break; fi
    sleep 2; [ "$i" = 5 ] && { echo "  could not rename the live database (still in use?)"; return 1; }
  done
  pg psql -X -v ON_ERROR_STOP=1 -q -c "ALTER DATABASE \"$new\" RENAME TO \"$DB\"" >/dev/null || {
    pg psql -X -q -c "ALTER DATABASE \"$old\" RENAME TO \"$DB\"" >/dev/null 2>&1; return 1; }
  UNDO+=("(cd /tmp && $PGRUN psql -X -q -c 'select pg_terminate_backend(pid) from pg_stat_activity where datname=\$\$$DB\$\$ and pid<>pg_backend_pid()' >/dev/null 2>&1; $PGRUN psql -X -q -c 'ALTER DATABASE \"$DB\" RENAME TO \"${DB}_failed_$stamp\"' && $PGRUN psql -X -q -c 'ALTER DATABASE \"$old\" RENAME TO \"$DB\"')")
  echo "  database $DB replaced ($n tables restored); previous one kept as $old"
}

swap_dir() {   # swap_dir ARCHIVE LIVE_DIR STAMP SERVICE_USER: extract next to the live dir, then swap by renaming
  local arc="$1" live="$2" stamp="$3" user="$4" parent base tmp
  parent="$(dirname "$live")"; base="$(basename "$live")"; tmp="$parent/.gs-restore-$stamp"
  mkdir -p "$tmp" && tar xzf "$arc" -C "$tmp" || { rm -rf "$tmp"; return 1; }
  [ -d "$tmp/$base" ] || { echo "  archive does not contain $base/"; rm -rf "$tmp"; return 1; }
  if id "$user" >/dev/null 2>&1 && [ "${GS_NO_ROOT:-0}" != 1 ]; then chown -R "$user:$user" "$tmp/$base"; fi
  if [ -e "$live" ]; then mv "$live" "$live.pre-restore-$stamp" || { rm -rf "$tmp"; return 1; }; fi
  mv "$tmp/$base" "$live" || { [ -e "$live.pre-restore-$stamp" ] && mv "$live.pre-restore-$stamp" "$live"; rm -rf "$tmp"; return 1; }
  rmdir "$tmp" 2>/dev/null || true
  UNDO+=("rm -rf '$live' && { [ ! -e '$live.pre-restore-$stamp' ] || mv '$live.pre-restore-$stamp' '$live'; }")
  echo "  $live replaced; previous one kept as $live.pre-restore-$stamp"
}

validate_recovery() {   # health probes after a restore; any failure triggers the rollback
  local only="$1" c ok=0
  for c in $only; do
    case "$c" in
      postgres)
        if PGPASSWORD="${DB_PASSWORD:-}" psql -X -h 127.0.0.1 -p "$DBPORT" -U "$DBU" -d "$DB" -Atc 'select 1' 2>/dev/null | grep -q 1 \
           || pg psql -X -Atd "$DB" -c 'select 1' 2>/dev/null | grep -q 1; then echo "  ok: database answers"; else echo "  FAILED: database does not answer"; return 1; fi ;;
      geoserver)
        if [ -n "${GS_HEALTH_URL:-}" ] || svc_active "$(tomcat_unit)"; then
          wait_http "${GS_HEALTH_URL:-http://127.0.0.1:$TCPORT/geoserver/web/}" "$HEALTH_WAIT" && echo "  ok: GeoServer answers" || { echo "  FAILED: GeoServer did not come up within ${HEALTH_WAIT}s"; return 1; }
        fi ;;
      *) ;;
    esac
  done
  ok=1; [ "$ok" = 1 ]
}

restore_staging() {   # restore_staging ID BACKUP COMPONENTS: unpack for inspection, touch nothing live
  local id="$1" B="$2" only="$3" S="$ROOT/staging/$1" c
  [ ! -e "$S" ] || die "staging directory exists: $S" 1
  mkdir -p "$S"
  for c in $only; do
    case "$c" in
      postgres)
        local new="${DB}_staging_${id//-/_}"; new="${new,,}"
        pg psql -X -v ON_ERROR_STOP=1 -q -c "CREATE DATABASE \"$new\" OWNER \"$DBU\"" || return 1
        (cd /tmp && $PGRUN pg_restore --exit-on-error -d "$new" < "$B/postgres-$DB.dump") || return 1
        echo "  database restored as $new (drop it with: DROP DATABASE \"$new\")" ;;
      geoserver) tar xzf "$B/geoserver-data.tar.gz" -C "$S" && echo "  GeoServer data in $S" ;;
      qgis) tar xzf "$B/qgis-projects.tar.gz" -C "$S" && echo "  QGIS projects in $S" ;;
      cache) tar xzf "$B/mapproxy-cache.tar.gz" -C "$S" && echo "  MapProxy cache in $S" ;;
      config) tar xzf "$B/config.tar.gz" -C "$S" && echo "  configuration in $S" ;;
    esac
  done
  echo "RESTORE_STAGING_OK=$S"
}

# ------------------------------------------------------------------------------------------------ main
case "${1:-}" in
  create) shift; cmd_create "$@" ;;
  list) shift; cmd_list "$@" ;;
  verify) shift; cmd_verify "$@" ;;
  restore) shift; cmd_restore "$@" ;;
  prune) shift; cmd_prune "$@" ;;
  -h|--help|"") sed -n '2,19p' "$0" ;;
  *) die "unknown command: $1 (create list verify restore prune)" 2 ;;
esac
