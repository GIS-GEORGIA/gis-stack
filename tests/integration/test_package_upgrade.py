"""Upgrade of the packaged service from the previous release to the current tree, in a throw-away systemd container.  Opt-in: GISSTACK_TEST_INSTALLER=1.

The previous package is BUILT from a git ref (GISSTACK_UPGRADE_FROM, default v0.1.7: the last release, whose web service ran as root); the current one is
built from the working tree.  Needs `git` with that ref available; the deb needs `dpkg-deb` on the host.  The rpm packages are built INSIDE a Rocky 9
container (`rpm-build` installed there), so no rpm tooling is needed on the host: the same upgrade story is then run with `rpm` / `dnf` (RpmUpgradeTests).

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


class Helpers:
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


@unittest.skipUnless(sysd.enabled() and shutil.which("dpkg-deb") and ref_exists(FROM), "GISSTACK_TEST_INSTALLER=1, docker, dpkg-deb and the git ref of the previous release needed")
class DebUpgradeTests(Helpers, unittest.TestCase):
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

    def test_upgrade_failure_handling_and_rollback(self):
        with sysd.Box("ubuntu-24.04") as box:
            box.put(self.old, "/root/old.deb")
            box.put(self.new, "/root/new.deb")
            box.sh("apt-get update -qq && apt-get install -y -qq /root/old.deb python3-waitress >/dev/null", timeout=900)       # waitress is a Recommends: `dpkg -i` of the new package would not pull it
            self.assertIn(self.old_version, box.sh("gisstack --version").stdout)
            box.sh("systemctl start gisstack-web")
            self.assertGreater(self.wait_http(box), 0)
            self.assertEqual(self.uid_of_service(box), "0", "the previous release runs the web service as root (the premise of this test)")
            token = box.sh(f"cat {HOME}/web.token").stdout.strip()
            self.assertTrue(self.api_ok(box, token))
            # state a real installation has: settings, a saved project, a generated secret
            box.sh(f"GISSTACK_HOME={HOME} gisstack settings --lang en >/dev/null 2>&1 || true")
            box.sh(f"GISSTACK_HOME={HOME} gisstack generate -o /tmp/gen >/dev/null && mkdir -p {HOME}/secrets && umask 077 && echo sekret-marker > {HOME}/secrets/demo.json")
            before_other = box.sh(f"cd {HOME} && find . -type f -not -name web.token -printf '%P %m\\n' | sort").stdout.splitlines()

            # --- failure: a truncated package must not damage the working installation
            box.sh("head -c 3000 /root/new.deb > /root/broken.deb")
            bad = box.sh("dpkg -i /root/broken.deb", check=False)
            self.assertNotEqual(bad.returncode, 0)
            self.assertIn(self.old_version, box.sh("gisstack --version").stdout)                       # old version still installed and runnable
            self.assertTrue(self.api_ok(box, token), "the running service must survive a failed upgrade")

            # --- upgrade (the old root service is RUNNING with the old token in memory)
            old_journal = box.sh("journalctl -u gisstack-web --no-pager", check=False).stdout
            self.assertIn(token, old_journal, "premise: the previous release printed its token to the journal")
            t0 = box.sh("date '+%Y-%m-%d %H:%M:%S'").stdout.strip()
            time.sleep(1)
            up = box.sh("dpkg -i /root/new.deb 2>&1", timeout=900)
            self.assertNotIn(token, up.stdout + up.stderr, "the package scripts must never print a token")
            new_version = re.search(r'__version__ = "([^"]+)"', (ROOT / "gisstack" / "__init__.py").read_text()).group(1)
            self.assertIn(new_version, box.sh("gisstack --version").stdout)
            self.assertEqual(box.sh("systemctl cat gisstack-web-root.service >/dev/null && echo yes").stdout.strip(), "yes")      # the opt-in root unit arrives with the upgrade
            self.assertEqual(box.sh(f"cat {HOME}/secrets/demo.json").stdout.strip(), "sekret-marker")                           # data untouched by the package step
            self.assertTrue(box.sh("test -x /usr/lib/gisstack/rotate-token-on-upgrade.sh && echo yes").stdout.strip() == "yes")

            # S-1: the package replaced the token BY ITSELF and restarted the service - no manual step
            self.assertGreater(self.wait_http(box), 0, box.sh("journalctl -u gisstack-web --no-pager | tail -20", check=False).stdout)
            self.assertNotEqual(self.uid_of_service(box), "0", "after the upgrade the service must not run as root")
            self.assertFalse(self.api_ok(box, token), "the old token (readable in the old journal) must be rejected after the upgrade, also by the running service")
            rotated = box.sh(f"cat {HOME}/web.token").stdout.strip()
            self.assertNotEqual(rotated, token)
            self.assertTrue(self.api_ok(box, rotated))
            self.assertEqual(box.sh(f"stat -c %a {HOME}/web.token").stdout.strip(), "600")
            self.assertNotEqual(box.sh(f"stat -c %U {HOME}/web.token").stdout.strip(), "root", "the new token belongs to the service user")
            self.assertEqual(box.sh("test -e /etc/gisstack-installer/token-protected && echo yes").stdout.strip(), "yes")
            after = box.sh(f"cd {HOME} && find . -type f -not -name web.token -printf '%P %m\\n' | sort").stdout
            self.assertEqual(sorted(set(before_other) - set(after.splitlines())), [], "files lost or changed mode in the upgrade")
            self.assertEqual(box.sh(f"stat -L -c %a {HOME}").stdout.strip(), "700")
            journal = box.sh(f"journalctl -u gisstack-web --no-pager --since '{t0}'", check=False).stdout
            self.assertIn("waitress production server", journal, journal[-1800:])
            self.assertNotIn(rotated, journal)                                                                                   # the new release never prints it
            self.assertNotIn(token, journal)

            # no rotation on ordinary restarts, nor when the same package is installed again (later upgrades)
            box.sh("systemctl restart gisstack-web")
            self.assertGreater(self.wait_http(box), 0)
            self.assertEqual(box.sh(f"cat {HOME}/web.token").stdout.strip(), rotated, "a restart must not rotate the token")
            box.sh("dpkg -i /root/new.deb >/dev/null 2>&1", timeout=300)
            self.assertGreater(self.wait_http(box), 0)
            self.assertEqual(box.sh(f"cat {HOME}/web.token").stdout.strip(), rotated, "a later upgrade must not rotate it again")
            self.assertTrue(self.api_ok(box, rotated))

            # the documented manual recovery path still works (docs/USAGE.md): delete the token, restart
            box.sh(f"systemctl stop gisstack-web; rm -f {HOME}/web.token; systemctl start gisstack-web")
            self.assertGreater(self.wait_http(box), 0)
            manual = box.sh(f"cat {HOME}/web.token").stdout.strip()
            self.assertNotEqual(manual, rotated)
            self.assertFalse(self.api_ok(box, rotated))
            self.assertTrue(self.api_ok(box, manual))
            self.assertEqual(box.sh(f"stat -c %a {HOME}/web.token").stdout.strip(), "600")
            token = manual

            # --- rollback: install the previous package again; the old root service reads the same state
            box.sh("systemctl stop gisstack-web; dpkg -i /root/old.deb >/dev/null", timeout=300)
            self.assertIn(self.old_version, box.sh("gisstack --version").stdout)
            box.sh("systemctl daemon-reload; systemctl start gisstack-web")
            self.assertGreater(self.wait_http(box), 0, box.sh("journalctl -u gisstack-web --no-pager | tail -20", check=False).stdout)
            self.assertEqual(self.uid_of_service(box), "0")
            self.assertEqual(box.sh(f"cat {HOME}/web.token").stdout.strip(), token)
            self.assertTrue(self.api_ok(box, token))

    def test_fresh_install_keeps_its_token_across_restarts_and_reinstalls(self):
        """The rotation is for upgrades over an exposed token only: a fresh installation's token is never replaced."""
        with sysd.Box("ubuntu-24.04") as box:
            box.put(self.new, "/root/new.deb")
            box.sh("apt-get update -qq && apt-get install -y -qq /root/new.deb >/dev/null", timeout=900)
            self.assertEqual(box.sh("test -e /etc/gisstack-installer/token-protected && echo yes").stdout.strip(), "yes")
            box.sh("systemctl start gisstack-web")
            self.assertGreater(self.wait_http(box), 0)
            token = box.sh(f"cat {HOME}/web.token").stdout.strip()
            self.assertGreaterEqual(len(token), 20)
            self.assertEqual(box.sh(f"stat -c %a {HOME}/web.token").stdout.strip(), "600")
            self.assertNotEqual(self.uid_of_service(box), "0")
            for step in ("systemctl restart gisstack-web", "dpkg -i /root/new.deb >/dev/null 2>&1; systemctl try-restart gisstack-web"):
                box.sh(step, timeout=300)
                self.assertGreater(self.wait_http(box), 0)
                self.assertEqual(box.sh(f"cat {HOME}/web.token").stdout.strip(), token)
                self.assertTrue(self.api_ok(box, token))


def working_tree(dest: Path) -> Path:
    """The current tree without VCS data / build output, so that the rpm is built from exactly what is being tested."""
    dest.mkdir(parents=True)
    tar = subprocess.Popen(["tar", "-c", "-C", str(ROOT), "--exclude=.git", "--exclude=dist", "--exclude=build", "--exclude=__pycache__", "--exclude=*.pyc", "."], stdout=subprocess.PIPE)
    subprocess.run(["tar", "-x", "-C", str(dest)], stdin=tar.stdout, check=True)
    tar.wait()
    # rpm refuses to "upgrade" to the same name-version-release, and the working tree usually still carries the version of the last release (deb does not care):
    # build it as release 2, i.e. what the next package of that version would be.  The content is the current tree.
    spec = dest / "packaging" / "gisstack.spec"
    spec.write_text(spec.read_text().replace("Release:        1%{?dist}", "Release:        2%{?dist}"))
    return dest


@unittest.skipUnless(sysd.enabled() and ref_exists(FROM), "GISSTACK_TEST_INSTALLER=1, docker and the git ref of the previous release needed")
class RpmUpgradeTests(Helpers, unittest.TestCase):
    """The same upgrade story as the deb, with real rpm / dnf on Rocky 9.  The packages are built in the container (rpmbuild is not needed on the host)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        old_src = cls.tmp / "old"
        old_src.mkdir()
        arc = subprocess.run(["git", "-C", str(ROOT), "archive", FROM], capture_output=True, check=True).stdout
        subprocess.run(["tar", "-x", "-C", str(old_src)], input=arc, check=True)
        cls.old_src, cls.new_src = old_src, working_tree(cls.tmp / "new")
        cls.old_version = re.search(r'__version__ = "([^"]+)"', (old_src / "gisstack" / "__init__.py").read_text()).group(1)
        cls.new_version = re.search(r'__version__ = "([^"]+)"', (ROOT / "gisstack" / "__init__.py").read_text()).group(1)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def build(self, box):
        """Build both rpms inside the container; returns nothing, leaves /root/old.rpm and /root/new.rpm."""
        box.sh("dnf -y -q install rpm-build tar sed >/dev/null", timeout=900)
        for name, src in (("old", self.old_src), ("new", self.new_src)):
            box.put(src, f"/root/src-{name}")
            box.sh(f"cd /root/src-{name} && ./packaging/build.sh rpm >/dev/null && cp dist/*.rpm /root/{name}.rpm", timeout=600)

    def test_upgrade_failure_handling_and_rollback(self):
        with sysd.Box("rocky-9") as box:
            self.build(box)
            box.sh("dnf -y -q install epel-release >/dev/null && dnf -y -q install /root/old.rpm >/dev/null", timeout=900)                       # flask / jinja2 / yaml come from EPEL
            box.sh("dnf -y -q install python3-waitress >/dev/null 2>&1 || true", timeout=600)          # a weak dependency: the old release does not need it, the new one prefers it
            self.assertIn(self.old_version, box.sh("gisstack --version").stdout)
            box.sh("systemctl start gisstack-web")
            self.assertGreater(self.wait_http(box), 0, box.sh("journalctl -u gisstack-web --no-pager | tail -20", check=False).stdout)
            self.assertEqual(self.uid_of_service(box), "0", "the previous release runs the web service as root (the premise of this test)")
            token = box.sh(f"cat {HOME}/web.token").stdout.strip()
            self.assertTrue(self.api_ok(box, token))
            box.sh(f"GISSTACK_HOME={HOME} gisstack settings --lang en >/dev/null 2>&1 || true")
            box.sh(f"GISSTACK_HOME={HOME} gisstack generate -o /tmp/gen >/dev/null && mkdir -p {HOME}/secrets && umask 077 && echo sekret-marker > {HOME}/secrets/demo.json")
            before_other = box.sh(f"cd {HOME} && find . -type f -not -name web.token -printf '%P %m\\n' | sort").stdout.splitlines()

            # --- failure: a truncated package must not damage the working installation
            box.sh("head -c 3000 /root/new.rpm > /root/broken.rpm")
            bad = box.sh("rpm -U /root/broken.rpm", check=False)
            self.assertNotEqual(bad.returncode, 0)
            self.assertIn(self.old_version, box.sh("gisstack --version").stdout)
            self.assertTrue(self.api_ok(box, token), "the running service must survive a failed upgrade")

            # --- upgrade over the RUNNING old root service
            old_journal = box.sh("journalctl -u gisstack-web --no-pager", check=False).stdout
            self.assertIn(token, old_journal, "premise: the previous release printed its token to the journal")
            t0 = box.sh("date '+%Y-%m-%d %H:%M:%S'").stdout.strip()
            time.sleep(1)
            up = box.sh("rpm -U /root/new.rpm 2>&1", timeout=900)
            self.assertNotIn(token, up.stdout + up.stderr, "the package scripts must never print a token")
            self.assertIn(self.new_version, box.sh("gisstack --version").stdout)
            self.assertEqual(box.sh("rpm -q gisstack-installer").stdout.count("gisstack-installer-"), 1, "exactly one version installed after the upgrade")
            self.assertEqual(box.sh("systemctl cat gisstack-web-root.service >/dev/null && echo yes").stdout.strip(), "yes")
            self.assertEqual(box.sh(f"cat {HOME}/secrets/demo.json").stdout.strip(), "sekret-marker")
            self.assertEqual(box.sh("test -x /usr/lib/gisstack/rotate-token-on-upgrade.sh && echo yes").stdout.strip(), "yes")

            self.assertGreater(self.wait_http(box), 0, box.sh("journalctl -u gisstack-web --no-pager | tail -20", check=False).stdout)
            self.assertNotEqual(self.uid_of_service(box), "0", "after the upgrade the service must not run as root")
            self.assertFalse(self.api_ok(box, token), "the old token (readable in the old journal) must be rejected after the upgrade")
            rotated = box.sh(f"cat {HOME}/web.token").stdout.strip()
            self.assertNotEqual(rotated, token)
            self.assertTrue(self.api_ok(box, rotated))
            self.assertEqual(box.sh(f"stat -c %a {HOME}/web.token").stdout.strip(), "600")
            self.assertNotEqual(box.sh(f"stat -c %U {HOME}/web.token").stdout.strip(), "root")
            self.assertEqual(box.sh("test -e /etc/gisstack-installer/token-protected && echo yes").stdout.strip(), "yes")
            after = box.sh(f"cd {HOME} && find . -type f -not -name web.token -printf '%P %m\\n' | sort").stdout
            self.assertEqual(sorted(set(before_other) - set(after.splitlines())), [], "files lost or changed mode in the upgrade")
            self.assertEqual(box.sh(f"stat -L -c %a {HOME}").stdout.strip(), "700")
            journal = box.sh(f"journalctl -u gisstack-web --no-pager --since '{t0}'", check=False).stdout
            self.assertNotIn(rotated, journal)
            self.assertNotIn(token, journal)

            # no rotation on restart, nor on reinstalling the same package
            box.sh("systemctl restart gisstack-web")
            self.assertGreater(self.wait_http(box), 0)
            self.assertEqual(box.sh(f"cat {HOME}/web.token").stdout.strip(), rotated)
            box.sh("rpm -U --replacepkgs /root/new.rpm >/dev/null 2>&1", timeout=300)
            self.assertGreater(self.wait_http(box), 0)
            self.assertEqual(box.sh(f"cat {HOME}/web.token").stdout.strip(), rotated, "a later upgrade must not rotate it again")
            self.assertTrue(self.api_ok(box, rotated))

            # --- rollback: the previous package again; the old root service reads the same state
            box.sh("systemctl stop gisstack-web; rpm -U --oldpackage /root/old.rpm >/dev/null 2>&1", timeout=300)
            self.assertIn(self.old_version, box.sh("gisstack --version").stdout)
            box.sh("systemctl daemon-reload; systemctl start gisstack-web")
            self.assertGreater(self.wait_http(box), 0, box.sh("journalctl -u gisstack-web --no-pager | tail -20", check=False).stdout)
            self.assertEqual(self.uid_of_service(box), "0")
            self.assertEqual(box.sh(f"cat {HOME}/web.token").stdout.strip(), rotated)
            self.assertTrue(self.api_ok(box, rotated))

    def test_fresh_install_keeps_its_token_across_restarts_and_reinstalls(self):
        with sysd.Box("rocky-9") as box:
            self.build(box)
            box.sh("dnf -y -q install epel-release >/dev/null && dnf -y -q install /root/new.rpm >/dev/null", timeout=900)
            self.assertEqual(box.sh("test -e /etc/gisstack-installer/token-protected && echo yes").stdout.strip(), "yes")
            box.sh("systemctl start gisstack-web")
            self.assertGreater(self.wait_http(box), 0, box.sh("journalctl -u gisstack-web --no-pager | tail -20", check=False).stdout)
            token = box.sh(f"cat {HOME}/web.token").stdout.strip()
            self.assertGreaterEqual(len(token), 20)
            self.assertEqual(box.sh(f"stat -c %a {HOME}/web.token").stdout.strip(), "600")
            self.assertNotEqual(self.uid_of_service(box), "0")
            for step in ("systemctl restart gisstack-web", "rpm -U --replacepkgs /root/new.rpm >/dev/null 2>&1; systemctl try-restart gisstack-web"):
                box.sh(step, timeout=300)
                self.assertGreater(self.wait_http(box), 0)
                self.assertEqual(box.sh(f"cat {HOME}/web.token").stdout.strip(), token)
                self.assertTrue(self.api_ok(box, token))



if __name__ == "__main__":
    unittest.main()
