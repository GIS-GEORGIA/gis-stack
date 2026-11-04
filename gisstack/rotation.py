"""Credential rotation for an installed (native) stack: PostgreSQL role password and GeoServer admin password.

The new password travels over the command's *stdin* (never in an argument list), is validated on both sides, is applied and
verified against the running service first, and only then written to /etc/gisstack/stack.env and credentials.txt on the server and
to the local secret store.  If verification fails the old password is restored and nothing is written.  Containers and Kubernetes
keep their passwords in volumes/secrets that need their own procedure - not supported yet and refused with a clear message."""
from __future__ import annotations

import re
import secrets
from typing import Any, Optional

from . import config, deploy, journal, runner, transport

WHAT = ("postgres", "geoserver")
_PW = re.compile(r"^[A-Za-z0-9_.\-]{8,64}$")

_PRELUDE = r"""set -eu
IFS= read -r NEWPW || true
case "$NEWPW" in ''|*[!A-Za-z0-9_.-]*) echo "ROTATE_ERROR=bad-password"; exit 2;; esac
[ "${#NEWPW}" -ge 8 ] && [ "${#NEWPW}" -le 64 ] || { echo "ROTATE_ERROR=bad-password"; exit 2; }
export NEWPW
[ -r /etc/gisstack/stack.env ] || { echo "ROTATE_ERROR=not-installed"; exit 3; }
# shellcheck source=/dev/null
. /etc/gisstack/stack.env
hexof() { printf '%s' "$1" | od -An -v -tx1 | tr -d ' \n'; }
cfgq() { local s=${1//\\/\\\\}; s=${s//\"/\\\"}; printf '%s' "$s"; }
setkv() {   # setkv FILE VAR: rewrite VAR="${VAR:-...}" in a stack.env style file; the value comes from the environment, not argv
  local t; t="$(mktemp)"
  awk -v var="$2" 'BEGIN { pw = ENVIRON["NEWPW"] } index($0, var "=") == 1 { print var "=\"${" var ":-" pw "}\""; next } { print }' "$1" > "$t"
  cat "$t" > "$1"; rm -f "$t"
}
setcred() {  # setcred FILE KEY-PREFIX-REGEX ...: replace the password in credentials.txt lines (awk, value via environment)
  [ -f "$1" ] || return 0
  local t; t="$(mktemp)"
  awk -v mode="$2" 'BEGIN { pw = ENVIRON["NEWPW"] }
    mode == "pg" && /^PostgreSQL:/ { sub(/password=[^ ]*$/, "password=" pw) }
    mode == "gs" && /^GeoServer admin:/ { sub(/ \/ .*$/, " / " pw) }
    { print }' "$1" > "$t"
  cat "$t" > "$1"; rm -f "$t"
}
"""

_POSTGRES = _PRELUDE + r"""
alter() {   # alter PASSWORD: change the role's password, the password goes to psql as hex on stdin
  { printf '\\set u_hex %s\n\\set p_hex %s\n' "$(hexof "$DB_USER")" "$(hexof "$1")"
    cat <<'SQL'
SELECT convert_from(decode(:'u_hex', 'hex'), 'UTF8') AS u, convert_from(decode(:'p_hex', 'hex'), 'UTF8') AS p \gset
SELECT format('ALTER ROLE %I PASSWORD %L', :'u', :'p') \gexec
SQL
  } | (cd /tmp && runuser -u postgres -- psql -X -v ON_ERROR_STOP=1 >/dev/null)
}
login_ok() { PGPASSWORD="$1" psql -X -h 127.0.0.1 -p "${PG_PORT:-5432}" -U "$DB_USER" -d "$DB_NAME" -Atc 'select 1' 2>/dev/null | grep -qx 1; }
alter "$NEWPW"
if ! login_ok "$NEWPW"; then
  alter "$DB_PASSWORD" || true
  echo "ROTATE_ERROR=verify-failed"; exit 4
fi
setkv /etc/gisstack/stack.env DB_PASSWORD
setcred /etc/gisstack/credentials.txt pg
echo "ROTATED=postgres"
"""

_GEOSERVER = _PRELUDE + r"""
base="http://127.0.0.1:${TOMCAT_PORT:-8080}/geoserver/rest"
gs() {   # gs USER PASSWORD [JSON-BODY] URL: credentials and body on curl's stdin (--config -)
  local u="$1" p="$2" body="$3" url="$4"
  { printf 'user = "%s"\n' "$(cfgq "$u:$p")"
    if [ -n "$body" ]; then printf 'request = "PUT"\nheader = "Content-Type: application/json"\ndata = "%s"\n' "$(cfgq "$body")"; fi
  } | curl -fsS -o /dev/null --config - "$url"
}
body="$(printf '{"newPassword":"%s"}' "$(cfgq "$NEWPW")")"
gs admin "$GS_ADMIN_PASSWORD" "$body" "$base/security/self/password" || { echo "ROTATE_ERROR=change-failed"; exit 4; }
verified=0
for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do      # GeoServer applies the change a moment after it answers 200
  if gs admin "$NEWPW" "" "$base/about/version"; then verified=1; break; fi
  sleep 1
done
if [ "$verified" != 1 ]; then
  oldbody="$(printf '{"newPassword":"%s"}' "$(cfgq "$GS_ADMIN_PASSWORD")")"
  gs admin "$NEWPW" "$oldbody" "$base/security/self/password" || gs admin admin "$GS_ADMIN_PASSWORD" "" "$base/about/version" || true
  echo "ROTATE_ERROR=verify-failed"; exit 4
fi
setkv /etc/gisstack/stack.env GS_ADMIN_PASSWORD
setcred /etc/gisstack/credentials.txt gs
echo "ROTATED=geoserver"
"""

_ERRORS = {"bad-password": "პაროლი არასწორია (8-64 სიმბოლო: A-Z a-z 0-9 _ . -)", "not-installed": "სერვერზე gis-stack არ არის დაყენებული",
           "verify-failed": "ახალი პაროლი ვერ გადამოწმდა — ძველი აღდგენილია, არაფერი შეცვლილა",
           "change-failed": "პაროლის შეცვლა ვერ მოხერხდა (ძველი პაროლი არ ემთხვევა?) — არაფერი შეცვლილა"}


def script(what: str) -> str:
    if what not in WHAT:
        raise deploy.DeployError("უცნობი მონაცემი: " + what)
    return _POSTGRES if what == "postgres" else _GEOSERVER


def new_password() -> str:
    return secrets.token_urlsafe(18)


def rotate(cfg: dict[str, Any], what: str, mode: str, ssh: Optional[dict[str, Any]] = None, new: Optional[str] = None) -> dict[str, Any]:
    """Change one credential on the server.  Returns {"what", "target", "stored"}; raises DeployError (nothing changed) on failure."""
    target = cfg["deploy_target"]
    if target != "native":
        raise deploy.DeployError("პაროლების ბრუნვა ჯერ მხოლოდ native სამიზნეზეა; კონტეინერებისთვის შეცვალე კონფიგურაცია და თავიდან გაუშვი")
    sc = script(what)
    pw = new or new_password()
    if not _PW.match(pw):
        raise deploy.DeployError(_ERRORS["bad-password"])
    mask = runner.redactor([pw, *runner.secrets_of(cfg)])
    host = "localhost" if mode == "local" else str((ssh or {}).get("host", ""))
    with journal.Operation(f"rotate:{what}", redact=mask, target=target, mode=mode, host=host, project=cfg.get("project", "")):
        try:
            out = transport.run(mode, ssh, sc, root=True, timeout=180, input_text=pw + "\n")
        except deploy.DeployError as e:
            raise deploy.DeployError(mask(str(e))) from e
    err = re.search(r"ROTATE_ERROR=([a-z-]+)", out)
    if err or f"ROTATED={what}" not in out:
        raise deploy.DeployError(_ERRORS.get(err.group(1), "ბრუნვა ვერ მოხერხდა") if err else "ბრუნვა ვერ დადასტურდა")
    config.store_secret(cfg["project"], "postgres.password" if what == "postgres" else "geoserver.admin_password", pw)
    return {"what": what, "target": target, "stored": True}
