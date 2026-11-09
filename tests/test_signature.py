"""Release signature verification in the updater (Phase 12).  cosign itself is replaced by a fake runner: what is tested is OUR policy
(which command is run, what counts as verified, when we refuse); the real signing/verification runs in the release workflow."""
from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from gisstack import updater
from gisstack.updater import UpdateError

BASE = updater.ASSET_PREFIX + "v9.9.9/"
SDIST = b"fake sdist bytes"
SUMS = f"{hashlib.sha256(SDIST).hexdigest()}  gisstack_installer-9.9.9.tar.gz\n".encode()


class _Resp:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def read(self, n: int = -1) -> bytes:
        d, self.data = (self.data, b"") if n < 0 else (self.data[:n], self.data[n:])
        return d

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener(sdist: bytes = SDIST):
    def op(req, timeout=0):
        u = req.full_url
        return _Resp(SUMS if u.endswith("SHA256SUMS") else b'{"bundle": 1}' if u.endswith(".sigstore.json") else sdist)
    return op


ASSET = {"name": "gisstack_installer-9.9.9.tar.gz", "url": BASE + "gisstack_installer-9.9.9.tar.gz"}
SUMS_A = {"name": "SHA256SUMS", "url": BASE + "SHA256SUMS"}
SIG_A = {"name": updater.SIG_NAME, "url": BASE + updater.SIG_NAME}


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        p = mock.patch.dict(os.environ, {}, clear=False)
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("GISSTACK_REQUIRE_SIGNATURE", None)

    def run_ok(self, code=0, err=""):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            return SimpleNamespace(returncode=code, stdout="", stderr=err)
        return run, calls

    def test_verified_uses_the_release_workflow_identity_and_cleans_up(self):
        run, calls = self.run_ok()
        st = updater.verify_signature(SUMS, b"{}", self.td, run=run, which=lambda n: "/usr/bin/cosign")
        self.assertEqual(st, "verified")
        cmd = calls[0]
        self.assertEqual(cmd[:2], ["/usr/bin/cosign", "verify-blob"])
        self.assertIn(updater.SIGNER_IDENTITY, cmd)
        self.assertIn(updater.OIDC_ISSUER, cmd)
        self.assertNotIn("--insecure-ignore-tlog", cmd)
        self.assertEqual(list(Path(self.td).iterdir()), [])                 # temporary files are gone

    def test_identity_pattern_accepts_only_our_release_workflow(self):
        import re
        ok = "https://github.com/GIS-GEORGIA/gis-stack/.github/workflows/release.yml@refs/tags/v0.1.5"
        self.assertTrue(re.match(updater.SIGNER_IDENTITY, ok))
        for bad in ("https://github.com/evil/gis-stack/.github/workflows/release.yml@refs/tags/v0.1.5",
                    "https://github.com/GIS-GEORGIA/gis-stack/.github/workflows/tests.yml@refs/tags/v0.1.5",
                    "https://github.com/GIS-GEORGIA/gis-stack/.github/workflows/release.yml@refs/heads/main",
                    ok + "x", "x" + ok):
            self.assertIsNone(re.match(updater.SIGNER_IDENTITY, bad), bad)

    def test_a_bad_signature_always_refuses(self):
        run, _ = self.run_ok(1, "error: none of the expected identities matched")
        with self.assertRaises(UpdateError) as cm:
            updater.verify_signature(SUMS, b"{}", self.td, run=run, which=lambda n: "/x/cosign")
        self.assertIn("identities", str(cm.exception))
        self.assertEqual(list(Path(self.td).iterdir()), [])

    def test_missing_pieces_warn_by_default_and_refuse_when_required(self):
        run, calls = self.run_ok()
        self.assertEqual(updater.verify_signature(SUMS, None, self.td, run=run), "unsigned")
        self.assertEqual(updater.verify_signature(SUMS, b"{}", self.td, run=run, which=lambda n: None), "no-cosign")
        self.assertEqual(calls, [])
        os.environ["GISSTACK_REQUIRE_SIGNATURE"] = "1"
        with self.assertRaises(UpdateError):
            updater.verify_signature(SUMS, None, self.td, run=run)
        with self.assertRaises(UpdateError):
            updater.verify_signature(SUMS, b"{}", self.td, run=run, which=lambda n: None)

    def test_cosign_failing_to_start_is_an_update_error(self):
        def boom(cmd, **kw):
            raise OSError("exec format error")
        with self.assertRaises(UpdateError):
            updater.verify_signature(SUMS, b"{}", self.td, run=boom, which=lambda n: "/x/cosign")


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        os.environ.pop("GISSTACK_REQUIRE_SIGNATURE", None)

    def test_signed_download_reports_verified_and_still_checks_the_hash(self):
        with mock.patch.object(updater, "verify_signature", return_value="verified") as v:
            path, st = updater.download_verified(ASSET, SUMS_A, self.td, opener(), SIG_A)
            self.assertEqual((st, path.read_bytes()), ("verified", SDIST))
            self.assertEqual(v.call_args[0][0], SUMS)                         # the signature covers exactly the SHA256SUMS that was used
            with self.assertRaises(UpdateError):
                updater.download_verified(ASSET, SUMS_A, self.td, opener(b"tampered"), SIG_A)
            self.assertFalse((Path(self.td) / ASSET["name"]).exists())

    def test_nothing_is_downloaded_when_the_signature_fails(self):
        def bad(*a, **k):
            raise UpdateError("bad signature")
        with mock.patch.object(updater, "verify_signature", bad):
            with self.assertRaises(UpdateError):
                updater.download_verified(ASSET, SUMS_A, self.td, opener(), SIG_A)
        self.assertFalse((Path(self.td) / ASSET["name"]).exists())

    def test_unsigned_release_still_downloads_with_a_warning_status(self):
        path, st = updater.download_verified(ASSET, SUMS_A, self.td, opener(), None)
        self.assertEqual(st, "unsigned")
        self.assertEqual(path.read_bytes(), SDIST)
        self.assertIn("SHA-256", updater.signature_line("unsigned"))

    def test_foreign_signature_url_is_refused(self):
        with self.assertRaises(UpdateError):
            updater.download_verified(ASSET, SUMS_A, self.td, opener(), {"name": updater.SIG_NAME, "url": "https://evil.example/x.json"})

    def test_check_exposes_the_signature_asset(self):
        data = {"tag_name": "v9.9.9", "name": "x", "body": "", "published_at": "2026-12-01T10:00:00Z", "html_url": "https://github.com/GIS-GEORGIA/gis-stack/releases/tag/v9.9.9",
                "assets": [{"name": "gisstack_installer-9.9.9.tar.gz", "browser_download_url": ASSET["url"], "size": 1},
                           {"name": "SHA256SUMS", "browser_download_url": SUMS_A["url"], "size": 1},
                           {"name": updater.SIG_NAME, "browser_download_url": SIG_A["url"], "size": 1}]}
        import json

        def op(req, timeout=0):
            return _Resp(json.dumps(data).encode())
        info = updater.check(op)
        self.assertEqual(info["signature"]["name"], updater.SIG_NAME)


if __name__ == "__main__":
    unittest.main()
