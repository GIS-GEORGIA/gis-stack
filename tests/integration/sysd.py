"""Systemd-enabled distro containers for installer tests (Phase 11b).

Opt-in: `GISSTACK_TEST_INSTALLER=1`.  Which distributions run: `GISSTACK_TEST_DISTROS=ubuntu-24.04,debian-12` (default `ubuntu-24.04`;
`all` = every entry of DISTROS).  Images are built once and cached (tag `gisstack-it-<distro>:1`).  Containers are privileged and throw-away;
nothing here ever touches a real server - the only machine involved is the container."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

DISTROS = {
    "ubuntu-22.04": ("ubuntu:22.04", "deb"),
    "ubuntu-24.04": ("ubuntu:24.04", "deb"),
    "debian-12": ("debian:12", "deb"),
    "rocky-9": ("rockylinux:9", "rpm"),
    "alma-9": ("almalinux:9", "rpm"),
}
DOCKERFILE = {
    "deb": """FROM {base}
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq && apt-get install -y -qq systemd systemd-sysv dbus sudo python3 ca-certificates curl >/dev/null && rm -rf /var/lib/apt/lists/*
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
""",
    "rpm": """FROM {base}
RUN dnf -y -q install systemd sudo python3 ca-certificates curl-minimal procps-ng >/dev/null && dnf clean all >/dev/null
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
""",
}


def enabled() -> bool:
    return os.environ.get("GISSTACK_TEST_INSTALLER") == "1" and shutil.which("docker") is not None


def wanted() -> list[str]:
    raw = os.environ.get("GISSTACK_TEST_DISTROS", "ubuntu-24.04").strip()
    names = list(DISTROS) if raw == "all" else [x.strip() for x in raw.split(",") if x.strip()]
    return [n for n in names if n in DISTROS]


def _docker(*args: str, timeout: int = 600, check: bool = True, input_text: Optional[str] = None) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=check, input=input_text)


BUILD_ATTEMPTS = 3


def build(tag: str, context: str, attempts: int = BUILD_ATTEMPTS, pause: float = 20.0) -> None:
    """`docker build` of a test image.  The build runs apt/dnf against public mirrors, which fail now and then (seen on CI: ubuntu-22.04, 51 s,
    no gis-stack code involved); retry a few times, and when it still fails say *why* - the plain CalledProcessError hid the build's stderr."""
    last = ""
    for n in range(1, attempts + 1):
        p = _docker("build", "-q", "-t", tag, context, timeout=900, check=False)
        if p.returncode == 0:
            return
        last = (p.stderr or p.stdout or "").strip()[-1500:]
        if n < attempts:
            time.sleep(pause * n)
    raise RuntimeError(f"docker build of {tag} failed {attempts} times; last output:\n{last}")


def image(distro: str) -> str:
    base, family = DISTROS[distro]
    tag = f"gisstack-it-{distro}:1"
    if _docker("image", "inspect", tag, check=False).returncode == 0:
        return tag
    d = tempfile.mkdtemp()
    try:
        (Path(d) / "Dockerfile").write_text(DOCKERFILE[family].format(base=base))
        build(tag, d)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return tag


class Box:
    """A running systemd container.  `with Box("ubuntu-24.04") as b: b.sh("systemctl is-active postgresql")`."""

    def __init__(self, distro: str) -> None:
        self.distro, self.family = distro, DISTROS[distro][1]
        self.id = ""

    def __enter__(self) -> Box:
        tag = image(self.distro)
        self.id = _docker("run", "-d", "--privileged", "--cgroupns=host", "-v", "/sys/fs/cgroup:/sys/fs/cgroup:rw", "--tmpfs", "/run", "--tmpfs", "/run/lock", tag).stdout.strip()
        for _ in range(60):
            state = self.sh("systemctl is-system-running", check=False).stdout.strip()
            if state in ("running", "degraded"):
                return self
            time.sleep(1)
        self.__exit__(None, None, None)
        raise RuntimeError("systemd did not come up in the container")

    def __exit__(self, *exc: object) -> None:
        if self.id:
            _docker("rm", "-f", self.id, check=False)

    def sh(self, script: str, timeout: int = 900, check: bool = True, input_text: Optional[str] = None) -> subprocess.CompletedProcess:
        p = _docker("exec", "-i", self.id, "bash", "-c", script, timeout=timeout, check=False, input_text=input_text)
        if check and p.returncode != 0:
            raise AssertionError(f"in {self.distro}: {script[:120]!r} exited {p.returncode}\n{(p.stdout + p.stderr)[-1500:]}")
        return p

    def put(self, src: str | Path, dest: str) -> None:
        """Copy a directory's contents to `dest` (a directory), or a single file to the path `dest`."""
        if Path(src).is_dir():
            self.sh(f"mkdir -p {dest}")
            _docker("cp", f"{src}/.", f"{self.id}:{dest}")
        else:
            self.sh(f"mkdir -p $(dirname {dest})")
            _docker("cp", str(src), f"{self.id}:{dest}")

    def argv(self, script: str) -> list[str]:
        """What `gisstack.transport.argv` should return so gis-stack's own code (status, managers) runs inside this container."""
        return ["docker", "exec", "-i", self.id, "bash", "-c", script]
