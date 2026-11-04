"""Day-to-day operations on a running stack: live status, start / stop / restart, logs.

Works for the three targets (native systemd units, Compose containers, Kubernetes workloads), on this
machine or over SSH.  Everything is read with ordinary tools (systemctl, ss, curl, docker, kubectl), so no
agent has to be installed on the server."""
from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import threading
import time
from typing import Any, Iterator

from . import catalog, deploy, generate, journal, runner

ACTIONS = ("start", "stop", "restart", "enable", "disable")
ORDER = ["postgis", "tomcat", "geoserver", "qgis_server", "mapproxy", "caddy", "webmap"]   # start order
QGIS_PORT, MAPPROXY_PORT = 9993, 8081
_NAMES = {c["id"]: c["ka"] for c in catalog.COMPONENTS}


# --------------------------------------------------------------------------- transport
def _argv(mode: str, ssh: dict[str, Any] | None, script: str, *, root: bool) -> list[str]:
    """Command that runs a bash script here or on the server (script is an argument, so stdin stays free)."""
    if mode == "local":
        pre = ["sudo", "-n"] if root and hasattr(os, "geteuid") and os.geteuid() != 0 else []
        return pre + ["bash", "-c", script]
    if mode != "ssh":
        raise deploy.DeployError("უცნობი რეჟიმი")
    ssh = ssh or {}
    sudo = "sudo -n " if root and ssh.get("user", "root") != "root" else ""
    return deploy._ssh_base(ssh) + [f"{sudo}bash -c {shlex.quote(script)}"]


def _run(mode: str, ssh: dict[str, Any] | None, script: str, *, root: bool = False, timeout: int = 40,
         input_text: str | None = None) -> str:
    """Run a script here / on the server and return stdout.  `input_text` goes to its stdin (use it for secrets, never argv)."""
    p = runner.run(_argv(mode, ssh, script, root=root), timeout=timeout, error=deploy.DeployError, input_text=input_text)
    if p.returncode != 0 and not p.stdout.strip():
        raise deploy.DeployError((p.stderr or "command failed").strip().splitlines()[-1])
    return p.stdout


def _stream(mode: str, ssh: dict[str, Any] | None, script: str, *, root: bool = False,
            stop: threading.Event | None = None, deadline: float | None = None) -> Iterator[str]:
    """Stream a script's output.  `stop` ends it (not an error); `deadline` bounds it (None: log following has no limit)."""
    yield from runner.stream(_argv(mode, ssh, script, root=root), stop=stop, error=deploy.DeployError, echo=False,
                             deadline=deadline, idle=None)


# --------------------------------------------------------------------------- helpers
def _target(cfg: dict[str, Any], target: str | None) -> str:
    t = target or cfg["deploy_target"]
    if t not in ("native", "compose", "k8s"):
        raise deploy.DeployError("უცნობი სამიზნე")
    return t


def _components(cfg: dict[str, Any], target: str, wanted: list[str] | None) -> list[str]:
    on = generate.effective_components(cfg, target)
    ids = [c for c in ORDER if on.get(c)]
    if wanted is None:
        return ids
    out = []
    for w in wanted:
        w = "qgis_server" if w == "qgis" else w
        if w not in ORDER:
            raise deploy.DeployError(f"უცნობი კომპონენტი: {w}")
        out.append(w)
    return [c for c in ORDER if c in out]


def _public(cfg: dict[str, Any]) -> tuple[str, str, int]:
    """(base url, domain, port) of the Caddy entry point as seen from the server itself."""
    scheme = "http" if cfg["tls"] == "none" else "https"
    return f"{scheme}://{cfg['domain']}", cfg["domain"].split(":")[0], 80 if scheme == "http" else 443


_PUBLIC_PATH = {"geoserver": "/geoserver/web/", "mapproxy": "/mapproxy/demo/",
                "qgis_server": "/qgis/?SERVICE=WMS&REQUEST=GetCapabilities", "caddy": "/", "webmap": "/"}


def _urls(cfg: dict[str, Any], comp: str) -> list[str]:
    if comp not in _PUBLIC_PATH or not cfg["components"].get("caddy"):
        return []
    base = _public(cfg)[0]
    return [base + (_PUBLIC_PATH[comp] if comp != "qgis_server" else "/qgis/")]


def _checks(cfg: dict[str, Any], target: str, ids: list[str]) -> list[dict[str, Any]]:
    """What to probe from the server itself.  kind: http (needs 2xx/3xx), alive (any real answer), tcp."""
    out: list[dict[str, Any]] = []
    base, domain, port = _public(cfg)
    edge = bool(generate.effective_components(cfg, target).get("caddy"))
    if target == "native":
        tp = int(cfg["tomcat"]["port"])
        if "postgis" in ids:
            out.append({"comp": "postgis", "label": f"tcp {cfg['postgres']['port']}", "kind": "tcp", "port": int(cfg["postgres"]["port"])})
        if "tomcat" in ids:
            out.append({"comp": "tomcat", "label": f"http :{tp}", "kind": "alive", "url": f"http://127.0.0.1:{tp}/"})
        if "geoserver" in ids:
            out.append({"comp": "geoserver", "label": f"WMS :{tp}", "kind": "http",
                        "url": f"http://127.0.0.1:{tp}/geoserver/ows?service=wms&request=GetCapabilities"})
        if "qgis_server" in ids:
            out.append({"comp": "qgis_server", "label": f"fcgi :{QGIS_PORT}", "kind": "tcp", "port": QGIS_PORT})
        if "mapproxy" in ids:
            out.append({"comp": "mapproxy", "label": f"http :{MAPPROXY_PORT}", "kind": "http",
                        "url": f"http://127.0.0.1:{MAPPROXY_PORT}/demo/"})
    if edge:
        for comp in ids:
            if comp in _PUBLIC_PATH:
                out.append({"comp": comp, "label": "public", "kind": "alive" if comp == "qgis_server" else "http",
                            "url": base + _PUBLIC_PATH[comp], "resolve": f"{domain}:{port}:127.0.0.1"})
    return out


def _check_ok(kind: str, code: str) -> bool:
    if kind == "tcp":
        return code == "open"
    try:
        n = int(code)
    except ValueError:
        return False
    if kind == "alive":
        return n > 0 and n not in (502, 503, 504)
    return 200 <= n < 400


def _probe_script(checks: list[dict[str, Any]]) -> str:
    lines = [
        "http() { echo \"@@http $1 $(curl -sk -m 5 -o /dev/null -w '%{http_code}' \"${@:2}\" 2>/dev/null)\"; }",
        "tcp() { if (exec 3<>/dev/tcp/127.0.0.1/$2) 2>/dev/null; then echo \"@@tcp $1 open\"; else echo \"@@tcp $1 closed\"; fi; }",
    ]
    for i, c in enumerate(checks):
        if c["kind"] == "tcp":
            lines.append(f"tcp {i} {int(c['port'])}")
        else:
            res = f"--resolve {c['resolve']} " if c.get("resolve") else ""
            lines.append(f"http {i} {res}'{c['url']}'")
    return "\n".join(lines) + "\n"


def _row(cfg: dict[str, Any], comp: str) -> dict[str, Any]:
    return {"id": comp, "name": _NAMES.get(comp, comp), "state": "unknown", "detail": "", "enabled": None,
            "pid": None, "since": "", "restarts": None, "listen": [], "urls": _urls(cfg, comp), "checks": [],
            "ok": False, "unit": ""}


def _finish(rows: list[dict[str, Any]], checks: list[dict[str, Any]], results: dict[int, str]) -> None:
    for i, c in enumerate(checks):
        code = results.get(i, "")
        for r in rows:
            if r["id"] == c["comp"]:
                r["checks"].append({"label": c["label"], "code": code, "ok": _check_ok(c["kind"], code),
                                    "url": c.get("url", "")})
    for r in rows:
        r["ok"] = r["state"] == "running" and all(c["ok"] for c in r["checks"])


# --------------------------------------------------------------------------- native
_PG_HEAD = '''. /etc/os-release 2>/dev/null || true
case " ${ID_LIKE:-} ${ID:-} " in
  *" debian "*|*" ubuntu "*) PGU="postgresql@@V@-main.service"; PGE="postgresql.service" ;;
  *) PGU="postgresql-@V@.service"; PGE="$PGU" ;;
esac
'''


def _native_units(cfg: dict[str, Any]) -> dict[str, list[str]]:
    return {"postgis": ["$PGU"], "tomcat": ["gisstack-tomcat.service"], "geoserver": ["gisstack-tomcat.service"],
            "qgis_server": ["gisstack-qgis.socket", "gisstack-qgis.service"],
            "mapproxy": ["gisstack-mapproxy.service"], "caddy": ["caddy.service"], "webmap": []}


def _native_status_script(cfg: dict[str, Any], checks: list[dict[str, Any]]) -> str:
    head = _PG_HEAD.replace("@V@", str(cfg["postgres"]["version"]))
    shows = "\n".join([
        'show() { echo "@@unit $1 $2"; systemctl show -p LoadState,ActiveState,SubState,UnitFileState,MainPID,'
        'ActiveEnterTimestamp,NRestarts,Listen "$2" 2>&1; }',
        'show postgis "$PGU"', "show tomcat gisstack-tomcat.service", "show qgis_server gisstack-qgis.service",
        "show qgis_socket gisstack-qgis.socket", "show mapproxy gisstack-mapproxy.service", "show caddy caddy.service",
    ])
    return (head + shows + '\necho "@@ss"; ss -Hltn 2>&1\n'
            'echo "@@file webmap $([ -f /var/www/gisstack/webmap/index.html ] && echo present || echo absent)"\n'
            + _probe_script(checks))


def _normalize_addr(a: str) -> str:
    if a.startswith("*:"):
        return "0.0.0.0:" + a[2:]
    m = re.match(r"^\[::ffff:(\d+\.\d+\.\d+\.\d+)\]:(\d+)$", a)      # Java's dual-stack sockets
    return f"{m.group(1)}:{m.group(2)}" if m else a


def parse_native(out: str) -> dict[str, Any]:
    """Split the status script's output into units / listening sockets / probe results."""
    units: dict[str, dict[str, str]] = {}
    ss: list[str] = []
    http: dict[int, str] = {}
    files: dict[str, str] = {}
    cur: dict[str, str] | None = None
    mode = ""
    for line in out.splitlines():
        if line.startswith("@@unit "):
            _, name, unit = line.split(None, 2)
            cur = units.setdefault(name, {"_unit": unit})
            mode = "unit"
        elif line.startswith("@@ss"):
            mode, cur = "ss", None
        elif line.startswith("@@http ") or line.startswith("@@tcp "):
            parts = line.split()
            with contextlib.suppress(ValueError, IndexError):
                http[int(parts[1])] = parts[2] if len(parts) > 2 else ""
            mode, cur = "", None
        elif line.startswith("@@file "):
            parts = line.split()
            files[parts[1]] = parts[2] if len(parts) > 2 else ""
            mode, cur = "", None
        elif mode == "unit" and cur is not None and "=" in line:
            k, v = line.split("=", 1)
            cur[k] = v
        elif mode == "ss" and line.strip():
            f = line.split()
            if len(f) >= 4:
                ss.append(_normalize_addr(f[3]))
    return {"units": units, "ss": ss, "http": http, "files": files}


def _state_of(show: dict[str, str]) -> tuple[str, str]:
    if not show or show.get("LoadState") == "not-found" or "ActiveState" not in show:
        return "missing", "unit not found"
    a, sub = show.get("ActiveState", ""), show.get("SubState", "")
    state = {"active": "running", "inactive": "stopped", "failed": "failed", "activating": "starting",
             "deactivating": "stopping", "reloading": "running"}.get(a, "unknown")
    if a == "active" and sub == "exited":
        state = "running"          # oneshot wrappers such as postgresql.service
    return state, f"{a} ({sub})"


def _listening(ss: list[str], ports: list[int]) -> list[str]:
    out = []
    for a in ss:
        try:
            if int(a.rsplit(":", 1)[1]) in ports:
                out.append(a)
        except (ValueError, IndexError):
            continue
    return sorted(set(out))


def _native_rows(cfg: dict[str, Any], ids: list[str], parsed: dict[str, Any], checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ports = {"postgis": [int(cfg["postgres"]["port"])], "tomcat": [int(cfg["tomcat"]["port"])],
             "geoserver": [int(cfg["tomcat"]["port"])], "qgis_server": [QGIS_PORT],
             "mapproxy": [MAPPROXY_PORT], "caddy": [80, 443], "webmap": []}
    unit_key = {"postgis": "postgis", "tomcat": "tomcat", "geoserver": "tomcat", "qgis_server": "qgis_server",
                "mapproxy": "mapproxy", "caddy": "caddy"}
    rows = []
    for comp in ids:
        r = _row(cfg, comp)
        if comp == "webmap":
            r["state"] = "running" if parsed["files"].get("webmap") == "present" else "missing"
            r["detail"] = "static files served by Caddy" if r["state"] == "running" else "files not installed"
            r["unit"] = "/var/www/gisstack/webmap"
            r["listen"] = _listening(parsed["ss"], [80, 443])
        else:
            show = parsed["units"].get(unit_key[comp], {})
            r["state"], r["detail"] = _state_of(show)
            r["unit"] = show.get("_unit", "")
            ufs = show.get("UnitFileState", "")
            r["enabled"] = True if ufs in ("enabled", "enabled-runtime") else (False if ufs in ("disabled", "masked") else None)
            pid = show.get("MainPID", "0")
            r["pid"] = int(pid) if pid.isdigit() and int(pid) > 0 else None
            r["since"] = show.get("ActiveEnterTimestamp", "")
            r["restarts"] = int(show["NRestarts"]) if show.get("NRestarts", "").isdigit() else None
            r["listen"] = _listening(parsed["ss"], ports[comp])
            if comp == "qgis_server":
                sock = parsed["units"].get("qgis_socket", {})
                if sock.get("Listen"):
                    r["detail"] += f"; socket {sock.get('ActiveState', '?')}"
        rows.append(r)
    _finish(rows, checks, parsed["http"])
    return rows


# --------------------------------------------------------------------------- compose
def _rt(cfg: dict[str, Any]) -> str:
    return "podman" if cfg.get("runtime") == "podman" else "docker"


_COMPOSE_SVC = {"qgis_server": "qgis", "webmap": "caddy"}


def _svc(comp: str) -> str:
    return _COMPOSE_SVC.get(comp, comp)


def _compose_status_script(cfg: dict[str, Any], checks: list[dict[str, Any]]) -> str:
    rt, p = _rt(cfg), cfg["project"]
    return (f"{rt} compose -p {p} ps -a --format json 2>&1\n"
            "echo '@@inspect'\n"
            f"for id in $({rt} compose -p {p} ps -a -q 2>/dev/null); do "
            f"{rt} inspect -f '{{{{.Config.Labels}}}}|{{{{range .NetworkSettings.Networks}}}}{{{{.IPAddress}}}} {{{{end}}}}' $id 2>/dev/null; done\n"
            + _probe_script(checks))


def _json_items(text: str) -> list[dict[str, Any]]:
    text = text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else [data]
    except ValueError:
        items = []
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("{"):
                with contextlib.suppress(ValueError):
                    items.append(json.loads(line))
        return items


def parse_compose(out: str) -> dict[str, Any]:
    head, _, rest = out.partition("@@inspect")
    ips: dict[str, str] = {}
    probe_lines = []
    for line in rest.splitlines():
        if line.startswith("@@"):
            probe_lines.append(line)
        elif "|" in line:
            labels, _, ip = line.rpartition("|")
            m = re.search(r"com\.docker\.compose\.service:([^\s\]]+)", labels)
            if m and ip.strip():
                ips[m.group(1)] = ip.split()[0]
    http = parse_native("\n".join(probe_lines))["http"]
    return {"items": _json_items(head), "ips": ips, "http": http}


def _compose_rows(cfg: dict[str, Any], ids: list[str], parsed: dict[str, Any], checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_svc = {i.get("Service"): i for i in parsed["items"]}
    rows = []
    for comp in ids:
        r = _row(cfg, comp)
        it = by_svc.get(_svc(comp))
        r["unit"] = _svc(comp)
        if not it:
            r["state"], r["detail"] = "missing", "container not created"
        else:
            st, health = str(it.get("State", "")), str(it.get("Health", ""))
            r["state"] = {"running": "running", "exited": "stopped", "created": "stopped", "dead": "failed",
                          "restarting": "starting", "paused": "stopped"}.get(st, "unknown")
            if st == "running" and health == "starting":
                r["state"] = "starting"
            if st == "running" and health == "unhealthy":
                r["state"] = "failed"
            r["detail"] = str(it.get("Status", st))
            r["since"] = str(it.get("RunningFor", "") or it.get("Status", ""))
            ip = parsed["ips"].get(_svc(comp), "") if st == "running" else ""
            for pub in it.get("Publishers") or []:
                tgt, port = pub.get("TargetPort"), pub.get("PublishedPort")
                proto = pub.get("Protocol", "tcp")
                if port:
                    r["listen"].append(f"{pub.get('URL') or '0.0.0.0'}:{port} -> {tgt}/{proto}")  # nosec B104  # noqa: S104 - display text only
                elif ip:
                    r["listen"].append(f"{ip}:{tgt}/{proto} (container)")
            if ip and not r["listen"]:
                r["listen"].append(f"{ip} (container)")
            r["listen"] = sorted(set(r["listen"]))
            r["enabled"] = None
        rows.append(r)
    _finish(rows, checks, parsed["http"])
    return rows


# --------------------------------------------------------------------------- kubernetes
def _k8s_status_script(cfg: dict[str, Any]) -> str:
    return f"kubectl -n {cfg['k8s']['namespace']} get pods,svc,deploy,sts -o json 2>&1\n"


def parse_k8s(out: str) -> list[dict[str, Any]]:
    try:
        return json.loads(out).get("items", [])
    except (ValueError, AttributeError):
        raise deploy.DeployError("kubectl: " + out.strip().splitlines()[-1] if out.strip() else "kubectl: empty answer") from None


def _k8s_name(comp: str) -> str:
    return {"qgis_server": "qgis", "webmap": "caddy"}.get(comp, comp)


def _k8s_rows(cfg: dict[str, Any], ids: list[str], items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for comp in ids:
        name = _k8s_name(comp)
        r = _row(cfg, comp)
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


# --------------------------------------------------------------------------- public API
def status(cfg: dict[str, Any], mode: str, ssh: dict[str, Any] | None = None, target: str | None = None,
           components: list[str] | None = None) -> dict[str, Any]:
    """Current state of every selected program.  Raises DeployError if the host cannot be queried."""
    t = _target(cfg, target)
    ids = _components(cfg, t, components)
    t0 = time.time()
    if t == "native":
        checks = _checks(cfg, t, ids)
        out = _run(mode, ssh, _native_status_script(cfg, checks))
        rows = _native_rows(cfg, ids, parse_native(out), checks)
    elif t == "compose":
        checks = _checks(cfg, t, ids)
        out = _run(mode, ssh, _compose_status_script(cfg, checks))
        if out.lstrip().startswith(("Error", "error", "Cannot", "permission")) and "@@inspect" not in out:
            raise deploy.DeployError(out.strip().splitlines()[0])
        rows = _compose_rows(cfg, ids, parse_compose(out), checks)
    else:
        out = _run(mode, ssh, _k8s_status_script(cfg))
        rows = _k8s_rows(cfg, ids, parse_k8s(out))
    return {"target": t, "project": cfg["project"], "domain": cfg["domain"], "time": time.strftime("%H:%M:%S"),
            "took": round(time.time() - t0, 2), "rows": rows}


def control_script(cfg: dict[str, Any], target: str, action: str, ids: list[str]) -> str:
    """Shell script that applies start / stop / restart / enable / disable to the given programs."""
    if action not in ACTIONS:
        raise deploy.DeployError(f"უცნობი ქმედება: {action}")
    if not ids:
        raise deploy.DeployError("კომპონენტი არ არის არჩეული")
    order = ids if action != "stop" else list(reversed(ids))
    lines = ["set -u"]
    if target == "native":
        lines.append(_PG_HEAD.replace("@V@", str(cfg["postgres"]["version"])))
        units = _native_units(cfg)
        seen: list[str] = []
        for comp in order:
            if comp == "webmap":
                lines.append('echo "webmap: static files served by Caddy - nothing to ' + action + '"')
                continue
            us = units[comp]
            if comp == "postgis" and action in ("enable", "disable"):
                us = ["$PGE"]
            if action == "stop":
                us = list(reversed(us))            # service first, then its socket
            us = [u for u in us if u not in seen]
            seen += us
            if not us:
                continue
            lines.append(f'echo "== {action} {comp}: {" ".join(us)}"')
            lines.append(f'systemctl {action} {" ".join(us)} && echo ok || {{ echo "FAILED: {comp}"; rc=1; }}')
        lines.append("exit ${rc:-0}")
        return "\n".join(lines) + "\n"
    if target == "compose":
        rt, p = _rt(cfg), cfg["project"]
        svcs = []
        for comp in order:
            s = _svc(comp)
            if s not in svcs:
                svcs.append(s)
        names = " ".join(svcs)
        if action in ("start", "stop", "restart"):
            lines.append(f"{rt} compose -p {p} {action} {names}")
        else:
            pol = "unless-stopped" if action == "enable" else "no"
            lines.append(f'for s in {names}; do for id in $({rt} compose -p {p} ps -a -q "$s"); do '
                         f'{rt} update --restart {pol} "$id" >/dev/null && echo "$s: restart policy = {pol}"; done; done')
        return "\n".join(lines) + "\n"
    ns = cfg["k8s"]["namespace"]
    done: set[str] = set()
    for comp in order:
        n = _k8s_name(comp)
        if n in done:
            continue
        done.add(n)
        kind = "statefulset" if n == "postgis" else "deployment"
        if action == "start":
            lines.append(f"kubectl -n {ns} scale {kind}/{n} --replicas=1")
        elif action == "stop":
            lines.append(f"kubectl -n {ns} scale {kind}/{n} --replicas=0")
        elif action == "restart":
            lines.append(f"kubectl -n {ns} rollout restart {kind}/{n}")
        else:
            lines.append(f'echo "{n}: {action} is not applicable on Kubernetes (use start / stop)"')
    return "\n".join(lines) + "\n"


def control(cfg: dict[str, Any], mode: str, ssh: dict[str, Any] | None, action: str,
            components: list[str] | None = None, target: str | None = None) -> Iterator[str]:
    """Apply an action to some programs (components=None: the whole stack).  Yields log lines."""
    t = _target(cfg, target)
    ids = _components(cfg, t, components)
    script = control_script(cfg, t, action, ids)
    yield f"== {action}: {', '.join(ids)}"
    host = "localhost" if mode == "local" else str((ssh or {}).get("host", ""))
    with journal.Operation(f"control:{action}", target=t, mode=mode, host=host, project=cfg.get("project", ""), components=ids):
        yield from _stream(mode, ssh, script, root=(t == "native"), deadline=900)
    yield "== დასრულდა"


def logs_script(cfg: dict[str, Any], target: str, comp: str, lines: int = 100, follow: bool = False) -> str:
    n = max(1, min(int(lines), 5000))
    f = " -f" if follow else ""
    if comp not in ORDER:
        raise deploy.DeployError(f"უცნობი კომპონენტი: {comp}")
    if target == "native":
        if comp == "webmap":
            comp = "caddy"
        units = {"postgis": "$PGU", "tomcat": "gisstack-tomcat", "geoserver": "gisstack-tomcat",
                 "qgis_server": "gisstack-qgis", "mapproxy": "gisstack-mapproxy", "caddy": "caddy"}[comp]
        s = _PG_HEAD.replace("@V@", str(cfg["postgres"]["version"]))
        extra = ""
        if comp == "postgis":
            extra = f'L=/var/log/postgresql/postgresql-{cfg["postgres"]["version"]}-main.log\n[ -r "$L" ] && tail -n {n}{" -F" if follow else ""} "$L" &\n'
        if comp == "geoserver":
            extra = f'L=/var/lib/gisstack/geoserver_data/logs/geoserver.log\n[ -r "$L" ] && tail -n {n}{" -F" if follow else ""} "$L" &\n'
        return s + extra + f"journalctl --no-pager -n {n}{f} -u {units}\nwait\n"
    if target == "compose":
        return f"{_rt(cfg)} compose -p {cfg['project']} logs --no-color --tail {n}{f} {_svc(comp)}\n"
    kind = "statefulset" if _k8s_name(comp) == "postgis" else "deployment"
    return f"kubectl -n {cfg['k8s']['namespace']} logs --tail {n}{f} {kind}/{_k8s_name(comp)}\n"


def logs(cfg: dict[str, Any], mode: str, ssh: dict[str, Any] | None, component: str, lines: int = 100,
         follow: bool = False, stop: threading.Event | None = None, target: str | None = None) -> Iterator[str]:
    t = _target(cfg, target)
    comp = "qgis_server" if component == "qgis" else component
    script = logs_script(cfg, t, comp, lines, follow)
    yield from _stream(mode, ssh, script, root=(t == "native"), stop=stop)


# --------------------------------------------------------------------------- console formatting
_COLORS = {"running": "32", "stopped": "33", "starting": "36", "stopping": "36", "failed": "31", "missing": "90", "unknown": "90"}


def format_table(result: dict[str, Any], color: bool = False) -> str:
    """Plain-text status table (one program per block of lines)."""
    rows = result["rows"]
    head = f"{result['project']} · {result['target']} · {result['domain']} · {result['time']}"
    cols = ("PROGRAM", "STATE", "AUTOSTART", "PID", "LISTEN (IP:PORT)", "CHECKS")
    table = []
    for r in rows:
        listen = r["listen"] or ["-"]
        checks = [f"{'✔' if c['ok'] else '✘'} {c['label']} {c['code']}" for c in r["checks"]] or ["-"]
        auto = {True: "yes", False: "no", None: "-"}[r["enabled"]]
        n = max(len(listen), len(checks))
        for i in range(n):
            table.append((r["id"] if i == 0 else "", r["state"] if i == 0 else "", auto if i == 0 else "",
                          str(r["pid"] or "-") if i == 0 else "", listen[i] if i < len(listen) else "",
                          checks[i] if i < len(checks) else ""))
    widths = [max(len(str(x[i])) for x in [cols] + table) for i in range(len(cols))]
    out = [head, ""]
    out.append("  ".join(c.ljust(widths[i]) for i, c in enumerate(cols)).rstrip())
    for row in table:
        cells = [str(c).ljust(widths[i]) for i, c in enumerate(row)]
        if color and row[1] in _COLORS:
            cells[1] = f"\033[{_COLORS[row[1]]}m{cells[1]}\033[0m"
        out.append("  ".join(cells).rstrip())
    urls = [(r["id"], u) for r in rows for u in r["urls"]]
    if urls:
        out.append("")
        out += [f"{k}: {u}" for k, u in urls]
    return "\n".join(out)
