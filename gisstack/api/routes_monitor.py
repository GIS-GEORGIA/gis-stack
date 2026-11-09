"""/api/v1 routes of the monitoring and dashboard features (each behind its feature flag)."""
from __future__ import annotations

from typing import Any

from .. import journal, ops, transport
from ..core import features, paths
from ..core.errors import DeployError
from ..monitoring import alerts as alerts_mod, rulestore, service
from ..monitoring.store import Store, StoreError
from .mount import Req
from .registry import ApiError, registry
from .routes import INT, OBJ, STR, STRLIST, STATUS, body, lines_job, machine, remote  # noqa: F401
from .schema import Schema

R = registry.route
PROJECT: Schema = {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,30}$"}
HOST: Schema = {"type": "string", "pattern": r"^[A-Za-z0-9._:\[\]-]{1,100}$", "maxLength": 100}
MACHINE_Q: dict[str, Schema] = {"project": PROJECT, "host": HOST}


def store_of(req: Req) -> Store:
    """Open an existing history (a GET never creates one)."""
    project, host = req.query["project"], req.query.get("host", "localhost")
    db = paths.monitor_db(project, host)
    if not db.exists():
        raise ApiError(404, "not_found", "no monitoring history for this machine yet")
    try:
        return Store(db)
    except StoreError as e:
        raise ApiError(500, "store", str(e)) from e


SNAPSHOT: Schema = {"type": "object", "properties": {"ts": {"type": ["number", "null"]}, "age_seconds": {"type": ["number", "null"]}, "metrics": OBJ,
                                                     "alerts": {"type": "array"}, "availability": OBJ}}


@R("POST", "/monitor/collect", "Take one real sample now, store it, evaluate alerts (and notify)", "operate", tags=("monitoring",), feature="monitoring", errors=("transport",),
   body=body(), response={"type": "object", "properties": {"snapshot": SNAPSHOT, "notifications": {"type": "array"}, "errors": STRLIST}})
def collect_now(req: Req) -> dict[str, Any]:
    cfg, mode, ssh, target = machine(req)
    mon = service.Monitor(cfg, mode, ssh, target)
    try:
        res = mon.tick()
        return {"snapshot": service.snapshot(mon.store), "notifications": res["notifications"], "errors": res["errors"]}
    except DeployError as e:
        raise ApiError(502, e.code, str(e)) from e
    finally:
        mon.store.close()


@R("GET", "/metrics", "Newest stored sample, firing alerts and availability", "read", tags=("monitoring",), feature="monitoring", errors=("not_found",),
   query={"type": "object", "required": ["project"], "properties": MACHINE_Q, "additionalProperties": False}, response=SNAPSHOT)
def metrics(req: Req) -> dict[str, Any]:
    st = store_of(req)
    try:
        return service.snapshot(st)
    finally:
        st.close()


@R("GET", "/metrics/history", "Stored values of one metric", "read", tags=("monitoring",), feature="monitoring", errors=("not_found",),
   query={"type": "object", "required": ["project", "name"], "additionalProperties": False, "properties": {
       **MACHINE_Q, "name": {"type": "string", "minLength": 1, "maxLength": 100}, "since": {"type": "number", "minimum": 0}, "step": {"type": "number", "minimum": 1},
       "limit": {"type": "integer", "minimum": 1, "maximum": 20000}, "labels": {"type": "string", "description": "JSON object of label filters"}}},
   response={"type": "array", "items": {"type": "object", "required": ["ts", "value"], "properties": {"ts": {"type": "number"}, "labels": OBJ, "value": {"type": "number"}}}})
def history(req: Req) -> list[dict[str, Any]]:
    import json
    import time
    q = req.query
    labels = None
    if "labels" in q:
        try:
            labels = json.loads(q["labels"])
        except ValueError as e:
            raise ApiError(400, "bad_request", "labels must be a JSON object") from e
        if not (isinstance(labels, dict) and all(isinstance(v, str) for v in labels.values())):
            raise ApiError(400, "bad_request", "labels must be an object of strings")
    st = store_of(req)
    try:
        return st.series(q["name"], labels, since=time.time() - float(q.get("since", 3600)), step=q.get("step"), limit=int(q.get("limit", 5000)))
    finally:
        st.close()


@R("GET", "/alerts", "Alert state and recent events", "read", tags=("monitoring",), feature="monitoring", errors=("not_found",),
   query={"type": "object", "required": ["project"], "properties": MACHINE_Q, "additionalProperties": False},
   response={"type": "object", "properties": {"alerts": {"type": "array"}, "events": {"type": "array"}}})
def alerts(req: Req) -> dict[str, Any]:
    st = store_of(req)
    try:
        return {"alerts": st.alert_state(), "events": st.events(limit=100)}
    finally:
        st.close()


RULE: Schema = {"type": "object", "required": ["id", "builtin", "metric", "op", "threshold", "for", "level", "enabled", "text", "modified"], "properties": {
    "id": STR, "builtin": {"type": "boolean"}, "metric": STR, "op": STR, "text": STR, "description": STR, "threshold": {"type": "number"}, "for": INT,
    "level": {"type": "string", "enum": ["warn", "crit"]}, "enabled": {"type": "boolean"}, "renotify": {"type": "number"}, "default": OBJ, "modified": {"type": "boolean"}}}
RULES: Schema = {"type": "object", "required": ["revision", "rules"], "properties": {"revision": STR, "modified": {"type": ["number", "null"]}, "metrics": STRLIST, "rules": {"type": "array", "items": RULE}}}
RID: Schema = {"type": "string", "pattern": "^[a-z][a-z0-9_]{2,40}$"}
REV: Schema = {"type": "string", "pattern": "^[0-9a-f]{1,64}$"}
RULE_FIELDS: dict[str, Schema] = {
    "metric": {"type": "string", "enum": list(alerts_mod.KNOWN_METRICS)}, "op": {"type": "string", "enum": list(alerts_mod.OPS)}, "threshold": {"type": "number"},
    "for": {"type": "integer", "minimum": 1, "maximum": 1000}, "level": {"type": "string", "enum": ["warn", "crit"]}, "text": {"type": "string", "maxLength": 200},
    "description": {"type": "string", "maxLength": 200}, "enabled": {"type": "boolean"}, "renotify": {"type": "number", "minimum": 0, "maximum": alerts_mod.MAX_RENOTIFY}}
RULE_ERRORS = ("rules", "conflict", "not_found")


def rules_call(req: Req, event: str, fn: Any, *args: Any) -> dict[str, Any]:
    """Run one rules change, translate its errors, and audit it (the audit line names the rule and the fields, never a notifier secret)."""
    try:
        res = fn(*args)
    except (alerts_mod.RulesError, rulestore.RulesConflict, rulestore.RulesNotFound) as e:
        req.app.config["AUDIT"].write(event=event, principal=req.principal.id, scope=req.principal.scope, rule=req.path.get("id") or req.body.get("id"), ok=False, error=e.code)
        raise ApiError.from_exc(e) from e
    req.app.config["AUDIT"].write(event=event, principal=req.principal.id, scope=req.principal.scope, rule=req.path.get("id") or req.body.get("id"), ok=True,
                                  fields=sorted(k for k in req.body if k not in ("revision", "id")), revision=res["revision"])
    return res


@R("GET", "/monitor/rules", "Alert rules (built-in and custom) with effective values, the current revision and last modification time", "read", tags=("monitoring",),
   feature="monitoring", errors=("rules",), response=RULES)
def rules_get(req: Req) -> dict[str, Any]:
    try:
        return rulestore.view()
    except alerts_mod.RulesError as e:
        raise ApiError.from_exc(e) from e


@R("POST", "/monitor/rules", "Create a custom alert rule (id must be new; `revision` is the one read from GET)", "admin", tags=("monitoring",), feature="monitoring",
   errors=RULE_ERRORS, body={"type": "object", "additionalProperties": False, "required": ["revision", "id", "metric", "op", "threshold", "for", "level", "text"],
                             "properties": {"revision": REV, "id": RID, **RULE_FIELDS}}, response=RULES)
def rules_create(req: Req) -> dict[str, Any]:
    rule = {k: v for k, v in req.body.items() if k not in ("revision", "id")}
    return rules_call(req, "monitor.rule.create", lambda: rulestore.create(req.body["id"], rule, req.body["revision"]))


@R("PUT", "/monitor/rules/{id}", "Change a rule. Built-in: threshold, for, level, enabled, renotify (or reset=true to restore the defaults). Custom: any field",
   "admin", tags=("monitoring",), feature="monitoring", errors=RULE_ERRORS, params={"id": "rule id"},
   body={"type": "object", "additionalProperties": False, "required": ["revision"], "properties": {"revision": REV, "reset": {"type": "boolean"}, **RULE_FIELDS}},
   response=RULES)
def rules_update(req: Req) -> dict[str, Any]:
    rid = req.path["id"]
    fields = {k: v for k, v in req.body.items() if k not in ("revision", "reset")}
    if req.body.get("reset"):
        return rules_call(req, "monitor.rule.reset", lambda: rulestore.reset(rid, req.body["revision"]))
    return rules_call(req, "monitor.rule.update", lambda: rulestore.update(rid, fields, req.body["revision"]))


@R("DELETE", "/monitor/rules/{id}", "Delete a custom rule (built-in rules can only be disabled)", "admin", tags=("monitoring",), feature="monitoring", errors=RULE_ERRORS,
   params={"id": "rule id"}, query={"type": "object", "additionalProperties": False, "required": ["revision"], "properties": {"revision": REV}}, response=RULES)
def rules_delete(req: Req) -> dict[str, Any]:
    return rules_call(req, "monitor.rule.delete", lambda: rulestore.delete(req.path["id"], req.query["revision"]))


# --------------------------------------------------------------------------- dashboard
@R("GET", "/dashboard/config", "What the dashboard needs to start: version, features, projects, settings", "read", tags=("dashboard",), feature="dashboard",
   response={"type": "object", "required": ["version", "features"], "properties": {"version": STR, "features": STRLIST, "is_root": {"type": "boolean"},
                                                                                   "projects": STRLIST, "settings": OBJ}})
def dash_config(req: Req) -> dict[str, Any]:
    import os

    from .. import projects, settings
    from .. import __version__
    return {"version": __version__, "features": sorted(features.enabled_set()), "is_root": hasattr(os, "geteuid") and os.geteuid() == 0,
            "projects": projects.list_projects(), "settings": settings.view()}


@R("POST", "/dashboard/summary", "Live status + newest stored metrics + recent operations in one call (each part reports its own error)", "read", tags=("dashboard",),
   feature="dashboard", body=body(), response={"type": "object", "required": ["project", "target", "errors"], "properties": {
       "project": STR, "target": STR, "mode": STR, "domain": STR, "status": OBJ, "monitoring": OBJ,
       "operations": {"type": "array"}, "errors": OBJ}})
def dash_summary(req: Req) -> dict[str, Any]:
    cfg, mode, ssh, target = machine(req)
    out: dict[str, Any] = {"project": cfg["project"], "target": target, "mode": mode, "domain": cfg["domain"], "errors": {}}
    try:
        out["status"] = ops.status(cfg, mode, ssh, target)
    except DeployError as e:
        out["status"], out["errors"]["status"] = None, {"message": str(e), "code": e.code}
    out["monitoring"] = None
    if features.enabled("monitoring"):
        db = paths.monitor_db(cfg["project"], transport.host_label(mode, ssh))
        if db.exists():
            try:
                st = Store(db)
                try:
                    out["monitoring"] = service.snapshot(st)
                finally:
                    st.close()
            except StoreError as e:
                out["errors"]["monitoring"] = {"message": str(e), "code": e.code}
    out["operations"] = journal.operations(10, cfg["project"])
    return out
