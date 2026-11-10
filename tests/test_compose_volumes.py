"""A Compose file must never contain an empty top-level `volumes:` (Docker Compose rejects it: "volumes must be a mapping").
Found by the real backup test on a QGIS-Server-only stack."""
import itertools
import tempfile
import unittest
from pathlib import Path

import yaml

from gisstack import config, generate

OPTIONAL = ("postgis", "geoserver", "qgis_server", "mapproxy", "caddy")


class ComposeVolumes(unittest.TestCase):
    def test_every_component_combination_yields_a_valid_volumes_section(self):
        for n in range(1, 4):
            for combo in itertools.combinations(OPTIONAL, n):
                comps = {k: k in combo for k in (*OPTIONAL, "tomcat", "webmap")}
                cfg = config.resolve({"project": "vol", "targets": ["compose"], "deploy_target": "compose", "components": comps}, generate_secrets=False, persist_secrets=False)
                with tempfile.TemporaryDirectory() as d:
                    generate.generate_bundle(cfg, Path(d))
                    doc = yaml.safe_load((Path(d) / "compose" / "docker-compose.yml").read_text())
                vols = doc.get("volumes", {})
                self.assertTrue("volumes" not in doc or isinstance(vols, dict) and vols, (combo, vols))


if __name__ == "__main__":
    unittest.main()
