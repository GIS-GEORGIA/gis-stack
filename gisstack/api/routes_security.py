"""/api/v1 routes of the Security page: permissions posture of this machine and the audit trail.  Both need `admin` because the audit
log names principals and hosts; neither returns secret values (the audit log never stores them)."""
from __future__ import annotations

from typing import Any

from .. import security_report
from .mount import Req
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
