"""Docker Compose target against a real Docker daemon (Phase 11b).  Opt-in: GISSTACK_TEST_DOCKER=1 (pulls postgis/postgis if missing).

Generates a PostGIS-only Compose bundle, starts it, waits for the healthcheck, drives gis-stack's own status code at it, restarts the service
through gis-stack's control script, and removes everything (containers and volumes) at the end."""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from gisstack import config, generate, ops

IMAGE = "postgis/postgis:17-3.5"


def available() -> bool:
    if os.environ.get("GISSTACK_TEST_DOCKER") != "1" or not shutil.which("docker"):
        return False
    if subprocess.run(["docker", "compose", "version"], capture_output=True).returncode != 0:
        return False
    return subprocess.run(["docker", "pull", "-q", IMAGE], capture_output=True).returncode == 0


@unittest.skipUnless(available(), "GISSTACK_TEST_DOCKER=1 and docker compose needed")
class ComposeTests(unittest.TestCase):
    def test_postgis_up_status_restart_down(self):
        project = "gsit" + uuid.uuid4().hex[:6]
        with socket.socket() as sk:
            sk.bind(("127.0.0.1", 0))
            port = sk.getsockname()[1]                      # never collide with a PostgreSQL that already runs on this machine
        cfg = config.resolve({"project": project, "postgres": {"port": port}, "targets": ["compose"], "deploy_target": "compose",
                              "components": {"postgis": True, "geoserver": False, "tomcat": False, "qgis_server": False, "mapproxy": False, "caddy": False, "webmap": False}},
                             generate_secrets=True)
        out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, out, True)
        generate.generate_bundle(cfg, out)
        d = out / "compose"
        base = ["docker", "compose", "-p", project, "-f", str(d / "docker-compose.yml"), "--env-file", str(d / ".env")]
        self.addCleanup(lambda: subprocess.run([*base, "down", "-v", "--remove-orphans"], capture_output=True, timeout=180))
        up = subprocess.run([*base, "up", "-d", "--wait", "--wait-timeout", "120"], capture_output=True, text=True, timeout=240)
        self.assertEqual(up.returncode, 0, up.stdout + up.stderr)

        st = ops.status(cfg, "local", None, "compose")
        row = next(r for r in st["rows"] if r["id"] == "postgis")
        self.assertEqual(row["state"], "running", row)

        ver = subprocess.run([*base, "exec", "-T", "postgis", "psql", "-U", cfg["postgres"]["user"], "-d", cfg["postgres"]["db"], "-Atc", "select postgis_version()"],
                             capture_output=True, text=True, timeout=60)
        self.assertRegex(ver.stdout, r"^\d+\.\d+", ver.stderr)

        script = ops.control_script(cfg, "compose", "restart", ["postgis"])
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=180)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for _ in range(60):
            row = next(x for x in ops.status(cfg, "local", None, "compose")["rows"] if x["id"] == "postgis")
            if row["state"] == "running":
                break
            time.sleep(2)
        self.assertEqual(row["state"], "running", row)


if __name__ == "__main__":
    unittest.main()
