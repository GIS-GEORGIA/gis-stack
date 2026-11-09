"""Where a managed program runs, and how to run a command *inside* it.

The managers (PostGIS, GeoServer, MapProxy) were written for the native target: their scripts run on the server and call the program's tools
there.  `Place` lets the same scripts reach a program that lives in a Compose container or a Kubernetes pod: it defines the bash function
`gs_in USER CMD ARGS...` in the script's prelude, which runs CMD inside the right container (Compose: `docker exec -i`, Kubernetes:
`kubectl exec -i`).  stdin is passed through, so secrets and payloads keep travelling on stdin and never on a command line.

Compose finds the container through the labels Compose itself sets (`com.docker.compose.project` / `.service`), so no compose file is needed;
Kubernetes addresses `TYPE/NAME` in the namespace from the configuration.  `kubectl exec` cannot choose the OS user, so there `USER` is ignored
(the pod's own user is used) and no environment can be passed: callers must not depend on either."""
from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any

from .core.errors import DeployError

# component -> (compose service, kubernetes TYPE/NAME)
WORKLOADS = {
    "postgis": ("postgis", "statefulset/postgis"),
    "geoserver": ("geoserver", "deployment/geoserver"),
    "mapproxy": ("mapproxy", "deployment/mapproxy"),
}
INTERNAL_PORTS = {"postgis": 5432, "geoserver": 8080, "mapproxy": 8081}
_FORWARDED_ENV = ("PGPASSWORD", "PGCONNECT_TIMEOUT", "PGOPTIONS")


@dataclass(frozen=True)
class Place:
    kind: str                      # native | compose | k8s
    component: str
    project: str = ""
    namespace: str = ""
    runtime: str = "docker"

    @property
    def native(self) -> bool:
        return self.kind == "native"

    @property
    def internal_port(self) -> int:
        return INTERNAL_PORTS[self.component]

    @property
    def can_choose_user(self) -> bool:
        return self.kind != "k8s"

    @property
    def can_pass_env(self) -> bool:
        return self.kind != "k8s"

    def prelude(self) -> str:
        """Bash that defines `gs_in` (nothing for native).  Fails the script with a clear message when the container / pod is not there."""
        if self.native:
            return ""
        svc, workload = WORKLOADS[self.component]
        if self.kind == "compose":
            rt = shlex.quote(self.runtime)
            envs = " ".join(f"-e {v}" for v in _FORWARDED_ENV)
            return (
                f"GS_CTR=\"$({rt} ps -q --filter label=com.docker.compose.project={shlex.quote(self.project)} "
                f"--filter label=com.docker.compose.service={shlex.quote(svc)} | head -n1)\"\n"
                f"[ -n \"$GS_CTR\" ] || {{ echo \"{svc}: container is not running\" >&2; exit 98; }}\n"
                f"gs_in() {{ local u=\"$1\"; shift; {rt} exec -i ${{u:+-u \"$u\"}} {envs} \"$GS_CTR\" \"$@\"; }}\n")
        if self.kind == "k8s":
            ns = shlex.quote(self.namespace)
            return (
                f"kubectl -n {ns} get {workload} -o name >/dev/null 2>&1 || {{ echo \"{workload}: not found in namespace {self.namespace}\" >&2; exit 98; }}\n"
                f"gs_in() {{ shift; kubectl -n {ns} exec -i {workload} -- \"$@\"; }}\n")
        raise DeployError(f"unknown target {self.kind}")


def place_of(cfg: dict[str, Any], component: str) -> Place:
    target = cfg.get("deploy_target", "native")
    if component not in WORKLOADS:
        raise DeployError(f"no container mapping for {component}")
    if target == "native":
        return Place("native", component)
    if target == "compose":
        return Place("compose", component, project=str(cfg["project"]), runtime="podman" if cfg.get("runtime") == "podman" else "docker")
    if target == "k8s":
        return Place("k8s", component, namespace=str(cfg["k8s"]["namespace"]))
    raise DeployError(f"unknown target {target}")
