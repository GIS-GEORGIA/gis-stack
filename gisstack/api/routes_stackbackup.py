"""/api/v1 routes of backup and restore for the container targets (Docker Compose, Podman, Kubernetes).  The native target keeps `/backups/*`.

Reading (list, inspect, verify) needs `read`; creating and checking a restore need `operate`; restoring, recovering, deleting, pruning and exporting
need `admin`.  Every state-changing call is audited.  Encryption keys and passphrases are never part of a request: encrypted backups use public-key
recipients (`encrypt_to`); a passphrase can only come from the server's own environment (`GISSTACK_BACKUP_PASSPHRASE`) or the CLI."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Iterator, Optional

from flask import Response

from .. import backup_crypto
from ..backups import model, provider
from ..core.errors import GisStackError
from .mount import Req
from .registry import ApiError, registry
from .routes import BOOL, INT, OBJ, STR, STRLIST, body, local_problems, machine, start_job
from .schema import Schema

R = registry.route
KW: dict[str, Any] = {"tags": ("stack-backups",)}
ID: Schema = {"type": "string", "pattern": r"^(pre-)?\d{8}T\d{6}Z-[0-9a-f]{4}$"}
COMPONENT: Schema = {"type": "string", "enum": list(model.COMPONENTS)}
COMPS: Schema = {"type": "array", "minItems": 1, "items": COMPONENT}
ROW: Schema = {"type": "object", "required": ["id", "status"], "properties": {"id": STR, "status": {"type": "string", "enum": ["ok", "partial", "failed", "damaged"]}, "kind": STR,
                                                                              "created": STR, "bytes": INT, "components": STRLIST, "encrypted": BOOL, "note": STR}}
REPORT: Schema = {"type": "object", "required": ["ok", "problems"], "properties": {"ok": BOOL, "problems": STRLIST, "checked": INT, "id": STR}}


def prov_of(req: Req, *, encrypt_to: Optional[list[str]] = None) -> tuple[provider.BackupProvider, dict[str, Any], str, dict[str, Any]]:
    cfg, mode, ssh, _target = machine(req)
    try:
        spec = backup_crypto.Spec(recipients=list(encrypt_to)) if encrypt_to else None
        return provider.provider_for(cfg, mode, ssh, encryption=spec), cfg, mode, ssh
    except GisStackError as e:
        raise ApiError(400, "deploy", str(e)) from e


def audit(req: Req, event: str, **fields: Any) -> None:
    req.app.config["AUDIT"].write(event=event, principal=req.principal.id, scope=req.principal.scope, **fields)


def guarded(fn: Any, *a: Any, **kw: Any) -> Any:
    try:
        return fn(*a, **kw)
    except GisStackError as e:
        raise ApiError.from_exc(e, 404 if e.code == "not_found" else 400 if e.code in ("backup", "restore", "integrity") else None) from e


@R("POST", "/stack-backups/list", "Backups of this stack (newest first); partial, failed and damaged ones are listed with that status, never as ok", "read", **KW,
   errors=("deploy",), body=body(), response={"type": "object", "required": ["backups", "restore_pending"], "properties": {"backups": {"type": "array", "items": ROW}, "restore_pending": BOOL,
                                                                                                               "components": {"type": "array", "items": OBJ}}})
def list_backups(req: Req) -> dict[str, Any]:
    p, _cfg, _mode, _ssh = prov_of(req)
    comps = [{"id": n, "restorable": p.kind in c["restorable_on"] or (p.family == "compose" and "compose" in c["restorable_on"]), "optional": c["optional"]} for n, c in model.COMPONENTS.items()]
    return {"backups": p.list_backups(), "restore_pending": p.store.read_state() is not None, "components": comps}


@R("POST", "/stack-backups/inspect", "One backup: manifest (no secrets) and its integrity status", "read", **KW, errors=("deploy", "not_found", "integrity"),
   body=body({"id": ID}, ("id",)), response=OBJ)
def inspect_backup(req: Req) -> dict[str, Any]:
    p, *_ = prov_of(req)
    return guarded(p.inspect, req.body["id"])


@R("POST", "/stack-backups/verify", "Re-read every file and check checksums, the file set and the archive structure", "read", **KW, errors=("deploy", "not_found"),
   body=body({"id": ID}, ("id",)), response=REPORT)
def verify_backup(req: Req) -> dict[str, Any]:
    p, *_ = prov_of(req)
    return guarded(p.verify, req.body["id"])


@R("POST", "/stack-backups/check", "Pre-restore validation (integrity, compatibility, workloads running, PostgreSQL version) - changes nothing", "operate", **KW,
   errors=("deploy", "not_found"), body=body({"id": ID, "components": COMPS}, ("id",)), response={"type": "object", "required": ["ok", "problems"], "properties": {
       "ok": BOOL, "problems": STRLIST, "components": STRLIST, "backup": STR, "created": STR, "kind": STR}})
def check_restore(req: Req) -> dict[str, Any]:
    p, *_ = prov_of(req)
    return guarded(p.check_restore, req.body["id"], req.body.get("components"))


@R("POST", "/stack-backups/create", "Create a verified backup (job). Nothing is sealed unless every file was re-read and checked", "operate", **KW, job=True, errors=("deploy", "busy"),
   body=body({"components": COMPS, "note": {"type": "string", "maxLength": model.MAX_NOTE}, "with_cache": BOOL, "encrypt_to": {"type": "array", "items": STR, "maxItems": 10}}))
def create_backup(req: Req) -> str:
    b = req.body
    p, cfg, mode, _ssh = prov_of(req, encrypt_to=b.get("encrypt_to"))
    local_problems(cfg["deploy_target"], mode)
    audit(req, "stack_backup.create", project=cfg["project"], target=p.kind, components=b.get("components") or "default", encrypted=bool(b.get("encrypt_to")))
    return start_job(req, lambda job, bundle: p.create(b.get("components"), note=b.get("note", ""), with_cache=bool(b.get("with_cache"))))


@R("POST", "/stack-backups/restore", "Restore a backup (job): validate, automatic pre-restore backup, stage, reversible swap, health check; undone automatically on failure. Needs confirm=true",
   "admin", **KW, job=True, errors=("deploy", "busy", "restore"), body=body({"id": ID, "components": COMPS, "confirm": BOOL}, ("id", "confirm")))
def restore_backup(req: Req) -> str:
    b = req.body
    p, cfg, mode, _ssh = prov_of(req)
    if b.get("confirm") is not True:
        raise ApiError(400, "bad_request", "confirm must be true")
    local_problems(cfg["deploy_target"], mode)
    rep = guarded(p.check_restore, b["id"], b.get("components"))
    who = {"project": cfg["project"], "target": p.kind, "backup": b["id"], "components": rep["components"]}
    if not rep["ok"]:
        audit(req, "stack_backup.restore", ok=False, error="refused", **who)
        raise ApiError(409, "restore", "restore refused, nothing was changed: " + "; ".join(rep["problems"]), rep)
    audit(req, "stack_backup.restore", ok=True, phase="start", **who)
    return start_job(req, lambda job, bundle: p.restore(b["id"], b.get("components"), confirm=True))


@R("POST", "/stack-backups/recover", "Put the previous data back after a restore that was interrupted (job); safe to repeat", "admin", **KW, job=True, errors=("deploy", "busy"), body=body())
def recover_restore(req: Req) -> str:
    p, cfg, mode, _ssh = prov_of(req)
    local_problems(cfg["deploy_target"], mode)
    audit(req, "stack_backup.recover", project=cfg["project"], target=p.kind)
    return start_job(req, lambda job, bundle: p.recover())


@R("POST", "/stack-backups/delete", "Delete one backup (explicit; needs confirm=true)", "admin", **KW, errors=("deploy", "not_found"),
   body=body({"id": ID, "confirm": BOOL}, ("id", "confirm")), response={"type": "object", "required": ["deleted"], "properties": {"deleted": STR}})
def delete_backup(req: Req) -> dict[str, Any]:
    p, cfg, *_ = prov_of(req)
    if req.body.get("confirm") is not True:
        raise ApiError(400, "bad_request", "confirm must be true")
    guarded(p.delete, req.body["id"])
    audit(req, "stack_backup.delete", ok=True, project=cfg["project"], target=p.kind, backup=req.body["id"])
    return {"deleted": req.body["id"]}


@R("POST", "/stack-backups/prune", "Apply the retention policy (keeps the newest intact backup whatever the policy says); dry_run shows what would go", "admin", **KW,
   errors=("deploy",), body=body({"keep": {"type": "integer", "minimum": 1, "maximum": 1000}, "days": {"type": "integer", "minimum": 1, "maximum": 3650},
                                  "keep_pre": {"type": "integer", "minimum": 1, "maximum": 1000}, "dry_run": BOOL}), response=OBJ)
def prune_backups(req: Req) -> dict[str, Any]:
    p, cfg, *_ = prov_of(req)
    b = req.body
    res = guarded(p.prune, keep=int(b.get("keep", 14)), days=b.get("days"), keep_pre=int(b.get("keep_pre", 5)), dry_run=bool(b.get("dry_run", True)))
    if not res["dry_run"]:
        audit(req, "stack_backup.prune", ok=True, project=cfg["project"], target=p.kind, removed=len(res["removed"]) + len(res["removed_leftovers"]))
    return res


@R("POST", "/stack-backups/export", "Download one verified backup as a .tar (still encrypted if it was made encrypted; contains GeoServer's own credential hashes when it holds GeoServer data)",
   "admin", **KW, raw="binary", errors=("deploy", "not_found", "integrity"), body=body({"id": ID}, ("id",)))
def export_backup(req: Req) -> Response:
    import tarfile
    p, cfg, *_ = prov_of(req)
    bid = req.body["id"]
    rep = guarded(p.verify, bid)
    if not rep["ok"]:
        raise ApiError(409, "integrity", "the backup is damaged and is not exported: " + "; ".join(rep["problems"][:3]))
    fd, tmp = tempfile.mkstemp(prefix="gsexport", suffix=".tar")
    os.close(fd)
    os.chmod(tmp, 0o600)
    with tarfile.open(tmp, "w") as tf:
        tf.add(p.store.path(bid), arcname=bid)
    audit(req, "stack_backup.export", ok=True, project=cfg["project"], target=p.kind, backup=bid)

    def stream() -> Iterator[bytes]:
        try:
            with open(tmp, "rb") as f:
                yield from iter(lambda: f.read(1 << 20), b"")
        finally:
            Path(tmp).unlink(missing_ok=True)
    resp = Response(stream(), mimetype="application/x-tar")
    resp.headers["Content-Disposition"] = f'attachment; filename="{cfg["project"]}-{bid}.tar"'
    return resp


