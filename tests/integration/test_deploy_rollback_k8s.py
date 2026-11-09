"""Deploy -> failed upgrade -> rollback against a REAL Kubernetes cluster (kind in CI; a throw-away cluster locally).  Opt-in: GISSTACK_TEST_K8S=1.

Creates and deletes a namespace named gsrbk-<random>; NEVER point this at a cluster that matters.  The bad deployment is PostgreSQL 16
started on the PersistentVolume that PostgreSQL 17 initialised: the pod crash-loops, `apply.sh` fails its rollout wait (up to 300 s) and the
record is `failed`; rollback re-applies the PostgreSQL 17 manifests and the data on the volume is still there."""
from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from gisstack import config, deploy, deployments, generate, services
from gisstack.core.errors import DeployError


def cluster_ready() -> bool:
    if os.environ.get("GISSTACK_TEST_K8S") != "1" or not shutil.which("kubectl"):
        return False
    return subprocess.run(["kubectl", "get", "--raw", "/readyz"], capture_output=True, timeout=30).returncode == 0


def kubectl(*args: str, timeout: int = 400) -> subprocess.CompletedProcess:
    return subprocess.run(["kubectl", *args], capture_output=True, text=True, timeout=timeout)


@unittest.skipUnless(cluster_ready(), "GISSTACK_TEST_K8S=1 and a reachable Kubernetes cluster (kubectl) needed")
class DeployRollbackOnKubernetes(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.ns = "gsrbk-" + uuid.uuid4().hex[:6]
        kubeconfig = os.environ.get("KUBECONFIG") or str(Path.home() / ".kube" / "config")
        env = mock.patch.dict(os.environ, {"GISSTACK_HOME": self.home, "KUBECONFIG": kubeconfig, "GISSTACK_VERIFY_TIMEOUT": "120"})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(shutil.rmtree, self.home, True)
        self.dirs: list[Path] = []
        self.addCleanup(self.destroy)

    def destroy(self):
        kubectl("delete", "namespace", self.ns, "--wait=false", timeout=120)
        for d in self.dirs:
            shutil.rmtree(d, ignore_errors=True)

    def cfg(self, pg_version):
        return config.resolve({"project": "gsrbk", "postgres": {"version": pg_version}, "targets": ["k8s"], "deploy_target": "k8s", "k8s": {"namespace": self.ns},
                               "components": {"postgis": True, "geoserver": False, "tomcat": False, "qgis_server": False, "mapproxy": False, "caddy": False, "webmap": False}},
                              generate_secrets=True, persist_secrets=True)

    def bundle(self, cfg):
        out = Path(tempfile.mkdtemp())
        self.dirs.append(out)
        generate.generate_bundle(cfg, out)
        return out

    def psql(self, cfg, sql):
        r = kubectl("-n", self.ns, "exec", "statefulset/postgis", "--", "psql", "-U", cfg["postgres"]["user"], "-d", cfg["postgres"]["db"], "-Atc", sql)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout.strip()

    def test_failed_upgrade_is_recorded_and_rolled_back_without_losing_data_or_secrets(self):
        cfg1 = self.cfg("17")
        lines = list(deploy.deploy(cfg1, self.bundle(cfg1), "k8s", "local"))
        self.assertTrue(any("ყველა პროგრამა მუშაობს" in x for x in lines), "\n".join(lines[-15:]))
        rec1 = deployments.records("gsrbk")[0]
        self.assertEqual((rec1["outcome"], rec1["target"]), ("ok", "k8s"))
        self.psql(cfg1, "create table keepme(v text); insert into keepme values ('before-upgrade')")

        cfg2 = self.cfg("16")
        self.assertEqual(cfg2["postgres"]["password"], cfg1["postgres"]["password"])
        with contextlib.suppress(DeployError):                                # apply.sh gives up waiting for the rollout: recorded as failed
            list(deploy.deploy(cfg2, self.bundle(cfg2), "k8s", "local"))
        rec2 = deployments.records("gsrbk")[0]
        self.assertIn(rec2["outcome"], ("failed", "unhealthy"), rec2)

        plan = services.rollback_plan(cfg2, "local", None, "k8s")
        self.assertTrue(plan["eligible"], plan)
        self.assertEqual(plan["target"]["id"], rec1["id"])
        snap, _ = services.rollback_config(cfg2, "local", None, "k8s", rec1["id"])
        self.assertEqual(snap["postgres"]["version"], "17")
        # a StatefulSet that never became ready does not always replace its pod by itself; that is Kubernetes behaviour, reported by the test if it happens
        list(services.rollback_steps(snap, self.bundle(snap), "k8s", "local", None, rec1["id"]))
        rec3 = deployments.records("gsrbk")[0]
        self.assertEqual((rec3["kind"], rec3["outcome"], rec3["rollback_of"]), ("rollback", "ok", rec1["id"]))

        self.assertEqual(self.psql(cfg1, "select v from keepme"), "before-upgrade")
        again = services.rollback_plan(cfg2, "local", None, "k8s", rec1["id"])
        self.assertFalse(again["eligible"])
        self.assertIn("nothing to do", again["reason"])


if __name__ == "__main__":
    unittest.main()
