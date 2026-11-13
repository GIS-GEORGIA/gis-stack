"""packaging/rotate-token-on-upgrade.sh: the one-time token replacement on upgrade (S-1), run with temporary directories and a fake systemctl.
The real thing - a deb upgraded over the previous release in a systemd container - is tests/integration/test_package_upgrade.py."""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "packaging" / "rotate-token-on-upgrade.sh"
SECRET = "SEKRET-TOKEN-VALUE-0123456789abcdef"


@unittest.skipUnless(shutil.which("sh"), "sh needed")
class RotateTests(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.addCleanup(self.fix_and_remove)
        self.state, self.mark_dir, self.bin = self.td / "state", self.td / "etc", self.td / "bin"
        for d in (self.state, self.bin):
            d.mkdir()
        (self.bin / "systemctl").write_text('#!/bin/sh\necho "$*" >> "$FAKE_LOG"\nexit 0\n')
        (self.bin / "systemctl").chmod(0o755)
        self.log = self.td / "systemctl.log"

    def fix_and_remove(self):
        self.state.chmod(0o755)
        shutil.rmtree(self.td, ignore_errors=True)

    def run_script(self, path_extra=True):
        env = {**os.environ, "GISSTACK_STATE_DIR": str(self.state), "GISSTACK_MARK_DIR": str(self.mark_dir), "FAKE_LOG": str(self.log),
               "PATH": (f"{self.bin}:" if path_extra else "") + os.environ["PATH"]}
        return subprocess.run(["sh", str(SCRIPT)], capture_output=True, text=True, env=env, timeout=30)

    def restarts(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def token(self):
        (self.state / "web.token").write_text(SECRET)

    def test_upgrade_over_an_exposed_token_removes_it_once_and_restarts_the_services(self):
        self.token()
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.state / "web.token").exists())
        self.assertTrue((self.mark_dir / "token-protected").exists())
        self.assertEqual(self.restarts(), ["try-restart gisstack-web.service gisstack-web-root.service"])      # the old token held in memory dies too
        self.assertNotIn(SECRET, r.stdout + r.stderr)                                        # never printed

    def test_later_runs_do_not_rotate_again_not_on_upgrades_and_not_on_restarts(self):
        self.token()
        self.run_script()
        (self.state / "web.token").write_text("the-new-token")                              # the service created a new one
        self.log.unlink()
        for _ in range(3):
            r = self.run_script()
            self.assertEqual(r.returncode, 0)
        self.assertEqual((self.state / "web.token").read_text(), "the-new-token")
        self.assertEqual(self.restarts(), [])

    def test_fresh_install_only_writes_the_marker_and_touches_no_service(self):
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.mark_dir / "token-protected").exists())
        self.assertEqual(self.restarts(), [])
        self.assertEqual(r.stdout.strip(), "")
        self.token()                                                                          # the token of a fresh installation survives the next upgrade
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual((self.state / "web.token").read_text(), SECRET)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root can delete from a read-only directory")
    def test_an_undeletable_token_does_not_fail_the_package_and_is_retried_next_time(self):
        self.token()
        self.state.chmod(0o555)
        r = self.run_script()
        self.assertEqual(r.returncode, 0)                                                     # a package installation must never fail because of this
        self.assertIn("could not delete the old web token", r.stderr)
        self.assertIn("rm -f", r.stderr)                                                      # the recovery path is spelled out
        self.assertFalse((self.mark_dir / "token-protected").exists())                        # no marker: the next run tries again
        self.assertNotIn(SECRET, r.stdout + r.stderr)
        self.state.chmod(0o755)
        self.assertEqual(self.run_script().returncode, 0)
        self.assertFalse((self.state / "web.token").exists())
        self.assertTrue((self.mark_dir / "token-protected").exists())

    def test_without_systemctl_the_token_is_still_replaced(self):
        self.token()
        bare = self.td / "bare"                                                              # a PATH that has the basics but no systemctl (never the host's)
        bare.mkdir()
        for tool in ("sh", "mkdir", "rm"):
            (bare / tool).symlink_to(shutil.which(tool))
        env = {**os.environ, "GISSTACK_STATE_DIR": str(self.state), "GISSTACK_MARK_DIR": str(self.mark_dir), "PATH": str(bare)}
        r = subprocess.run([str(bare / "sh"), str(SCRIPT)], capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.state / "web.token").exists())
        self.assertIn("restart the web service", r.stderr)

    def test_the_script_is_executable_in_the_source_tree_and_the_build_keeps_it_so(self):
        self.assertTrue(SCRIPT.stat().st_mode & stat.S_IXUSR)
        build = (ROOT / "packaging" / "build.sh").read_text()
        self.assertLess(build.index('find "$STAGE/usr/lib/gisstack" -type f -exec chmod 644'), build.index('chmod 755 "$STAGE/usr/lib/gisstack/rotate-token-on-upgrade.sh"'))
        for f in ("gisstack.spec", "nfpm.yaml", "postinstall.sh"):
            self.assertIn("rotate-token-on-upgrade" if f != "nfpm.yaml" else "postinstall.sh", (ROOT / "packaging" / f).read_text(), f)


if __name__ == "__main__":
    unittest.main()
