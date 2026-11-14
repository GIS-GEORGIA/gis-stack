"""Optional CPU / memory limits (S-4 remainder) enforced by the real engine: a generated Compose stack with `limits` is started and the limits are read back
from Docker (HostConfig) AND from the container's own cgroup files (what the kernel enforces); PostGIS must still become healthy under a memory limit.
Opt-in: GISSTACK_TEST_INTEGRATION=1 (Docker with cgroup v2)."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from gisstack import config, generate

ENABLED = os.environ.get("GISSTACK_TEST_INTEGRATION") == "1" and shutil.which("docker") is not None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(ENABLED, "set GISSTACK_TEST_INTEGRATION=1 (needs Docker)")
class LimitsAreEnforced(unittest.TestCase):
    def up(self, components: dict, limits: dict) -> tuple[str, Path]:
        cfg = config.resolve({**config.default_config(), "project": "gslim_" + uuid.uuid4().hex[:6], "domain": "gis.test", "tls": "internal",
                              "components": components, "limits": limits}, generate_secrets=True, persist_secrets=False)
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        generate.generate_bundle(cfg, tmp / "b")
        d = tmp / "b" / "compose"
        env = {**os.environ, "HTTP_PORT": str(free_port()), "HTTPS_PORT": str(free_port())}
        base = ["docker", "compose", "-p", cfg["project"], "-f", str(d / "docker-compose.yml"), "--project-directory", str(d)]
        self.addCleanup(subprocess.run, base + ["down", "-v", "--remove-orphans"], capture_output=True, env=env)
        r = subprocess.run(base + ["up", "-d", "--wait", "--wait-timeout", "240"], capture_output=True, text=True, env=env, timeout=400)
        self.assertEqual(r.returncode, 0, r.stdout[-800:] + r.stderr[-1500:])
        return cfg["project"], d

    def container(self, project: str, service: str) -> str:
        out = subprocess.run(["docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={project}", "--filter", f"label=com.docker.compose.service={service}"],
                             capture_output=True, text=True).stdout.split()
        self.assertEqual(len(out), 1, (service, out))
        return out[0]

    def test_caddy_limits_reach_docker_and_the_kernel(self):
        project, _ = self.up({"postgis": False, "tomcat": False, "geoserver": False, "qgis_server": False, "mapproxy": False, "caddy": True, "webmap": False},
                             {"caddy": {"memory_mb": 192, "cpus": 0.5}})
        cid = self.container(project, "caddy")
        host = json.loads(subprocess.run(["docker", "inspect", cid, "--format", "{{json .HostConfig}}"], capture_output=True, text=True).stdout)
        self.assertEqual(host["Memory"], 192 * 1024 * 1024)
        self.assertEqual(host["NanoCpus"], 500_000_000)
        mem = subprocess.run(["docker", "exec", cid, "cat", "/sys/fs/cgroup/memory.max"], capture_output=True, text=True)
        if mem.returncode != 0:
            self.skipTest("cgroup v2 files not readable in the container")
        self.assertEqual(int(mem.stdout.strip()), 192 * 1024 * 1024)
        self.assertEqual(subprocess.run(["docker", "exec", cid, "cat", "/sys/fs/cgroup/cpu.max"], capture_output=True, text=True).stdout.split()[:1], ["50000"])

    def test_postgis_is_healthy_under_a_memory_limit(self):
        project, _ = self.up({"postgis": True, "tomcat": False, "geoserver": False, "qgis_server": False, "mapproxy": False, "caddy": False, "webmap": False},
                             {"postgis": {"memory_mb": 1024, "cpus": 1}})
        cid = self.container(project, "postgis")
        host = json.loads(subprocess.run(["docker", "inspect", cid, "--format", "{{json .HostConfig}}"], capture_output=True, text=True).stdout)
        self.assertEqual(host["Memory"], 1024 * 1024 * 1024)
        for _ in range(20):
            r = subprocess.run(["docker", "exec", cid, "pg_isready", "-q"], capture_output=True)
            if r.returncode == 0:
                break
            time.sleep(1)
        self.assertEqual(r.returncode, 0)
        oom = subprocess.run(["docker", "inspect", cid, "--format", "{{.State.OOMKilled}}"], capture_output=True, text=True).stdout.strip()
        self.assertEqual(oom, "false")


if __name__ == "__main__":
    unittest.main()
