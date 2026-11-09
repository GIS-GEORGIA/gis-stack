"""/api/v1 routes of the MapProxy manager (feature flag `mapproxy_manager`).  Reading needs `read`; every change (config edit, restore, seeding,
cache cleanup) needs `admin`, defaults to a dry run where it can, is journalled and audited, and runs on the server through the shared transport."""
from __future__ import annotations

from typing import Any

from flask import Response, make_response

from .. import journal, targets, transport
from ..mapproxy import manager as mpm
from ..mapproxy.server import PlaceGateway, ScriptGateway
from .mount import Req
from .registry import ApiError, registry
from .routes import BOOL, INT, OBJ, STR, body, machine, remote, start_job
from .schema import Schema

R = registry.route
NAME: Schema = {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_]{0,40}$"}
KW: dict[str, Any] = {"tags": ("mapproxy",), "feature": "mapproxy_manager", "errors": ("not_found", "conflict", "config", "upstream", "transport")}
BBOX: Schema = {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4}


def gateway(req: Req) -> tuple[ScriptGateway, dict[str, Any], str]:
    cfg, mode, ssh, _ = machine(req)
    place = targets.place_of(cfg, "mapproxy")
    gw: ScriptGateway = ScriptGateway(mode, ssh) if place.native else PlaceGateway(place, mode, ssh)
    return gw, cfg, transport.host_label(mode, ssh)


def audit(req: Req, event: str, **fields: Any) -> None:
    req.app.config["AUDIT"].write(event=event, principal=req.principal.id, scope=req.principal.scope, **fields)


@R("POST", "/mapproxy/config", "The live configuration summarised: layers, caches, sources (credentials hidden), grids, seeds", "read", body=body(), **KW,
   response={"type": "object", "required": ["layers", "caches", "sources", "grids"], "properties": {
       "services": {"type": "array"}, "layers": {"type": "array"}, "caches": {"type": "array"}, "sources": {"type": "array"}, "grids": {"type": "array"},
       "seeds": {"type": "array"}, "cache_dir": STR, "demo": BOOL}})
def get_config(req: Req) -> dict[str, Any]:
    gw, _, _ = gateway(req)
    return remote(mpm.snapshot, gw)["config"]


@R("POST", "/mapproxy/validate", "Validate the live configuration (and seed.yaml) with MapProxy itself, strictly", "read", body=body(), **KW,
   response={"type": "object", "required": ["ok", "errors"], "properties": {"ok": BOOL, "errors": {"type": "array"}, "warnings": {"type": "array"}}})
def validate(req: Req) -> dict[str, Any]:
    gw, _, _ = gateway(req)
    return remote(mpm.validate, gw)


@R("POST", "/mapproxy/health", "Service, HTTP, cache directory, disk, configuration validity, advertised layers", "read", body=body(), **KW,
   response={"type": "object", "required": ["ok", "findings"], "properties": {"ok": BOOL, "findings": {"type": "array"}, "service": STR, "http": INT, "seeding": BOOL,
                                                                            "layers_served": {"type": "array"}, "config_valid": {"type": ["boolean", "null"]}}})
def health(req: Req) -> dict[str, Any]:
    gw, _, _ = gateway(req)
    return remote(mpm.health, gw)


@R("POST", "/mapproxy/cache", "Cache size, tile count and tiles per level for every cache", "read", body=body(), **KW,
   response={"type": "object", "required": ["available", "caches"], "properties": {"available": BOOL, "cache_dir": STR, "disk_total_kb": {"type": ["integer", "null"]},
                                                                                   "disk_free_kb": {"type": ["integer", "null"]}, "caches": {"type": "array"},
                                                                                   "unmanaged": {"type": "array"}, "total_bytes": INT}})
def cache(req: Req) -> dict[str, Any]:
    gw, _, _ = gateway(req)
    return remote(mpm.cache_stats, gw)


@R("POST", "/mapproxy/tile", "Preview ONE tile through the running MapProxy (PNG/JPEG; a missing tile is fetched and cached)", "read", raw="binary", **KW,
   body=body({"layer": NAME, "grid": {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_]{0,60}$"}, "z": {"type": "integer", "minimum": 0, "maximum": 24},
              "x": {"type": "integer", "minimum": 0}, "y": {"type": "integer", "minimum": 0}}, ("layer", "grid", "z", "x", "y")))
def tile(req: Req) -> Response:
    gw, _, _ = gateway(req)
    b = req.body
    t = remote(mpm.tile_preview, gw, b["layer"], b["grid"], b["z"], b["x"], b["y"])
    resp = make_response(t.body)
    resp.headers["Content-Type"] = t.content_type
    resp.headers["Cache-Control"] = "no-store"
    return resp


@R("POST", "/mapproxy/config/backups", "Configuration backups made automatically before every change (newest first)", "read", body=body(), **KW,
   response={"type": "array", "items": {"type": "object", "required": ["id"], "properties": {"id": STR, "created": INT, "bytes": INT}}})
def config_backups(req: Req) -> list[dict[str, Any]]:
    gw, _, _ = gateway(req)
    return remote(mpm.backups, gw)


@R("POST", "/mapproxy/config/edit", "Add/remove a layer or a grid: validated by MapProxy, backed up, installed atomically (dry_run defaults to true)", "admin",
   body=body({"op": {"type": "string", "enum": sorted(mpm.OPS)}, "params": OBJ, "dry_run": BOOL}, ("op", "params")), **KW,
   response={"type": "object", "required": ["changes", "dry_run"], "properties": {"changes": {"type": "array"}, "dry_run": BOOL, "backup": STR, "warnings": {"type": "array"}, "note": STR}})
def edit(req: Req) -> dict[str, Any]:
    gw, cfg, host = gateway(req)
    b = req.body
    dry = b.get("dry_run", True) is not False
    with journal.Operation("mapproxy:edit", project=cfg["project"], host=host, change=b["op"], dry_run=dry):
        res = remote(mpm.edit, gw, b["op"], b["params"], dry)
    if not dry:
        audit(req, "mapproxy.edit", project=cfg["project"], host=host, op=b["op"], changes=res["changes"], backup=res["backup"])
    return res


@R("POST", "/mapproxy/config/restore", "Put a configuration backup back (the current one is backed up first); confirm=true is required", "admin",
   body=body({"id": {"type": "string", "pattern": r"^\d{8}-\d{6}-[0-9a-f]{4}$"}, "confirm": {"type": "boolean", "const": True}}, ("id", "confirm")), **KW,
   response={"type": "object", "required": ["restored"], "properties": {"restored": STR, "backup_of_previous": STR}})
def restore(req: Req) -> dict[str, Any]:
    gw, cfg, host = gateway(req)
    with journal.Operation("mapproxy:restore", project=cfg["project"], host=host, backup=req.body["id"]):
        res = remote(mpm.restore, gw, req.body["id"])
    audit(req, "mapproxy.restore", project=cfg["project"], host=host, backup=req.body["id"], previous=res["backup_of_previous"])
    return res


@R("POST", "/mapproxy/seed", "Estimate (default) or run seeding of caches (job): a dry run always comes first, real runs need confirm=true and stay under max_tiles", "admin",
   job=True, **KW,
   body=body({"caches": {"type": "array", "items": {"type": "string", "pattern": "^[A-Za-z0-9_]{1,60}$"}, "minItems": 1, "maxItems": 20},
              "grids": {"type": "array", "items": {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_]{0,60}$"}, "maxItems": 10},
              "level_from": {"type": "integer", "minimum": 0, "maximum": 20}, "level_to": {"type": "integer", "minimum": 0, "maximum": 20}, "bbox": BBOX,
              "refresh": BOOL, "concurrency": {"type": "integer", "minimum": 1, "maximum": 4},
              "max_tiles": {"type": "integer", "minimum": 1, "maximum": mpm.MAX_TILES_HARD}, "dry_run": BOOL, "confirm": BOOL}, ("caches", "level_to", "bbox")))
def seed(req: Req) -> str:
    gw, cfg, host = gateway(req)
    b = dict(req.body)
    dry = b.get("dry_run", True) is not False
    if not dry and b.get("confirm") is not True:
        raise ApiError(400, "plan", "real seeding needs confirm=true")
    remote(lambda: mpm.build_seed(mpm.snapshot(gw)["conf"], {"level_from": 0, **b}))      # validate eagerly: bad input is a 400, not a failed job

    def steps(job: Any, _bundle: Any) -> Any:
        with journal.Operation("mapproxy:seed", project=cfg["project"], host=host, caches=",".join(b["caches"]), dry_run=dry):
            yield from mpm.seed(gw, {"level_from": 0, **b}, job.stop)
    audit(req, "mapproxy.seed", project=cfg["project"], host=host, caches=b["caches"], levels=[b.get("level_from", 0), b["level_to"]], dry_run=dry)
    return start_job(req, steps)


@R("POST", "/mapproxy/cache/cleanup", "Delete a cache's tiles (or only tiles older than N days). Dry run by default; real deletion needs dry_run=false AND confirm=true", "admin",
   body=body({"cache": {"type": "string", "pattern": "^[A-Za-z0-9_]{1,60}$"}, "older_than_days": {"type": "integer", "minimum": 1, "maximum": 36500},
              "dry_run": BOOL, "confirm": BOOL}, ("cache",)), **KW,
   response={"type": "object", "required": ["cache", "dry_run", "files", "bytes"], "properties": {"cache": STR, "dry_run": BOOL, "entries": {"type": "array"}, "files": INT, "bytes": INT,
                                                                                               "older_than_days": {"type": ["integer", "null"]}, "note": STR}})
def cleanup(req: Req) -> dict[str, Any]:
    gw, cfg, host = gateway(req)
    b = req.body
    dry = b.get("dry_run", True) is not False
    if not dry and b.get("confirm") is not True:
        raise ApiError(400, "plan", "deleting cached tiles cannot be undone: send dry_run=false together with confirm=true")
    with journal.Operation("mapproxy:cleanup", project=cfg["project"], host=host, cache=b["cache"], dry_run=dry):
        res = remote(mpm.cleanup, gw, b["cache"], b.get("older_than_days"), dry)
    if not dry:
        audit(req, "mapproxy.cleanup", project=cfg["project"], host=host, cache=b["cache"], older_than_days=b.get("older_than_days"), files=res["files"], bytes=res["bytes"])
    return res


