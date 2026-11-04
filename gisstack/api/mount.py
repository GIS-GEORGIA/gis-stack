"""Flask integration: turns the route table into /api/v1 endpoints - authorisation scope, request validation, envelope, error mapping, audit."""
from __future__ import annotations

import logging
import secrets as _secrets
from dataclasses import dataclass
from typing import Any, Callable, Optional

from flask import Flask, Response, jsonify, make_response, request

from ..core import features, log
from ..core.errors import GisStackError
from . import API_VERSION
from .auth import Principal
from .registry import PREFIX, ApiError, Registry, Route, scope_allows
from .schema import coerce_query, validate

_log = log.get("api")


@dataclass
class Req:
    """What a handler gets: already validated input and the authenticated principal."""
    body: dict[str, Any]
    query: dict[str, Any]
    path: dict[str, str]
    principal: Principal
    app: Flask


def envelope(data: Any = None, error: Optional[ApiError] = None, status: int = 200) -> Response:
    request_id = _secrets.token_hex(4)
    payload = {"data": data if error is None else None,
               "error": None if error is None else {"message": error.message, "code": error.code, **({"details": error.details} if error.details else {})},
               "meta": {"version": API_VERSION, "request_id": request_id}}
    resp = make_response(jsonify(payload), status)
    resp.headers["X-Request-Id"] = request_id
    return resp


def _error(e: ApiError) -> Response:
    return envelope(error=e, status=e.status)


def mount(app: Flask, registry: Registry, version: str) -> None:
    def make_view(route: Route) -> Callable[..., Response]:
        def view(**path_args: str) -> Response:
            audit = app.config.get("AUDIT")
            principal: Optional[Principal] = getattr(request, "principal", None)
            if principal is None:                                   # the guard answers 401 first; this is belt and braces
                return _error(ApiError(401, "unauthorized", "authentication required"))
            if route.feature and not features.enabled(route.feature):
                return _error(ApiError(404, "feature_disabled", f"switched off (GISSTACK_FEATURES={route.feature})"))
            if not scope_allows(principal.scope, route.scope):
                if audit:
                    audit.write(event="denied", why="scope", ip=request.remote_addr or "?", method=request.method, path=request.path, status=403,
                                principal=principal.id, scope=principal.scope, needs=route.scope)
                return _error(ApiError(403, "forbidden", f"this token has scope '{principal.scope}'; '{route.scope}' is required"))
            body: dict[str, Any] = {}
            if route.body is not None:
                raw = request.get_json(silent=True)
                if not isinstance(raw, dict):
                    return _error(ApiError(400, "bad_request", "a JSON object body is required"))
                problems = validate(raw, route.body, registry.components)
                if problems:
                    return _error(ApiError(400, "bad_request", "the request is not valid", problems))
                body = raw
            query: dict[str, Any] = {}
            if route.query:
                query, problems = coerce_query(dict(request.args.items()), route.query)
                if not problems:
                    problems = [f"{k}: is required" for k in route.query.get("required", []) if k not in query]
                if not problems and route.query.get("additionalProperties") is False:
                    problems = [f"{k}: unknown parameter" for k in request.args if k not in route.query.get("properties", {})]
                if problems:
                    return _error(ApiError(400, "bad_request", "the query is not valid", problems))
            req = Req(body, query, path_args, principal, app)
            try:
                result = route.handler(req)
            except ApiError as e:
                return _error(e)
            except GisStackError as e:
                return _error(ApiError.from_exc(e))
            except Exception as e:  # noqa: BLE001 - never leak internals; the log has the traceback
                _log.exception("unhandled error in %s %s", route.method, route.path)
                return _error(ApiError(500, "internal", f"internal error ({type(e).__name__})"))
            if route.raw:
                return result if isinstance(result, Response) else make_response(result)
            if route.job:
                return envelope({"job": result})
            return envelope(result)
        view.__name__ = f"v1_{route.operation_id}"
        return view

    for r in registry.routes:
        app.add_url_rule(r.flask_path, endpoint=f"v1_{r.operation_id}", view_func=make_view(r), methods=[r.method])

    @app.errorhandler(404)
    def not_found(e: Any) -> Response:
        if request.path.startswith(PREFIX + "/") or request.path == PREFIX:
            return _error(ApiError(404, "not_found", "no such API endpoint"))
        return e if isinstance(e, Response) else make_response(getattr(e, "description", "Not Found"), 404)

    @app.errorhandler(405)
    def not_allowed(e: Any) -> Response:
        if request.path.startswith(PREFIX + "/"):
            return _error(ApiError(405, "bad_request", "method not allowed for this endpoint"))
        return make_response("Method Not Allowed", 405)

    @app.errorhandler(413)
    def too_large(e: Any) -> Response:
        if request.path.startswith(PREFIX + "/"):
            return _error(ApiError(413, "too_large", "the request body is too large"))
        return make_response("Payload Too Large", 413)
    logging.getLogger("gisstack.api").debug("mounted %d routes (api %s, app %s)", len(registry.routes), API_VERSION, version)
