"""Native installer in real systemd distro containers (Phase 11b).  Opt-in: GISSTACK_TEST_INSTALLER=1 (see tests/integration/sysd.py).

Installs PostgreSQL/PostGIS with the generated `install.sh` (real apt/dnf downloads, a few minutes per distribution), then drives gis-stack's OWN
code (ops.status, PostGIS manager) at the container, checks re-running is harmless, and that uninstall keeps data unless --purge."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gisstack import config, generate, ops, transport
from gisstack.postgis import manager as pgm
from gisstack.postgis.server import ScriptGateway
from . import sysd

CFG = {"project": "it", "targets": ["native"], "deploy_target": "native",
       "components": {"postgis": True, "tomcat": False, "geoserver": False, "qgis_server": False, "mapproxy": False, "caddy": False, "webmap": False}}


@unittest.skipUnless(sysd.enabled(), "set GISSTACK_TEST_INSTALLER=1 (needs docker; downloads packages inside containers)")
class InstallerTests(unittest.TestCase):
    def test_postgis_install_status_manager_uninstall(self):
        names = sysd.wanted()
        self.assertTrue(names, "GISSTACK_TEST_DISTROS names no known distribution")
        for distro in names:
            with self.subTest(distro=distro), sysd.Box(distro) as box:
                self.one(distro, box)

    def one(self, distro: str, box: sysd.Box) -> None:
        cfg = config.resolve(CFG, generate_secrets=True)
        out = Path(tempfile.mkdtemp())
        generate.generate_bundle(cfg, out)
        box.put(out / "native", "/root/b")

        listing = box.sh("cd /root/b && ./install.sh --list").stdout.split()
        self.assertEqual(listing[:2], ["base", "postgis"], distro)
        box.sh("cd /root/b && ./install.sh --only postgis", timeout=1500)

        self.assertEqual(box.sh("systemctl is-active postgresql* | grep -c '^active'", check=False).stdout.strip() != "0", True, f"{distro}: PostgreSQL not active")
        db = cfg["postgres"]["db"]
        ver = box.sh(f"su postgres -c \"psql -d {db} -Atc 'select postgis_version()'\"").stdout
        self.assertRegex(ver, r"^\d+\.\d+", distro)
        self.assertEqual(box.sh("stat -c %a /etc/gisstack/credentials.txt").stdout.strip(), "600", distro)

        box.sh("cd /root/b && ./install.sh --only postgis", timeout=900)            # re-running is harmless (idempotent)

        # gis-stack's own code against the real installation, executed inside the container
        with mock.patch.object(transport, "argv", lambda mode, ssh, script, *, root: box.argv(script)):
            st = ops.status(cfg, "local", None, "native")
            row = next(r for r in st["rows"] if r["id"] == "postgis")
            self.assertEqual(row["state"], "running", (distro, row))
            gw = ScriptGateway(cfg, "local", None)
            inv = pgm.inventory(gw, db)
            self.assertTrue(inv["extensions"], distro)
            self.assertTrue(any(e["name"] == "postgis" for e in inv["extensions"]), distro)
            self.assertTrue(pgm.health(inv)["ok"], (distro, pgm.health(inv)))

        # uninstall without --purge keeps the data, with --purge removes it
        box.sh("cd /root/b && ./uninstall.sh --only postgis", timeout=600)
        kept = box.sh(f"su postgres -c \"psql -Atc \\\"select count(*) from pg_database where datname='{db}'\\\"\"", check=False).stdout.strip()
        self.assertEqual(kept, "1", f"{distro}: data must survive an uninstall without --purge")
        box.sh("cd /root/b && ./uninstall.sh --only postgis --purge", timeout=600)
        gone = box.sh(f"su postgres -c \"psql -Atc \\\"select count(*) from pg_database where datname='{db}'\\\"\"", check=False).stdout.strip()
        self.assertEqual(gone, "0", f"{distro}: --purge must remove the database")


if __name__ == "__main__":
    unittest.main()
