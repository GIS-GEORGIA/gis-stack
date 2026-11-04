"""All /api/v1 routes.  Each handler is a thin wrapper over the same functions the CLI and the desktop GUI use (config, ops, deploy,
backup, rotation, monitoring ...), so the three interfaces cannot disagree about behaviour or safety rules."""
from __future__ import annotations

import io
import itertools
import os
import tempfile
from pathlib import Path
from typing import Any, Iterator, Optional

from flask import Response, make_response

from .. import (__version__, backup as backup_mod, catalog, config, deploy, discover, existing, generate, i18n, journal, ops, preflight as preflight_mod,
                projects, rotation, services, settings, updater)
from ..core import features, paths
from ..core.errors import ConfigError, DeployError, GisStackError, PlanError, ProjectError
from . import jobs
from .mount import Req
from .registry import ApiError, SCOPES, registry
from .schema import Schema

R = registry.route
MASK = "••••••••"
MAX_PREVIEW_BYTES = 120_000

# --------------------------------------------------------------------------- shared schemas
STR: Schema = {"type": "string"}
BOOL: Schema = {"type": "boolean"}
INT: Schema = {"type": "integer"}
OBJ: Schema = {"type": "object"}
STRLIST: Schema = {"type": "array", "items": STR}
SSH: Schema = registry.component("Ssh", {"type": "object", "properties": {"host": STR, "port": {"type": ["integer", "string"]}, "user": STR, "key": STR},
                                         "additionalProperties": False})
CONFIG: Schema = registry.component("Config", {"type": "object", "description": "A gis-stack configuration (see /catalog for the defaults); sparse is fine."})
MODE: Schema = {"type": "string", "enum": ["local", "ssh"]}
COMPONENT: Schema = {"type": "string", "enum": list(deploy.CLEAN_COMPONENTS)}
JOB_ID: Schema = {"type": "string", "pattern": "^[0-9a-f]{12}$"}
BACKUP_ID: Schema = {"type": "string", "pattern": r"^(pre-)?\d{8}-\d{6}-[0-9a-f]{8}$"}
ROW: Schema = registry.component("ServiceRow", {"type": "object", "required": ["id", "state"], "properties": {
    "id": STR, "name": STR, "state": {"type": "string", "enum": ["running", "stopped", "starting", "stopping", "failed", "missing", "unknown"]},
    "detail": STR, "enabled": {"type": ["boolean", "null"]}, "pid": {"type": ["integer", "null"]}, "since": STR, "restarts": {"type": ["integer", "null"]},
    "listen": STRLIST, "urls": STRLIST, "checks": {"type": "array"}, "ok": BOOL, "unit": STR}})
STATUS: Schema = registry.component("Status", {"type": "object", "required": ["target", "rows"], "properties": {
    "target": STR, "project": STR, "domain": STR, "time": STR, "took": {"type": "number"}, "rows": {"type": "array", "items": ROW}}})
JOB_DATA: Schema = {"type": "object", "required": ["job"], "properties": {"job": JOB_ID}}


def body(props: Optional[dict[str, Schema]] = None, required: tuple[str, ...] = ()) -> Schema:
    """Request body of a route that talks about a machine: {config?, mode, ssh?} plus the route's own fields."""
    return {"type": "object", "required": ["mode", *required], "additionalProperties": False,
            "properties": {"config": CONFIG, "mode": MODE, "ssh": SSH, **(props or {})}}


# --------------------------------------------------------------------------- helpers
def cfg_of(req: Req, persist: bool = False) -> dict[str, Any]:
    try:
        return config.resolve(req.body.get("config") or {}, persist_secrets=persist, generate_secrets=persist)
    except ConfigError as e:
        raise ApiError(400, "config", str(e)) from e


def machine(req: Req, persist: bool = False) -> tuple[dict[str, Any], str, dict[str, Any], str]:
    cfg = cfg_of(req, persist)
    return cfg, req.body["mode"], req.body.get("ssh") or {}, cfg["deploy_target"]


def remote(fn: Any, *a: Any, **kw: Any) -> Any:
    """Run something that talks to the machine: DeployError means "could not do it there" -> 502 (unless it is a plain input error)."""
    try:
        return fn(*a, **kw)
    except DeployError as e:
        raise ApiError.from_exc(e, 400 if e.code in ("deploy", "plan") and not isinstance(e, PlanError) and "command failed" not in str(e) else None) from e


def need_native(cfg: dict[str, Any]) -> None:
    if cfg["deploy_target"] != "native":
        raise ApiError(400, "deploy", "this needs the native target")


def local_problems(target: str, mode: str) -> None:
    problems = deploy.preflight(target, mode)
    if problems:
        raise ApiError(400, "deploy", "; ".join(problems))


def start_job(req: Req, steps: Any, **kw: Any) -> str:
    return jobs.start(req.app, steps, **kw).id


def lines_job(req: Req, gen_factory: Any, **kw: Any) -> str:
    """Validate eagerly (the generator raises before its first line), then run it as a locked job."""
    try:
        gen = gen_factory()
        first = next(gen, None)
    except GisStackError as e:
        raise ApiError.from_exc(e, 400) from e
    return start_job(req, lambda job, bundle: itertools.chain([first] if first is not None else [], gen), **kw)


def feature_gate(name: str) -> None:
    if not features.enabled(name):
        raise ApiError(404, "feature_disabled", f"switched off (GISSTACK_FEATURES={name})")


# --------------------------------------------------------------------------- meta
@R("GET", "/meta", "Version, enabled features, and who you are", "read", tags=("meta",),
   response={"type": "object", "required": ["version", "api_version", "features", "scopes", "principal"], "properties": {
       "version": STR, "api_version": INT, "features": STRLIST, "scopes": STRLIST, "is_root": BOOL,
       "principal": {"type": "object", "properties": {"id": STR, "name": STR, "scope": STR, "kind": STR}}}})
def meta(req: Req) -> dict[str, Any]:
    p = req.principal
    return {"version": __version__, "api_version": 1, "features": sorted(features.enabled_set()), "scopes": list(SCOPES),
            "is_root": hasattr(os, "geteuid") and os.geteuid() == 0,
            "principal": {"id": p.id, "name": p.name, "scope": p.scope, "kind": p.kind}}


@R("GET", "/openapi.json", "This API as an OpenAPI 3.1 document", "read", tags=("meta",), raw="json")
def openapi_doc(req: Req) -> Response:
    from flask import jsonify
    return jsonify(registry.openapi(__version__))


@R("GET", "/catalog", "What can be installed: components, versions, extensions, defaults", "read", tags=("config",), legacy=("/api/catalog",),
   response={"type": "object", "required": ["catalog", "defaults"], "properties": {"catalog": OBJ, "defaults": CONFIG, "is_root": BOOL}})
def get_catalog(req: Req) -> dict[str, Any]:
    d = config.default_config()
    for sec, key in (("postgres", "password"), ("geoserver", "admin_password"), ("tomcat", "manager_password")):
        d[sec][key] = ""
    return {"catalog": catalog.ui_catalog(), "defaults": d, "is_root": hasattr(os, "geteuid") and os.geteuid() == 0}


@R("GET", "/i18n", "Georgian -> English translation table used by every interface", "read", tags=("meta",), legacy=("/api/i18n",),
   response={"type": "object", "required": ["en"], "properties": {"en": OBJ}})
def get_i18n(req: Req) -> dict[str, Any]:
    return {"en": i18n.EN}


@R("GET", "/settings", "Interface language and theme (shared by the web and desktop GUI)", "read", tags=("settings",), legacy=("/api/settings",),
   response={"type": "object", "properties": {"lang": STR, "theme": STR}})
def get_settings(req: Req) -> dict[str, Any]:
    return settings.view()


@R("PUT", "/settings", "Change the interface language and/or theme", "admin", tags=("settings",), legacy=("/api/settings",),
   body={"type": "object", "additionalProperties": False, "properties": {"lang": {"type": "string", "enum": list(settings.LANGS)},
                                                                         "theme": {"type": "string", "enum": list(settings.THEMES)}}},
   response={"type": "object", "properties": {"lang": STR, "theme": STR}})
def put_settings(req: Req) -> dict[str, Any]:
    try:
        settings.save(req.body.get("lang"), req.body.get("theme"))
    except ValueError as e:
        raise ApiError(400, "bad_request", str(e)) from e
    return settings.view()


# --------------------------------------------------------------------------- configuration and projects
@R("POST", "/config/validate", "Validate a configuration without touching anything", "read", tags=("config",),
   body={"type": "object", "required": ["config"], "additionalProperties": False, "properties": {"config": CONFIG}},
   response={"type": "object", "required": ["valid"], "properties": {"valid": BOOL, "errors": STRLIST, "warnings": STRLIST, "deploy_target": STR,
                                                                      "components": {"type": "array", "items": STR}}})
def validate_config(req: Req) -> dict[str, Any]:
    try:
        cfg = config.resolve(req.body["config"], generate_secrets=False)
    except ConfigError as e:
        return {"valid": False, "errors": str(e).splitlines(), "warnings": []}
    return {"valid": True, "errors": [], "warnings": cfg["_meta"]["warnings"], "deploy_target": cfg["deploy_target"], "components": config.selected(cfg)}


@R("GET", "/projects", "Saved projects (configurations)", "read", tags=("config",), legacy=("/api/projects",),
   response={"type": "object", "required": ["projects"], "properties": {"projects": STRLIST}})
def list_projects(req: Req) -> dict[str, Any]:
    return {"projects": projects.list_projects()}


@R("GET", "/projects/{name}", "One saved project, complete with defaults", "read", tags=("config",), legacy=("/api/projects/<name>",), errors=("not_found",),
   params={"name": "project name"}, response={"type": "object", "required": ["config"], "properties": {"config": CONFIG}})
def get_project(req: Req) -> dict[str, Any]:
    try:
        return {"config": projects.load_full(req.path["name"])}
    except ProjectError as e:
        raise ApiError(404, "not_found", str(e)) from e


@R("PUT", "/projects/{name}", "Save a project, or only one program's settings of it", "admin", tags=("config",), legacy=("/api/save",),
   params={"name": "project name"}, body={"type": "object", "required": ["config"], "additionalProperties": False, "properties": {"config": CONFIG, "component": COMPONENT}},
   response={"type": "object", "required": ["saved"], "properties": {"saved": STR}})
def put_project(req: Req) -> dict[str, Any]:
    given = req.body["config"]
    raw = dict(given, project=req.path["name"])
    try:
        config.resolve(raw, generate_secrets=False)                     # validation, with the reason
        comp = req.body.get("component")
        path = projects.save(raw) if comp is None else projects.save_component(raw, comp)
    except ConfigError as e:
        raise ApiError(400, "config", str(e)) from e
    except ProjectError as e:
        raise ApiError(400, "project", str(e)) from e
    return {"saved": str(path)}


@R("POST", "/bundle/preview", "Render the files of a bundle (passwords masked)", "read", tags=("config",), legacy=("/api/preview",),
   body={"type": "object", "required": [], "additionalProperties": False, "properties": {"config": CONFIG}},
   response={"type": "object", "required": ["files"], "properties": {"files": OBJ, "warnings": STRLIST}})
def preview(req: Req) -> dict[str, Any]:
    cfg = cfg_of(req, persist=False)
    mask = [cfg["postgres"]["password"], cfg["geoserver"]["admin_password"], cfg["tomcat"]["manager_password"]]
    files: dict[str, str] = {}
    with tempfile.TemporaryDirectory() as td:
        out = generate.generate_bundle(cfg, Path(td) / "bundle")
        for p in sorted(out.rglob("*")):
            if not p.is_file() or "vendor" in p.parts or p.suffix in (".png", ".tgz") or p.stat().st_size > MAX_PREVIEW_BYTES:
                continue
            txt = p.read_text(encoding="utf-8", errors="replace")
            for s in mask:
                if s:
                    txt = txt.replace(s, MASK)
            files[p.relative_to(out).as_posix()] = txt
    return {"files": files, "warnings": cfg["_meta"]["warnings"]}


@R("POST", "/bundle", "Download the bundle as a zip (contains the generated passwords)", "admin", tags=("config",), legacy=("/api/bundle",), raw="binary",
   body={"type": "object", "required": [], "additionalProperties": False, "properties": {"config": CONFIG}})
def bundle(req: Req) -> Response:
    cfg = cfg_of(req, persist=True)
    with tempfile.TemporaryDirectory() as td:
        out = generate.generate_bundle(cfg, Path(td) / f"gisstack-{cfg['project']}")
        data = generate.bundle_zip_bytes(out)
    resp = make_response(io.BytesIO(data).getvalue())
    resp.headers["Content-Type"] = "application/zip"
    resp.headers["Content-Disposition"] = f'attachment; filename="gisstack-{cfg["project"]}.zip"'
    return resp


# --------------------------------------------------------------------------- operating a stack
@R("POST", "/ops/status", "Live state of every program", "read", tags=("operate",), legacy=("/api/ops/status",), errors=("transport",),
   body=body({"components": STRLIST}), response=STATUS)
def ops_status(req: Req) -> dict[str, Any]:
    cfg, mode, ssh, target = machine(req)
    return remote(ops.status, cfg, mode, ssh, target, req.body.get("components") or None)


@R("POST", "/ops/preflight", "Can the machine take the selected stack? (OS, memory, disk, network, clock)", "read", tags=("operate",), legacy=("/api/preflight",),
   errors=("transport",), body=body(), response={"type": "object", "required": ["ok", "checks"], "properties": {
       "ok": BOOL, "target": STR, "checks": {"type": "array", "items": {"type": "object", "required": ["id", "status", "message"],
                                                                         "properties": {"id": STR, "status": {"type": "string", "enum": ["ok", "warn", "fail"]},
                                                                                        "message": STR, "detail": STR}}}, "facts": OBJ}})
def ops_preflight(req: Req) -> dict[str, Any]:
    cfg, mode, ssh, target = machine(req)
    return remote(preflight_mod.run, cfg, mode, ssh, target)


@R("POST", "/ops/control", "Start / stop / restart / enable / disable programs (job)", "operate", tags=("operate",), legacy=("/api/ops/control",), job=True, errors=("busy",),
   body=body({"action": {"type": "string", "enum": list(ops.ACTIONS)}, "components": {"type": ["array", "null"], "items": COMPONENT}}, ("action",)))
def ops_control(req: Req) -> str:
    cfg, mode, ssh, target = machine(req)
    comps = req.body.get("components")
    if comps is not None and not comps:
        raise ApiError(400, "bad_request", "components must be null or a non-empty list")
    if mode == "local" and target == "native":
        local_problems("native", "local")
    return lines_job(req, lambda: ops.control(cfg, mode, ssh, req.body["action"], comps, target))


@R("POST", "/ops/logs", "Read or follow a program's log (job; cancel it to stop following)", "read", tags=("operate",), legacy=("/api/ops/logs",), job=True,
   body=body({"component": {"type": "string", "enum": [*ops.ORDER, "qgis"]}, "lines": {"type": "integer", "minimum": 1, "maximum": 5000}, "follow": BOOL}, ("component",)))
def ops_logs(req: Req) -> str:
    cfg, mode, ssh, target = machine(req)
    b = req.body

    def steps(job: jobs.Job, bundle_dir: Any) -> Iterator[str]:
        yield from ops.logs(cfg, mode, ssh, b["component"], int(b.get("lines") or 100), bool(b.get("follow")), job.stop, target)
    return start_job(req, steps, locked=False)


@R("GET", "/jobs/{id}", "Progress of a background job", "read", tags=("jobs",), legacy=("/api/jobs/<jid>",), errors=("not_found",), params={"id": "job id"},
   query={"type": "object", "properties": {"since": {"type": "integer", "minimum": 0}}, "additionalProperties": False},
   response={"type": "object", "required": ["lines", "next", "done", "ok"], "properties": {"lines": STRLIST, "next": INT, "done": BOOL, "ok": BOOL}})
def get_job(req: Req) -> dict[str, Any]:
    job = req.app.config["JOBS"].get(req.path["id"])
    if job is None:
        raise ApiError(404, "not_found", "no such job (it may have expired)")
    since = int(req.query.get("since", 0))
    with job.lock:
        lines = job.lines[since:]
    return {"lines": lines, "next": since + len(lines), "done": job.done, "ok": job.ok}


@R("POST", "/jobs/{id}/cancel", "Ask a job to stop (log following ends at once)", "operate", tags=("jobs",), legacy=("/api/jobs/<jid>/cancel",), errors=("not_found",),
   params={"id": "job id"}, response={"type": "object", "properties": {"ok": BOOL}})
def cancel_job(req: Req) -> dict[str, Any]:
    job = req.app.config["JOBS"].get(req.path["id"])
    if job is None:
        raise ApiError(404, "not_found", "no such job")
    job.stop.set()
    return {"ok": True}


@R("GET", "/journal", "History of operations (deployments, backups, restores, rotations ...)", "read", tags=("operate",),
   query={"type": "object", "properties": {"project": STR, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, "additionalProperties": False},
   response={"type": "array", "items": {"type": "object", "required": ["id", "op", "status"], "properties": {
       "id": STR, "op": STR, "project": STR, "target": STR, "mode": STR, "host": STR, "started": {"type": ["number", "null"]}, "ended": {"type": ["number", "null"]},
       "status": {"type": "string", "enum": ["ok", "error", "aborted", "running"]}, "seconds": {"type": ["number", "null"]}, "error": STR}}})
def get_journal(req: Req) -> list[dict[str, Any]]:
    return journal.operations(int(req.query.get("limit", 50)), req.query.get("project"))


# --------------------------------------------------------------------------- deploy / clean / apply
@R("POST", "/deployments", "Deploy the stack (job). Runs the pre-flight and port-conflict checks first", "admin", tags=("deploy",), legacy=("/api/deploy",), job=True,
   errors=("preflight", "conflict", "busy", "transport"), body=body({"skip_preflight": BOOL}))
def deploy_stack(req: Req) -> str:
    cfg, mode, ssh, target = machine(req, persist=True)
    local_problems(target, mode)
    remote(services.ensure_ready, cfg, mode, ssh, target, skip_preflight=bool(req.body.get("skip_preflight")))
    rep = services.find_conflicts(cfg, mode, ssh, target)
    if rep:
        raise ApiError(409, "conflict", "required ports are taken by other programs", rep)
    return start_job(req, lambda job, b: deploy.deploy(cfg, b, target, mode, ssh), cfg=cfg, make_bundle=True, cleanup_bundle=(mode == "ssh"))


@R("POST", "/cleanups", "Remove the whole stack or some programs (job). Purge deletes data", "admin", tags=("deploy",), legacy=("/api/clean",), job=True, errors=("busy",),
   body=body({"components": {"type": ["array", "null"], "items": COMPONENT}, "purge": BOOL, "remove_postgres": BOOL}))
def clean_stack(req: Req) -> str:
    cfg, mode, ssh, target = machine(req, persist=True)
    b = req.body
    comps = b.get("components")
    if comps is not None and not comps:
        raise ApiError(400, "bad_request", "components must be null or a non-empty list")
    try:
        deploy.clean_args(target, comps, bool(b.get("purge")), bool(b.get("remove_postgres")))
    except DeployError as e:
        raise ApiError(400, "deploy", str(e)) from e
    local_problems(target, mode)
    return start_job(req, lambda job, bundle: deploy.clean(cfg, bundle, target, mode, ssh, comps, bool(b.get("purge")), bool(b.get("remove_postgres"))),
                     cfg=cfg, make_bundle=True, cleanup_bundle=(mode == "ssh"))


@R("POST", "/components/{id}/apply", "Roll out only one program with the current settings (job)", "admin", tags=("deploy",), legacy=("/api/apply",), job=True,
   params={"id": "component id"}, errors=("busy",), body=body({"save": BOOL}))
def apply_component(req: Req) -> str:
    cfg, mode, ssh, target = machine(req, persist=True)
    comp = req.path["id"]
    try:
        deploy.apply_args(target, comp)
    except DeployError as e:
        raise ApiError(400, "deploy", str(e)) from e
    local_problems(target, mode)
    if req.body.get("save"):
        try:
            projects.save_component(dict(req.body.get("config") or {}, project=cfg["project"]), comp)
        except ProjectError as e:
            raise ApiError(400, "project", str(e)) from e
    return start_job(req, lambda job, bundle: deploy.apply_component(cfg, bundle, target, mode, ssh, comp), cfg=cfg, make_bundle=True, cleanup_bundle=(mode == "ssh"))


# --------------------------------------------------------------------------- programs that are already on the server
PLAN: Schema = {"type": "object"}


@R("POST", "/existing/scan", "What is already installed / which of our ports are taken", "read", tags=("existing",), legacy=("/api/existing/scan",), errors=("transport",),
   body=body(), response=OBJ)
def existing_scan(req: Req) -> dict[str, Any]:
    cfg, mode, ssh, target = machine(req)
    need_native(cfg)
    return remote(discover.scan, cfg, mode, ssh, target)


@R("POST", "/existing/backup", "Back up the settings of programs already installed (job)", "operate", tags=("existing",), legacy=("/api/existing/backup",), job=True,
   body=body({"components": {"type": "array", "minItems": 1, "items": {"type": "string", "enum": list(discover.COMPONENTS)}}, "with_data": BOOL}, ("components",)))
def existing_backup(req: Req) -> str:
    cfg, mode, ssh, target = machine(req)
    b = req.body
    return start_job(req, lambda job, bundle: existing.backup(cfg, target, mode, ssh, b["components"], with_data=bool(b.get("with_data"))))


@R("POST", "/existing/execute", "Back up -> remove -> (install) -> import for programs already on the server (job)", "admin", tags=("existing",), legacy=("/api/existing/execute",),
   job=True, errors=("plan", "busy"), body=body({"plan": PLAN, "with_data": BOOL, "install": BOOL, "confirm_data_loss": BOOL}, ("plan",)))
def existing_execute(req: Req) -> str:
    cfg, mode, ssh, target = machine(req, persist=True)
    need_native(cfg)
    try:
        plan = existing.normalize_plan(req.body["plan"])
    except PlanError as e:
        raise ApiError(400, "plan", str(e)) from e
    local_problems(target, mode)
    confirm = req.body.get("confirm_data_loss") is True
    if any(v["purge"] and v["action"] in ("remove", "reuse") for v in plan.values()) and not confirm:
        raise ApiError(400, "plan", "deleting data needs explicit confirmation (confirm_data_loss: true)")

    def steps(job: jobs.Job, bundle_dir: Any) -> Iterator[str]:
        bdir = paths.bundles_dir() / f"{cfg['project']}-{job.id}"

        def make(c: dict[str, Any]) -> Path:
            generate.generate_bundle(c, bdir)
            os.chmod(bdir, 0o700)
            return bdir
        yield from existing.run(cfg, target, mode, ssh, plan, make, with_data=bool(req.body.get("with_data")), install=bool(req.body.get("install", True)),
                                confirm_data_loss=confirm)
    return start_job(req, steps)


# --------------------------------------------------------------------------- backups (native)
@R("POST", "/backups/list", "Backups on the server", "read", tags=("backups",), legacy=("/api/backup/list",), errors=("transport",), body=body(),
   response={"type": "object", "required": ["backups"], "properties": {"backups": {"type": "array", "items": {"type": "object", "required": ["id", "status"], "properties": {
       "id": STR, "status": STR, "kind": STR, "created": STR, "bytes": INT, "components": STRLIST}}}}})
def backups_list(req: Req) -> dict[str, Any]:
    cfg, mode, ssh, _ = machine(req)
    need_native(cfg)
    return {"backups": remote(backup_mod.list_backups, cfg, mode, ssh)}


COMPS_BK: Schema = {"type": "array", "items": {"type": "string", "enum": list(backup_mod.COMPONENTS)}}


@R("POST", "/backups/create", "Create a verified backup (job)", "operate", tags=("backups",), legacy=("/api/backup/create",), job=True,
   body=body({"components": COMPS_BK, "with_cache": BOOL, "note": {"type": "string", "maxLength": 200}}))
def backups_create(req: Req) -> str:
    cfg, mode, ssh, _ = machine(req)
    need_native(cfg)
    b = req.body
    return lines_job(req, lambda: backup_mod.create(cfg, mode, ssh, b.get("components") or None, with_cache=bool(b.get("with_cache")), note=b.get("note") or ""))


@R("POST", "/backups/verify", "Re-read a backup and check every checksum (job)", "operate", tags=("backups",), legacy=("/api/backup/verify",), job=True,
   body=body({"id": BACKUP_ID}, ("id",)))
def backups_verify(req: Req) -> str:
    cfg, mode, ssh, _ = machine(req)
    need_native(cfg)
    return lines_job(req, lambda: backup_mod.verify(cfg, mode, ssh, req.body["id"]))


@R("POST", "/backups/prune", "Delete old backups (job). Defaults to a dry run; the newest backup is never deleted", "admin", tags=("backups",), legacy=("/api/backup/prune",),
   job=True, body=body({"keep": {"type": "integer", "minimum": 1}, "keep_pre": {"type": "integer", "minimum": 0}, "days": {"type": ["integer", "null"], "minimum": 0},
                        "dry_run": BOOL}))
def backups_prune(req: Req) -> str:
    cfg, mode, ssh, _ = machine(req)
    need_native(cfg)
    b = req.body
    return lines_job(req, lambda: backup_mod.prune(cfg, mode, ssh, int(b.get("keep", 14)), b.get("days"), int(b.get("keep_pre", 5)), dry_run=b.get("dry_run", True) is not False))


@R("POST", "/backups/restore", "Restore a backup (job). Defaults to a plan; replacing live data needs dry_run=false AND confirm=true", "admin", tags=("backups",),
   legacy=("/api/backup/restore",), job=True, errors=("busy",),
   body=body({"id": BACKUP_ID, "only": COMPS_BK, "dry_run": BOOL, "staging": BOOL, "confirm": BOOL, "pre_backup": BOOL, "rollback": BOOL}, ("id",)))
def backups_restore(req: Req) -> str:
    cfg, mode, ssh, _ = machine(req)
    need_native(cfg)
    b = req.body
    return lines_job(req, lambda: backup_mod.restore(cfg, mode, ssh, b["id"], only=b.get("only") or None, dry_run=b.get("dry_run", True) is not False,
                                                      staging=bool(b.get("staging")), confirm=b.get("confirm") is True,
                                                      pre_backup=b.get("pre_backup", True) is not False, rollback=b.get("rollback", True) is not False))


# --------------------------------------------------------------------------- credentials and updates
@R("POST", "/credentials/rotate", "Change the PostgreSQL role / GeoServer admin password on a native server (verified, rolled back on failure)", "admin",
   tags=("security",), errors=("transport",), body=body({"what": {"type": "string", "enum": list(rotation.WHAT)}}, ("what",)),
   response={"type": "object", "properties": {"what": STR, "target": STR, "stored": BOOL}})
def rotate(req: Req) -> dict[str, Any]:
    cfg, mode, ssh, _ = machine(req, persist=True)
    return remote(rotation.rotate, cfg, req.body["what"], mode, ssh)


@R("POST", "/updates/check", "Is there a newer release on GitHub?", "read", tags=("updates",), legacy=("/api/update/check",), errors=("update",),
   body={"type": "object", "additionalProperties": False, "properties": {}}, response=OBJ)
def updates_check(req: Req) -> dict[str, Any]:
    try:
        return updater.check()
    except updater.UpdateError as e:
        raise ApiError(502, "update", str(e)) from e


@R("POST", "/updates/apply", "Download, verify (SHA-256) and install the new release into this virtualenv (job)", "admin", tags=("updates",), legacy=("/api/update/apply",),
   job=True, body={"type": "object", "additionalProperties": False, "properties": {}}, errors=("update", "busy"))
def updates_apply(req: Req) -> str:
    try:
        info = updater.check()
    except updater.UpdateError as e:
        raise ApiError(502, "update", str(e)) from e
    if not info["newer"] or not info["can_auto"]:
        raise ApiError(400, "update", "automatic update is not available here")

    def steps(job: jobs.Job, bundle_dir: Any) -> Iterator[str]:
        with tempfile.TemporaryDirectory() as td:
            yield from updater.apply(info, td)
    return start_job(req, steps)
