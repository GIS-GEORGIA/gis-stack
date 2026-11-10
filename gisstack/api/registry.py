"""Route table and OpenAPI generation.

Every /api/v1 route is declared once with `@registry.route(...)`: method, path, required scope, request schemas and response schema.
The Flask mount (`mount.py`) enforces exactly this declaration and `openapi()` documents exactly this declaration."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..core.errors import GisStackError
from .schema import Schema

SCOPES = ("read", "operate", "admin")      # each scope includes the ones before it
PREFIX = "/api/v1"
HTTP_STATUS = {
    "config": 400, "secret": 400, "project": 400, "plan": 400, "deploy": 400, "rules": 400, "bad_request": 400, "validation": 400,
    "unauthorized": 401, "forbidden": 403, "not_found": 404, "feature_disabled": 404,
    "conflict": 409, "hostkey_unknown": 409, "hostkey_changed": 409, "not_ready": 409, "sql": 400, "postgis": 502, "mapproxy": 502, "preflight": 409, "busy": 409, "too_large": 413, "rate_limited": 429,
    "transport": 502, "upstream": 502, "geoserver": 502, "runner": 502, "update": 502, "notify": 502, "unreachable": 502, "timeout": 504, "store": 500, "error": 500, "internal": 500,
}


class ApiError(Exception):
    """An error with an HTTP status and a stable machine-readable code (the `error.code` of the envelope)."""

    def __init__(self, status: int, code: str, message: str, details: Optional[Any] = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details

    @classmethod
    def from_exc(cls, e: BaseException, status: Optional[int] = None) -> ApiError:
        code = e.code if isinstance(e, GisStackError) else "internal"
        return cls(status or HTTP_STATUS.get(code, 500), code, str(e))


def scope_allows(have: str, need: str) -> bool:
    return have in SCOPES and need in SCOPES and SCOPES.index(have) >= SCOPES.index(need)


@dataclass
class Route:
    method: str
    path: str                                  # relative to /api/v1, e.g. "/ops/status" or "/jobs/{id}"
    summary: str
    scope: str
    handler: Callable[..., Any]
    tags: tuple[str, ...] = ()
    description: str = ""
    body: Optional[Schema] = None              # request body schema
    query: Optional[Schema] = None             # query-string schema (object with scalar properties)
    response: Optional[Schema] = None          # schema of `data` in the envelope
    job: bool = False                          # starts a background job: data = {"job": "<id>"}
    raw: Optional[str] = None                  # "json" (no envelope), "binary" or "text" for non-envelope answers
    legacy: tuple[str, ...] = ()               # old /api/... paths this replaces (they stay, deprecated, for one release)
    errors: tuple[str, ...] = ()               # extra error codes this route can return (documentation)
    feature: Optional[str] = None              # feature flag that must be on (else 404 feature_disabled)
    params: dict[str, str] = field(default_factory=dict)   # path parameter -> description

    @property
    def full_path(self) -> str:
        return PREFIX + self.path

    @property
    def flask_path(self) -> str:
        return PREFIX + re.sub(r"\{(\w+)\}", r"<\1>", self.path)

    @property
    def path_params(self) -> list[str]:
        return re.findall(r"\{(\w+)\}", self.path)

    @property
    def operation_id(self) -> str:
        name = re.sub(r"[^A-Za-z0-9]+", "_", f"{self.method.lower()}_{self.path}").strip("_")
        return name


class Registry:
    def __init__(self) -> None:
        self.routes: list[Route] = []
        self.components: dict[str, Schema] = {}

    def component(self, name: str, schema: Schema) -> Schema:
        self.components[name] = schema
        return {"$ref": f"#/components/schemas/{name}"}

    def route(self, method: str, path: str, summary: str, scope: str, **kw: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        if scope not in SCOPES:
            raise ValueError(f"unknown scope {scope!r}")
        if method not in ("GET", "POST", "PUT", "DELETE"):
            raise ValueError(f"unsupported method {method!r}")

        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            if any(r.method == method and r.path == path for r in self.routes):
                raise ValueError(f"duplicate route {method} {path}")
            self.routes.append(Route(method, path, summary, scope, fn, **kw))
            return fn
        return deco

    def find(self, method: str, path: str) -> Optional[Route]:
        return next((r for r in self.routes if r.method == method and r.path == path), None)

    # ------------------------------------------------------------------ OpenAPI 3.1
    def openapi(self, version: str, server: str = "/") -> dict[str, Any]:
        comps: dict[str, Schema] = {
            "Error": {"type": "object", "required": ["message", "code"], "properties": {"message": {"type": "string"}, "code": {"type": "string"},
                                                                                         "details": {}}},
            "Meta": {"type": "object", "properties": {"version": {"type": "integer", "const": 1}, "request_id": {"type": "string"}}, "additionalProperties": True},
            **self.components,
        }
        paths: dict[str, Any] = {}
        for r in sorted(self.routes, key=lambda r: (r.path, r.method)):
            op: dict[str, Any] = {"operationId": r.operation_id, "summary": r.summary, "tags": list(r.tags) or ["general"], "x-required-scope": r.scope,
                                  "security": [{"bearer": []}, {"session": []}]}
            if r.description:
                op["description"] = r.description
            if r.legacy:
                op["x-replaces"] = list(r.legacy)
            params: list[dict[str, Any]] = [{"name": n, "in": "path", "required": True, "schema": {"type": "string"}, "description": r.params.get(n, "")} for n in r.path_params]
            if r.query:
                for k, s in r.query.get("properties", {}).items():
                    params.append({"name": k, "in": "query", "required": k in r.query.get("required", []), "schema": s})
            if params:
                op["parameters"] = params
            if r.body is not None:
                op["requestBody"] = {"required": True, "content": {"application/json": {"schema": r.body}}}
            data = {"type": "object", "required": ["job"], "properties": {"job": {"type": "string"}}} if r.job else (r.response or {})
            if r.raw == "binary":
                ok: dict[str, Any] = {"description": "binary download", "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}}
            elif r.raw == "text":
                ok = {"description": "plain text", "content": {"text/plain": {"schema": {"type": "string"}}}}
            elif r.raw == "json":
                ok = {"description": "JSON document (no envelope)", "content": {"application/json": {"schema": r.response or {"type": "object"}}}}
            else:
                ok = {"description": "success", "content": {"application/json": {"schema": {
                    "type": "object", "required": ["data", "error", "meta"],
                    "properties": {"data": data, "error": {"type": "null"}, "meta": {"$ref": "#/components/schemas/Meta"}}}}}}
            err = {"description": "error", "content": {"application/json": {"schema": {
                "type": "object", "required": ["data", "error", "meta"],
                "properties": {"data": {"type": "null"}, "error": {"$ref": "#/components/schemas/Error"}, "meta": {"$ref": "#/components/schemas/Meta"}}}}}}
            responses: dict[str, Any] = {"200": ok, "401": {**err, "description": "authentication required"}, "403": {**err, "description": f"token scope below '{r.scope}'"}}
            if r.body is not None or r.query:
                responses["400"] = {**err, "description": "invalid request (error.details lists every problem)"}
            if r.job:
                responses["409"] = {**err, "description": "another operation is already running"}
            for code in r.errors:
                responses.setdefault(str(HTTP_STATUS.get(code, 500)), {**err, "description": code})
            op["responses"] = responses
            paths.setdefault(r.path, {})[r.method.lower()] = op
        return {
            "openapi": "3.1.0",
            "info": {"title": "GIS Stack API", "version": version, "description": "Versioned HTTP API of the GIS Stack installer and operator. Every JSON answer is {data, error, meta}; errors carry a stable `code`."},
            "servers": [{"url": server.rstrip("/") + PREFIX}],
            "tags": [{"name": t} for t in sorted({t for r in self.routes for t in (r.tags or ("general",))})],
            "paths": paths,
            "components": {
                "schemas": comps,
                "securitySchemes": {
                    "bearer": {"type": "http", "scheme": "bearer", "description": "An API token (gisstack token create) or the web access token."},
                    "session": {"type": "apiKey", "in": "cookie", "name": "gs_session", "description": "Browser session created by opening the printed link."},
                },
            },
        }


registry = Registry()
