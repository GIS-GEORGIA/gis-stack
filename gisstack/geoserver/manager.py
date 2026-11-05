"""GeoServer manager (Phase 4): inventory, capabilities, preview, health and a few guarded changes - official REST API only.

Every function takes a `Gateway` (requests are made from the server itself, with the admin credentials kept on that side).  GeoServer's own
answer decides what is allowed: 401/403 from its security layer become `GeoServerError(code="forbidden")`, nothing is second-guessed here."""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from typing import Any, Optional

from ..core.errors import DeployError
from .gateway import SAFE_NAME, Gateway, Response


class GeoServerError(DeployError):
    """GeoServer answered, but not with success.  `status` is its HTTP status, `code` our mapped meaning."""
    def __init__(self, message: str, status: int = 0, code: str = "geoserver") -> None:
        super().__init__(message)
        self.status, self.code = status, code


def _fail(r: Response, what: str) -> GeoServerError:
    if r.status in (401, 403):
        return GeoServerError(f"geoserver: {what}: not allowed (HTTP {r.status}) - check geoserver.admin_user / admin_password", r.status, "forbidden")
    if r.status == 404:
        return GeoServerError(f"geoserver: {what}: not found", 404, "not_found")
    if r.status in (409, 400, 405):
        return GeoServerError(f"geoserver: {what}: rejected (HTTP {r.status}): {_snippet(r)}", r.status, "conflict" if r.status == 409 else "bad_request")
    return GeoServerError(f"geoserver: {what}: HTTP {r.status}", r.status, "upstream")


def _snippet(r: Response) -> str:
    return re.sub(r"\s+", " ", r.body[:200].decode("utf-8", "replace")).strip()


def name_ok(name: str, what: str = "name") -> str:
    if not isinstance(name, str) or not SAFE_NAME.match(name):
        raise GeoServerError(f"geoserver: invalid {what} {name!r}", 400, "bad_request")
    return name


def layer_ok(name: str) -> str:
    """`workspace:layer` or `layer`."""
    ws, sep, rest = name.partition(":")
    name_ok(ws, "layer")
    if sep:
        name_ok(rest, "layer")
    return name


def _json(gw: Gateway, path: str, what: str, query: Optional[dict[str, str]] = None) -> Any:
    r = gw.request("GET", path, query=query, accept="application/json")
    if r.status != 200:
        raise _fail(r, what)
    try:
        return json.loads(r.body.decode("utf-8") or "null")
    except ValueError as e:
        raise GeoServerError(f"geoserver: {what}: answer is not JSON", r.status, "upstream") from e


def _items(doc: Any, outer: str, inner: str) -> list[dict[str, Any]]:
    """GeoServer's JSON quirk: an empty collection is the string "", a single item may be an object."""
    node = (doc or {}).get(outer) if isinstance(doc, dict) else None
    if not isinstance(node, dict):
        return []
    v = node.get(inner)
    if isinstance(v, dict):
        return [v]
    return [x for x in (v or []) if isinstance(x, dict)]


def version(gw: Gateway) -> dict[str, str]:
    doc = _json(gw, "/rest/about/version.json", "version")
    res = {}
    for res_ in (doc or {}).get("about", {}).get("resource", []):
        if isinstance(res_, dict) and res_.get("@name") in ("GeoServer", "GeoTools", "GeoWebCache"):
            res[res_["@name"]] = str(res_.get("Version", ""))
    return res


def workspaces(gw: Gateway) -> list[dict[str, Any]]:
    return [{"name": w.get("name", "")} for w in _items(_json(gw, "/rest/workspaces.json", "workspaces"), "workspaces", "workspace")]


def datastores(gw: Gateway, ws: str) -> list[dict[str, Any]]:
    name_ok(ws, "workspace")
    return [{"name": d.get("name", ""), "workspace": ws} for d in _items(_json(gw, f"/rest/workspaces/{ws}/datastores.json", "datastores"), "dataStores", "dataStore")]


def layers(gw: Gateway) -> list[dict[str, Any]]:
    out = []
    for lay in _items(_json(gw, "/rest/layers.json", "layers"), "layers", "layer"):
        n = str(lay.get("name", ""))
        ws, _, short = n.rpartition(":")
        out.append({"name": n, "workspace": ws, "title": short or n})
    return out


def layer_detail(gw: Gateway, name: str) -> dict[str, Any]:
    layer_ok(name)
    lay = (_json(gw, f"/rest/layers/{name}.json", "layer") or {}).get("layer", {})
    res = lay.get("resource", {}) if isinstance(lay, dict) else {}
    return {"name": lay.get("name", name), "type": lay.get("type", ""), "enabled": lay.get("enabled", True),
            "default_style": (lay.get("defaultStyle") or {}).get("name", ""), "resource": res.get("name", ""), "resource_type": res.get("@class", "")}


def layergroups(gw: Gateway) -> list[dict[str, Any]]:
    return [{"name": g.get("name", "")} for g in _items(_json(gw, "/rest/layergroups.json", "layer groups"), "layerGroups", "layerGroup")]


def styles(gw: Gateway) -> list[dict[str, Any]]:
    return [{"name": s.get("name", "")} for s in _items(_json(gw, "/rest/styles.json", "styles"), "styles", "style")]


def inventory(gw: Gateway) -> dict[str, Any]:
    """Everything in one call; a part that fails reports its own error so one broken listing does not hide the rest."""
    out: dict[str, Any] = {"errors": {}}
    for key, fn in (("version", version), ("workspaces", workspaces), ("layers", layers), ("layergroups", layergroups), ("styles", styles)):
        try:
            out[key] = fn(gw)
        except GeoServerError as e:
            out["errors"][key] = str(e)
            out[key] = {} if key == "version" else []
            if e.code == "forbidden":                  # wrong credentials: the other calls would say the same
                break
    stores = []
    for w in out.get("workspaces", []):
        try:
            stores += datastores(gw, w["name"])
        except GeoServerError as e:
            out["errors"].setdefault("datastores", str(e))
    out["datastores"] = stores
    return out


# --------------------------------------------------------------------------- OGC capabilities
SERVICES = {
    "WMS": ("/ows", {"service": "WMS", "version": "1.3.0", "request": "GetCapabilities"}),
    "WFS": ("/ows", {"service": "WFS", "version": "2.0.0", "request": "GetCapabilities"}),
    "WMTS": ("/gwc/service/wmts", {"REQUEST": "GetCapabilities"}),
}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_capabilities(xml: bytes, service: str) -> dict[str, Any]:
    """Layer names/titles and formats from a capabilities document.  Documents carrying a DTD / entities are refused (XXE, billion laughs)."""
    head = xml[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in xml.lower():
        raise GeoServerError("geoserver: capabilities document has a DTD - refused", 0, "upstream")
    try:
        root = ET.fromstring(xml)  # noqa: S314  # nosec B314 - DTDs/entities are rejected above
    except ET.ParseError as e:
        raise GeoServerError("geoserver: capabilities is not valid XML", 0, "upstream") from e
    if _local(root.tag) == "ServiceExceptionReport" or _local(root.tag) == "ExceptionReport":
        raise GeoServerError("geoserver: service exception: " + re.sub(r"\s+", " ", "".join(root.itertext()))[:200].strip(), 0, "upstream")
    items = []
    kind = {"WMS": "Layer", "WFS": "FeatureType", "WMTS": "Layer"}[service]
    for el in root.iter():
        if _local(el.tag) != kind:
            continue
        name = next((c.text for c in el if _local(c.tag) in ("Name", "Identifier") and c.text), None)
        if not name:
            continue
        title = next((c.text for c in el if _local(c.tag) == "Title" and c.text), "")
        items.append({"name": name.strip(), "title": (title or "").strip()})
    formats = sorted({(e.text or "").strip() for e in root.iter() if _local(e.tag) == "Format" and (e.text or "").strip()})
    return {"service": service, "version": root.get("version", ""), "layers": items, "formats": formats}


def capabilities(gw: Gateway, service: str) -> dict[str, Any]:
    if service not in SERVICES:
        raise GeoServerError(f"geoserver: unknown service {service!r}", 400, "bad_request")
    path, q = SERVICES[service]
    r = gw.request("GET", path, query=q, accept="application/xml,text/xml", timeout=40)
    if r.status != 200:
        raise _fail(r, f"{service} capabilities")
    return parse_capabilities(r.body, service)


# --------------------------------------------------------------------------- preview
PREVIEW_FORMATS = ("image/png", "image/jpeg")
MAX_PIXELS = 2048


def preview(gw: Gateway, layers_: str, bbox: str, width: int, height: int, srs: str = "EPSG:4326", fmt: str = "image/png",
            style: str = "") -> Response:
    """WMS GetMap proxied from the server.  Only small raster previews: sizes are capped and the answer must really be an image."""
    for part in layers_.split(","):
        layer_ok(part)
    if fmt not in PREVIEW_FORMATS:
        raise GeoServerError(f"geoserver: preview format must be one of {', '.join(PREVIEW_FORMATS)}", 400, "bad_request")
    if not (16 <= width <= MAX_PIXELS and 16 <= height <= MAX_PIXELS):
        raise GeoServerError(f"geoserver: preview size must be 16..{MAX_PIXELS} px", 400, "bad_request")
    if not re.match(r"^EPSG:\d{4,6}$", srs):
        raise GeoServerError("geoserver: srs must look like EPSG:4326", 400, "bad_request")
    try:
        nums = [float(x) for x in bbox.split(",")]
    except ValueError:
        nums = []
    if len(nums) != 4 or not (nums[0] < nums[2] and nums[1] < nums[3]):
        raise GeoServerError("geoserver: bbox must be minx,miny,maxx,maxy", 400, "bad_request")
    if style and not re.match(r"^[A-Za-z0-9_:.-]{1,80}$", style):
        raise GeoServerError("geoserver: invalid style", 400, "bad_request")
    q = {"service": "WMS", "version": "1.1.1", "request": "GetMap", "layers": layers_, "styles": style, "bbox": ",".join(repr(n) for n in nums),
         "width": str(width), "height": str(height), "srs": srs, "format": fmt}
    r = gw.request("GET", "/ows", query=q, accept=fmt, timeout=40)
    if r.status != 200:
        raise _fail(r, "preview")
    if r.content_type not in PREVIEW_FORMATS:
        msg = _snippet(r) if r.content_type in ("application/vnd.ogc.se_xml", "text/xml", "application/xml") else r.content_type
        raise GeoServerError(f"geoserver: preview failed: {msg}", 0, "upstream")
    return r


# --------------------------------------------------------------------------- health and validation
def health(gw: Gateway) -> dict[str, Any]:
    """REST reachable + credentials accepted + WMS answering + at least a version.  Each check stands alone."""
    checks: list[dict[str, Any]] = []

    def run(label: str, fn: Any) -> Any:
        try:
            val = fn()
            checks.append({"check": label, "ok": True, "detail": ""})
            return val
        except DeployError as e:
            checks.append({"check": label, "ok": False, "detail": str(e)})
            return None
    ver = run("rest", lambda: version(gw))
    run("wms", lambda: capabilities(gw, "WMS"))
    ws = run("workspaces", lambda: workspaces(gw))
    return {"ok": all(c["ok"] for c in checks), "version": ver or {}, "workspaces": len(ws) if ws is not None else None, "checks": checks}


def validate(gw: Gateway) -> list[dict[str, str]]:
    """Configuration problems an operator would want to know: empty stores, layers without a style, unused workspaces."""
    inv = inventory(gw)
    issues: list[dict[str, str]] = [{"level": "error", "what": k, "message": v} for k, v in inv["errors"].items()]
    used = {lay["workspace"] for lay in inv["layers"]}
    stores_by_ws = {d["workspace"] for d in inv["datastores"]}
    for w in inv["workspaces"]:
        if w["name"] not in used and w["name"] not in stores_by_ws:
            issues.append({"level": "info", "what": f"workspace {w['name']}", "message": "has no datastores or layers"})
    for ws in {d["workspace"] for d in inv["datastores"]} - used:
        issues.append({"level": "warn", "what": f"workspace {ws}", "message": "has datastores but no published layers"})
    if not inv["layers"]:
        issues.append({"level": "info", "what": "layers", "message": "nothing is published yet"})
    return issues


# --------------------------------------------------------------------------- guarded changes
def create_workspace(gw: Gateway, name: str) -> dict[str, str]:
    name_ok(name, "workspace")
    r = gw.request("POST", "/rest/workspaces", body=json.dumps({"workspace": {"name": name}}).encode(), content_type="application/json")
    if r.status not in (200, 201):
        raise _fail(r, f"create workspace {name}")
    return {"name": name}


def delete_workspace(gw: Gateway, name: str, recurse: bool = False) -> None:
    name_ok(name, "workspace")
    r = gw.request("DELETE", f"/rest/workspaces/{name}", query={"recurse": "true" if recurse else "false"})
    if r.status != 200:
        raise _fail(r, f"delete workspace {name}")


def reload_catalog(gw: Gateway) -> None:
    r = gw.request("POST", "/rest/reload")
    if r.status != 200:
        raise _fail(r, "reload")
