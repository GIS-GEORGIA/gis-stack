"""Stack backup and restore with `--mode ssh` against a REAL sshd and a REAL Compose stack.  Opt-in: GISSTACK_TEST_INTEGRATION=1 (+ an `sshd` binary).

The "server" is a throw-away sshd on 127.0.0.1 (own host key, key-only login as the current user, who runs the Docker stack); gis-stack reaches the stack only
through it.  Asserted: an unknown host is refused BEFORE anything runs (strict, no silent trust); explicit trust by fingerprint works (a wrong one is refused);
backup and restore over ssh work and are verified; a CHANGED host key is refused (nothing runs, nothing is overwritten); a connection killed mid-transfer
leaves no valid backup and no half-written entry that could be mistaken for one, and no dump process keeps running on the server; a tampered backup is
refused at verify and at restore; backup files stay owner-only on the machine that runs gis-stack."""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from gisstack import sshtrust, transport
from gisstack.backups import provider
from gisstack.backups.model import BackupError
from gisstack.core.errors import DeployError, HostKeyChanged, HostKeyUnknown

from .test_stack_backup_compose import PG_IMAGE, StackBase, pulled

SSHD = shutil.which("sshd") or "/usr/sbin/sshd"
USER = os.environ.get("USER") or __import__("getpass").getuser()


@unittest.skipUnless(pulled(PG_IMAGE) and os.path.exists(SSHD) and shutil.which("ssh-keygen"), "GISSTACK_TEST_INTEGRATION=1, docker, the PostGIS image and sshd needed")
class SshModeBackup(StackBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.sdir = Path(tempfile.mkdtemp())
        cls.client_key = cls.sdir / "client"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(cls.client_key)], check=True)
        cls.procs: list[subprocess.Popen] = []
        cls.logs: list = []
        cls.port = cls.start_sshd()
        cls.ssh = {"host": "127.0.0.1", "port": cls.port, "user": USER, "key": str(cls.client_key)}

    @classmethod
    def tearDownClass(cls):
        for p in cls.procs:
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
        for f in cls.logs:
            f.close()
        shutil.rmtree(cls.sdir, ignore_errors=True)
        super().tearDownClass()

    @classmethod
    def start_sshd(cls, port=None, regen=False):
        hk = cls.sdir / "host_ed25519"
        if regen:
            hk.unlink(missing_ok=True)
            (cls.sdir / "host_ed25519.pub").unlink(missing_ok=True)
        if not hk.exists():
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(hk)], check=True)
        auth = cls.sdir / "authorized_keys"
        auth.write_text(cls.client_key.with_suffix(".pub").read_text())
        auth.chmod(0o600)
        if port is None:
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
        cfg = cls.sdir / "sshd_config"
        cfg.write_text(f"Port {port}\nListenAddress 127.0.0.1\nHostKey {hk}\nAuthorizedKeysFile {auth}\nPidFile {cls.sdir}/pid\nPasswordAuthentication no\n"
                       "KbdInteractiveAuthentication no\nUsePAM no\nStrictModes no\nPubkeyAuthentication yes\nMaxStartups 100:30:200\nLoginGraceTime 30\n")
        if "persourcepenalties" in subprocess.run([SSHD, "-T", "-f", str(cfg)], capture_output=True, text=True).stdout.lower():
            cfg.write_text(cfg.read_text() + "PerSourcePenalties no\n")
        log = open(cls.sdir / "sshd.log", "ab")  # noqa: SIM115 - lives as long as the process; closed in tearDownClass
        cls.logs.append(log)
        p = subprocess.Popen([SSHD, "-D", "-e", "-f", str(cfg)], stdout=subprocess.DEVNULL, stderr=log)
        cls.procs.append(p)
        for _ in range(60):
            if p.poll() is not None:
                raise unittest.SkipTest("sshd cannot run unprivileged here: " + (cls.sdir / "sshd.log").read_text(errors="replace")[-300:])
            with socket.socket() as s:
                s.settimeout(0.2)
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    return port
            time.sleep(0.1)
        raise unittest.SkipTest("sshd did not start")

    def fp(self):
        return subprocess.run(["ssh-keygen", "-lf", str(self.sdir / "host_ed25519.pub")], capture_output=True, text=True, check=True).stdout.split()[1]

    def prov(self, **kw):
        return provider.ComposeBackupProvider(self.cfg, "ssh", self.ssh, **kw)

    def local(self):
        return provider.ComposeBackupProvider(self.cfg)

    def test_1_to_6_the_ssh_mode_trust_policy_backup_restore_and_failure_handling(self):
        # a clean ssh environment: own known_hosts under GISSTACK_HOME, strict policy, no silent trust
        with mock.patch.dict(os.environ, {"GISSTACK_SSH_STRICT": "1"}):
            os.environ.pop("GISSTACK_SSH_TOFU", None)
            self.local().psql("drop table if exists sshbk; create table sshbk(v text); insert into sshbk values ('one')", db=self.cfg["postgres"]["db"])

            # --- unknown host: refused before anything runs
            before = {r["id"] for r in self.local().list_backups()}
            with self.assertRaises(HostKeyUnknown):
                list(self.prov().create(["postgres"]))
            self.assertEqual(self.new_valid(before), [], "an unknown host must not produce a valid backup")      # a FAILED entry is recorded (it raises the backup_failed alert)

            # --- explicit trust: a wrong fingerprint is refused, the right one works
            with self.assertRaises(DeployError):
                sshtrust.trust("127.0.0.1", self.port, expected="SHA256:" + "A" * 43)
            with self.assertRaises(HostKeyUnknown):
                list(self.prov().create(["postgres"]))                                           # still untrusted after the refused attempt
            sshtrust.trust("127.0.0.1", self.port, expected=self.fp())

            # --- backup and restore over ssh
            p = self.prov()
            lines = list(p.create(["postgres"], note="over ssh"))
            self.assertTrue(any("sealed" in x for x in lines), lines)
            bid = p.result["id"]
            self.assertTrue(p.verify(bid)["ok"])
            d = p.store.path(bid)
            self.assertEqual(oct(d.stat().st_mode & 0o777), "0o700")
            for f in d.iterdir():
                self.assertEqual(oct(f.stat().st_mode & 0o777), "0o600", f.name)                 # owner-only on the machine that runs gis-stack
            self.assertNotIn(self.cfg["postgres"]["password"].encode(), b"".join(f.read_bytes() for f in d.iterdir() if f.suffix in ("", ".json", ".log")))
            self.local().psql("update sshbk set v = 'two'", db=self.cfg["postgres"]["db"])
            r = self.prov()
            lines = list(r.restore(bid, ["postgres"], confirm=True))
            self.assertTrue(any("verified healthy" in x for x in lines), lines)
            self.assertEqual(self.local().psql("select v from sshbk", db=self.cfg["postgres"]["db"]), "one")

            # --- tampered backup: refused at verify and at restore
            tar = d / "postgres.dump"
            data = bytearray(tar.read_bytes())
            data[len(data) // 2] ^= 0xFF
            tar.chmod(0o600)
            tar.write_bytes(bytes(data))
            self.assertFalse(self.prov().verify(bid)["ok"])
            with self.assertRaises(Exception):
                list(self.prov().restore(bid, ["postgres"], confirm=True))
            self.assertEqual(self.local().psql("select v from sshbk", db=self.cfg["postgres"]["db"]), "one")      # nothing changed

            # --- interrupted transfer: kill the ssh client while the dump is streaming
            self.local().psql("create table if not exists bigt as select md5(random()::text) a, md5(random()::text) b, md5(random()::text) c from generate_series(1, 2500000)",
                              db=self.cfg["postgres"]["db"], timeout=600)
            known = {x["id"] for x in self.local().list_backups()}
            killed = threading.Event()

            def killer():
                for _ in range(600):
                    parts = list(self.local().store.root.glob("*.partial/postgres.dump"))
                    if parts and parts[0].stat().st_size > 1_000_000:
                        subprocess.run(["pkill", "-9", "-f", f"ssh .*-p {self.port}"], capture_output=True)
                        killed.set()
                        return
                    time.sleep(0.05)
            t = threading.Thread(target=killer, daemon=True)
            t.start()
            with self.assertRaises((BackupError, DeployError)):
                list(self.prov().create(["postgres"], note="will be interrupted"))
            t.join(30)
            self.assertTrue(killed.is_set(), "the transfer finished before it could be interrupted; the table must be bigger")
            now = self.local().list_backups()
            self.assertEqual([x["id"] for x in now if x["status"] == "ok" and x["id"] not in known], [], "an interrupted backup must never look valid")
            self.assertFalse(list(self.local().store.root.glob("*.partial")), "no .partial directory may remain")
            time.sleep(5)
            left = subprocess.run(["docker", "exec", self.ctr("postgis"), "sh", "-c", "pgrep -x pg_dump | wc -l"], capture_output=True, text=True).stdout.strip()
            self.assertEqual(left, "0", "a dump process kept running on the server after the connection died")
            self.local().psql("drop table bigt", db=self.cfg["postgres"]["db"])

            # --- changed host key: refused, nothing runs
            self.procs[-1].terminate()
            self.procs[-1].wait(5)
            type(self).start_sshd(port=self.port, regen=True)                                    # same address, new key
            before = {x["id"] for x in self.local().list_backups()}
            with self.assertRaises(HostKeyChanged):
                list(self.prov().create(["postgres"]))
            self.assertEqual(self.new_valid(before), [])
            self.assertEqual(self.local().psql("select v from sshbk", db=self.cfg["postgres"]["db"]), "one")
            self.assertTrue(transport.adhoc_policy() == "strict")

    def new_valid(self, before):
        return [x["id"] for x in self.local().list_backups() if x["id"] not in before and x["status"] == "ok"]

    def ctr(self, service):
        return subprocess.run(["docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={self.project}", "--filter", f"label=com.docker.compose.service={service}"],
                              capture_output=True, text=True).stdout.split()[0]


if __name__ == "__main__":
    unittest.main()
