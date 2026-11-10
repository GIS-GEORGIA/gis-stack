"""Backup and restore against a REAL Podman engine (rootful or rootless, with the podman-compose or docker-compose provider).  Opt-in: GISSTACK_TEST_PODMAN=1.

The stack is deployed through gis-stack's own path (`deploy.deploy` -> `up.sh up` with runtime=podman) and verified healthy by gis-stack itself; then the same
cases as on Docker Compose and Kubernetes (stackbackup_cases.py) run against it with `PodmanBackupProvider`.  Each class uses a unique project and a temp
GISSTACK_HOME and removes its containers and volumes afterwards.  Healthchecks need a running systemd (as on any normal Podman host); in a container
without systemd Podman never runs them and no stack becomes "healthy" - run it under systemd (see docs/TESTING.md)."""
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
from unittest import mock

from gisstack import config, deploy, deployments, generate
from gisstack.backups import provider

from .stackbackup_cases import GeoServerCases, PostgresCases

PG_IMAGE = "docker.io/postgis/postgis:17-3.5"
GS_IMAGE = "docker.osgeo.org/geoserver:2.28.6"


def podman_ready() -> bool:
    if os.environ.get("GISSTACK_TEST_PODMAN") != "1" or not shutil.which("podman"):
        return False
    return subprocess.run(["podman", "compose", "version"], capture_output=True).returncode == 0


def pulled(image: str) -> bool:
    return podman_ready() and subprocess.run(["podman", "pull", "-q", image], capture_output=True).returncode == 0


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class PodmanStackBase(unittest.TestCase):
    components: dict = {}

    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp()
        cls.env = mock.patch.dict(os.environ, {"GISSTACK_HOME": cls.home, "GISSTACK_VERIFY_TIMEOUT": "180", "GISSTACK_VERIFY_SETTLE": "3"})
        cls.env.start()
        cls.project = "gspd" + uuid.uuid4().hex[:6]
        comps = {"postgis": True, "geoserver": False, "tomcat": False, "qgis_server": False, "mapproxy": False, "caddy": False, "webmap": False, **cls.components}
        cls.cfg = config.resolve({"project": cls.project, "runtime": "podman", "postgres": {"port": free_port()}, "targets": ["compose"], "deploy_target": "compose",
                                  "components": comps}, generate_secrets=True, persist_secrets=True)
        cls.out = Path(tempfile.mkdtemp())
        generate.generate_bundle(cfg=cls.cfg, out_dir=cls.out)
        lines: list[str] = []
        try:
            for line in deploy.deploy(cls.cfg, cls.out, "compose", "local"):
                lines.append(line)
        except Exception as e:                                       # keep what the deployment printed: without it a CI log only says "exit 1"
            cls.tearDownClass()
            raise AssertionError(f"deployment on Podman failed: {e}\n--- output ---\n" + "\n".join(lines[-40:])) from e
        rec = deployments.records(cls.project)[0]
        if rec["outcome"] != "ok":
            cls.tearDownClass()
            raise AssertionError(f"deployment on Podman is {rec['outcome']}:\n" + "\n".join(lines[-12:]))

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["bash", "-c", f"ids=$(podman ps -aq --filter label=com.docker.compose.project={cls.project}); [ -z \"$ids\" ] || podman rm -f $ids >/dev/null 2>&1; "
                        f"vols=$(podman volume ls -q | grep '^{cls.project}_'); [ -z \"$vols\" ] || podman volume rm -f $vols >/dev/null 2>&1; "
                        f"podman network ls -q --filter name={cls.project} | xargs -r podman network rm >/dev/null 2>&1"], capture_output=True, timeout=300)
        cls.env.stop()
        shutil.rmtree(cls.out, ignore_errors=True)
        shutil.rmtree(cls.home, ignore_errors=True)

    def prov(self, **kw):
        return provider.PodmanBackupProvider(self.cfg, **kw)

    def run_gen(self, gen):
        return list(gen)

    def psql(self, sql, db=None):
        return self.prov().psql(sql, db=db or self.cfg["postgres"]["db"])

    def databases(self):
        return set(self.prov().psql("select datname from pg_database", db="postgres").splitlines())

    def ctr(self, service):
        return subprocess.run(["podman", "ps", "-q", "--filter", f"label=com.docker.compose.project={self.project}", "--filter", f"label=com.docker.compose.service={service}"],
                              capture_output=True, text=True).stdout.split()[0]


@unittest.skipUnless(pulled(PG_IMAGE), "GISSTACK_TEST_PODMAN=1, podman with a compose provider and the PostGIS image needed")
class PostgresBackupRestoreOnPodman(PostgresCases, PodmanStackBase):
    def stop_postgres(self):
        subprocess.run(["podman", "stop", "-t", "5", self.ctr("postgis")], check=True, capture_output=True)

    def start_postgres(self):
        subprocess.run(["podman", "start", f"{self.project}_postgis_1"], capture_output=True)
        for _ in range(60):
            if subprocess.run(["podman", "healthcheck", "run", f"{self.project}_postgis_1"], capture_output=True).returncode == 0:
                return
            time.sleep(2)

    def test_0_the_stack_was_deployed_by_podman_and_gisstack_reads_it_back(self):
        """Not a backup case: gis-stack's own deployment, status and record on a real Podman engine."""
        from gisstack import ops
        row = ops.status(self.cfg, "local", None, "compose")["rows"][0]
        self.assertEqual((row["id"], row["state"], row["ok"]), ("postgis", "running", True), row)
        rec = deployments.records(self.project)[0]
        self.assertEqual((rec["outcome"], rec["target"]), ("ok", "compose"))
        self.assertTrue(any("5432" in x for x in row["listen"]), row["listen"])
        images = subprocess.run(["podman", "ps", "--format", "{{.Image}}", "--filter", f"label=com.docker.compose.project={self.project}"], capture_output=True, text=True).stdout
        self.assertIn("docker.io/postgis/postgis", images)


@unittest.skipUnless(pulled(PG_IMAGE) and pulled(GS_IMAGE), "GISSTACK_TEST_PODMAN=1, podman with a compose provider and the PostGIS + GeoServer images needed")
class GeoServerFilesBackupRestoreOnPodman(GeoServerCases, PodmanStackBase):
    components = {"geoserver": True}

    def gs_exec(self, args):
        return subprocess.run(["podman", "exec", "-i", self.ctr("geoserver"), *args], capture_output=True, text=True).stdout


if __name__ == "__main__":
    unittest.main()
