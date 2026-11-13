"""S-4: generated containers start from "no capabilities, no privilege gain" and get back only what their image needs (gisstack/hardening.py).
The real engines decide whether that is enough: tests/integration/test_hardening.py (Docker Compose, Podman) and the kind suites."""
import tempfile
import unittest
from pathlib import Path

import yaml

from gisstack import config, generate, hardening

ALL = {"postgis": True, "geoserver": True, "tomcat": True, "qgis_server": True, "mapproxy": True, "caddy": True, "webmap": True}


def bundle(target, **raw):
    cfg = config.resolve({"project": "hard", "targets": [target], "deploy_target": target, "components": ALL, **raw}, generate_secrets=False, persist_secrets=False)
    with tempfile.TemporaryDirectory() as d:
        generate.generate_bundle(cfg, Path(d))
        if target == "compose":
            return yaml.safe_load((Path(d) / "compose" / "docker-compose.yml").read_text())
        return [doc for f in sorted((Path(d) / "k8s").glob("*.yaml")) for doc in yaml.safe_load_all(f.read_text()) if doc]


class ComposeTests(unittest.TestCase):
    def test_every_service_drops_all_capabilities_and_cannot_gain_privileges(self):
        doc = bundle("compose")
        self.assertEqual(sorted(doc["services"]), ["caddy", "geoserver", "mapproxy", "postgis", "qgis", "tomcat"])
        for name, svc in doc["services"].items():
            self.assertEqual(svc["cap_drop"], ["ALL"], name)
            if name in hardening.NO_PRIVILEGE_LOCK:
                self.assertNotIn("security_opt", svc, name)               # measured: no-new-privileges stops the gosu hand-over of the postgres image (rootful Podman 4.9 + AppArmor)
            else:
                self.assertEqual(svc["security_opt"], ["no-new-privileges:true"], name)
            self.assertNotIn("privileged", svc)

    def test_each_service_gets_back_exactly_what_it_needs_and_nothing_else(self):
        doc = bundle("compose")
        want = {"postgis": ["CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"], "qgis": ["CHOWN", "SETGID", "SETUID"], "caddy": ["NET_BIND_SERVICE"]}
        for name, svc in doc["services"].items():
            self.assertEqual(svc.get("cap_add", []), want.get(name, []), name)
        dangerous = {"SYS_ADMIN", "NET_ADMIN", "SYS_PTRACE", "SYS_MODULE", "DAC_READ_SEARCH", "NET_RAW", "MKNOD", "SYS_CHROOT", "KILL", "SETPCAP"}
        for svc in doc["services"].values():
            self.assertFalse(dangerous & set(svc.get("cap_add", [])))

    def test_it_can_be_switched_off_for_custom_images(self):
        doc = bundle("compose", security={"harden_containers": False})
        for name, svc in doc["services"].items():
            for key in ("cap_drop", "cap_add", "security_opt"):
                self.assertNotIn(key, svc, f"{name}.{key}")

    def test_the_podman_flavour_is_hardened_too(self):
        doc = bundle("compose", runtime="podman")
        for svc in doc["services"].values():
            self.assertEqual(svc["cap_drop"], ["ALL"])

    def test_a_non_boolean_switch_is_refused(self):
        for bad in ("yes", 1, None, "false"):
            with self.assertRaises(config.ConfigError, msg=repr(bad)):
                config.resolve({"project": "hard", "security": {"harden_containers": bad}}, generate_secrets=False, persist_secrets=False)


class KubernetesTests(unittest.TestCase):
    def workloads(self, **raw):
        return {d["metadata"]["name"]: d["spec"]["template"]["spec"] for d in bundle("k8s", **raw) if d.get("kind") in ("Deployment", "StatefulSet")}

    def test_every_container_including_init_containers_is_hardened_and_the_pod_uses_the_default_seccomp_profile(self):
        w = self.workloads()
        self.assertEqual(sorted(w), ["caddy", "geoserver", "mapproxy", "postgis", "qgis", "tomcat"])
        want = {"postgis": ["CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"], "qgis": ["CHOWN", "SETGID", "SETUID"], "caddy": ["NET_BIND_SERVICE"]}
        for name, spec in w.items():
            self.assertEqual(spec["securityContext"], {"seccompProfile": {"type": "RuntimeDefault"}}, name)
            for c in spec.get("initContainers", []) + spec["containers"]:
                sc = c["securityContext"]
                if c["name"] in hardening.NO_PRIVILEGE_LOCK:
                    self.assertNotIn("allowPrivilegeEscalation", sc)
                else:
                    self.assertIs(sc["allowPrivilegeEscalation"], False, f"{name}/{c['name']}")
                self.assertEqual(sc["capabilities"]["drop"], ["ALL"])
                self.assertEqual(sc["capabilities"].get("add", []), want.get(c["name"], []), f"{name}/{c['name']}")

    def test_switched_off_leaves_the_manifests_as_they_were(self):
        for name, spec in self.workloads(security={"harden_containers": False}).items():
            self.assertNotIn("securityContext", spec, name)
            for c in spec.get("initContainers", []) + spec["containers"]:
                self.assertNotIn("securityContext", c, f"{name}/{c['name']}")


class ModuleTests(unittest.TestCase):
    def test_every_generated_workload_has_an_entry(self):
        self.assertEqual(sorted(hardening.CAPS), ["caddy", "geoserver", "init", "mapproxy", "postgis", "qgis", "tomcat"])

    def test_disabled_snippets_are_empty(self):
        self.assertEqual(hardening.compose_snippet("postgis", False), "")
        self.assertEqual(hardening.k8s_container_snippet("postgis", False), "")
        self.assertEqual(hardening.k8s_pod_snippet(False), "")


if __name__ == "__main__":
    unittest.main()
