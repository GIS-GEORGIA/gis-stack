"""deb / rpm packages installed with the real package managers in distro containers (Phase 11b).  Opt-in: GISSTACK_TEST_INSTALLER=1.

The package is built from the working tree by packaging/build.sh (deb needs dpkg-deb, rpm needs rpmbuild; a format whose builder is missing is skipped).
Checks: it installs with its dependencies, `gisstack` runs and reports the right version, `generate` writes a bundle, the web unit is installed, and
removing the package takes the program away."""
from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

from . import sysd

ROOT = Path(__file__).resolve().parents[2]
VERSION = re.search(r'__version__ = "([^"]+)"', (ROOT / "gisstack" / "__init__.py").read_text()).group(1)


def build(kind: str) -> Path | None:
    tool = {"deb": "dpkg-deb", "rpm": "rpmbuild"}[kind]
    if not shutil.which(tool):
        return None
    subprocess.run([str(ROOT / "packaging" / "build.sh"), kind], cwd=ROOT, check=True, capture_output=True, timeout=600)
    found = sorted((ROOT / "dist").glob("*.deb" if kind == "deb" else "*.rpm"), key=lambda p: p.stat().st_mtime)
    return found[-1] if found else None


@unittest.skipUnless(sysd.enabled(), "set GISSTACK_TEST_INSTALLER=1 (needs docker)")
class PackageTests(unittest.TestCase):
    def test_install_run_remove(self):
        done = 0
        for distro in sysd.wanted():
            family = sysd.DISTROS[distro][1]
            pkg = build(family)
            if pkg is None:
                continue
            done += 1
            with self.subTest(distro=distro), sysd.Box(distro) as box:
                box.put(pkg, f"/root/pkg.{family}")
                if family == "deb":
                    box.sh("apt-get update -qq && apt-get install -y -qq /root/pkg.deb >/dev/null", timeout=900)
                else:
                    box.sh("dnf -y -q install /root/pkg.rpm >/dev/null", timeout=900)
                self.assertIn(VERSION, box.sh("gisstack --version").stdout, distro)
                box.sh("gisstack generate -o /tmp/gen && test -f /tmp/gen/native/install.sh && bash -n /tmp/gen/native/install.sh")
                self.assertEqual(box.sh("systemctl cat gisstack-web.service >/dev/null && echo yes").stdout.strip(), "yes", distro)
                box.sh("apt-get remove -y -qq gisstack-installer >/dev/null" if family == "deb" else "dnf -y -q remove gisstack-installer >/dev/null")
                self.assertNotEqual(box.sh("command -v gisstack", check=False).returncode, 0, f"{distro}: program still present after removal")
        if not done:
            self.skipTest("no package builder available (dpkg-deb / rpmbuild) for the selected distributions")


if __name__ == "__main__":
    unittest.main()
