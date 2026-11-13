"""`gisstack clean` without --purge must keep the data on Kubernetes, as it does on Compose and native (audit 2026-11).

Found with a real kind cluster: the whole-stack clean ran `kubectl delete -k .`, which deletes the Namespace, with it every PersistentVolumeClaim and - the default
reclaim policy being Delete - the volumes: a PostGIS database was gone after a clean that was not asked to purge anything.  The test goes through the real
entry point (`deploy.clean`, the generated apply.sh) against a real cluster.  Opt-in: GISSTACK_TEST_K8S=1.  Creates and deletes a namespace named gsck-<random>;
NEVER point it at a cluster that matters."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from gisstack import config, deploy, generate

from .test_stack_backup_k8s import cluster_ready, kubectl


@unittest.skipUnless(cluster_ready(), "GISSTACK_TEST_K8S=1 and a reachable cluster needed")
class CleanKeepsVolumes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp()
        kubeconfig = os.environ.get("KUBECONFIG") or str(Path.home() / ".kube" / "config")
        cls.env = mock.patch.dict(os.environ, {"GISSTACK_HOME": cls.home, "KUBECONFIG": kubeconfig, "GISSTACK_VERIFY_TIMEOUT": "240", "GISSTACK_VERIFY_SETTLE": "3"})
        cls.env.start()
        cls.ns = "gsck-" + uuid.uuid4().hex[:6]
        comps = {"postgis": True, "geoserver": False, "tomcat": False, "qgis_server": False, "mapproxy": False, "caddy": False, "webmap": False}
        cls.cfg = config.resolve({"project": "gsck", "targets": ["k8s"], "deploy_target": "k8s", "k8s": {"namespace": cls.ns}, "components": comps},
                                 generate_secrets=True, persist_secrets=True)
        cls.out = Path(tempfile.mkdtemp())
        generate.generate_bundle(cls.cfg, cls.out)

    @classmethod
    def tearDownClass(cls):
        kubectl("delete", "namespace", cls.ns, "--wait=false", timeout=120)
        cls.env.stop()
        shutil.rmtree(cls.out, ignore_errors=True)
        shutil.rmtree(cls.home, ignore_errors=True)

    def deploy_and_wait(self):
        list(deploy.deploy(self.cfg, self.out, "k8s", "local"))
        self.assertEqual(kubectl("-n", self.ns, "rollout", "status", "statefulset/postgis", "--timeout=300s").returncode, 0)

    def sql(self, q):
        pg = self.cfg["postgres"]
        r = kubectl("-n", self.ns, "exec", "postgis-0", "--", "psql", "-U", pg["user"], "-d", pg["db"], "-Atqc", q)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_clean_without_purge_keeps_the_namespace_the_volume_and_the_data_and_purge_removes_them(self):
        self.deploy_and_wait()
        self.sql("create table precious(id int); insert into precious values (42)")

        list(deploy.clean(self.cfg, self.out, "k8s", "local"))                                   # no purge
        self.assertEqual(kubectl("get", "namespace", self.ns).returncode, 0, "the namespace (which owns the volumes) must stay")
        pvc = kubectl("-n", self.ns, "get", "pvc", "data-postgis-0", "-o", "jsonpath={.status.phase}")
        self.assertEqual(pvc.stdout, "Bound", pvc.stderr)
        self.assertEqual(kubectl("-n", self.ns, "get", "statefulset,deployment,service", "-o", "name").stdout.strip(), "", "the workloads are removed")

        self.deploy_and_wait()                                                                     # the same stack comes back with its data
        self.assertEqual(self.sql("select id from precious"), "42")

        list(deploy.clean(self.cfg, self.out, "k8s", "local", purge=True))                         # purge: everything incl. the data
        for _ in range(60):
            if kubectl("get", "namespace", self.ns).returncode != 0:
                break
            __import__("time").sleep(2)
        self.assertNotEqual(kubectl("get", "namespace", self.ns).returncode, 0)


if __name__ == "__main__":
    unittest.main()
