"""PostGIS manager (Phase 5): inventory, a guarded SQL editor and a time-boxed write elevation.

Safety model (roadmap): the DATABASE ROLE and the READ ONLY transaction are the boundary - the statement splitter below is only for UX.
* Reads run as the dedicated role `gisstack_ro` (no write privileges, no superuser, no file/program access, statement timeout, 3 connections).
* Writes need an explicit elevation (admin scope, confirmation, <= 15 minutes, bound to the caller, the project and the machine) and every write
  is journalled and audited with a statement hash.  They run as the stack's own database role, never as a superuser."""
from __future__ import annotations

import csv
import hashlib
import io
import re
import secrets
import threading
import time
from typing import Any, Callable, Optional

from ..core.errors import DeployError
from . import sqlsplit
from .server import MAX_BYTES, NULL_TOKEN, Gateway, Raw

csv.field_size_limit(max(csv.field_size_limit(), MAX_BYTES + 1024))          # one big geometry/text cell must not crash the parser
DEFAULT_LIMIT, MAX_LIMIT = 1000, 5000
MAX_SQL_CHARS = 100_000
RO_TIMEOUT_S = 15
MAX_ELEVATION_MIN = 15
INDEX_WARN_ROWS = 1000


class PostgisError(DeployError):
    def __init__(self, message: str, code: str = "postgis", details: Optional[dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.code, self.details = code, details or {}


# --------------------------------------------------------------------------- inventory
_Q = {
    "server": "select current_setting('server_version') as v, current_setting('max_connections') as maxc, pg_postmaster_start_time()::text as started, "
              "pg_is_in_recovery()::text as recovery, (select count(*) from pg_stat_activity where backend_type = 'client backend')::text as conns",
    "databases": "select datname, pg_database_size(datname)::text as bytes, pg_encoding_to_char(encoding) as encoding from pg_database "
                 "where not datistemplate and datallowconn order by datname",
    "connections": "select coalesce(state, 'none') as state, count(*)::text as n from pg_stat_activity where datname = current_database() "
                   "and backend_type = 'client backend' group by 1 order by 1",
    "extensions": "select extname, extversion from pg_extension order by 1",
    "available": "select name, default_version, coalesce(installed_version, '') as installed from pg_available_extensions where name in "
                 "('postgis','postgis_raster','postgis_topology','postgis_tiger_geocoder','address_standardizer','pg_stat_statements','pgrouting','pgcrypto',"
                 "'pg_trgm','hstore','btree_gist','pointcloud','h3') order by 1",
    "tables": "select n.nspname, c.relname, c.relkind::text, greatest(c.reltuples, 0)::bigint::text, pg_total_relation_size(c.oid)::text from pg_class c "
              "join pg_namespace n on n.oid = c.relnamespace where c.relkind in ('r','p','m','v','f') and n.nspname not in ('pg_catalog','information_schema') "
              "and n.nspname not like 'pg\\_toast%' and n.nspname not like 'pg\\_temp%' order by pg_total_relation_size(c.oid) desc limit 500",
    "spatial": "select f_table_schema, f_table_name, f_geometry_column, type, srid::text, coord_dimension::text, 'geometry' from geometry_columns "
               "union all select f_table_schema, f_table_name, f_geography_column, type, srid::text, '2', 'geography' from geography_columns order by 1, 2, 3 limit 1000",
    "gist": "select schemaname, tablename, indexname, indexdef from pg_indexes where indexdef ~* 'using (gist|spgist)' "
            "and schemaname not in ('pg_catalog','information_schema') limit 2000",
    "statements": "select left(regexp_replace(query, '\\s+', ' ', 'g'), 200), calls::text, round(total_exec_time::numeric, 1)::text, "
                  "round(mean_exec_time::numeric, 2)::text, rows::text from pg_stat_statements order by total_exec_time desc limit 10",
    "cache": "select coalesce(round(100.0 * sum(blks_hit) / nullif(sum(blks_hit + blks_read), 0), 1)::text, '') from pg_stat_database "
             "where datname = current_database()",
}


def _rows(text: str, header: bool = True) -> list[list[str]]:
    r = list(csv.reader(io.StringIO(text)))
    return r[1:] if header and r else r


def inventory(gw: Gateway, db: str) -> dict[str, Any]:
    """Everything the overview page needs; a section that fails reports its own error and the rest still show."""
    raw = gw.sections(db, _Q)
    errors: dict[str, str] = {}

    def sec(name: str, optional: bool = False) -> list[list[str]]:
        rc, text = raw.get(name, (1, "no answer"))
        if rc != 0:
            if not optional:
                errors[name] = _first_line(text)
            return []
        return _rows(text)
    server = sec("server")
    srv: dict[str, Any] = {"version": "", "max_connections": None, "started": "", "recovery": False, "connections": 0, "cache_hit_percent": None}
    if server:
        v, maxc, started, rec, conns = server[0][:5]
        srv.update(version=v, max_connections=int(maxc), started=started, recovery=rec == "true", connections=int(conns))
    cache = sec("cache", True)
    if cache and cache[0] and cache[0][0]:
        srv["cache_hit_percent"] = float(cache[0][0])
    exts = [{"name": r[0], "version": r[1]} for r in sec("extensions")]
    have = {e["name"] for e in exts}
    tables: list[dict[str, Any]] = [{"schema": r[0], "name": r[1], "kind": {"r": "table", "p": "partitioned", "m": "materialized view", "v": "view", "f": "foreign"}.get(r[2], r[2]),
               "rows_estimate": int(r[3]), "bytes": int(r[4])} for r in sec("tables")]
    gist: dict[tuple[str, str], list[str]] = {}
    for r in sec("gist"):
        gist.setdefault((r[0], r[1]), []).append(r[3])
    spatial = []
    for r in sec("spatial", optional="postgis" not in have):
        col = r[2]
        idx = any(re.search(r"\(\s*\"?" + re.escape(col) + r"\"?[\s,)]", d) for d in gist.get((r[0], r[1]), []))
        spatial.append({"schema": r[0], "table": r[1], "column": col, "type": r[3], "srid": int(r[4]), "dimensions": int(r[5] or 2), "kind": r[6], "spatial_index": idx})
    schemas: dict[str, dict[str, Any]] = {}
    for t in tables:
        s = schemas.setdefault(t["schema"], {"name": t["schema"], "tables": 0, "bytes": 0})
        s["tables"] += 1
        s["bytes"] += t["bytes"]
    stmts = None
    if "pg_stat_statements" in have:
        rc, text = raw.get("statements", (1, ""))
        if rc == 0:
            stmts = [{"query": r[0], "calls": int(r[1]), "total_ms": float(r[2]), "mean_ms": float(r[3]), "rows": int(r[4])} for r in _rows(text)]
        else:
            errors["statements"] = _first_line(text)
    return {"database": db, "server": srv, "databases": [{"name": r[0], "bytes": int(r[1]), "encoding": r[2]} for r in sec("databases")],
            "connections": {r[0]: int(r[1]) for r in sec("connections")}, "extensions": exts,
            "available": [{"name": r[0], "default_version": r[1], "installed": r[2]} for r in sec("available")],
            "schemas": sorted(schemas.values(), key=lambda s: -s["bytes"]), "tables": tables, "spatial": spatial,
            "statements": stmts, "errors": errors}


def health(inv: dict[str, Any]) -> dict[str, Any]:
    """Judgement calls over an inventory: nothing here is guessed, every finding names the fact it rests on."""
    out: list[dict[str, str]] = []
    s = inv["server"]
    if s["max_connections"] and s["connections"] >= 0.8 * s["max_connections"]:
        out.append({"level": "warn", "what": "connections", "message": f"{s['connections']} of {s['max_connections']} connections in use"})
    if s["recovery"]:
        out.append({"level": "info", "what": "recovery", "message": "the server is a standby (in recovery)"})
    if s["cache_hit_percent"] is not None and s["cache_hit_percent"] < 90:
        out.append({"level": "warn", "what": "cache", "message": f"buffer cache hit ratio is {s['cache_hit_percent']} % (< 90)"})
    rows = {(t["schema"], t["name"]): t["rows_estimate"] for t in inv["tables"]}
    small = 0
    for sp in inv["spatial"]:
        if sp["spatial_index"] or sp["kind"] != "geometry":
            continue
        if rows.get((sp["schema"], sp["table"]), 0) >= INDEX_WARN_ROWS:
            out.append({"level": "warn", "what": f"{sp['schema']}.{sp['table']}.{sp['column']}",
                        "message": f"geometry column without a GiST/SP-GiST index (about {rows[(sp['schema'], sp['table'])]} rows)"})
        else:
            small += 1
    if small:
        out.append({"level": "info", "what": "spatial indexes", "message": f"{small} geometry column(s) in small or empty tables have no spatial index (fine until they grow past {INDEX_WARN_ROWS} rows)"})
    have = {e["name"] for e in inv["extensions"]}
    if "postgis" not in have:
        out.append({"level": "error", "what": "postgis", "message": "the postgis extension is not installed in this database"})
    elif "pg_stat_statements" not in have:
        out.append({"level": "info", "what": "pg_stat_statements", "message": "not installed: no query statistics"})
    for k, v in inv["errors"].items():
        out.append({"level": "warn" if k == "statements" else "error", "what": k, "message": v})      # statistics are optional; a broken listing is not
    return {"ok": not any(f["level"] == "error" for f in out), "findings": out}


def _first_line(text: str) -> str:
    return re.sub(r"\s+", " ", (text.strip().splitlines() or [""])[0])[:300]


# --------------------------------------------------------------------------- the editor
def clamp(limit: Optional[int], offset: Optional[int]) -> tuple[int, int]:
    return max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT)), max(0, int(offset or 0))


def one_statement(sql: str) -> tuple[str, str]:
    """(statement without trailing ';', its first keyword).  Raises PostgisError when it is empty, too long, several statements or a psql command."""
    if not isinstance(sql, str) or not sql.strip():
        raise PostgisError("postgis: empty statement", "bad_request")
    if len(sql) > MAX_SQL_CHARS:
        raise PostgisError(f"postgis: statement longer than {MAX_SQL_CHARS} characters", "bad_request")
    try:
        parts = sqlsplit.split_statements(sql)
    except sqlsplit.SqlSyntax as e:
        raise PostgisError(f"postgis: {e}", "bad_request") from e
    if not parts:
        raise PostgisError("postgis: empty statement", "bad_request")
    if len(parts) > 1:
        raise PostgisError("postgis: one statement at a time", "bad_request")
    stmt = parts[0]
    if stmt.lstrip().startswith("\\"):
        raise PostgisError("postgis: psql meta-commands are not supported", "bad_request")
    return stmt, sqlsplit.first_keyword(stmt)


_ERR_MAP = (
    (re.compile(r"read-only transaction|permission denied|must be owner|must be superuser", re.I), "forbidden"),
    (re.compile(r"statement timeout|canceling statement", re.I), "timeout"),
    (re.compile(r"password authentication failed|role .* does not exist|no pg_hba", re.I), "not_ready"),
    (re.compile(r"syntax error|does not exist|invalid input|violates|duplicate key|cannot|operator|column .* of relation|ambiguous", re.I), "sql"),
    (re.compile(r"connection refused|could not connect|server closed|timeout expired", re.I), "unreachable"),
)


def _raise_for(raw: Raw, op: str) -> None:
    if raw.rc == 96:
        raise PostgisError("postgis: the read-only editor is not set up on this server yet (run the editor setup first)", "not_ready")
    if raw.rc == 97:
        raise PostgisError("postgis: could not pass the statement to the server", "postgis")
    if raw.rc == 141 or (raw.rc == 0):
        return
    msg = _first_line(raw.err) or f"psql exit {raw.rc}"
    code = next((c for rx, c in _ERR_MAP if rx.search(raw.err)), "sql")
    raise PostgisError(f"postgis: {op}: {msg}", code)


def parse_result(text: str, truncated: bool, limit: Optional[int] = None) -> dict[str, Any]:
    rows = list(csv.reader(io.StringIO(text))) if text else []
    cols = rows[0] if rows else []
    data = [[None if c == NULL_TOKEN else c for c in r] for r in rows[1:]]
    if truncated and data:
        data = data[:-1]                                    # the last row may be cut in the middle
    has_more = False
    if limit is not None and len(data) > limit:
        data, has_more = data[:limit], True
    return {"columns": cols, "rows": data, "has_more": has_more}


def wrap_for_paging(stmt: str, kw: str, limit: int, offset: int) -> tuple[str, bool]:
    if kw in sqlsplit.PAGEABLE:
        # the user's statement IS the feature; ints are clamped above; the role and the read-only transaction are the boundary (docs/POSTGIS.md)
        return f"select * from (\n{stmt}\n) as gs_q limit {int(limit) + 1} offset {int(offset)}", True  # noqa: S608  # nosec B608
    return stmt, False


def query(gw: Gateway, db: str, sql: str, limit: Optional[int] = None, offset: Optional[int] = None, clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Run one statement as the read-only role.  SELECT-like statements are paged; anything else (EXPLAIN, SHOW ...) is capped by size."""
    stmt, kw = one_statement(sql)
    lim, off = clamp(limit, offset)
    text, paged = wrap_for_paging(stmt, kw, lim, off)
    t0 = clock()
    raw = gw.run(text, role="ro", db=db, write=False)
    _raise_for(raw, "query")
    res = parse_result(raw.out, raw.truncated, lim if paged else MAX_LIMIT)
    res.update(row_count=len(res["rows"]), offset=off if paged else 0, limit=lim, paged=paged, truncated=raw.truncated, elapsed_ms=int((clock() - t0) * 1000))
    return res


def execute(gw: Gateway, db: str, sql: str, clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Run one statement as the stack's own role (the caller has already checked elevation).  Returns rows (RETURNING / SELECT) and the command tag."""
    stmt, _ = one_statement(sql)
    t0 = clock()
    raw = gw.run(stmt, role="rw", db=db, write=True, timeout=90)
    _raise_for(raw, "statement")
    lines = raw.out.split("\n") if raw.out else []
    if lines and lines[0] == "SET":
        lines = lines[1:]
    tag = ""
    if lines and re.match(r"^(INSERT \d+ \d+|UPDATE \d+|DELETE \d+|MERGE \d+|SELECT \d+|COPY \d+|[A-Z][A-Z ]+[A-Z])$", lines[-1]):
        tag = lines.pop()
    res = parse_result("\n".join(lines), raw.truncated)
    res.update(row_count=len(res["rows"]), tag=tag, truncated=raw.truncated, elapsed_ms=int((clock() - t0) * 1000))
    return res


def statement_digest(sql: str) -> dict[str, Any]:
    """What the audit log keeps of a statement: a hash and a short, single-line start (statements may contain data)."""
    return {"sha256": hashlib.sha256(sql.encode("utf-8")).hexdigest()[:16], "start": re.sub(r"\s+", " ", sql.strip())[:120], "chars": len(sql)}


# --------------------------------------------------------------------------- elevation
class Elevations:
    """Per-process, in-memory write permissions: bound to who asked, which project and which machine, short-lived, revocable.  A restart forgets them."""
    MAX_OPEN = 5

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock, self._lock = clock, threading.Lock()
        self._items: dict[str, dict[str, Any]] = {}

    def _purge(self) -> None:
        now = self._clock()
        for k in [k for k, v in self._items.items() if v["expires"] <= now]:
            del self._items[k]

    def grant(self, principal: str, project: str, host: str, minutes: float) -> dict[str, Any]:
        if not 0 < minutes <= MAX_ELEVATION_MIN:
            raise PostgisError(f"postgis: elevation lasts 1..{MAX_ELEVATION_MIN} minutes", "bad_request")
        with self._lock:
            self._purge()
            if len(self._items) >= self.MAX_OPEN:
                raise PostgisError("postgis: too many open elevations", "conflict")
            eid = secrets.token_urlsafe(18)
            exp = self._clock() + minutes * 60
            self._items[eid] = {"principal": principal, "project": project, "host": host, "expires": exp}
            return {"elevation": eid, "expires": exp, "minutes": minutes}

    def check(self, eid: str, principal: str, project: str, host: str) -> None:
        with self._lock:
            self._purge()
            it = self._items.get(eid)
            if it is None or it["principal"] != principal or it["project"] != project or it["host"] != host:
                raise PostgisError("postgis: no valid write elevation (it may have expired or belong to someone else)", "forbidden")

    def revoke(self, eid: str, principal: str) -> bool:
        with self._lock:
            it = self._items.get(eid)
            if it and it["principal"] == principal:
                del self._items[eid]
                return True
            return False
