"""Encryption of downloaded backup copies (backup_crypto + backup.download/upload/offsite + CLI), against the REAL gpg."""
import io
import os
import shutil
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
from test_backup_manager import Base  # noqa: E402

from gisstack import backup, backup_crypto, cli, plugins  # noqa: E402
from gisstack.backup_crypto import Spec  # noqa: E402
from gisstack.core.errors import DeployError  # noqa: E402

HAVE_GPG = shutil.which("gpg") is not None
PW = "correct horse battery staple"


class GpgHome(unittest.TestCase):
    """A throw-away GNUPGHOME (short path: gpg-agent sockets have a length limit)."""

    def setUp(self):
        self.gh = tempfile.mkdtemp(prefix="gn", dir="/tmp")
        os.chmod(self.gh, 0o700)
        p = mock.patch.dict(os.environ, {"GNUPGHOME": self.gh})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._kill_agent)
        self.addCleanup(shutil.rmtree, self.gh, True)
        self.td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.td, True)

    def _kill_agent(self):
        import subprocess
        subprocess.run(["gpgconf", "--kill", "all"], capture_output=True, env={**os.environ, "GNUPGHOME": self.gh})

    def make_key(self, uid="backup-test@example.org"):
        import subprocess
        subprocess.run(["gpg", "--batch", "--pinentry-mode", "loopback", "--passphrase", "", "--quick-generate-key", uid, "default", "default", "never"],
                       check=True, capture_output=True)
        return uid


class PureTests(unittest.TestCase):
    def test_spec_rules(self):
        Spec(passphrase=PW).validate()
        Spec(recipients=["backup@example.org"]).validate()
        Spec(recipients=["0xDEADBEEF", "ABCD1234ABCD1234ABCD1234ABCD1234ABCD1234"]).validate()
        for bad in (Spec(), Spec(passphrase=PW, recipients=["a@b.c"]), Spec(passphrase="short"), Spec(recipients=["--output=/etc/passwd"]),
                    Spec(recipients=["-r"]), Spec(recipients=["a\nb@c.d"]), Spec(recipients=[""])):
            with self.assertRaises(DeployError, msg=bad):
                bad.validate()

    def test_passphrase_sources(self):
        td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, td, True)
        f = td / "pw"
        f.write_text(PW + "\nsecond line ignored\n")
        f.chmod(0o600)
        self.assertEqual(backup_crypto.read_passphrase(str(f)), PW)
        f.chmod(0o644)
        with self.assertRaises(DeployError):
            backup_crypto.read_passphrase(str(f))                           # a readable-by-others passphrase file is refused
        with self.assertRaises(DeployError):
            backup_crypto.read_passphrase(str(td / "missing"))
        with mock.patch.dict(os.environ, {backup_crypto.ENV_PASSPHRASE: "from-the-environment"}):
            self.assertEqual(backup_crypto.read_passphrase(), "from-the-environment")
        with mock.patch.dict(os.environ, {backup_crypto.ENV_PASSPHRASE: ""}):
            self.assertIsNone(backup_crypto.read_passphrase())
            with mock.patch("sys.stdin.isatty", return_value=True):
                answers = iter(["one", "two"])
                with self.assertRaises(DeployError):
                    backup_crypto.read_passphrase(prompt=True, confirm=True, ask=lambda _: next(answers))   # mismatch
                self.assertEqual(backup_crypto.read_passphrase(prompt=True, confirm=True, ask=lambda _: "same"), "same")
            with mock.patch("sys.stdin.isatty", return_value=False), self.assertRaises(DeployError):
                backup_crypto.read_passphrase(prompt=True)                  # no terminal: never hang waiting for input


@unittest.skipUnless(HAVE_GPG, "gpg needed")
class RoundTripTests(GpgHome):
    def plain(self, data=b"SECRET-MARKER credentials.txt contents\n" * 50):
        p = self.td / "backup.tar.gz"
        p.write_bytes(data)
        return p

    def test_passphrase_roundtrip_hides_content_and_removes_plaintext(self):
        src = self.plain()
        enc = backup_crypto.encrypt(src, self.td / "backup.tar.gz.gpg", Spec(passphrase=PW))
        self.assertFalse(src.exists())
        self.assertNotIn(b"SECRET-MARKER", enc.read_bytes())
        self.assertEqual(stat.S_IMODE(enc.stat().st_mode), 0o600)
        backup_crypto.check_packets(enc)
        out = backup_crypto.decrypt(enc, self.td / "back.tar.gz", PW)
        self.assertIn(b"SECRET-MARKER", out.read_bytes())
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)

    def test_wrong_passphrase_tampering_and_overwrite_are_refused(self):
        enc = backup_crypto.encrypt(self.plain(), self.td / "x.gpg", Spec(passphrase=PW))
        with self.assertRaises(DeployError):
            backup_crypto.decrypt(enc, self.td / "w.tar.gz", "a different passphrase")
        self.assertFalse((self.td / "w.tar.gz").exists() or (self.td / "w.tar.gz.partial").exists())     # nothing half-written is left
        data = bytearray(enc.read_bytes())
        data[len(data) // 2] ^= 0xFF
        bad = self.td / "bad.gpg"
        bad.write_bytes(bytes(data))
        with self.assertRaises(DeployError):
            backup_crypto.decrypt(bad, self.td / "t.tar.gz", PW)           # integrity protection: a modified file does not decrypt
        with self.assertRaises(DeployError):
            backup_crypto.encrypt(self.plain(), enc, Spec(passphrase=PW))   # never overwrites
        done = backup_crypto.decrypt(enc, self.td / "back.tar.gz", PW)
        with self.assertRaises(DeployError):
            backup_crypto.decrypt(enc, done, PW)                            # decrypting never overwrites either

    def test_passphrase_never_appears_in_arguments(self):
        seen = []
        real = backup_crypto.subprocess.run

        def spy(cmd, **kw):
            seen.append(list(cmd))
            return real(cmd, **kw)
        with mock.patch.object(backup_crypto.subprocess, "run", spy):
            backup_crypto.encrypt(self.plain(), self.td / "p.gpg", Spec(passphrase=PW))
        self.assertTrue(seen)
        self.assertFalse([c for c in seen if any(PW in a for a in c)])

    def test_public_key_roundtrip(self):
        uid = self.make_key()
        src = self.plain()
        enc = backup_crypto.encrypt(src, self.td / "k.gpg", Spec(recipients=[uid]))
        backup_crypto.check_packets(enc, [uid])
        self.assertNotIn(b"SECRET-MARKER", enc.read_bytes())
        out = backup_crypto.decrypt(enc, self.td / "k.tar.gz")             # no passphrase: gpg uses the private key
        self.assertIn(b"SECRET-MARKER", out.read_bytes())
        with self.assertRaises(DeployError):
            backup_crypto.encrypt(self.plain(), self.td / "u.gpg", Spec(recipients=["nobody-has-this-key@example.org"]))
        self.assertFalse((self.td / "u.gpg").exists() or (self.td / "u.gpg.partial").exists())

    def test_check_packets_rejects_non_encrypted_files(self):
        with self.assertRaises(DeployError):
            backup_crypto.check_packets(self.plain())


@unittest.skipUnless(HAVE_GPG, "gpg needed")
class BackupFlowTests(GpgHome, Base):
    def setUp(self):
        GpgHome.setUp(self)
        Base.setUp(self)
        self.dest = self.td / "copies"

    def test_download_encrypted_leaves_only_the_encrypted_copy(self):
        bid, _ = self.create()
        enc = backup.download(self.cfg, "local", None, bid, str(self.dest), Spec(passphrase=PW))
        self.assertTrue(str(enc).endswith(f"{bid}.tar.gz.gpg"))
        leftovers = [p for p in self.dest.rglob("*") if p.is_file() and not p.name.endswith(".gpg")]
        self.assertEqual(leftovers, [], "plaintext or staging files were left behind")
        self.assertFalse(list(self.dest.rglob("gisstack-enc-*")))
        with backup_crypto.decrypted(enc, PW) as plain:
            self.assertGreater(backup.verify_archive(plain), 0)
        with self.assertRaises(DeployError):
            backup.download(self.cfg, "local", None, bid, str(self.dest), Spec(passphrase=PW))      # never overwrites

    def test_download_without_encryption_is_unchanged(self):
        bid, _ = self.create()
        out = backup.download(self.cfg, "local", None, bid, str(self.dest))
        self.assertTrue(str(out).endswith(f"{bid}.tar.gz"))
        self.assertGreater(backup.verify_archive(out), 0)

    def test_a_failed_encryption_leaves_nothing_behind(self):
        bid, _ = self.create()
        with mock.patch.object(backup_crypto, "encrypt", side_effect=DeployError("boom")), self.assertRaises(DeployError):
            backup.download(self.cfg, "local", None, bid, str(self.dest), Spec(passphrase=PW))
        self.assertEqual([p for p in self.dest.rglob("*") if p.is_file()], [])

    def test_missing_gpg_fails_before_anything_is_downloaded(self):
        bid, _ = self.create()
        with mock.patch.object(backup_crypto.shutil, "which", return_value=None), mock.patch.object(backup, "verify_archive") as v, self.assertRaises(DeployError):
            backup.download(self.cfg, "local", None, bid, str(self.dest), Spec(passphrase=PW))
        v.assert_not_called()

    def test_public_key_download(self):
        uid = self.make_key()
        bid, _ = self.create()
        enc = backup.download(self.cfg, "local", None, bid, str(self.dest), Spec(recipients=[uid]))
        out = backup_crypto.decrypt(enc, self.td / "plain.tar.gz")
        self.assertGreater(backup.verify_archive(out), 0)

    def test_upload_decrypts_locally_and_wrong_passphrase_uploads_nothing(self):
        bid, _ = self.create()
        enc = backup.download(self.cfg, "local", None, bid, str(self.dest), Spec(passphrase=PW))
        shutil.rmtree(self.box.root / bid)                                # as if the server were new
        with self.assertRaises(DeployError):
            backup.upload(self.cfg, "local", None, enc, "not the passphrase")
        self.assertFalse((self.box.root / bid).exists())
        self.assertEqual(backup.upload(self.cfg, "local", None, enc, PW), bid)
        self.assertTrue((self.box.root / bid / "MANIFEST.json").exists())
        self.assertFalse(list(Path(tempfile.gettempdir()).glob("gisstack-dec-*")), "decrypted temporary directory was not removed")

    def test_offsite_hands_plugins_the_encrypted_file(self):
        bid, _ = self.create()
        got = []

        class Plug:
            name = "fake"

            def store(self, archive, backup_id):
                got.append((Path(archive).name, Path(archive).read_bytes()[:2]))
                return "ok"
        with mock.patch.object(plugins, "get", return_value=[Plug()]):
            lines = list(backup.offsite(self.cfg, "local", None, bid, Spec(passphrase=PW)))
        self.assertEqual(got[0][0], f"{bid}.tar.gz.gpg")
        self.assertNotEqual(got[0][1], b"\x1f\x8b")                       # not a gzip: it is the encrypted file
        self.assertTrue(any(bid in x for x in lines))


@unittest.skipUnless(HAVE_GPG, "gpg needed")
class CliTests(GpgHome, Base):
    def setUp(self):
        GpgHome.setUp(self)
        Base.setUp(self)
        self.pwf = self.td / "pw"
        self.pwf.write_text(PW + "\n")
        self.pwf.chmod(0o600)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_download_encrypt_decrypt_upload(self):
        bid, _ = self.create()
        cfgf = self.td / "c.json"
        cfgf.write_text('{"targets": ["native"], "deploy_target": "native", "project": "bk"}')
        dest = self.td / "dl"
        rc, out, err = self.run_cli("backup", "download", bid, "--mode", "local", "-c", str(cfgf), "--dest", str(dest), "--passphrase-file", str(self.pwf))
        self.assertEqual(rc, 0, err)
        enc = Path(out.strip().splitlines()[-1])
        self.assertTrue(enc.name.endswith(".tar.gz.gpg") and enc.is_file())
        rc, out, err = self.run_cli("backup", "decrypt", str(enc), "--passphrase-file", str(self.pwf))
        self.assertEqual(rc, 0, err)
        self.assertTrue(enc.with_suffix("").is_file())
        self.assertIn("SHA-256", out)
        rc, _, err = self.run_cli("backup", "decrypt", str(enc), "--passphrase-file", str(self.pwf))
        self.assertEqual(rc, 1)                                           # will not overwrite
        wrong = self.td / "wrong"
        wrong.write_text("some other passphrase\n")
        wrong.chmod(0o600)
        rc, _, err = self.run_cli("backup", "decrypt", str(enc), "-o", str(self.td / "x.tar.gz"), "--passphrase-file", str(wrong))
        self.assertEqual(rc, 1)
        shutil.rmtree(self.box.root / bid)
        rc, out, err = self.run_cli("backup", "upload", str(enc), "--mode", "local", "-c", str(cfgf), "--passphrase-file", str(self.pwf))
        self.assertEqual((rc, f"UPLOAD_OK={bid}" in out), (0, True), err)

    def test_both_encryption_modes_together_are_refused(self):
        bid, _ = self.create()
        cfgf = self.td / "c.json"
        cfgf.write_text('{"targets": ["native"], "deploy_target": "native", "project": "bk"}')
        with self.assertRaises(DeployError):
            cli._encrypt_spec(type("A", (), {"encrypt": True, "encrypt_to": ["a@b.c"], "passphrase_file": None})())


if __name__ == "__main__":
    unittest.main()
