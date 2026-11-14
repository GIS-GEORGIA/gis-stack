"""Native MapProxy on Ubuntu 22.04 (Python 3.10 system interpreter): audit finding S-12.

The generated `install.sh --only mapproxy` is run in a real systemd container. By default it must obtain Python 3.11 from the
pinned-key deadsnakes source and install the fixed Pillow 13; with GISSTACK_NO_THIRD_PARTY_REPOS=1 it must add no third-party source
and fall back (with a warning) to the Python 3.10 list. Opt-in: GISSTACK_TEST_INSTALLER=1, distro ubuntu-22.04 must be selected."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gisstack import config, generate
from . import sysd

CFG = {"project": "it", "targets": ["native"], "deploy_target": "native",
       "components": {"postgis": False, "tomcat": False, "geoserver": False, "qgis_server": False, "mapproxy": True, "caddy": False, "webmap": False}}
LIST = "/etc/apt/sources.list.d/gisstack-deadsnakes.list"
PROBE = "/opt/gisstack/mapproxy-venv/bin/python -c 'import sys, PIL; print(sys.version_info[0], sys.version_info[1], PIL.__version__)'"


@unittest.skipUnless(sysd.enabled() and "ubuntu-22.04" in sysd.wanted(), "set GISSTACK_TEST_INSTALLER=1 with ubuntu-22.04 in GISSTACK_TEST_DISTROS")
class MapproxyPythonTests(unittest.TestCase):
    def bundle(self, box: sysd.Box) -> None:
        out = Path(tempfile.mkdtemp())
        generate.generate_bundle(config.resolve(CFG, generate_secrets=True), out)
        box.put(out / "native", "/root/b")

    def test_python311_from_pinned_source_gives_fixed_pillow(self):
        with sysd.Box("ubuntu-22.04") as box:
            self.bundle(box)
            self.assertEqual(box.sh("python3 -c 'import sys; print(sys.version_info[1])'").stdout.strip(), "10")
            box.sh("cd /root/b && ./install.sh --only mapproxy", timeout=1500)
            major, minor, pillow = box.sh(PROBE).stdout.split()
            self.assertEqual((major, minor), ("3", "11"))
            self.assertTrue(pillow.startswith("13."), pillow)
            self.assertIn("signed-by=/usr/share/keyrings/gisstack-deadsnakes.gpg", box.sh(f"cat {LIST}").stdout)
            fpr = box.sh("gpg --show-keys --with-colons /usr/share/keyrings/gisstack-deadsnakes.gpg").stdout
            self.assertIn("F23C5A6CF475977595C89F51BA6932366A755776", fpr)
            self.assertEqual(box.sh("systemctl is-active gisstack-mapproxy", check=False).stdout.strip(), "active")
            box.sh("cd /root/b && ./install.sh --only mapproxy", timeout=900)      # re-running is harmless
            self.assertEqual(box.sh(PROBE).stdout.split()[:2], ["3", "11"])

    def test_opt_out_adds_no_third_party_source_and_warns(self):
        with sysd.Box("ubuntu-22.04") as box:
            self.bundle(box)
            res = box.sh("cd /root/b && GISSTACK_NO_THIRD_PARTY_REPOS=1 ./install.sh --only mapproxy 2>&1", timeout=1500)
            self.assertIn("known vulnerabilities", res.stdout)
            self.assertEqual(box.sh(f"test -e {LIST} && echo yes || echo no").stdout.strip(), "no")
            major, minor, pillow = box.sh(PROBE).stdout.split()
            self.assertEqual((major, minor), ("3", "10"))
            self.assertTrue(pillow.startswith("12."), pillow)


if __name__ == "__main__":
    unittest.main()
