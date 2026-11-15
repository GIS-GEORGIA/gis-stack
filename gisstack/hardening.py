"""Container hardening (audit S-4): what each generated workload is allowed to keep.

Every container starts from "nothing": all Linux capabilities dropped and (except PostgreSQL, see NO_PRIVILEGE_LOCK) no privilege gain through setuid binaries (Compose / Podman: `cap_drop: [ALL]`,
`security_opt: no-new-privileges`; Kubernetes: `capabilities.drop: [ALL]`, `allowPrivilegeEscalation: false`, `seccompProfile: RuntimeDefault`).  A workload gets back
ONLY what its image demonstrably needs - found by starting the real image with everything dropped and adding capabilities one by one (see
docs/SECURITY-REVIEW-2026-11.md for the experiment):

* postgis  - the official postgres entrypoint starts as root, chowns / chmods the data directory and drops to `postgres`: CHOWN, DAC_OVERRIDE, FOWNER, SETGID, SETUID
* qgis     - Apache starts as root, creates and chowns the mod_fcgid socket and switches to www-data: CHOWN, SETGID, SETUID (with only SETGID + SETUID the
             container "runs" but cannot serve a request: `couldn't bind unix domain socket` - found by the real test, not by looking at `docker ps`)
* caddy    - binds ports 80 / 443: NET_BIND_SERVICE
* geoserver, tomcat, mapproxy - nothing (MapProxy already runs as an unprivileged user)

Read-only root filesystem (Compose / Podman only), where measured to work with real images (`docker run --read-only`, 2026-11-14): caddy (writes only its
volumes /data and /config) and postgis (tmpfs /tmp and /var/run/postgresql) and mapproxy (tmpfs /tmp for the gunicorn workers and `HOME=/tmp`, because gunicorn's control socket lives in
`~/.gunicorn`: without it the log shows `Read-only file system: /home/mapproxy/.gunicorn`, with a root-owned tmpfs on the home `Permission denied`; the cache volume stays writable).
NOT done, with the measured reason: geoserver and tomcat (the startup script
rewrites /usr/local/tomcat/conf/server.xml and Tomcat writes its logs below /usr/local/tomcat), qgis (the entrypoint `sed -i`s /etc/apache2 files).  Kubernetes: not done (needs emptyDir volumes per workload, not tested).
CPU / memory limits are optional (`limits.<service>.memory_mb` / `cpus`, 0 = none; they depend on the machine, so nothing is limited by default).  `security.harden_containers: false` switches the whole thing off for images that need more (custom images).
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


# Workloads that must NOT get no-new-privileges / allowPrivilegeEscalation:false.  The official postgres entrypoint starts as root and hands over with `gosu postgres ...`
# (an exec): on rootful Podman 4.9.3 + AppArmor (GitHub's ubuntu-24.04 runner, kernel 7.0 azure) that exec never completes when no-new-privileges is set - PID 1 stays
# `gosu postgres docker-entrypoint.sh`, no postgres process, no output.  Measured by running the same image with each restriction alone (podman.yml bisect step):
# baseline ok, capabilities alone ok, no-new-privileges alone BROKEN, no-new-privileges + apparmor=unconfined ok.  The capability set is kept.
NO_PRIVILEGE_LOCK = frozenset({"postgis"})


# "/tmp" is a path INSIDE the container that gets a tmpfs mount; gis-stack never creates or opens a host temp file here (ruff S108 / bandit B108 are false positives).
CONTAINER_TMP = "/tmp"  # noqa: S108  # nosec B108
READ_ONLY_TMPFS: dict[str, tuple[str, ...]] = {"caddy": (), "postgis": (CONTAINER_TMP, "/var/run/postgresql"), "mapproxy": (CONTAINER_TMP,)}
# gunicorn needs a writable temp dir for its workers and a home for its control socket (`~/.gunicorn`); HOME=/tmp puts the socket in the tmpfs and works with every
# gunicorn version (`--no-control-socket` exists only in new ones, and the image installs gunicorn unpinned).
READ_ONLY_ENV: dict[str, dict[str, str]] = {"mapproxy": {"HOME": CONTAINER_TMP}}


def compose_snippet(service: str, enabled: bool) -> str:
    """The YAML lines (indented for a Compose service) that go below `restart:`; empty when hardening is off."""
    if not enabled:
        return ""
    lines = ["    cap_drop:", "      - ALL"] if service in NO_PRIVILEGE_LOCK else ["    security_opt:", "      - no-new-privileges:true", "    cap_drop:", "      - ALL"]
    caps = CAPS[service]
    if caps:
        lines += ["    cap_add:"] + [f"      - {c}" for c in caps]
    if service in READ_ONLY_TMPFS:
        lines.append("    read_only: true")
        if READ_ONLY_TMPFS[service]:
            lines += ["    tmpfs:"] + [f"      - {t}" for t in READ_ONLY_TMPFS[service]]
        if READ_ONLY_ENV.get(service):
            lines += ["    environment:"] + [f"      {k}: {v}" for k, v in READ_ONLY_ENV[service].items()]
    return "\n".join(lines)


def _cpu_text(cpus: float) -> str:
    return f"{cpus:g}"


def limits_compose(service: str, limits: dict) -> str:
    """`mem_limit` / `cpus` lines for a Compose service (empty when nothing is set for it)."""
    lim = limits.get(service) or {}
    lines = []
    if lim.get("memory_mb"):
        lines.append(f"    mem_limit: {int(lim['memory_mb'])}m")
    if lim.get("cpus"):
        lines.append(f"    cpus: {_cpu_text(float(lim['cpus']))}")
    return "\n".join(lines)


def limits_k8s(service: str, limits: dict, indent: int, wrap: bool) -> str:
    """Kubernetes `limits:` (wrap=True: inside a fresh `resources:` block, else to be placed below an existing `requests:`)."""
    lim = limits.get(service) or {}
    items = []
    if lim.get("memory_mb"):
        items.append(f"memory: {int(lim['memory_mb'])}Mi")
    if lim.get("cpus"):
        items.append(f"cpu: \"{_cpu_text(float(lim['cpus']))}\"")
    if not items:
        return ""
    pad = " " * indent
    body = [f"{pad}limits:"] + [f"{pad}  {i}" for i in items]
    return "\n".join([f"{' ' * (indent - 2)}resources:"] + body) if wrap else "\n".join(body)


def k8s_container_snippet(service: str, enabled: bool, indent: int = 10) -> str:
    """The `securityContext:` block of a container (indented to `indent` spaces); empty when hardening is off."""
    if not enabled:
        return ""
    pad = " " * indent
    lines = [f"{pad}securityContext:"] + ([] if service in NO_PRIVILEGE_LOCK else [f"{pad}  allowPrivilegeEscalation: false"]) + [f"{pad}  capabilities:", f"{pad}    drop: [ALL]"]
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
