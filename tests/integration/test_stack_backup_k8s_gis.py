"""Caddy, MapProxy and QGIS Server backup/restore, and PostgreSQL health + backup monitoring, on a REAL Kubernetes cluster (kind in CI).
Opt-in: GISSTACK_TEST_K8S=1.  Creates and deletes a namespace named gsgk-<random>; NEVER point this at a cluster that matters.

Status of this file: written to mirror tests/integration/test_stack_backup_gis.py (real Docker Compose, passing); it has NOT been run on a cluster
by its author - the CI job `kubernetes-gis (kind)` in integration.yml is its first real run, and nothing is called verified until that job is green."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from gisstack import config, generate
from gisstack.backups import provider
from gisstack.backups.model import RestoreError

from .test_stack_backup_k8s import cluster_ready, kubectl


class K8sGis(unittest.TestCase):
    components: dict = {}
    workloads: tuple = ()

    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp()
        kubeconfig = os.environ.get("KUBECONFIG") or str(Path.home() / ".kube" / "config")
        cls.env = mock.patch.dict(os.environ, {"GISSTACK_HOME": cls.home, "KUBECONFIG": kubeconfig, "GISSTACK_VERIFY_TIMEOUT": "240", "GISSTACK_VERIFY_SETTLE": "3"})
        cls.env.start()
        cls.ns = "gsgk-" + uuid.uuid4().hex[:6]
        comps = {"postgis": False, "geoserver": False, "tomcat": False, "qgis_server": False, "mapproxy": False, "caddy": False, "webmap": False, **cls.components}
        cls.cfg = config.resolve({"project": "gsgk", "targets": ["k8s"], "deploy_target": "k8s", "k8s": {"namespace": cls.ns}, "tls": "internal", "domain": "localhost",
                                  "components": comps}, generate_secrets=True, persist_secrets=True)
        cls.out = Path(tempfile.mkdtemp())
        generate.generate_bundle(cls.cfg, cls.out)
        r = kubectl("apply", "-k", str(cls.out / "k8s"))
        if r.returncode != 0:
            cls.tearDownClass()
            raise AssertionError("kubectl apply failed: " + r.stderr[-500:])
        for w in cls.workloads:
            r = kubectl("-n", cls.ns, "rollout", "status", w, "--timeout=600s")
            if r.returncode != 0:
                pods = kubectl("-n", cls.ns, "get", "pods", "-o", "wide").stdout
                logs = kubectl("-n", cls.ns, "logs", w, "--tail=40").stdout
                cls.tearDownClass()
                raise AssertionError(f"{w} not ready:\n{r.stderr[-300:]}\n{pods}\n{logs}")

    @classmethod
    def tearDownClass(cls):
        kubectl("delete", "namespace", cls.ns, "--wait=false", timeout=120)
        cls.env.stop()
        shutil.rmtree(cls.out, ignore_errors=True)
        shutil.rmtree(cls.home, ignore_errors=True)

    def prov(self, **kw):
        return provider.KubernetesBackupProvider(self.cfg, **kw)

    def x(self, workload, *args):
        return subprocess.run(["kubectl", "-n", self.ns, "exec", "-i", workload, "--", *args], capture_output=True, text=True, timeout=120)

    def create(self, comps, **kw):
        p = self.prov()
        lines = list(p.create(comps, **kw))
        self.assertTrue(any("sealed" in x for x in lines), lines)
        self.assertTrue(p.verify(p.result["id"])["ok"])
        return p, p.result

    def restore(self, bid, comps):
        r = self.prov()
        lines = list(r.restore(bid, comps, confirm=True))
        self.assertTrue(any("verified healthy" in x for x in lines), lines)
        return r


@unittest.skipUnless(cluster_ready(), "GISSTACK_TEST_K8S=1 and a reachable Kubernetes cluster (kubectl) needed")
class CaddyOnKubernetes(K8sGis):
    components = {"caddy": True}
    workloads = ("deployment/caddy",)
    ROOT = "/data/caddy/pki/authorities/local/root.crt"

    def root_sha(self):
        for _ in range(40):
            self.x("deployment/caddy", "sh", "-c", "wget -q --no-check-certificate -O /dev/null https://localhost/ 2>/dev/null; true")
            out = self.x("deployment/caddy", "sh", "-c", f"sha256sum {self.ROOT} 2>/dev/null | cut -d' ' -f1").stdout.strip()
            if len(out) == 64:
                return out
            time.sleep(2)
        return ""

    def test_local_ca_survives_loss_of_the_volume_contents(self):
        before = self.root_sha()
        self.assertEqual(len(before), 64, "Caddy did not create its local CA")
        p, m = self.create(["caddy"])
        self.x("deployment/caddy", "sh", "-c", "rm -rf /data/caddy")
        kubectl("-n", self.ns, "rollout", "restart", "deployment/caddy")
        kubectl("-n", self.ns, "rollout", "status", "deployment/caddy", "--timeout=300s")
        lost = self.root_sha()
        self.assertNotEqual(lost, before)
        r = self.restore(m["id"], ["caddy"])
        self.assertEqual(self.x("deployment/caddy", "sh", "-c", f"sha256sum {self.ROOT} | cut -d' ' -f1").stdout.strip(), before)
        self.assertIn(".gs-pre-", r.result["retained_previous_data"]["caddy"])


@unittest.skipUnless(cluster_ready(), "GISSTACK_TEST_K8S=1 and a reachable Kubernetes cluster (kubectl) needed")
class MapProxyOnKubernetes(K8sGis):
    components = {"mapproxy": True}
    workloads = ("deployment/mapproxy",)

    def test_cache_restore_and_config_is_backup_only(self):
        self.x("deployment/mapproxy", "sh", "-c", "mkdir -p /var/lib/mapproxy/cache_data/sentinel && echo tile-v1 > /var/lib/mapproxy/cache_data/sentinel/0.png")
        p0, m0 = self.create(None)
        self.assertEqual(sorted(m0["components"]), ["mapproxy_config"])
        p, m = self.create(None, with_cache=True)
        self.x("deployment/mapproxy", "sh", "-c", "echo tile-v2 > /var/lib/mapproxy/cache_data/sentinel/0.png; echo extra > /var/lib/mapproxy/cache_data/sentinel/1.png")
        with self.assertRaises(RestoreError):
            list(self.prov().restore(m["id"], ["mapproxy_config"], confirm=True))
        self.restore(m["id"], ["mapproxy_cache"])
        self.assertEqual(self.x("deployment/mapproxy", "cat", "/var/lib/mapproxy/cache_data/sentinel/0.png").stdout.strip(), "tile-v1")
        self.assertNotEqual(self.x("deployment/mapproxy", "test", "-e", "/var/lib/mapproxy/cache_data/sentinel/1.png").returncode, 0)


@unittest.skipUnless(cluster_ready(), "GISSTACK_TEST_K8S=1 and a reachable Kubernetes cluster (kubectl) needed")
class QgisOnKubernetes(K8sGis):
    components = {"qgis_server": True}
    workloads = ("deployment/qgis",)

    def test_projects_backup_and_restore_on_the_pvc(self):
        self.x("deployment/qgis", "sh", "-c", "echo '<qgis projectname=\"gs-sentinel\"/>' > /etc/qgisserver/demo.qgs")
        p, m = self.create(["qgis"])
        self.x("deployment/qgis", "sh", "-c", "echo changed > /etc/qgisserver/demo.qgs; echo extra > /etc/qgisserver/extra.qgs")
        chk = p.check_restore(m["id"], ["qgis"])
        self.assertTrue(chk["ok"], chk)                                                   # restorable in place on Kubernetes (a PVC, not a read-only bind mount)
        self.restore(m["id"], ["qgis"])
        self.assertIn("gs-sentinel", self.x("deployment/qgis", "cat", "/etc/qgisserver/demo.qgs").stdout)
        self.assertNotEqual(self.x("deployment/qgis", "test", "-e", "/etc/qgisserver/extra.qgs").returncode, 0)


@unittest.skipUnless(cluster_ready(), "GISSTACK_TEST_K8S=1 and a reachable Kubernetes cluster (kubectl) needed")
class MonitoringOnKubernetes(K8sGis):
    components = {"postgis": True}
    workloads = ("statefulset/postgis",)

    def test_postgres_health_and_backup_alert_lifecycle(self):
        from gisstack.monitoring import service
        mon = service.Monitor(self.cfg, "local", None, providers=[])
        try:
            res = mon.tick()
            m = {n: v for n, _, v in res["metrics"]}
            self.assertEqual(m.get("pg_up"), 1.0, res["errors"])
            self.assertEqual(m.get("backup_count"), 0.0)
            p = self.prov()
            list(p.create(["postgres"]))
            with mock.patch.object(provider.BackupProvider, "_verify_dump", side_effect=RuntimeError("connection lost")), self.assertRaises(RuntimeError):
                list(p.create(["postgres"]))
            names = {n["rule"]: n["state"] for n in mon.tick()["notifications"]}
            self.assertEqual(names.get("backup_failed"), "firing")
            time.sleep(1.1)
            list(p.create(["postgres"]))
            names = {n["rule"]: n["state"] for n in mon.tick()["notifications"]}
            self.assertEqual(names.get("backup_failed"), "resolved")
        finally:
            mon.store.close()


if __name__ == "__main__":
    unittest.main()
