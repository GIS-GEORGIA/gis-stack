"""Upgrade of the packaged service from the previous release to the current tree, in a throw-away systemd container.  Opt-in: GISSTACK_TEST_INSTALLER=1.

The previous package is BUILT from a git ref (GISSTACK_UPGRADE_FROM, default v0.1.7: the last release, whose web service ran as root); the current one is
built from the working tree.  Needs `git` with that ref available and `dpkg-deb` (deb only: `rpmbuild` is not available on most dev machines, so the
rpm upgrade is skipped here and stays UNVERIFIED).

Asserted: state (token, settings, a saved project, a secret file) survives with its content; the token still authenticates; after the upgrade and a
restart the service runs UNPRIVILEGED with the same token and the state is owned by the service user with restrictive modes; a package that fails to
install (truncated file) leaves the working installation untouched; a downgrade (rollback) works and the old root service reads the state again."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from . import sysd

ROOT = Path(__file__).resolve().parents[2]
FROM = os.environ.get("GISSTACK_UPGRADE_FROM", "v0.1.7")
HOME = "/var/lib/gisstack-installer"


def ref_exists(ref: str) -> bool:
    return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--verify", "-q", ref + "^{commit}"], capture_output=True).returncode == 0


def build_deb(src: Path) -> Path:
    subprocess.run([str(src / "packaging" / "build.sh"), "deb"], cwd=src, check=True, capture_output=True, timeout=600)
    return sorted((src / "dist").glob("*.deb"), key=lambda p: p.stat().st_mtime)[-1]


@unittest.skipUnless(sysd.enabled() and shutil.which("dpkg-deb") and ref_exists(FROM), "GISSTACK_TEST_INSTALLER=1, docker, dpkg-deb and the git ref of the previous release needed")
class DebUpgradeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        old_src = cls.tmp / "old"
        old_src.mkdir()
        arc = subprocess.run(["git", "-C", str(ROOT), "archive", FROM], capture_output=True, check=True).stdout
        subprocess.run(["tar", "-x", "-C", str(old_src)], input=arc, check=True)
        cls.old = build_deb(old_src)
        cls.new = build_deb(ROOT)
        cls.old_version = re.search(r'__version__ = "([^"]+)"', (old_src / "gisstack" / "__init__.py").read_text()).group(1)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def wait_http(self, box, secs=40):
        for _ in range(secs):
            code = box.sh("curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8765/ || true", check=False).stdout.strip()
            if code.isdigit() and int(code) > 0:
                return int(code)
            time.sleep(1)
        return 0

    def uid_of_service(self, box):
        pid = box.sh("systemctl show -p MainPID --value gisstack-web").stdout.strip()
        self.assertTrue(pid.isdigit() and int(pid) > 1, pid)
        return box.sh(f"stat -c %u /proc/{pid}").stdout.strip()

    def api_ok(self, box, token):
        return box.sh(f"curl -s -o /dev/null -w '%{{http_code}}' -H 'Authorization: Bearer {token}' http://127.0.0.1:8765/api/v1/meta").stdout.strip() == "200"

    def test_upgrade_failure_handling_and_rollback(self):
        with sysd.Box("ubuntu-24.04") as box:
            box.put(self.old, "/root/old.deb")
            box.put(self.new, "/root/new.deb")
            box.sh("apt-get update -qq && apt-get install -y -qq /root/old.deb >/dev/null", timeout=900)
            self.assertIn(self.old_version, box.sh("gisstack --version").stdout)
            box.sh("systemctl start gisstack-web")
            self.assertGreater(self.wait_http(box), 0)
            self.assertEqual(self.uid_of_service(box), "0", "the previous release runs the web service as root (the premise of this test)")
            token = box.sh(f"cat {HOME}/web.token").stdout.strip()
            self.assertTrue(self.api_ok(box, token))
            # state a real installation has: settings, a saved project, a generated secret
            box.sh(f"GISSTACK_HOME={HOME} gisstack settings --lang en >/dev/null 2>&1 || true")
            box.sh(f"GISSTACK_HOME={HOME} gisstack generate -o /tmp/gen >/dev/null && mkdir -p {HOME}/secrets && umask 077 && echo sekret-marker > {HOME}/secrets/demo.json")
            before = box.sh(f"cd {HOME} && find . -type f -printf '%P %m\\n' | sort").stdout

            # --- failure: a truncated package must not damage the working installation
            box.sh("head -c 3000 /root/new.deb > /root/broken.deb")
            bad = box.sh("dpkg -i /root/broken.deb", check=False)
            self.assertNotEqual(bad.returncode, 0)
            self.assertIn(self.old_version, box.sh("gisstack --version").stdout)                       # old version still installed and runnable
            self.assertTrue(self.api_ok(box, token), "the running service must survive a failed upgrade")

            # --- upgrade
            box.sh("apt-get install -y -qq /root/new.deb >/dev/null 2>&1 || dpkg -i /root/new.deb >/dev/null", timeout=900)
            new_version = re.search(r'__version__ = "([^"]+)"', (ROOT / "gisstack" / "__init__.py").read_text()).group(1)
            self.assertIn(new_version, box.sh("gisstack --version").stdout)
            self.assertEqual(box.sh("systemctl cat gisstack-web-root.service >/dev/null && echo yes").stdout.strip(), "yes")      # the opt-in root unit arrives with the upgrade
            self.assertEqual(box.sh(f"cat {HOME}/secrets/demo.json").stdout.strip(), "sekret-marker")                           # data untouched by the package step
            old_journal = box.sh("journalctl -u gisstack-web --no-pager", check=False).stdout
            self.assertIn(token, old_journal, "premise: the previous release printed its token to the journal")          # FINDING: it stays valid after the upgrade (see docs)
            t0 = box.sh("date '+%Y-%m-%d %H:%M:%S'").stdout.strip()
            time.sleep(1)
            box.sh("systemctl daemon-reload; systemctl restart gisstack-web")
            self.assertGreater(self.wait_http(box), 0, box.sh("journalctl -u gisstack-web --no-pager | tail -20", check=False).stdout)
            self.assertNotEqual(self.uid_of_service(box), "0", "after the upgrade the service must not run as root")
            self.assertEqual(box.sh(f"cat {HOME}/web.token").stdout.strip(), token)                                              # same token: existing clients keep working
            self.assertTrue(self.api_ok(box, token))
            after = box.sh(f"cd {HOME} && find . -type f -printf '%P %m\\n' | sort").stdout
            self.assertEqual(sorted(set(before.splitlines()) - set(after.splitlines())), [], "files lost or changed mode in the upgrade")
            self.assertEqual(box.sh(f"stat -L -c %a {HOME}").stdout.strip(), "700")
            owner = box.sh(f"stat -c %U {HOME}/web.token").stdout.strip()
            self.assertNotEqual(owner, "root", "the state must belong to the unprivileged service user after the first start")
            journal = box.sh(f"journalctl -u gisstack-web --no-pager --since '{t0}'", check=False).stdout
            self.assertIn("waitress production server", journal)
            self.assertNotIn(token, journal)                                                                                     # the new release never prints it

            # --- the documented token rotation after the upgrade (docs/USAGE.md, section 10): the old token (printed to the journal by <= 0.1.7) must stop working
            box.sh("systemctl stop gisstack-web")
            # NOT `gisstack web --rotate-token` as root: that leaves a root-owned token the unprivileged service cannot read (found by this test) - the service creates a new one itself
            box.sh(f"rm -f {HOME}/web.token")
            box.sh("systemctl start gisstack-web")
            self.assertGreater(self.wait_http(box), 0, box.sh("journalctl -u gisstack-web --no-pager | tail -20", check=False).stdout)
            rotated = box.sh(f"cat {HOME}/web.token").stdout.strip()
            self.assertEqual(box.sh(f"stat -c %a {HOME}/web.token").stdout.strip(), "600")
            self.assertNotEqual(box.sh(f"stat -c %U {HOME}/web.token").stdout.strip(), "root")
            self.assertNotEqual(rotated, token, "the token must change")
            self.assertFalse(self.api_ok(box, token), "the old token (readable in the old journal) must no longer work")
            self.assertTrue(self.api_ok(box, rotated))
            self.assertNotEqual(self.uid_of_service(box), "0")
            token = rotated

            # --- rollback: install the previous package again; the old root service reads the same state
            box.sh("systemctl stop gisstack-web; dpkg -i /root/old.deb >/dev/null", timeout=300)
            self.assertIn(self.old_version, box.sh("gisstack --version").stdout)
            box.sh("systemctl daemon-reload; systemctl start gisstack-web")
            self.assertGreater(self.wait_http(box), 0, box.sh("journalctl -u gisstack-web --no-pager | tail -20", check=False).stdout)
            self.assertEqual(self.uid_of_service(box), "0")
            self.assertEqual(box.sh(f"cat {HOME}/web.token").stdout.strip(), token)
            self.assertTrue(self.api_ok(box, token))


if __name__ == "__main__":
    unittest.main()
