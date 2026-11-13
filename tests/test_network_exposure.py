"""S-2: without Caddy the generated Compose file must not publish GeoServer / MapProxy / QGIS Server / Tomcat on every host interface by default.
`network.bind` (default 127.0.0.1) decides; 0.0.0.0 is an explicit choice; an existing configuration written by gisstack (schema_version 1) keeps its old
behaviour, written down explicitly, with a warning - it never changes silently."""
import tempfile
import unittest
from pathlib import Path

import yaml

from gisstack import config, generate

DIRECT = {"geoserver": True, "mapproxy": True, "qgis_server": True, "tomcat": True, "webmap": False, "caddy": False, "postgis": True}


def compose_of(raw):
    cfg = config.resolve({"project": "net", "targets": ["compose"], "deploy_target": "compose", **raw}, generate_secrets=False, persist_secrets=False)
    with tempfile.TemporaryDirectory() as d:
        generate.generate_bundle(cfg, Path(d))
        return cfg, yaml.safe_load((Path(d) / "compose" / "docker-compose.yml").read_text())


def published(doc):
    out = {}
    for name, svc in doc["services"].items():
        for p in svc.get("ports") or []:
            out.setdefault(name, []).append(str(p))
    return out


class ExposureTests(unittest.TestCase):
    def test_a_new_configuration_publishes_the_direct_ports_on_loopback_only(self):
        cfg, doc = compose_of({"components": DIRECT})
        pub = published(doc)
        self.assertEqual(sorted(pub), ["geoserver", "mapproxy", "postgis", "qgis", "tomcat"], pub)
        for svc, ports in pub.items():
            for p in ports:
                self.assertTrue(p.startswith("127.0.0.1:"), f"{svc}: {p} is published on every interface")
        self.assertEqual(cfg["network"]["bind"], "127.0.0.1")

    def test_with_caddy_only_caddy_is_public_and_postgres_stays_on_loopback(self):
        _, doc = compose_of({"components": {**DIRECT, "caddy": True}})
        pub = published(doc)
        self.assertEqual(sorted(pub), ["caddy", "postgis"], pub)
        self.assertTrue(all(p.startswith("127.0.0.1:") for p in pub["postgis"]))
        self.assertTrue(all(not p.startswith("127.") for p in pub["caddy"]))                    # the intended public entry point

    def test_remote_access_is_an_explicit_choice_and_is_warned_about(self):
        cfg, doc = compose_of({"components": DIRECT, "network": {"bind": "0.0.0.0"}})
        self.assertTrue(all(p.startswith("0.0.0.0:") for ports in published(doc).values() for p in ports if not p.startswith("127.0.0.1:5432") and "5432" not in p))
        self.assertTrue(any("network.bind = 0.0.0.0" in w for w in cfg["_meta"]["warnings"]), cfg["_meta"]["warnings"])

    def test_a_specific_address_and_ipv6_are_supported(self):
        _, doc = compose_of({"components": DIRECT, "network": {"bind": "10.1.2.3"}})
        self.assertTrue(all(p.startswith("10.1.2.3:") for s, ports in published(doc).items() if s != "postgis" for p in ports))
        _, doc6 = compose_of({"components": DIRECT, "network": {"bind": "::1"}})
        self.assertTrue(all(p.startswith("[::1]:") for s, ports in published(doc6).items() if s != "postgis" for p in ports), published(doc6))

    def test_postgres_is_still_governed_by_its_own_remote_access_switch(self):
        _, doc = compose_of({"components": {"postgis": True, "caddy": False}, "postgres": {"remote_access": True}, "network": {"bind": "127.0.0.1"}})
        self.assertFalse(published(doc)["postgis"][0].startswith("127.0.0.1:"))

    def test_existing_configuration_keeps_its_behaviour_explicitly_and_a_minimal_one_gets_the_safe_default(self):
        old, doc = compose_of({"components": DIRECT, "schema_version": 1})                      # written by gisstack before network.bind existed
        self.assertEqual(old["network"]["bind"], "0.0.0.0")
        self.assertTrue(any(p.startswith("0.0.0.0:") for ports in published(doc).values() for p in ports))
        self.assertTrue(any("network.bind = 0.0.0.0" in w for w in old["_meta"]["warnings"]))
        minimal, _ = compose_of({"components": DIRECT})                                         # no schema_version: hand-written
        self.assertEqual(minimal["network"]["bind"], "127.0.0.1")
        explicit, _ = compose_of({"components": DIRECT, "schema_version": 1, "network": {"bind": "127.0.0.1"}})
        self.assertEqual(explicit["network"]["bind"], "127.0.0.1")                              # the user's own value is never overwritten

    def test_bad_values_are_refused(self):
        for bad in ("", "localhost", "127.0.0.1; rm -rf /", "0.0.0.0/0", "1.2.3", 5, None):
            with self.assertRaises(config.ConfigError, msg=repr(bad)):
                config.resolve({"project": "net", "network": {"bind": bad}}, generate_secrets=False, persist_secrets=False)

    def test_a_config_from_a_newer_program_is_still_refused(self):
        with self.assertRaises(config.ConfigError):
            config.resolve({"project": "net", "schema_version": config.SCHEMA_VERSION + 1}, generate_secrets=False, persist_secrets=False)


if __name__ == "__main__":
    unittest.main()
