"""A download that times out, is interrupted or fails must not leave a half-written archive under the name of a backup (audit 2026-11: `existing.fetch_backup`
kept the partial `<ts>.tar.gz` after a timeout, where it looked like a backup - and `O_EXCL` then refused the retry)."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gisstack import backup, existing, i18n
from gisstack.core.errors import DeployError


def writes_then(exc):
    def fake(argv, stdin=None, stdout=None, stderr=None, timeout=None, **kw):
        stdout.write(b"\x1f\x8b partial archive bytes")
        stdout.flush()
        raise exc
    return fake


class PartialDownloads(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)

    def files(self, d):
        return sorted(p.name for p in Path(d).rglob("*") if p.is_file())

    def test_fetch_backup_timeout_leaves_nothing_and_is_a_clean_error(self):
        with mock.patch("gisstack.existing.subprocess.run", writes_then(subprocess.TimeoutExpired(["ssh"], 900))), \
                mock.patch("gisstack.existing.transport.argv", return_value=["x"]):
            with self.assertRaises(DeployError) as cm:
                existing.fetch_backup("local", None, "20260101-000000", self.tmp, "host")
        self.assertEqual(self.files(self.tmp), [])
        self.assertIn("Backup download failed: timed out", i18n.tr(str(cm.exception), "en"))
        self.assertEqual(i18n.untranslated(str(cm.exception)), "")

    def test_fetch_backup_interrupt_leaves_nothing_and_the_retry_is_not_blocked(self):
        with mock.patch("gisstack.existing.subprocess.run", writes_then(KeyboardInterrupt())), \
                mock.patch("gisstack.existing.transport.argv", return_value=["x"]):
            with self.assertRaises(KeyboardInterrupt):
                existing.fetch_backup("local", None, "20260101-000000", self.tmp, "host")
        self.assertEqual(self.files(self.tmp), [])
        ok = subprocess.CompletedProcess(["x"], 0, b"", b"")

        def fine(argv, stdin=None, stdout=None, stderr=None, timeout=None, **kw):
            stdout.write(b"data")
            return ok
        with mock.patch("gisstack.existing.subprocess.run", fine), mock.patch("gisstack.existing.transport.argv", return_value=["x"]):
            out = existing.fetch_backup("local", None, "20260101-000000", self.tmp, "host")        # the same name works again
        self.assertEqual(out.read_bytes(), b"data")

    def test_backup_download_timeout_is_a_clean_error_without_a_leftover(self):
        with mock.patch("gisstack.backup.subprocess.run", writes_then(subprocess.TimeoutExpired(["ssh"], 3600))), \
                mock.patch("gisstack.backup.transport.argv", return_value=["x"]):
            with self.assertRaises(DeployError) as cm:
                backup.download({"project": "p"}, "local", None, "20260101-000000-abcd", str(self.tmp))
        self.assertEqual(self.files(self.tmp), [])
        self.assertEqual(i18n.untranslated(str(cm.exception)), "")


if __name__ == "__main__":
    unittest.main()
