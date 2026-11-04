"""/api/v1 routes of the monitoring and dashboard features (each behind its feature flag)."""
from __future__ import annotations

from typing import Any

from .. import journal, ops, transport
from ..core import features, paths
from ..core.errors import DeployError
from ..monitoring import service
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
