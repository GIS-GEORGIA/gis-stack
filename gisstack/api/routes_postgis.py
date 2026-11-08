"""/api/v1 routes of the PostGIS manager (feature flag `postgis_manager`).  The database role and the read-only transaction are the safety
boundary: reading needs `operate`, role setup / elevation / writes need `admin`, and every write is journalled and audited."""
from __future__ import annotations

from typing import Any

from .. import journal, transport
from ..postgis import manager as pgm
from ..postgis.server import ScriptGateway
from .mount import Req
from .registry import ApiError, registry
from .routes import BOOL, INT, OBJ, STR, body, machine, remote
from .schema import Schema

R = registry.route
DB: Schema = {"type": "string", "pattern": "^[A-Za-z_][A-Za-z0-9_]{0,62}$"}
KW: dict[str, Any] = {"tags": ("postgis",), "feature": "postgis_manager", "errors": ("forbidden", "not_ready", "sql", "timeout", "unreachable", "transport")}
CONFIRM: Schema = {"type": "boolean", "const": True}
RESULT: Schema = {"type": "object", "required": ["columns", "rows"], "properties": {
    "columns": {"type": "array", "items": STR}, "rows": {"type": "array"}, "row_count": INT, "has_more": BOOL, "truncated": BOOL, "elapsed_ms": INT,
    "offset": INT, "limit": INT, "paged": BOOL, "tag": STR}}


def gateway(req: Req) -> tuple[Any, dict[str, Any], str]:
    cfg, mode, ssh, target = machine(req)
    if target != "native":
        raise ApiError(400, "config", "PostGIS manager currently supports the native target only")
    return ScriptGateway(cfg, mode, ssh), cfg, transport.host_label(mode, ssh)


def elevations(req: Req) -> pgm.Elevations:
    return req.app.config.setdefault("PG_ELEVATIONS", pgm.Elevations())


@R("POST", "/postgis/inventory", "Databases, schemas, tables, spatial columns (SRID, type, index), extensions, connections, query statistics", "read",
   body=body({"database": DB}), **KW,
   response={"type": "object", "required": ["database", "server", "tables", "spatial", "errors"], "properties": {
       "database": STR, "server": OBJ, "databases": {"type": "array"}, "connections": OBJ, "extensions": {"type": "array"}, "available": {"type": "array"},
       "schemas": {"type": "array"}, "tables": {"type": "array"}, "spatial": {"type": "array"}, "statements": {"type": ["array", "null"]}, "errors": OBJ}})
def inventory(req: Req) -> dict[str, Any]:
    gw, cfg, _ = gateway(req)
    return remote(pgm.inventory, gw, req.body.get("database") or cfg["postgres"]["db"])


@R("POST", "/postgis/health", "Findings over the inventory: connections, cache, geometry columns without index, missing extensions", "read",
   body=body({"database": DB}), **KW,
   response={"type": "object", "required": ["ok", "findings"], "properties": {"ok": BOOL, "findings": {"type": "array"}, "server": OBJ}})
def health(req: Req) -> dict[str, Any]:
    gw, cfg, _ = gateway(req)
    inv = remote(pgm.inventory, gw, req.body.get("database") or cfg["postgres"]["db"])
    return {**pgm.health(inv), "server": inv["server"]}


@R("POST", "/postgis/editor/setup", "Create or refresh the least-privilege read-only role (idempotent); its password stays on the server", "admin",
   body=body({"statement_timeout": {"type": "integer", "minimum": 1, "maximum": 60}}), **KW,
   response={"type": "object", "required": ["role", "database"], "properties": {"role": STR, "database": STR, "statement_timeout": INT}})
def editor_setup(req: Req) -> dict[str, Any]:
    gw, cfg, host = gateway(req)
    db, timeout = cfg["postgres"]["db"], int(req.body.get("statement_timeout") or pgm.RO_TIMEOUT_S)
    with journal.Operation("postgis:editor-setup", project=cfg["project"], host=host, database=db):
        remote(gw.setup, db, cfg["postgres"]["user"], timeout)
    req.app.config["AUDIT"].write(event="postgis.setup", principal=req.principal.id, scope=req.principal.scope, database=db)
    return {"role": "gisstack_ro", "database": db, "statement_timeout": timeout}


@R("POST", "/postgis/query", "Run ONE statement read-only (dedicated role, read-only transaction, timeout, row and size limits, paging)", "operate",
   body=body({"sql": {"type": "string", "minLength": 1, "maxLength": pgm.MAX_SQL_CHARS}, "limit": {"type": "integer", "minimum": 1, "maximum": pgm.MAX_LIMIT},
              "offset": {"type": "integer", "minimum": 0}}, ("sql",)), **KW, response=RESULT)
def run_query(req: Req) -> dict[str, Any]:
    gw, cfg, _ = gateway(req)
    b = req.body
    return remote(pgm.query, gw, cfg["postgres"]["db"], b["sql"], b.get("limit"), b.get("offset"))


@R("POST", "/postgis/elevate", "Open a write elevation for this caller, project and machine (max 15 minutes); confirm=true is required", "admin",
   body=body({"minutes": {"type": "integer", "minimum": 1, "maximum": pgm.MAX_ELEVATION_MIN}, "confirm": CONFIRM}, ("confirm",)), **KW,
   response={"type": "object", "required": ["elevation", "expires"], "properties": {"elevation": STR, "expires": {"type": "number"}, "minutes": {"type": "number"}}})
def elevate(req: Req) -> dict[str, Any]:
    _, cfg, host = gateway(req)
    res = remote(elevations(req).grant, req.principal.id, cfg["project"], host, float(req.body.get("minutes") or 5))
    req.app.config["AUDIT"].write(event="postgis.elevate", principal=req.principal.id, scope=req.principal.scope, project=cfg["project"], host=host, minutes=res["minutes"])
    return res


@R("POST", "/postgis/elevate/revoke", "End a write elevation now", "admin", body=body({"elevation": STR}, ("elevation",)), **KW,
   response={"type": "object", "properties": {"revoked": BOOL}})
def revoke(req: Req) -> dict[str, Any]:
    ok = elevations(req).revoke(req.body["elevation"], req.principal.id)
    req.app.config["AUDIT"].write(event="postgis.revoke", principal=req.principal.id, ok=ok)
    return {"revoked": ok}


@R("POST", "/postgis/execute", "Run ONE statement that may change data (needs a valid elevation and confirm=true; journalled and audited)", "admin",
   body=body({"sql": {"type": "string", "minLength": 1, "maxLength": pgm.MAX_SQL_CHARS}, "elevation": STR, "confirm": CONFIRM}, ("sql", "elevation", "confirm")),
   **KW, response=RESULT)
def run_write(req: Req) -> dict[str, Any]:
    gw, cfg, host = gateway(req)
    b = req.body
    pgm.one_statement(b["sql"])                                          # reject before anything is recorded or sent
    remote(elevations(req).check, b["elevation"], req.principal.id, cfg["project"], host)
    digest = pgm.statement_digest(b["sql"])
    audit = req.app.config["AUDIT"]
    audit.write(event="postgis.write", phase="start", principal=req.principal.id, scope=req.principal.scope, project=cfg["project"], host=host, **digest)
    try:
        with journal.Operation("postgis:write", project=cfg["project"], host=host, sha256=digest["sha256"]):
            res = remote(pgm.execute, gw, cfg["postgres"]["db"], b["sql"])
    except Exception as e:
        audit.write(event="postgis.write", phase="end", ok=False, sha256=digest["sha256"], error=type(e).__name__)
        raise
    audit.write(event="postgis.write", phase="end", ok=True, sha256=digest["sha256"], tag=res["tag"], rows=res["row_count"])
    return res
