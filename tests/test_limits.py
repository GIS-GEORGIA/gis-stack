"""S-4 remainder: optional CPU / memory limits per container (`limits.<service>.memory_mb` / `cpus`, 0 = none).  Nothing is limited by default
(they depend on the machine); a JVM limit below heap + overhead is refused because the kernel would kill the container.
The real engine enforcing them: tests/integration/test_limits.py."""
import tempfile
import unittest
from pathlib import Path

import yaml

from gisstack import config, generate

ALL = {"postgis": True, "geoserver": True, "tomcat": True, "qgis_server": True, "mapproxy": True, "caddy": True, "webmap": True}
SERVICES = ["caddy", "geoserver", "mapproxy", "postgis", "qgis", "tomcat"]


def build(target, limits=None, **raw):
    body = {"project": "lim", "targets": [target], "deploy_target": target, "components": ALL, **raw}
    if limits is not None:
        body["limits"] = limits
    cfg = config.resolve(body, generate_secrets=False, persist_secrets=False)
    with tempfile.TemporaryDirectory() as d:
        generate.generate_bundle(cfg, Path(d))
        if target == "compose":
            return yaml.safe_load((Path(d) / "compose" / "docker-compose.yml").read_text())["services"]
        docs = [x for f in sorted((Path(d) / "k8s").glob("*.yaml")) for x in yaml.safe_load_all(f.read_text()) if x]
        return {x["metadata"]["name"]: x["spec"]["template"]["spec"]["containers"][0] for x in docs if x.get("kind") in ("Deployment", "StatefulSet")}


def refused(limits=None, **raw):
    with unittest.TestCase().assertRaises(config.ConfigError) as cm:
        config.resolve({"project": "lim", **({"limits": limits} if limits else {}), **raw}, generate_secrets=False, persist_secrets=False)
    return str(cm.exception)


class DefaultTests(unittest.TestCase):
    def test_nothing_is_limited_by_default(self):
        for name, svc in build("compose").items():
            self.assertNotIn("mem_limit", svc, name)
            self.assertNotIn("cpus", svc, name)
        for name, c in build("k8s").items():
            self.assertNotIn("limits", c.get("resources", {}), name)


class ComposeTests(unittest.TestCase):
    def test_limits_reach_exactly_the_configured_services(self):
        svc = build("compose", {"geoserver": {"memory_mb": 3072, "cpus": 2}, "caddy": {"memory_mb": 256, "cpus": 0}, "qgis": {"memory_mb": 0, "cpus": 1.5}})
        self.assertEqual((svc["geoserver"]["mem_limit"], svc["geoserver"]["cpus"]), ("3072m", 2))
        self.assertEqual(svc["caddy"]["mem_limit"], "256m")
        self.assertNotIn("cpus", svc["caddy"])
        self.assertEqual(svc["qgis"]["cpus"], 1.5)
        self.assertNotIn("mem_limit", svc["qgis"])
        for name in ("postgis", "tomcat", "mapproxy"):
            self.assertNotIn("mem_limit", svc[name])
        self.assertEqual(svc["geoserver"]["cap_drop"], ["ALL"])            # hardening is untouched

    def test_every_service_accepts_limits(self):
        svc = build("compose", {s: {"memory_mb": 4096, "cpus": 1} for s in SERVICES})
        for name in SERVICES:
            self.assertEqual(svc[name]["mem_limit"], "4096m", name)

    def test_podman_flavour(self):
        svc = build("compose", {"postgis": {"memory_mb": 1024, "cpus": 1}}, runtime="podman")
        self.assertEqual(svc["postgis"]["mem_limit"], "1024m")


class KubernetesTests(unittest.TestCase):
    def test_limits_are_merged_with_existing_requests(self):
        c = build("k8s", {s: {"memory_mb": 4096, "cpus": 2} for s in SERVICES})
        for name in SERVICES:
            self.assertEqual(c[name]["resources"]["limits"], {"memory": "4096Mi", "cpu": "2"}, name)
        self.assertEqual(c["geoserver"]["resources"]["requests"]["memory"], "2048Mi")      # the request survives
        self.assertIn("memory", c["postgis"]["resources"]["requests"])

    def test_fractional_cpu_and_one_dimension_only(self):
        c = build("k8s", {"mapproxy": {"memory_mb": 0, "cpus": 0.5}})
        self.assertEqual(c["mapproxy"]["resources"]["limits"], {"cpu": "0.5"})


class ValidationTests(unittest.TestCase):
    def test_bounds_and_types(self):
        for bad in ({"memory_mb": -1}, {"memory_mb": 10}, {"memory_mb": "lots"}, {"memory_mb": True}, {"memory_mb": 2.5}, {"cpus": -1}, {"cpus": 0.01}, {"cpus": "x"}, {"cpus": float("nan")}, {"cpus": 1000}):
            refused({"postgis": bad})

    def test_unknown_service_does_not_become_a_limit(self):
        cfg = config.resolve({"project": "lim", "limits": {"nginx": {"memory_mb": 100, "cpus": 1}}}, generate_secrets=False, persist_secrets=False)
        for svc in build("compose", {"nginx": {"memory_mb": 100, "cpus": 1}}).values():
            self.assertNotIn("mem_limit", svc)
        self.assertEqual(cfg["limits"]["geoserver"], {"memory_mb": 0, "cpus": 0})

    def test_a_jvm_limit_below_heap_plus_overhead_is_refused(self):
        msg = refused({"geoserver": {"memory_mb": 2048, "cpus": 0}})             # heap 2048
        self.assertIn("limits.geoserver.memory_mb", msg)
        self.assertIn("2304", msg)
        self.assertIn("limits.tomcat.memory_mb", refused({"tomcat": {"memory_mb": 1100, "cpus": 0}}))      # heap 1024 + 256 = 1280

    def test_a_jvm_limit_with_room_for_the_heap_is_accepted(self):
        cfg = config.resolve({"project": "lim", "geoserver": {"heap_mb": 512}, "limits": {"geoserver": {"memory_mb": 768, "cpus": 0}}}, generate_secrets=False, persist_secrets=False)
        self.assertEqual(cfg["limits"]["geoserver"]["memory_mb"], 768)
        self.assertEqual(config.resolve({"project": "lim", "limits": {"geoserver": {"memory_mb": 2304, "cpus": 0}}}, generate_secrets=False, persist_secrets=False)["limits"]["geoserver"]["memory_mb"], 2304)
