"""deb / rpm packages installed with the real package managers in distro containers (Phase 11b).  Opt-in: GISSTACK_TEST_INSTALLER=1.

The package is built from the working tree by packaging/build.sh (deb needs dpkg-deb, rpm needs rpmbuild; a format whose builder is missing is skipped).
Checks: it installs with its dependencies, `gisstack` runs and reports the right version, `generate` writes a bundle, the web unit is installed, and
removing the package takes the program away."""
from __future__ import annotations

import re
import shutil
import subprocess
import time
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
    def wait_http(self, box, secs=40):
        for _ in range(secs):
            code = box.sh("curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8765/ || true", check=False).stdout.strip()
            if code.isdigit() and int(code) > 0:
                return int(code)
            time.sleep(1)
        return 0

    def web_service(self, box, distro):
        """The packaged web service really starts, is NOT root, serves with the production server, keeps its token out of the journal and comes back after a crash."""
        box.sh("systemctl start gisstack-web")
        self.assertGreater(self.wait_http(box), 0, f"{distro}: the web service does not answer\n" + box.sh("journalctl -u gisstack-web --no-pager | tail -20", check=False).stdout)
        pid = box.sh("systemctl show -p MainPID --value gisstack-web").stdout.strip()
        self.assertTrue(pid.isdigit() and int(pid) > 1, pid)
        uid = box.sh(f"stat -c %u /proc/{pid}").stdout.strip()                       # no `ps` in minimal images
        self.assertNotEqual(uid, "0", f"{distro}: the web service runs as root")
        token = box.sh("cat /var/lib/gisstack-installer/web.token").stdout.strip()
        self.assertGreaterEqual(len(token), 20)
        self.assertEqual(box.sh("stat -c %a /var/lib/gisstack-installer/web.token").stdout.strip(), "600")
        self.assertEqual(box.sh("stat -L -c %a /var/lib/gisstack-installer").stdout.strip(), "700")
        journal = box.sh("journalctl -u gisstack-web --no-pager", check=False).stdout
        self.assertNotIn(token, journal, f"{distro}: the access token is in the journal")
        self.assertIn("waitress production server", journal, f"{distro}: not served by waitress\n" + journal[-600:])
        meta = box.sh(f"curl -s -H 'Authorization: Bearer {token}' http://127.0.0.1:8765/api/v1/meta").stdout
        self.assertIn('"version"', meta)
        self.assertEqual(box.sh("curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8765/api/v1/meta").stdout.strip(), "401")     # no token, no data
        box.sh(f"kill -9 {pid}")
        for _ in range(40):
            new = box.sh("systemctl show -p MainPID --value gisstack-web", check=False).stdout.strip()
            if new.isdigit() and int(new) > 1 and new != pid and self.wait_http(box, 2) > 0:
                break
            time.sleep(1)
        else:
            self.fail(f"{distro}: the service did not come back after a crash")
        box.sh("systemctl stop gisstack-web")

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
                    box.sh("dnf -y -q install epel-release >/dev/null && dnf -y -q install /root/pkg.rpm >/dev/null", timeout=900)      # python3-flask comes from EPEL
                self.assertIn(VERSION, box.sh("gisstack --version").stdout, distro)
                box.sh("gisstack generate -o /tmp/gen && test -f /tmp/gen/native/install.sh && bash -n /tmp/gen/native/install.sh")
                self.assertEqual(box.sh("systemctl cat gisstack-web.service >/dev/null && echo yes").stdout.strip(), "yes", distro)
                self.assertEqual(box.sh("systemctl cat gisstack-web-root.service >/dev/null && echo yes").stdout.strip(), "yes", distro)    # the opt-in root unit
                self.web_service(box, distro)
                box.sh("apt-get remove -y -qq gisstack-installer >/dev/null" if family == "deb" else "dnf -y -q remove gisstack-installer >/dev/null")
                self.assertNotEqual(box.sh("command -v gisstack", check=False).returncode, 0, f"{distro}: program still present after removal")
        if not done:
            self.skipTest("no package builder available (dpkg-deb / rpmbuild) for the selected distributions")


if __name__ == "__main__":
    unittest.main()
