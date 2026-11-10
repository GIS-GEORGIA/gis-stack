"""/api/v1 routes of the Security page: permissions posture of this machine and the audit trail.  Both need `admin` because the audit
log names principals and hosts; neither returns secret values (the audit log never stores them)."""
from __future__ import annotations

from typing import Any

from flask import Response, request

from .. import security_report
from .mount import Req, envelope
from .registry import registry
from .routes import STR

R = registry.route
KW: dict[str, Any] = {"tags": ("security",)}


@R("GET", "/security/posture", "Permissions and policy checks for the machine that runs gis-stack (read-only)", "admin", **KW,
   response={"type": "array", "items": {"type": "object", "required": ["id", "label", "status", "detail"], "properties": {
       "id": STR, "label": STR, "status": {"type": "string", "enum": ["ok", "warn", "fail", "info"]}, "detail": STR, "path": STR}}})
def posture(req: Req) -> list[dict[str, Any]]:
    return security_report.posture()


@R("GET", "/security/audit", "Newest administrative events from the audit log (optionally only those starting with a prefix such as postgis.)", "admin", **KW,
   query={"type": "object", "additionalProperties": False, "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": security_report.MAX_EVENTS}, "event": STR}},
   response={"type": "array", "items": {"type": "object"}})
def audit(req: Req) -> list[dict[str, Any]]:
    return security_report.audit_events(int(req.query.get("limit", 100)), str(req.query.get("event", "")))


@R("POST", "/session/logout", "End the browser session of the caller: the session is revoked on the server and its cookie removed (a bearer token is not a session and is unaffected)",
   "read", tags=("security",), legacy=("/api/logout",), raw="json",
   response={"type": "object", "required": ["revoked"], "properties": {"revoked": {"type": "boolean"}}})
def logout(req: Req) -> Response:
    # `read` is enough: ending your own session is not a privilege.  The legacy route is deprecated in favour of this one.
    cookie = str(req.app.config.get("SESSION_COOKIE", "gs_session"))
    sid = request.cookies.get(cookie, "")
    revoked = bool(sid) and req.principal.kind == "session"
    if revoked:
        req.app.config["SESSIONS"].revoke(sid)
    resp = envelope({"revoked": revoked})
    if sid:
        resp.delete_cookie(cookie)
    return resp
