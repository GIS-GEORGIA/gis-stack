"""/api/v1 routes of multi-server management (feature flag `multi_server`).

Profiles hold addresses, an environment tag and the PATH of an SSH key (never its content), plus a pinned host key.  Every other route
that talks to a machine accepts `"server": "<id>"` in its body and then uses that profile's own credentials and pinned key.
Reading needs `read`; changing profiles or pins needs `admin` and is audited."""
from __future__ import annotations

from typing import Any

from ..core.errors import GisStackError
from ..servers import hostkey, service, store
from .mount import Req
from .registry import ApiError, registry
from .routes import BOOL, INT, STR
from .schema import Schema

R = registry.route
ID: Schema = {"type": "string", "pattern": "^[a-z][a-z0-9_-]{0,30}$"}
KW: dict[str, Any] = {"tags": ("servers",), "feature": "multi_server"}
PROFILE: Schema = {"type": "object", "required": ["id", "pinned"], "properties": {
    "id": STR, "name": STR, "env": {"type": "string", "enum": list(store.ENVS)}, "host": STR, "port": INT, "user": STR, "key": STR, "notes": STR,
    "pinned": BOOL, "fingerprints": {"type": "array", "items": STR}, "created": INT, "updated": INT}}


def audit(req: Req, event: str, **fields: Any) -> None:
    req.app.config["AUDIT"].write(event=event, principal=req.principal.id, scope=req.principal.scope, **fields)


def guarded(fn: Any, *a: Any, **kw: Any) -> Any:
    try:
        return fn(*a, **kw)
    except GisStackError as e:
        raise ApiError.from_exc(e) from e


@R("GET", "/servers", "Saved server profiles (no key material is ever stored or returned)", "read", **KW, errors=("config",),
   response={"type": "array", "items": PROFILE})
def list_servers(req: Req) -> list[dict[str, Any]]:
    return guarded(store.list_servers)


@R("PUT", "/servers/{id}", "Create or update a server profile; changing host/port drops the pinned host key", "admin", **KW, errors=("config",), params={"id": "server id"},
   body={"type": "object", "additionalProperties": False, "required": ["host"],
         "properties": {"name": STR, "env": {"type": "string", "enum": list(store.ENVS)}, "host": STR, "port": INT, "user": STR, "key": STR, "notes": STR}},
   response=PROFILE)
def put_server(req: Req) -> dict[str, Any]:
    res = guarded(store.save, {**req.body, "id": req.path["id"]})
    audit(req, "servers.save", server=res["id"], env=res["env"], host=res["host"])
    return res


@R("DELETE", "/servers/{id}", "Delete a profile and its pinned host key (the server itself is not touched)", "admin", **KW, errors=("not_found",), params={"id": "server id"},
   response={"type": "object", "required": ["deleted"], "properties": {"deleted": STR}})
def delete_server(req: Req) -> dict[str, Any]:
    guarded(store.delete, req.path["id"])
    audit(req, "servers.delete", server=req.path["id"])
    return {"deleted": req.path["id"]}


@R("POST", "/servers/{id}/hostkey/scan", "Show the host keys the server presents (SHA256 fingerprints) without trusting them", "admin", **KW,
   errors=("not_found", "transport"), params={"id": "server id"},
   response={"type": "object", "required": ["keys"], "properties": {"keys": {"type": "array"}, "pinned": {"type": "array", "items": STR}}})
def scan(req: Req) -> dict[str, Any]:
    keys = guarded(hostkey.scan, req.path["id"])
    return {"keys": [{"type": k["type"], "fingerprint": k["fingerprint"]} for k in keys], "pinned": store.get(req.path["id"])["fingerprints"]}


@R("POST", "/servers/{id}/hostkey/pin", "Pin the host key whose fingerprint equals `fingerprint` (compare it with the server console first); replacing a pin needs replace=true",
   "admin", **KW, errors=("not_found", "config", "transport"), params={"id": "server id"},
   body={"type": "object", "additionalProperties": False, "required": ["fingerprint"],
         "properties": {"fingerprint": {"type": "string", "pattern": "^SHA256:[A-Za-z0-9+/]{43}$"}, "replace": BOOL}}, response=PROFILE)
def pin(req: Req) -> dict[str, Any]:
    res = guarded(hostkey.pin, req.path["id"], req.body["fingerprint"], replace=req.body.get("replace") is True)
    audit(req, "servers.pin", server=res["id"], fingerprints=res["fingerprints"], replace=req.body.get("replace") is True)
    return res


@R("POST", "/servers/{id}/probe", "Connect with the profile's own credentials and pinned key; reports reachability, OS, sudo, load and free disk", "read", **KW,
   errors=("not_found", "config"), params={"id": "server id"},
   response={"type": "object", "required": ["id", "reachable"], "properties": {"id": STR, "reachable": BOOL, "error": STR, "seconds": {"type": "number"}, "facts": {"type": "object"}}})
def probe(req: Req) -> dict[str, Any]:
    return guarded(service.probe, req.path["id"])


@R("POST", "/servers/compare", "Compare 2-6 servers: OS, versions, service states and configuration hashes; `differs` marks drift", "read", **KW, errors=("not_found", "config"),
   body={"type": "object", "additionalProperties": False, "required": ["ids"],
         "properties": {"ids": {"type": "array", "items": ID, "minItems": 2, "maxItems": service.MAX_COMPARE}}},
   response={"type": "object", "required": ["servers", "rows", "drift", "complete"], "properties": {
       "servers": {"type": "array"}, "rows": {"type": "array"}, "drift": INT, "complete": BOOL}})
def compare(req: Req) -> dict[str, Any]:
    return guarded(service.compare, req.body["ids"])


@R("GET", "/servers/{id}/history", "Operations (deploy, service control, edits ...) the journal recorded against this server", "read", **KW, errors=("not_found",),
   params={"id": "server id"}, response={"type": "array", "items": {"type": "object"}})
def history(req: Req) -> list[dict[str, Any]]:
    return guarded(service.history, req.path["id"])
