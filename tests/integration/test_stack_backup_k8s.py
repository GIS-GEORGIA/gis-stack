"""Backup and restore against a REAL Kubernetes cluster (kind in CI; a throw-away cluster locally).  Opt-in: GISSTACK_TEST_K8S=1.

Creates and deletes a namespace named gsbk-<random>; NEVER point this at a cluster that matters.  The same cases as on Docker Compose
(tests/integration/stackbackup_cases.py) run here through `kubectl exec`."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from gisstack import config, generate
from gisstack.backups import provider

from .stackbackup_cases import GeoServerCases, PostgresCases


def cluster_ready() -> bool:
    if os.environ.get("GISSTACK_TEST_K8S") != "1" or not shutil.which("kubectl"):
        return False
    return subprocess.run(["kubectl", "get", "--raw", "/readyz"], capture_output=True, timeout=30).returncode == 0


def kubectl(*args: str, timeout: int = 700) -> subprocess.CompletedProcess:
    return subprocess.run(["kubectl", *args], capture_output=True, text=True, timeout=timeout)


class K8sStackBase(unittest.TestCase):
    components: dict = {}
    workloads = ("statefulset/postgis",)

    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp()
        kubeconfig = os.environ.get("KUBECONFIG") or str(Path.home() / ".kube" / "config")
        cls.env = mock.patch.dict(os.environ, {"GISSTACK_HOME": cls.home, "KUBECONFIG": kubeconfig, "GISSTACK_VERIFY_TIMEOUT": "180", "GISSTACK_VERIFY_SETTLE": "3"})
        cls.env.start()
        cls.ns = "gsbk-" + uuid.uuid4().hex[:6]
        comps = {"postgis": True, "geoserver": False, "tomcat": False, "qgis_server": False, "mapproxy": False, "caddy": False, "webmap": False, **cls.components}
        cls.cfg = config.resolve({"project": "gsbk", "targets": ["k8s"], "deploy_target": "k8s", "k8s": {"namespace": cls.ns}, "components": comps},
                                 generate_secrets=True, persist_secrets=True)
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
                cls.tearDownClass()
                raise AssertionError(f"{w} not ready:\n{r.stderr[-300:]}\n{pods}")

    @classmethod
    def tearDownClass(cls):
        kubectl("delete", "namespace", cls.ns, "--wait=false", timeout=120)
        cls.env.stop()
        shutil.rmtree(cls.out, ignore_errors=True)
        shutil.rmtree(cls.home, ignore_errors=True)

    def prov(self, **kw):
        return provider.KubernetesBackupProvider(self.cfg, **kw)

    def run_gen(self, gen):
        return list(gen)

    def psql(self, sql, db=None):
        return self.prov().psql(sql, db=db or self.cfg["postgres"]["db"])

    def databases(self):
        return set(self.prov().psql("select datname from pg_database", db="postgres").splitlines())


@unittest.skipUnless(cluster_ready(), "GISSTACK_TEST_K8S=1 and a reachable Kubernetes cluster (kubectl) needed")
class PostgresBackupRestoreOnKubernetes(PostgresCases, K8sStackBase):
    def stop_postgres(self):
        kubectl("-n", self.ns, "scale", "statefulset/postgis", "--replicas=0")
        kubectl("-n", self.ns, "wait", "--for=delete", "pod/postgis-0", "--timeout=120s")

    def start_postgres(self):
        kubectl("-n", self.ns, "scale", "statefulset/postgis", "--replicas=1")
        kubectl("-n", self.ns, "rollout", "status", "statefulset/postgis", "--timeout=300s")


@unittest.skipUnless(cluster_ready(), "GISSTACK_TEST_K8S=1 and a reachable Kubernetes cluster (kubectl) needed")
class GeoServerFilesBackupRestoreOnKubernetes(GeoServerCases, K8sStackBase):
    components = {"geoserver": True}
    workloads = ("statefulset/postgis", "deployment/geoserver")

    def gs_exec(self, args):
        return subprocess.run(["kubectl", "-n", self.ns, "exec", "-i", "deployment/geoserver", "--", *args], capture_output=True, text=True).stdout


if __name__ == "__main__":
    unittest.main()
