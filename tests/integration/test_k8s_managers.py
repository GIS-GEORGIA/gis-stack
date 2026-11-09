"""The managers against a REAL Kubernetes cluster (kind in CI; any throw-away cluster locally).  Opt-in: GISSTACK_TEST_K8S=1, needs `kubectl` with a working current context.

Generates the Kubernetes bundle (PostGIS, GeoServer, MapProxy), applies it into a fresh namespace, waits for the rollouts, runs gis-stack's own manager code through
real `kubectl exec`, and deletes the namespace.  Pods pull their images themselves (several hundred MB; `kind load docker-image` first to save time).
NEVER point this at a cluster that matters: it creates and deletes a namespace named gsit-<random>."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from gisstack import config, generate, targets
from gisstack.core.errors import DeployError
from gisstack.geoserver import gateway as gs_gateway
from gisstack.geoserver import manager as gsm
from gisstack.mapproxy import manager as mpm
from gisstack.mapproxy.server import PlaceGateway
from gisstack.postgis import manager as pgm
from gisstack.postgis.server import ScriptGateway


def cluster_ready() -> bool:
    if os.environ.get("GISSTACK_TEST_K8S") != "1" or not shutil.which("kubectl"):
        return False
    return subprocess.run(["kubectl", "get", "--raw", "/readyz"], capture_output=True, timeout=30).returncode == 0


def kubectl(*args: str, timeout: int = 700) -> subprocess.CompletedProcess:
    return subprocess.run(["kubectl", *args], capture_output=True, text=True, timeout=timeout)


@unittest.skipUnless(cluster_ready(), "GISSTACK_TEST_K8S=1 and a reachable Kubernetes cluster (kubectl) needed")
class ManagersOnRealKubernetes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = "gsit-" + uuid.uuid4().hex[:8]
        cls.cfg = config.resolve({"project": "gsit", "targets": ["k8s"], "deploy_target": "k8s", "k8s": {"namespace": cls.ns},
                                  "components": {"postgis": True, "geoserver": True, "tomcat": False, "qgis_server": False, "mapproxy": True, "caddy": False, "webmap": False}},
                                 generate_secrets=True)
        cls.out = Path(tempfile.mkdtemp())
        cls.home = tempfile.mkdtemp()
        cls.p = mock.patch.dict(os.environ, {"HOME": cls.home})                 # the read-only role's password file goes to $HOME/.gisstack
        cls.p.start()
        generate.generate_bundle(cls.cfg, cls.out)
        r = kubectl("apply", "-k", str(cls.out / "k8s"))
        if r.returncode != 0:
            cls.tearDownClass()
            raise AssertionError("kubectl apply failed: " + r.stderr[-600:])
        for workload in ("statefulset/postgis", "deployment/geoserver", "deployment/mapproxy"):
            r = kubectl("-n", cls.ns, "rollout", "status", workload, "--timeout=600s")
            if r.returncode != 0:
                pods = kubectl("-n", cls.ns, "get", "pods", "-o", "wide").stdout
                cls.tearDownClass()
                raise AssertionError(f"{workload} did not become ready:\n{r.stderr[-400:]}\n{pods}")
        cls.db = cls.cfg["postgres"]["db"]

    @classmethod
    def tearDownClass(cls):
        kubectl("delete", "namespace", cls.ns, "--wait=false", timeout=120)
        cls.p.stop()
        shutil.rmtree(cls.out, ignore_errors=True)
        shutil.rmtree(cls.home, ignore_errors=True)

    # ------------------------------------------------------------------ PostGIS
    def test_postgis_inventory_role_boundary_and_elevated_write(self):
        gw = ScriptGateway(self.cfg, "local", None)
        inv = pgm.inventory(gw, self.db)
        self.assertTrue(any(e["name"] == "postgis" for e in inv["extensions"]))
        self.assertTrue(pgm.health(inv)["ok"], pgm.health(inv))
        gw.setup(self.db, self.cfg["postgres"]["user"], 15)
        self.assertEqual(pgm.query(gw, self.db, "select current_user")["rows"][0][0], "gisstack_ro")
        with self.assertRaises(DeployError):
            pgm.query(gw, self.db, "create table must_not_exist(a int)")
        with self.assertRaises(DeployError):
            pgm.query(gw, self.db, "\\! touch /tmp/pwned")
        self.assertNotEqual(kubectl("-n", self.ns, "exec", "statefulset/postgis", "--", "test", "-e", "/tmp/pwned").returncode, 0)
        pgm.execute(gw, self.db, "create table public.gs_probe(id int primary key)")
        pgm.execute(gw, self.db, "insert into public.gs_probe values (7)")
        self.assertEqual(pgm.query(gw, self.db, "select id from public.gs_probe")["rows"][0][0], "7")

    # ------------------------------------------------------------------ GeoServer
    def test_geoserver_health_and_workspace_lifecycle(self):
        gw = gs_gateway.for_config(self.cfg, "local", None)
        self.assertTrue(gsm.health(gw)["ok"])
        name = "gsit" + uuid.uuid4().hex[:6]
        gsm.create_workspace(gw, name)
        self.assertIn(name, [w["name"] for w in gsm.inventory(gw)["workspaces"]])
        gsm.delete_workspace(gw, name, recurse=False)
        self.assertNotIn(name, [w["name"] for w in gsm.inventory(gw)["workspaces"]])

    # ------------------------------------------------------------------ MapProxy
    def test_mapproxy_reads_and_seeds_in_the_pod_but_never_edits_the_configmap(self):
        gw = PlaceGateway(targets.place_of(self.cfg, "mapproxy"), "local", None)
        h = mpm.health(gw)
        self.assertTrue(h["ok"], h)
        snap = mpm.snapshot(gw)
        self.assertTrue(snap["config"]["layers"])
        cache = next(iter(snap["conf"]["caches"]))
        lines = list(mpm.seed(gw, {"caches": [cache], "level_from": 0, "level_to": 3, "bbox": [39, 41, 46, 43.5], "dry_run": True}))
        self.assertIsNotNone(mpm.estimate_tiles(lines), lines[-3:])
        self.assertTrue(mpm.cache_stats(gw)["available"])
        with self.assertRaises(DeployError) as cm:
            mpm.edit(gw, "add_layer", {"name": "x1", "source": "wms", "url": "http://127.0.0.1:1/wms", "layers": "a"}, dry_run=False)
        self.assertIn("ConfigMap", str(cm.exception))

    def test_a_missing_workload_is_a_clear_error_on_a_real_cluster(self):
        cfg = dict(self.cfg, k8s=dict(self.cfg["k8s"], namespace="gsit-does-not-exist"))
        with self.assertRaises(DeployError) as cm:
            pgm.inventory(ScriptGateway(cfg, "local", None), self.db)
        self.assertRegex(str(cm.exception), "not found")


if __name__ == "__main__":
    unittest.main()
