"""Deploy -> verify -> failed upgrade -> rollback against a REAL Docker Compose stack.  Opt-in: GISSTACK_TEST_DOCKER=1.

An isolated temp GISSTACK_HOME and a unique Compose project are used; everything (containers, volumes, files) is removed afterwards.
The "bad deployment" is a realistic one: PostgreSQL 16 started on a data directory that PostgreSQL 17 initialised refuses to start,
so the stack ends unhealthy while the PostgreSQL 17 data stays untouched - exactly the case rollback exists for."""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from gisstack import config, deploy, deployments, generate, services

IMAGES = ("postgis/postgis:17-3.5", "postgis/postgis:16-3.5")


def available() -> bool:
    if os.environ.get("GISSTACK_TEST_DOCKER") != "1" or not shutil.which("docker"):
        return False
    if subprocess.run(["docker", "compose", "version"], capture_output=True).returncode != 0:
        return False
    return all(subprocess.run(["docker", "pull", "-q", i], capture_output=True).returncode == 0 for i in IMAGES)


@unittest.skipUnless(available(), "GISSTACK_TEST_DOCKER=1 and docker compose (and the postgis 16/17 images) needed")
class DeployRollbackOnCompose(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.project = "gsrb" + uuid.uuid4().hex[:6]
        with socket.socket() as sk:
            sk.bind(("127.0.0.1", 0))
            self.port = sk.getsockname()[1]
        env = mock.patch.dict(os.environ, {"GISSTACK_HOME": self.home, "GISSTACK_VERIFY_TIMEOUT": "45"})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(shutil.rmtree, self.home, True)
        self.dirs: list[Path] = []
        self.addCleanup(self.destroy)

    def cfg(self, pg_version):
        return config.resolve({"project": self.project, "postgres": {"port": self.port, "version": pg_version}, "targets": ["compose"], "deploy_target": "compose",
                               "components": {"postgis": True, "geoserver": False, "tomcat": False, "qgis_server": False, "mapproxy": False, "caddy": False, "webmap": False}},
                              generate_secrets=True, persist_secrets=True)

    def bundle(self, cfg):
        out = Path(tempfile.mkdtemp())
        self.dirs.append(out)
        generate.generate_bundle(cfg, out)
        return out

    def compose(self, d, *args, **kw):
        return subprocess.run(["docker", "compose", "-p", self.project, "-f", str(d / "compose" / "docker-compose.yml"), "--env-file", str(d / "compose" / ".env"), *args],
                              capture_output=True, text=True, timeout=240, **kw)

    def destroy(self):
        for d in reversed(self.dirs):
            self.compose(d, "down", "-v", "--remove-orphans")
        for d in self.dirs:
            shutil.rmtree(d, ignore_errors=True)

    def psql(self, d, cfg, sql):
        r = self.compose(d, "exec", "-T", "postgis", "psql", "-U", cfg["postgres"]["user"], "-d", cfg["postgres"]["db"], "-Atc", sql)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout.strip()

    def test_failed_upgrade_is_detected_and_rolled_back_without_losing_data_or_secrets(self):
        # 1. fresh deployment, verified healthy by gis-stack itself
        cfg1 = self.cfg("17")
        b1 = self.bundle(cfg1)
        lines = list(deploy.deploy(cfg1, b1, "compose", "local"))
        self.assertTrue(any("ყველა პროგრამა მუშაობს" in x for x in lines), "\n".join(lines[-15:]))
        rec1 = deployments.records(self.project)[0]
        self.assertEqual((rec1["outcome"], rec1["target"]), ("ok", "compose"))
        self.assertTrue(rec1["after"][0]["ok"])
        self.psql(b1, cfg1, "create table keepme(v text); insert into keepme values ('before-upgrade')")

        # 2. a bad deployment: runs, but is not healthy -> recorded as such, not raised
        cfg2 = self.cfg("16")
        self.assertEqual(cfg2["postgres"]["password"], cfg1["postgres"]["password"])          # existing secrets are reused, not regenerated
        b2 = self.bundle(cfg2)
        lines = list(deploy.deploy(cfg2, b2, "compose", "local"))
        self.assertTrue(any(x.startswith("!! postgis") for x in lines), "\n".join(lines[-10:]))
        rec2 = deployments.records(self.project)[0]
        self.assertEqual(rec2["outcome"], "unhealthy")
        self.assertEqual(rec2["before"][0]["state"], "running")                                # the state before was captured from the live stack

        # 3. rollback is offered, needs the earlier healthy record, and restores it
        plan = services.rollback_plan(cfg2, "local", None, "compose")
        self.assertTrue(plan["eligible"], plan)
        self.assertEqual(plan["target"]["id"], rec1["id"])
        snap, _ = services.rollback_config(cfg2, "local", None, "compose", rec1["id"])
        self.assertEqual(snap["postgres"]["version"], "17")
        self.assertEqual(snap["postgres"]["password"], cfg1["postgres"]["password"])
        b3 = self.bundle(snap)
        list(services.rollback_steps(snap, b3, "compose", "local", None, rec1["id"]))
        rec3 = deployments.records(self.project)[0]
        self.assertEqual((rec3["kind"], rec3["outcome"], rec3["rollback_of"]), ("rollback", "ok", rec1["id"]))

        # 4. nothing was lost, the database is reachable with the original password, and repeating the rollback is a no-op
        self.assertEqual(self.psql(b3, cfg1, "select v from keepme"), "before-upgrade")
        self.assertIn("17", self.psql(b3, cfg1, "show server_version"))
        again = services.rollback_plan(cfg2, "local", None, "compose", rec1["id"])
        self.assertFalse(again["eligible"])
        self.assertIn("nothing to do", again["reason"])
        self.assertEqual(len(deployments.records(self.project)), 3)


if __name__ == "__main__":
    unittest.main()
