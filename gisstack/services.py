"""Application services: the rules that the CLI, the web GUI and the Tk GUI all apply before and while they run an operation
(audit A-03).  They take and return plain data, raise typed errors, and know nothing about argparse, Flask or Tk, so each UI only
has to translate the outcome into its own widgets (exit code, HTTP status, dialog)."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from . import deploy, deployments, discover, existing, preflight, transport
from .core.errors import DeployError

MODES = ("local", "ssh")


class Conflict(DeployError):
    """Somebody else's program holds a port the stack needs; `report` is the discover.scan() result to show the user."""
    code = "conflict"

    def __init__(self, message: str, report: dict[str, Any]) -> None:
        super().__init__(message)
        self.report = report


class PreflightFailed(DeployError):
    """The target machine cannot take the selected stack; `result` is the preflight.run() result."""
    code = "preflight"

    def __init__(self, message: str, result: dict[str, Any]) -> None:
        super().__init__(message)
        self.result = result


def check_mode(mode: Any) -> str:
    if mode not in MODES:
        raise DeployError("mode must be local or ssh")
    return str(mode)


def ensure_ready(cfg: dict[str, Any], mode: str, ssh: Optional[dict[str, Any]], target: str, *, skip_preflight: bool = False) -> Optional[dict[str, Any]]:
    """The machine pre-flight checks (OS, memory, disk, network, clock) unless skipped.  Missing local tools (root, docker, kubectl,
    ssh) are reported by `deploy.preflight` / `deploy.deploy` themselves.

    Raises PreflightFailed (a blocking check failed) or DeployError (machine unreachable).  Returns the result - warnings
    included - or None when skipped."""
    if skip_preflight:
        return None
    res = preflight.run(cfg, mode, ssh, target)
    if not res["ok"]:
        raise PreflightFailed("მანქანა არჩეული სტეკისთვის არ ვარგა", res)
    return res


def find_conflicts(cfg: dict[str, Any], mode: str, ssh: Optional[dict[str, Any]], target: str) -> Optional[dict[str, Any]]:
    """Scan a native target for programs on ports we need.  None when there is no conflict - or when the host cannot be inspected
    (e.g. no sudo): the installer then reports its own errors."""
    if target != "native":
        return None
    try:
        rep = discover.scan(cfg, mode, ssh, target)
    except DeployError:
        return None
    return rep if rep["conflicts"] else None


def deploy_steps(cfg: dict[str, Any], bundle: Path, target: str, mode: str, ssh: Optional[dict[str, Any]], *,
                 plan: Optional[dict[str, Any]] = None, make_bundle: Optional[Callable[[dict[str, Any]], Path]] = None,
                 with_data: bool = False, backup_dir: Optional[str] = None, confirm_data_loss: bool = False,
                 refuse_conflicts: bool = True) -> Iterator[str]:
    """Deploy, or - with a plan for programs already on the server - back up, remove, install and import.  Yields log lines.

    Without a plan and with refuse_conflicts, a port conflict raises Conflict before anything is touched."""
    if plan is not None:
        yield from existing.run(cfg, target, mode, ssh, plan, make_bundle, with_data=with_data, backup_dir=backup_dir,
                                confirm_data_loss=confirm_data_loss)
        return
    if refuse_conflicts:
        rep = find_conflicts(cfg, mode, ssh, target)
        if rep:
            raise Conflict("საჭირო პორტები დაკავებულია — აირჩიე რა ვუყოთ არსებულ პროგრამებს", rep)
    yield from deploy.deploy(cfg, bundle, target, mode, ssh)


class RollbackRefused(DeployError):
    """There is no verified recovery path; `plan` says why."""
    code = "not_ready"

    def __init__(self, message: str, plan: dict[str, Any]) -> None:
        super().__init__(message)
        self.plan = plan


class RollbackFailed(DeployError):
    code = "rollback"


def rollback_plan(cfg: dict[str, Any], mode: str, ssh: Optional[dict[str, Any]], target: str, record_id: str = "") -> dict[str, Any]:
    """Whether (and to what) this machine can be rolled back.  Reads local records only; touches nothing."""
    return deployments.plan_rollback(cfg["project"], target, mode, transport.host_label(mode, ssh), record_id)


def rollback_config(cfg: dict[str, Any], mode: str, ssh: Optional[dict[str, Any]], target: str, record_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """(configuration to re-apply, plan).  Raises RollbackRefused unless the recovery path is verified.  Current secrets are kept."""
    plan = rollback_plan(cfg, mode, ssh, target, record_id)
    if not plan["eligible"]:
        raise RollbackRefused(plan["reason"] or "rollback is not possible", plan)
    return deployments.with_current_secrets(deployments.snapshot(plan["target"]["id"]), cfg), plan


def rollback_steps(snap_cfg: dict[str, Any], bundle: Path, target: str, mode: str, ssh: Optional[dict[str, Any]], rollback_of: str) -> Iterator[str]:
    """Re-apply the earlier configuration (idempotent deploy, nothing purged) and require the machine to be healthy afterwards."""
    yield f"== rollback -> {rollback_of}"
    yield from deploy.deploy(snap_cfg, bundle, target, mode, ssh, kind="rollback", rollback_of=rollback_of)
    last = deployments.records(snap_cfg.get("project"), 1)
    if not last or last[0].get("outcome") != "ok":
        raise RollbackFailed("rollback finished, but the machine is not verified healthy - nothing further was changed; check the programs and the log")
