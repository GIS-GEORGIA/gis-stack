"""Backup and restore against a REAL Docker Compose stack (PostGIS, GeoServer).  Opt-in: GISSTACK_TEST_INTEGRATION=1 (several minutes, pulls images).

Each class starts an isolated throw-away Compose project (unique name, random ports, temp GISSTACK_HOME), runs gis-stack's own backup code
at it through `docker exec`, and removes everything afterwards.  Nothing here touches a stack that was not created by the test."""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from gisstack import config, generate
from gisstack.backups import provider

from .stackbackup_cases import GeoServerCases, PostgresCases

PG_IMAGE = "postgis/postgis:17-3.5"
GS_IMAGE = "docker.osgeo.org/geoserver:2.28.6"
ENABLED = os.environ.get("GISSTACK_TEST_INTEGRATION") == "1" and shutil.which("docker") is not None


def pulled(image: str) -> bool:
    return ENABLED and subprocess.run(["docker", "pull", "-q", image], capture_output=True).returncode == 0


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class StackBase(unittest.TestCase):
    components: dict = {}

    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp()
        cls.env = mock.patch.dict(os.environ, {"GISSTACK_HOME": cls.home, "GISSTACK_VERIFY_TIMEOUT": "120", "GISSTACK_VERIFY_SETTLE": "3"})
        cls.env.start()
        cls.project = "gsbk" + uuid.uuid4().hex[:6]
        comps = {"postgis": True, "geoserver": False, "tomcat": False, "qgis_server": False, "mapproxy": False, "caddy": False, "webmap": False, **cls.components}
        cls.cfg = config.resolve({"project": cls.project, "postgres": {"port": free_port()}, "targets": ["compose"], "deploy_target": "compose", "components": comps},
                                 generate_secrets=True, persist_secrets=True)
        cls.out = Path(tempfile.mkdtemp())
        generate.generate_bundle(cls.cfg, cls.out)
        yml = cls.out / "compose" / "docker-compose.yml"          # tests reach workloads with `exec`; fixed host ports (8080, 8081, 8380) would make concurrent runs collide
        yml.write_text(re.sub(r'    ports:\n      - "(?:8080:8080|8081:8081|8380:80)"\n', "", yml.read_text()))
        d = cls.out / "compose"
        cls.base = ["docker", "compose", "-p", cls.project, "-f", str(d / "docker-compose.yml"), "--env-file", str(d / ".env")]
        up = subprocess.run([*cls.base, "up", "-d", "--wait", "--wait-timeout", "300"], capture_output=True, text=True, timeout=400)
        if up.returncode != 0:
            ps = subprocess.run([*cls.base, "ps", "-a"], capture_output=True, text=True).stdout
            logs = subprocess.run([*cls.base, "logs", "--tail", "40", "--no-color"], capture_output=True, text=True).stdout
            cls.tearDownClass()                         # keep the evidence in the failure message: a timeout and a crash look the same otherwise
            raise AssertionError(f"compose up failed (rc {up.returncode}) after waiting\n{up.stdout}{up.stderr}\n--- ps -a\n{ps}\n--- last log lines\n{logs}")

    @classmethod
    def tearDownClass(cls):
        subprocess.run([*cls.base, "down", "-v", "--remove-orphans"], capture_output=True, timeout=240)
        cls.env.stop()
        shutil.rmtree(cls.out, ignore_errors=True)
        shutil.rmtree(cls.home, ignore_errors=True)

    def prov(self, **kw):
        return provider.ComposeBackupProvider(self.cfg, **kw)

    def run_gen(self, gen):
        return list(gen)

    def psql(self, sql, db=None):
        p = self.prov()
        return p.psql(sql, db=db or self.cfg["postgres"]["db"])

    def databases(self):
        return set(self.prov().psql("select datname from pg_database", db="postgres").splitlines())


@unittest.skipUnless(pulled(PG_IMAGE), "GISSTACK_TEST_INTEGRATION=1, docker and the PostGIS image needed")
class PostgresBackupRestore(PostgresCases, StackBase):
    def ctr(self, service):
        return subprocess.run(["docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={self.project}", "--filter", f"label=com.docker.compose.service={service}"],
                              capture_output=True, text=True).stdout.split()[0]

    def stop_postgres(self):
        subprocess.run(["docker", "stop", "-t", "5", self.ctr("postgis")], check=True, capture_output=True)

    def start_postgres(self):
        subprocess.run([*self.base, "start", "postgis"], capture_output=True)
        subprocess.run([*self.base, "up", "-d", "--wait", "--wait-timeout", "120"], capture_output=True, timeout=200)


@unittest.skipUnless(pulled(PG_IMAGE) and pulled(GS_IMAGE), "GISSTACK_TEST_INTEGRATION=1, docker and the PostGIS + GeoServer images needed")
class GeoServerFilesBackupRestore(GeoServerCases, StackBase):
    components = {"geoserver": True}

    def gs_exec(self, args):
        ctr = subprocess.run(["docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={self.project}", "--filter", "label=com.docker.compose.service=geoserver"],
                             capture_output=True, text=True).stdout.split()[0]
        return subprocess.run(["docker", "exec", "-i", ctr, *args], capture_output=True, text=True).stdout


if __name__ == "__main__":
    unittest.main()
