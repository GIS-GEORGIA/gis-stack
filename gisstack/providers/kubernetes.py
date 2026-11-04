"""Kubernetes provider: Deployments / StatefulSet in one namespace, driven through kubectl."""
from __future__ import annotations

import json
from typing import Any, Callable

from ..core.errors import DeployError
from . import common

ENTRY, CLEAN_ENTRY = "apply.sh", "apply.sh"
ROOT = False

def status_script(cfg: dict[str, Any]) -> str:
    return f"kubectl -n {cfg['k8s']['namespace']} get pods,svc,deploy,sts -o json 2>&1\n"


def parse(out: str) -> list[dict[str, Any]]:
    try:
        return json.loads(out).get("items", [])
    except (ValueError, AttributeError):
        raise DeployError("kubectl: " + out.strip().splitlines()[-1] if out.strip() else "kubectl: empty answer") from None


def workload_name(comp: str) -> str:
    return {"qgis_server": "qgis", "webmap": "caddy"}.get(comp, comp)


def rows(cfg: dict[str, Any], ids: list[str], items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for comp in ids:
        name = workload_name(comp)
        r = common.row(cfg, comp)
        r["unit"] = ("statefulset/" if name == "postgis" else "deployment/") + name
        workload = next((i for i in items if i["kind"] in ("Deployment", "StatefulSet") and i["metadata"]["name"] == name), None)
        pods = [i for i in items if i["kind"] == "Pod" and (i["metadata"].get("labels") or {}).get("app") == name]
        svc = next((i for i in items if i["kind"] == "Service" and i["metadata"]["name"] == name), None)
        if workload is None:
            r["state"], r["detail"] = "missing", "workload not found"
        elif workload["spec"].get("replicas", 1) == 0:
            r["state"], r["detail"] = "stopped", "scaled to 0"
        elif not pods:
            r["state"], r["detail"] = "starting", "no pod yet"
        else:
            p = pods[0]
            st = p.get("status", {})
            cs = (st.get("containerStatuses") or [{}])[0]
            ready = bool(cs.get("ready"))
            phase = st.get("phase", "")
            r["state"] = "running" if (phase == "Running" and ready) else ("failed" if phase in ("Failed", "Succeeded") else "starting")
            waiting = (cs.get("state") or {}).get("waiting", {}).get("reason")
            if waiting in ("CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull"):
                r["state"] = "failed"
            r["detail"] = f"{phase}{'' if ready else ' (not ready)'}{', ' + waiting if waiting else ''}"
            r["since"] = st.get("startTime", "")
            r["restarts"] = sum(c.get("restartCount", 0) for c in st.get("containerStatuses") or [])
            ports = [c.get("containerPort") for c in p["spec"]["containers"][0].get("ports", [])]
            if st.get("podIP"):
                r["listen"] += [f"{st['podIP']}:{pt}/tcp (pod)" for pt in ports] or [f"{st['podIP']} (pod)"]
        if svc:
            sp = svc["spec"]
            for pt in sp.get("ports", []):
                if sp.get("clusterIP") and sp["clusterIP"] != "None":
                    r["listen"].append(f"{sp['clusterIP']}:{pt['port']}/{pt.get('protocol', 'TCP').lower()} ({sp.get('type', 'ClusterIP')})")
                for ing in (svc.get("status", {}).get("loadBalancer", {}).get("ingress") or []):
                    r["listen"].append(f"{ing.get('ip') or ing.get('hostname')}:{pt['port']} (external)")
        r["listen"] = sorted(set(r["listen"]))
        rows.append(r)
    for r in rows:
        r["ok"] = r["state"] == "running"
    return rows


def fetch_status(cfg: dict[str, Any], ids: list[str], run: Callable[[str], str]) -> list[dict[str, Any]]:
    return rows(cfg, ids, parse(run(status_script(cfg))))


def _kind(name: str) -> str:
    return "statefulset" if name == "postgis" else "deployment"


def control_script(cfg: dict[str, Any], action: str, order: list[str]) -> str:
    lines = ["set -u"]
    ns = cfg["k8s"]["namespace"]
    done: set[str] = set()
    for comp in order:
        n = workload_name(comp)
        if n in done:
            continue
        done.add(n)
        kind = _kind(n)
        if action == "start":
            lines.append(f"kubectl -n {ns} scale {kind}/{n} --replicas=1")
        elif action == "stop":
            lines.append(f"kubectl -n {ns} scale {kind}/{n} --replicas=0")
        elif action == "restart":
            lines.append(f"kubectl -n {ns} rollout restart {kind}/{n}")
        else:
            lines.append(f'echo "{n}: {action} is not applicable on Kubernetes (use start / stop)"')
    return "\n".join(lines) + "\n"


def logs_script(cfg: dict[str, Any], comp: str, n: int, follow: bool) -> str:
    f = " -f" if follow else ""
    return f"kubectl -n {cfg['k8s']['namespace']} logs --tail {n}{f} {_kind(workload_name(comp))}/{workload_name(comp)}\n"


def clean_args(short: list[str], purge: bool, remove_postgres: bool, force_postgres: bool) -> list[str]:
    if remove_postgres:
        raise DeployError("PostgreSQL პაკეტების წაშლა მხოლოდ native სამიზნეზეა")
    return ["clean"] + (["--purge"] if purge else []) + short


def apply_args(component: str) -> list[str]:
    return ["only", {"qgis_server": "qgis", "webmap": "caddy"}.get(component, component)]
