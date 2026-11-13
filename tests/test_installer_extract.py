"""The native installer unpacks Tomcat with `extract_tree`: an interrupted or failed extraction must leave nothing a re-run takes for a finished
installation (audit 2026-11: `tar -xzf ... -C /opt` straight into place, then `[ ! -d /opt/apache-tomcat-X ]` skipped the re-extraction of a half-unpacked tree
and the installer reported success with a broken Tomcat).  Real bash + real tar; the function text is taken from the rendered install.sh."""
import re
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from gisstack import config, generate


def function_text():
    out = Path(tempfile.mkdtemp())
    try:
        generate.generate_bundle(config.resolve(config.default_config()), out)
        sh = (out / "native/install.sh").read_text(encoding="utf-8")
    finally:
        shutil.rmtree(out, ignore_errors=True)
    m = re.search(r"^extract_tree\(\) \{.*?^\}\n", sh, re.S | re.M)
    assert m, "extract_tree is missing from install.sh"
    return m.group(0)


@unittest.skipUnless(shutil.which("bash") and shutil.which("tar"), "bash and tar needed")
class ExtractTree(unittest.TestCase):
    NAME = "apache-tomcat-9.9.9"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.parent = self.tmp / "opt"
        self.parent.mkdir()
        src = self.tmp / "src" / self.NAME
        (src / "lib").mkdir(parents=True)
        (src / "lib" / "catalina.jar").write_bytes(b"jar" * 1000)
        (src / "bin").mkdir()
        (src / "bin" / "catalina.sh").write_text("#!/bin/sh\n" + "x" * 200000)          # big enough to be cut in the middle
        self.good = self.tmp / "good.tgz"
        with tarfile.open(self.good, "w:gz") as tf:
            tf.add(src, arcname=self.NAME)
        self.truncated = self.tmp / "cut.tgz"
        self.truncated.write_bytes(self.good.read_bytes()[: self.good.stat().st_size // 2])
        self.fn = function_text()

    def run_fn(self, tgz):
        script = f'set -Eeuo pipefail\n{self.fn}\nextract_tree "{tgz}" "{self.parent}" "{self.NAME}" lib/catalina.jar\n'
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)

    def leftovers(self):
        return sorted(p.name for p in self.parent.iterdir())

    def test_a_good_archive_is_installed_and_nothing_is_left_beside_it(self):
        r = self.run_fn(self.good)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.parent / self.NAME / "lib" / "catalina.jar").is_file())
        self.assertEqual(self.leftovers(), [self.NAME])

    def test_a_truncated_archive_fails_and_leaves_no_directory_a_rerun_would_trust(self):
        r = self.run_fn(self.truncated)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.leftovers(), [])
        r = self.run_fn(self.good)                                                  # the re-run installs completely
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.parent / self.NAME / "bin" / "catalina.sh").is_file())

    def test_a_half_unpacked_leftover_of_an_older_installer_is_replaced(self):
        broken = self.parent / self.NAME
        (broken / "bin").mkdir(parents=True)
        (broken / "bin" / "catalina.sh").write_text("partial")                      # no lib/catalina.jar: the old extraction stopped here
        r = self.run_fn(self.good)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.parent / self.NAME / "lib" / "catalina.jar").is_file())
        self.assertGreater((self.parent / self.NAME / "bin" / "catalina.sh").stat().st_size, 1000)
        self.assertEqual(self.leftovers(), [self.NAME])

    def test_a_complete_installation_is_left_alone(self):
        self.assertEqual(self.run_fn(self.good).returncode, 0)
        mark = self.parent / self.NAME / "local-change"
        mark.write_text("operator edit")
        self.assertEqual(self.run_fn(self.truncated).returncode, 0)                 # nothing to do: the archive is not even read
        self.assertTrue(mark.exists())

    def test_the_installer_uses_it_for_tomcat_and_checks_the_marker_instead_of_the_directory(self):
        out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, out, True)
        generate.generate_bundle(config.resolve(config.default_config()), out)
        sh = (out / "native/install.sh").read_text(encoding="utf-8")
        self.assertIn('extract_tree "$tmp/tc.tgz" /opt "apache-tomcat-$ver" lib/catalina.jar', sh)
        self.assertIn('[ ! -e "/opt/apache-tomcat-$ver/lib/catalina.jar" ]', sh)
        self.assertNotIn('tar -xzf "$tmp/tc.tgz" -C /opt', sh)
        subprocess.run(["bash", "-n", str(out / "native/install.sh")], check=True)


if __name__ == "__main__":
    unittest.main()
