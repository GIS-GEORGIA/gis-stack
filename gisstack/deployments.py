"""Deployment records, post-deployment health verification and the rollback plan (audit R-03, R-05).

Every `deploy.deploy()` writes one record under `~/.gisstack/deployments/`: the state found before, the outcome, the health found after
and a digest of the configuration.  The record itself holds **no secrets**; the configuration that was applied is kept in a separate
0600 snapshot that only a rollback reads and that no API returns.

What "rollback" means here, honestly: gis-stack cannot un-install packages or un-run a database migration.  The one recovery path it
can *verify* is to re-apply the configuration of an earlier deployment that was healthy afterwards.  That re-application is the same
idempotent deploy as always: it never purges anything, never touches data or backups, and does not remove programs that only the newer
configuration added.  After it, the same health verification runs and the result is reported as it is - a rollback that does not end
healthy is reported as failed and nothing further is done automatically."""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from . import fsutil, runner
from .core import paths
from .core.errors import DeployError

OUTCOMES = ("running", "ok", "unhealthy", "unverified", "failed", "aborted", "incomplete")
STALE_AFTER = 6 * 3600          # a "running" record older than this belongs to a process that no longer exists
KEEP_RECORDS = 200
KEEP_SNAPSHOTS = 20
SECRETISH = ("password", "token", "secret")
LIMITS = {
    "native": "Packages and files installed by the newer configuration are not removed; the installer is simply run again with the earlier settings.",
    "compose": "Containers and volumes created only by the newer configuration are left in place; `up` is run again with the earlier files.",
    "k8s": "Objects created only by the newer configuration are not pruned; `kubectl apply` is run again with the earlier manifests.",
}
ALWAYS = "No data, database, volume or backup is deleted or restored by a rollback."


def _dir() -> Path:
    return fsutil.private_dir(paths.deployments_dir())


def _rec_path(rid: str) -> Path:
    if not rid or not all(c in "0123456789abcdef" for c in rid):
        raise DeployError("bad deployment id")
    return _dir() / f"{rid}.json"


def _snap_path(rid: str) -> Path:
    return _dir() / f"{rid}.config"


def digest(cfg: dict[str, Any]) -> str:
    """Stable fingerprint of a configuration with its secret values masked out (so it reveals nothing about them)."""
    mask = runner.redactor(runner.secrets_of(cfg))
    return hashlib.sha256(mask(json.dumps(cfg, sort_keys=True, default=str)).encode("utf-8")).hexdigest()[:16]


def changed_keys(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    """Dotted names of the settings that differ between two configurations (names only - never values)."""
    out: list[str] = []

    def walk(x: Any, y: Any, key: str) -> None:
        if isinstance(x, dict) and isinstance(y, dict):
            for k in sorted(set(x) | set(y)):
                walk(x.get(k), y.get(k), f"{key}.{k}" if key else str(k))
        elif x != y:
            out.append(key)
    walk(a, b, "")
    return out


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"id": r["id"], "state": r.get("state", "unknown"), "ok": bool(r.get("ok")),
             "failing": [c["label"] for c in r.get("checks", []) if not c.get("ok")]} for r in rows]


def _write(rec: dict[str, Any]) -> None:
    fsutil.write_private(_rec_path(rec["id"]), json.dumps(rec, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def begin(cfg: dict[str, Any], target: str, mode: str, host: str, before: Optional[Callable[[], list[dict[str, Any]]]] = None, *,
          kind: str = "deploy", rollback_of: str = "") -> dict[str, Any]:
    """Write the record (outcome `running`) and keep the applied configuration for a later rollback.  `before` reads the current state."""
    mask = runner.redactor(runner.secrets_of(cfg))
    state: Any
    try:
        state = summarize(before()) if before else []
    except Exception as e:  # noqa: BLE001 - reading the old state must never stop a deployment
        state = {"unavailable": mask(f"{type(e).__name__}: {e}")[:300]}
    rec = {"id": uuid.uuid4().hex[:12], "ts": int(time.time()), "n": time.time_ns(), "kind": kind, "rollback_of": rollback_of, "project": cfg.get("project", ""),
           "target": target, "mode": mode, "host": host, "digest": digest(cfg), "before": state, "after": None, "outcome": "running", "error": "",
           "seconds": None}
    fsutil.write_private(_snap_path(rec["id"]), json.dumps(cfg, sort_keys=True, default=str))
    _write(rec)
    return rec


def finish(rec: dict[str, Any], outcome: str, error: str = "", after: Any = None) -> dict[str, Any]:
    if outcome not in OUTCOMES:
        raise ValueError(outcome)
    rec.update(outcome=outcome, error=error[:500], after=after, seconds=round(time.time() - rec["ts"], 1))
    _write(rec)
    prune()
    return rec


def _load(p: Path) -> Optional[dict[str, Any]]:
    try:
        r = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(r, dict) and r.get("outcome") == "running" and time.time() - float(r.get("ts", 0)) > STALE_AFTER:
        r["outcome"] = "incomplete"                           # started, never finished, and its process is long gone
    return r if isinstance(r, dict) and r.get("id") else None


def records(project: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
    """Newest first.  Damaged files are skipped."""
    d = paths.deployments_dir()
    if not d.is_dir():
        return []
    rows = [r for r in (_load(p) for p in d.glob("*.json")) if r and (project is None or r.get("project") == project)]
    rows.sort(key=lambda r: (r.get("ts", 0), r.get("n", 0)), reverse=True)     # `n` orders records made within the same second
    for r in rows:
        r["config_saved"] = _snap_path(r["id"]).is_file()
    return rows[: max(1, int(limit))]


def prune() -> None:
    """Bound the history.  Only deployment records and configuration snapshots are ever removed - never data or backups."""
    d = paths.deployments_dir()
    files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for i, p in enumerate(files):
        if i >= KEEP_RECORDS:
            p.unlink(missing_ok=True)
            p.with_suffix(".config").unlink(missing_ok=True)
        elif i >= KEEP_SNAPSHOTS:
            p.with_suffix(".config").unlink(missing_ok=True)


def snapshot(rid: str) -> dict[str, Any]:
    try:
        cfg = json.loads(_snap_path(rid).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise DeployError("the configuration of that deployment is no longer stored") from e
    if not isinstance(cfg, dict):
        raise DeployError("the stored configuration is damaged")
    return cfg


def verify(cfg: dict[str, Any], status: Callable[[], list[dict[str, Any]]], *, timeout: Optional[float] = None, interval: float = 5.0,
           sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Wait (up to `timeout` seconds, default $GISSTACK_VERIFY_TIMEOUT or 60) for every program to be running with all checks passing.

    Returns {state: healthy|unhealthy|unknown, rows: summary, error?}.  `unknown` means the machine could not be asked - never a pass."""
    if timeout is None:
        timeout = float(os.environ.get("GISSTACK_VERIFY_TIMEOUT", "60"))
    mask = runner.redactor(runner.secrets_of(cfg))
    waited = 0.0
    while True:
        try:
            rows = summarize(status())
        except Exception as e:  # noqa: BLE001 - an unanswerable status is "unknown", never a pass and never a crash
            return {"state": "unknown", "rows": [], "error": mask(f"{type(e).__name__}: {e}")[:300]}
        if rows and all(r["ok"] for r in rows):
            return {"state": "healthy", "rows": rows}
        if waited >= timeout:
            return {"state": "unhealthy" if rows else "unknown", "rows": rows}
        sleep(interval)
        waited += interval


def plan_rollback(project: str, target: str, mode: str, host: str, record_id: str = "") -> dict[str, Any]:
    """Can the machine go back to an earlier healthy deployment?  Pure reading; `eligible` is false with a `reason` whenever the
    recovery path cannot be verified."""
    mine = [r for r in records(project, 500) if (r.get("target"), r.get("mode"), r.get("host")) == (target, mode, host)]
    out: dict[str, Any] = {"eligible": False, "reason": "", "current": None, "target": None, "changes": [], "limitations": [LIMITS.get(target, ""), ALWAYS]}
    if not mine:
        out["reason"] = "no deployment has been recorded for this machine"
        return out
    cur = mine[0]
    out["current"] = {k: cur.get(k) for k in ("id", "ts", "outcome", "digest")}
    if cur["outcome"] == "running":
        out["reason"] = "a deployment is still running on this machine"
        return out
    if record_id:
        cand = next((r for r in mine if r["id"] == record_id), None)
        if cand is None:
            out["reason"] = "that deployment does not belong to this machine"
            return out
    else:
        cand = next((r for r in mine[1:] if r["outcome"] == "ok" and r.get("config_saved") and r["digest"] != cur["digest"]), None)
        if cand is None:
            out["reason"] = "there is no earlier deployment that was verified healthy and differs from the current one"
            return out
    out["target"] = {k: cand.get(k) for k in ("id", "ts", "outcome", "digest")}
    if cand["outcome"] != "ok":
        out["reason"] = "that deployment was not verified healthy, so it is not a recovery point"
    elif not cand.get("config_saved"):
        out["reason"] = "the configuration of that deployment is no longer stored"
    elif cur["outcome"] == "ok" and cur["digest"] == cand["digest"]:
        out["reason"] = "the machine already runs that configuration and is healthy - nothing to do"
    else:
        out["eligible"] = True
        out["changes"] = changed_keys(snapshot(cur["id"]), snapshot(cand["id"])) if cur.get("config_saved") else []
    return out


def with_current_secrets(old: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """The earlier configuration, but every password / token / secret takes its *current* value.  A password may have been rotated
    since that deployment; re-applying the old one would lock the stack out of its own database."""
    def walk(o: Any, c: Any, key: str = "") -> Any:
        if isinstance(o, dict):
            return {k: walk(v, c.get(k) if isinstance(c, dict) else None, str(k)) for k, v in o.items()}
        if isinstance(o, str) and any(w in key.lower() for w in SECRETISH) and isinstance(c, str) and c:
            return c
        return o
    out: dict[str, Any] = walk(old, current)
    return out
