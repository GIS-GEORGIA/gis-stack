"""`generate_bundle` replaces its output directory.  It must only ever replace a gis-stack bundle (audit 2026-11: `gisstack generate --out ~/Documents`
removed the whole directory without a word)."""
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from gisstack import cli, config, generate
from gisstack.core.errors import DeployError


class OutputDirectoryIsProtected(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.cfg = config.resolve(config.default_config())

    def test_a_directory_with_user_files_is_refused_and_untouched(self):
        d = self.tmp / "Documents"
        (d / "sub").mkdir(parents=True)
        (d / "thesis.odt").write_text("precious")
        (d / "sub" / "x").write_text("x")
        with self.assertRaises(DeployError) as cm:
            generate.generate_bundle(self.cfg, d)
        self.assertIn("not a gis-stack bundle", str(cm.exception))
        self.assertEqual((d / "thesis.odt").read_text(), "precious")
        self.assertEqual((d / "sub" / "x").read_text(), "x")
        self.assertEqual(sorted(p.name for p in d.iterdir()), ["sub", "thesis.odt"])

    def test_an_empty_or_missing_directory_is_fine(self):
        generate.generate_bundle(self.cfg, self.tmp / "new")
        (self.tmp / "empty").mkdir()
        generate.generate_bundle(self.cfg, self.tmp / "empty")
        self.assertTrue((self.tmp / "empty" / "stack.json").is_file())

    def test_a_previous_bundle_is_replaced_not_merged(self):
        d = self.tmp / "b"
        generate.generate_bundle(self.cfg, d)
        (d / "stale-file").write_text("old")
        generate.generate_bundle(self.cfg, d)
        self.assertFalse((d / "stale-file").exists())
        self.assertTrue((d / generate.BUNDLE_MARK).is_file())

    def test_a_bundle_from_an_older_release_without_the_marker_is_still_recognised(self):
        d = self.tmp / "old"
        generate.generate_bundle(self.cfg, d)
        (d / generate.BUNDLE_MARK).unlink()
        (d / "stale-file").write_text("old")
        generate.generate_bundle(self.cfg, d)
        self.assertFalse((d / "stale-file").exists())

    def test_a_file_or_symlink_is_not_replaced(self):
        f = self.tmp / "afile"
        f.write_text("data")
        with self.assertRaises(DeployError):
            generate.generate_bundle(self.cfg, f)
        self.assertEqual(f.read_text(), "data")
        real = self.tmp / "real"
        real.mkdir()
        (real / "keep").write_text("k")
        link = self.tmp / "link"
        link.symlink_to(real)
        with self.assertRaises(DeployError):
            generate.generate_bundle(self.cfg, link)
        self.assertEqual((real / "keep").read_text(), "k")

    def test_the_cli_reports_it_with_exit_2_and_deletes_nothing(self):
        d = self.tmp / "Docs"
        d.mkdir()
        (d / "mine.txt").write_text("m")
        out, err = StringIO(), StringIO()
        with mock.patch.dict(os.environ, {"GISSTACK_HOME": str(self.tmp / "home")}), redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["generate", "--out", str(d)])
        self.assertEqual(code, 2, err.getvalue())
        self.assertIn("not a gis-stack bundle", err.getvalue())
        self.assertEqual((d / "mine.txt").read_text(), "m")


if __name__ == "__main__":
    unittest.main()
