"""How SQL reaches PostgreSQL: through `psql` on the server itself (locally or over SSH), the same way monitoring and backups do.

* Inventory and role setup run as the `postgres` OS user (peer authentication, no password anywhere) with *fixed* SQL only.
* The read-only editor connects over TCP as the dedicated least-privilege role `gisstack_ro`; its password lives in a root-only file ON THE SERVER
  (`/var/lib/gisstack/postgis-ro.pw`) and never travels to the web application.  psql runs as the unprivileged user `nobody`.
* Elevated writes connect as the stack's own database role; its password goes to the script on stdin.
* The statement text goes in on stdin (base64) and reaches psql as ONE `-c` string that always starts with a fixed `SET ...;` prefix - a bare
  `psql -c '\\! cmd'` would run a shell command, a prefixed one makes every backslash a server syntax error.

`Gateway` is the interface the manager uses; `ScriptGateway` is the real one, tests plug in fakes."""
from __future__ import annotations

import base64
import re
import shlex
from dataclasses import dataclass
from typing import Any, Optional

from .. import targets, transport
from ..core.errors import DeployError

RO_ROLE = "gisstack_ro"
PW_FILE = "/var/lib/gisstack/postgis-ro.pw"
NULL_TOKEN = "\x01NULL\x01"
MAX_BYTES = 4 * 1024 * 1024
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


@dataclass
class Raw:
    rc: int
    err: str
    out: str
    truncated: bool = False


class Gateway:
    def sections(self, db: str, queries: dict[str, str]) -> dict[str, tuple[int, str]]:
        """Run fixed catalog queries as the superuser (peer).  name -> (rc, csv text or error text)."""
        raise NotImplementedError

    def setup(self, db: str, owner: str, statement_timeout_s: int) -> None:
        raise NotImplementedError

    def run(self, sql: str, *, role: str, db: str, write: bool, max_bytes: int = MAX_BYTES, timeout: int = 40) -> Raw:
        """role is 'ro' (dedicated role, read-only transaction) or 'rw' (the stack's own role)."""
        raise NotImplementedError


def _check_names(**names: str) -> None:
    for k, v in names.items():
        if not isinstance(v, str) or not NAME_RE.match(v):
            raise DeployError(f"postgis: invalid {k} {v!r}")


_PGRUN = '${GS_PGRUN-runuser -u postgres --}'
_PGRUN_LOW = '${GS_PGRUN-runuser -u nobody --}'          # unquoted on purpose: the empty override (tests) must vanish
_PWFILE = f'${{GS_PG_PWFILE:-{PW_FILE}}}'


class ScriptGateway(Gateway):
    def __init__(self, cfg: dict[str, Any], mode: str, ssh: Optional[dict[str, Any]]) -> None:
        self.cfg, self.mode, self.ssh = cfg, mode, ssh
        pg = cfg["postgres"]
        self.owner, self.password = str(pg["user"]), str(pg["password"])
        self.place = targets.place_of(cfg, "postgis")
        _check_names(owner=self.owner)
        # native: the published/host port; Compose/Kubernetes: psql runs INSIDE the container, so it is always the container's own port
        self.port = int(pg["port"]) if self.place.native else self.place.internal_port
        # who runs psql: native = OS users `postgres` / `nobody` (runuser); containers = `gs_in USER` (see targets.Place)
        # in a container the database superuser is the stack's own role (POSTGRES_USER), not a role called postgres: pass it as PGUSER
        self.pgrun = _PGRUN if self.place.native else f"gs_in postgres env PGUSER={self.owner}"
        self.pgrun_low = _PGRUN_LOW if self.place.native else "gs_in nobody"
        # the read-only role's password file lives on the machine that runs the script; native keeps it root-only under /var/lib/gisstack
        self.pwfile = _PWFILE if self.place.native else f"${{GS_PG_PWFILE:-$HOME/.gisstack/postgis-ro-{cfg['project']}.pw}}"

    def _sh(self, script: str, stdin: str = "", timeout: int = 40) -> str:
        # containers are reached with the operator's own docker / kubectl rights: sudo would use root's (empty) kubeconfig
        return transport.run(self.mode, self.ssh, self.place.prelude() + script, root=self.place.native, timeout=timeout, input_text=stdin or None)

    # ------------------------------------------------------------------ catalog (superuser, fixed SQL)
    def sections(self, db: str, queries: dict[str, str]) -> dict[str, tuple[int, str]]:
        _check_names(database=db)
        lines = ["set -u", "cd /tmp", f"PGRUN=\"{self.pgrun}\"",
                 'sec() { local out rc; out="$($PGRUN psql -X -q --csv -v ON_ERROR_STOP=1 -d ' + shlex.quote(db) + ' -c "$2" 2>&1)"; rc=$?; echo "@@$1 $rc"; printf \'%s\\n\' "$out"; }']
        for name, sql in queries.items():
            _check_names(section=name)
            lines.append(f"sec {name} {shlex.quote(sql)}")
        return parse_sections(self._sh("\n".join(lines) + "\n", timeout=60))

    # ------------------------------------------------------------------ the dedicated read-only role
    def setup(self, db: str, owner: str, statement_timeout_s: int) -> None:
        _check_names(database=db, owner=owner)
        secs = int(statement_timeout_s)
        script = rf"""set -eu
PGRUN="{self.pgrun}"; F="{self.pwfile}"
install -d -m 700 "$(dirname "$F")"
if [ ! -s "$F" ]; then ( umask 077; head -c 36 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32 > "$F" ); fi
chmod 600 "$F"
PW="$(cat "$F")"
[ "${{#PW}}" -ge 24 ] || {{ echo "postgis: password file looks wrong" >&2; exit 3; }}
cd /tmp
cat <<SQL | $PGRUN psql -X -q -v ON_ERROR_STOP=1 -d "{db}"
DO \$do\$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{RO_ROLE}') THEN
    CREATE ROLE {RO_ROLE} LOGIN PASSWORD '$PW' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION CONNECTION LIMIT 3;
  ELSE
    ALTER ROLE {RO_ROLE} LOGIN PASSWORD '$PW' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION CONNECTION LIMIT 3;
  END IF;
END \$do\$;
ALTER ROLE {RO_ROLE} SET default_transaction_read_only = on;
ALTER ROLE {RO_ROLE} SET statement_timeout = '{secs}s';
ALTER ROLE {RO_ROLE} SET idle_in_transaction_session_timeout = '30s';
GRANT CONNECT ON DATABASE "{db}" TO {RO_ROLE};
GRANT pg_read_all_stats TO {RO_ROLE};
DO \$do\$ DECLARE s text; BEGIN
  FOR s IN SELECT nspname FROM pg_namespace WHERE nspname NOT IN ('pg_catalog', 'information_schema') AND nspname NOT LIKE 'pg\_%' LOOP
    EXECUTE format('GRANT USAGE ON SCHEMA %I TO {RO_ROLE}', s);
    EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO {RO_ROLE}', s);
    EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I GRANT SELECT ON TABLES TO {RO_ROLE}', '{owner}', s);
  END LOOP;
END \$do\$;
SQL
echo setup-ok
"""  # noqa: S608  # nosec B608 - shell script; db/owner are checked by NAME_RE above, the timeout is an int, the password never appears here
        out = self._sh(script, timeout=90)
        if "setup-ok" not in out:
            raise DeployError("postgis: role setup did not finish")

    # ------------------------------------------------------------------ run one statement
    def run(self, sql: str, *, role: str, db: str, write: bool, max_bytes: int = MAX_BYTES, timeout: int = 40) -> Raw:
        _check_names(database=db)
        if role not in ("ro", "rw"):
            raise DeployError("postgis: bad role")
        user = RO_ROLE if role == "ro" else self.owner
        _check_names(user=user)
        prefix = "SET application_name = 'gisstack'; " if write else "SET TRANSACTION READ ONLY; "
        encoded = base64.b64encode((prefix + sql).encode("utf-8")).decode()
        if role == "ro":
            get_pw = f'F="{self.pwfile}"; [ -s "$F" ] || {{ echo "@@rc 96"; exit 0; }}; PW="$(cat "$F")"; IFS= read -r B64'
            stdin, options = encoded + "\n", ""
        else:
            get_pw = "IFS= read -r PW; IFS= read -r B64"
            stdin, options = self.password + "\n" + encoded + "\n", " PGOPTIONS='-c statement_timeout=60000'"
        # kubectl exec cannot forward environment variables: hand the (non-secret) client settings to psql through env(1) inside the pod
        inside = "" if self.place.can_pass_env else " env PGCONNECT_TIMEOUT=5" + (" 'PGOPTIONS=-c statement_timeout=60000'" if role == "rw" else "")
        flags = "-X --csv" + ("" if write else " -q") + " -P " + shlex.quote("null=" + NULL_TOKEN)
        script = f"""set -u
cd /tmp
{get_pw}
SQL="$(printf %s "$B64" | base64 -d)" || {{ echo "@@rc 97"; exit 0; }}
t="$(mktemp -d)"; trap 'rm -rf "$t"' EXIT
export PGPASSWORD="$PW" PGCONNECT_TIMEOUT=5{options}
{self.pgrun_low}{inside} psql {flags} -h 127.0.0.1 -p {self.port} -U {user} -d {db} -c "$SQL" 2>"$t/e" | head -c {int(max_bytes)} >"$t/o"
rc=${{PIPESTATUS[0]}}
echo "@@rc $rc"; echo "@@err"; head -c 1500 "$t/e"; echo "@@out"; cat "$t/o"
"""
        return parse_raw(self._sh(script, stdin=stdin, timeout=timeout), max_bytes)


def parse_sections(out: str) -> dict[str, tuple[int, str]]:
    res: dict[str, tuple[int, str]] = {}
    name: Optional[str] = None
    rc = 0
    buf: list[str] = []
    for line in out.splitlines():
        if line.startswith("@@"):
            if name is not None:
                res[name] = (rc, "\n".join(buf))
            head = line[2:].split(" ")
            name, rc, buf = head[0], int(head[1]) if len(head) > 1 and head[1].lstrip("-").isdigit() else 1, []
        elif name is not None:
            buf.append(line)
    if name is not None:
        res[name] = (rc, "\n".join(buf))
    return res


def parse_raw(out: str, max_bytes: int) -> Raw:
    rc, err_lines, out_lines, mode = 0, [], [], ""
    for line in out.split("\n"):
        if mode == "" and line.startswith("@@rc "):
            rc = int(line[5:].strip() or 0)
        elif line == "@@err" and mode == "":
            mode = "err"
        elif line == "@@out" and mode == "err":
            mode = "out"
        elif mode == "err":
            err_lines.append(line)
        elif mode == "out":
            out_lines.append(line)
    body = "\n".join(out_lines)
    if body.endswith("\n"):
        body = body[:-1]
    return Raw(rc, "\n".join(err_lines).strip(), body, truncated=len(body.encode("utf-8", "replace")) >= max_bytes)
