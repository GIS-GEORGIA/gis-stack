"""/api/v1 routes of the GeoServer manager (feature flag `geoserver_manager`).  GeoServer is reached from the server itself with the
credentials of the stack's configuration; they never appear in a request or an answer."""
from __future__ import annotations

from typing import Any

from flask import Response, make_response

from ..geoserver import manager as gsm
from ..geoserver.gateway import for_config
from .mount import Req
from .registry import ApiError, registry
from .routes import BOOL, OBJ, STR, body, machine, remote
from .schema import Schema

R = registry.route
NAME: Schema = {"type": "string", "pattern": r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}$"}
LAYER: Schema = {"type": "string", "pattern": r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}(:[A-Za-z0-9_][A-Za-z0-9_.-]{0,62})?$"}
TAGS = ("geoserver",)
KW: dict[str, Any] = {"tags": TAGS, "feature": "geoserver_manager", "errors": ("forbidden", "not_found", "upstream", "transport")}
NAMED: Schema = {"type": "array", "items": {"type": "object", "required": ["name"], "properties": {"name": STR}}}


def gateway(req: Req) -> Any:
    cfg, mode, ssh, _ = machine(req)
    return remote(for_config, cfg, mode, ssh)


def call(fn: Any, *a: Any, **kw: Any) -> Any:
    return remote(fn, *a, **kw)


@R("POST", "/geoserver/inventory", "Workspaces, datastores, layers, layer groups and styles (each part reports its own error)", "read", body=body(), **KW,
   response={"type": "object", "required": ["errors"], "properties": {
       "version": OBJ, "workspaces": NAMED, "datastores": {"type": "array"}, "layers": {"type": "array"}, "layergroups": NAMED, "styles": NAMED, "errors": OBJ}})
def inventory(req: Req) -> dict[str, Any]:
    return call(gsm.inventory, gateway(req))


@R("POST", "/geoserver/health", "REST reachable, credentials accepted, WMS answering", "read", body=body(), **KW,
   response={"type": "object", "required": ["ok", "checks"], "properties": {"ok": BOOL, "version": OBJ, "workspaces": {"type": ["integer", "null"]},
                                                                            "checks": {"type": "array"}}})
def health(req: Req) -> dict[str, Any]:
    return call(gsm.health, gateway(req))


@R("POST", "/geoserver/validate", "Configuration findings: empty workspaces, datastores without layers ...", "read", body=body(), **KW,
   response={"type": "array", "items": {"type": "object", "required": ["level", "what", "message"], "properties": {"level": STR, "what": STR, "message": STR}}})
def validate(req: Req) -> list[dict[str, str]]:
    return call(gsm.validate, gateway(req))


@R("POST", "/geoserver/capabilities", "Layers and formats a WMS / WFS / WMTS capabilities document advertises", "read",
   body=body({"service": {"type": "string", "enum": ["WMS", "WFS", "WMTS"]}}, ("service",)), **KW,
   response={"type": "object", "required": ["service", "layers"], "properties": {"service": STR, "version": STR, "layers": NAMED, "formats": {"type": "array"}}})
def capabilities(req: Req) -> dict[str, Any]:
    return call(gsm.capabilities, gateway(req), req.body["service"])


@R("POST", "/geoserver/layers/detail", "One layer: type, default style, enabled", "read", body=body({"name": LAYER}, ("name",)), **KW,
   response={"type": "object", "required": ["name"], "properties": {"name": STR, "type": STR, "enabled": BOOL, "default_style": STR, "resource": STR, "resource_type": STR}})
def layer_detail(req: Req) -> dict[str, Any]:
    return call(gsm.layer_detail, gateway(req), req.body["name"])


@R("POST", "/geoserver/preview", "Layer preview: WMS GetMap proxied from the server (PNG/JPEG, max 2048 px)", "read", raw="binary", **KW,
   body=body({"layers": {"type": "string", "maxLength": 400}, "bbox": {"type": "string", "maxLength": 120}, "width": {"type": "integer", "minimum": 16, "maximum": 2048},
              "height": {"type": "integer", "minimum": 16, "maximum": 2048}, "srs": {"type": "string", "maxLength": 12},
              "format": {"type": "string", "enum": list(gsm.PREVIEW_FORMATS)}, "style": {"type": "string", "maxLength": 80}}, ("layers", "bbox", "width", "height")))
def preview(req: Req) -> Response:
    b = req.body
    r = call(gsm.preview, gateway(req), b["layers"], b["bbox"], b["width"], b["height"], b.get("srs", "EPSG:4326"), b.get("format", "image/png"), b.get("style", ""))
    resp = make_response(r.body)
    resp.headers["Content-Type"] = r.content_type
    resp.headers["Cache-Control"] = "no-store"
    return resp


@R("POST", "/geoserver/workspaces", "Create a workspace", "admin", body=body({"name": NAME}, ("name",)), **KW,
   response={"type": "object", "required": ["name"], "properties": {"name": STR}})
def create_workspace(req: Req) -> dict[str, str]:
    return call(gsm.create_workspace, gateway(req), req.body["name"])


@R("POST", "/geoserver/workspaces/delete", "Delete a workspace; recurse=true also deletes everything in it and needs confirm=true", "admin",
   body=body({"name": NAME, "recurse": BOOL, "confirm": BOOL}, ("name",)), **KW,
   response={"type": "object", "properties": {"deleted": STR}})
def delete_workspace(req: Req) -> dict[str, str]:
    b = req.body
    if b.get("recurse") and b.get("confirm") is not True:
        raise ApiError(400, "plan", "recurse=true deletes all layers and data stores in the workspace: send confirm=true")
    call(gsm.delete_workspace, gateway(req), b["name"], bool(b.get("recurse")))
    return {"deleted": b["name"]}


@R("POST", "/geoserver/reload", "Reload GeoServer's catalog and configuration from disk", "operate", body=body(), **KW,
   response={"type": "object", "properties": {"reloaded": BOOL}})
def reload_catalog(req: Req) -> dict[str, Any]:
    call(gsm.reload_catalog, gateway(req))
    return {"reloaded": True}
