"""Container hardening (audit S-4): what each generated workload is allowed to keep.

Every container starts from "nothing": all Linux capabilities dropped and no privilege gain through setuid binaries (Compose / Podman: `cap_drop: [ALL]`,
`security_opt: no-new-privileges`; Kubernetes: `capabilities.drop: [ALL]`, `allowPrivilegeEscalation: false`, `seccompProfile: RuntimeDefault`).  A workload gets back
ONLY what its image demonstrably needs - found by starting the real image with everything dropped and adding capabilities one by one (see
docs/SECURITY-REVIEW-2026-11.md for the experiment):

* postgis  - the official postgres entrypoint starts as root, chowns / chmods the data directory and drops to `postgres`: CHOWN, DAC_OVERRIDE, FOWNER, SETGID, SETUID
* qgis     - Apache starts as root, creates and chowns the mod_fcgid socket and switches to www-data: CHOWN, SETGID, SETUID (with only SETGID + SETUID the
             container "runs" but cannot serve a request: `couldn't bind unix domain socket` - found by the real test, not by looking at `docker ps`)
* caddy    - binds ports 80 / 443: NET_BIND_SERVICE
* geoserver, tomcat, mapproxy - nothing (MapProxy already runs as an unprivileged user)

Not done, on purpose: read-only root filesystems (every image writes to paths that would have to be found and verified one by one) and CPU / memory
limits (they depend on the machine).  `security.harden_containers: false` switches the whole thing off for images that need more (custom images).
"""
from __future__ import annotations

CAPS: dict[str, tuple[str, ...]] = {
    "postgis": ("CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"),
    "geoserver": (),
    "tomcat": (),
    "qgis": ("CHOWN", "SETGID", "SETUID"),
    "mapproxy": (),
    "caddy": ("NET_BIND_SERVICE",),
    "init": (),                                   # the busybox init container that unpacks the web map
}


def compose_snippet(service: str, enabled: bool) -> str:
    """The YAML lines (indented for a Compose service) that go below `restart:`; empty when hardening is off."""
    if not enabled:
        return ""
    lines = ["    security_opt:", "      - no-new-privileges:true", "    cap_drop:", "      - ALL"]
    caps = CAPS[service]
    if caps:
        lines += ["    cap_add:"] + [f"      - {c}" for c in caps]
    return "\n".join(lines)


def k8s_container_snippet(service: str, enabled: bool, indent: int = 10) -> str:
    """The `securityContext:` block of a container (indented to `indent` spaces); empty when hardening is off."""
    if not enabled:
        return ""
    pad = " " * indent
    lines = [f"{pad}securityContext:", f"{pad}  allowPrivilegeEscalation: false", f"{pad}  capabilities:", f"{pad}    drop: [ALL]"]
    caps = CAPS[service]
    if caps:
        lines.append(f"{pad}    add: [{', '.join(caps)}]")
    return "\n".join(lines)


def k8s_pod_snippet(enabled: bool, indent: int = 6) -> str:
    """Pod-level `securityContext` (seccomp default profile)."""
    if not enabled:
        return ""
    pad = " " * indent
    return f"{pad}securityContext:\n{pad}  seccompProfile:\n{pad}    type: RuntimeDefault"
