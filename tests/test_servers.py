"""Multi-server management (Phase 9): profiles, pinned host keys, per-server credentials, health / comparison / history.

The ssh tests start a REAL unprivileged sshd on 127.0.0.1 (own host key, own client key) when sshd/ssh/ssh-keygen exist, so pinning, strict
host key checking and a changed host key are exercised against OpenSSH itself, not against mocks."""
from __future__ import annotations

import base64
import getpass
import json
import os
import shutil
import socket
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

try:
    import flask  # noqa: F401
    HAVE_FLASK = True
except ImportError:
    HAVE_FLASK = False

from gisstack import journal, transport
from gisstack.core.errors import DeployError, HostKeyChanged, HostKeyUnknown
from gisstack.servers import hostkey, service, store

SSHD = shutil.which("sshd") or ("/usr/sbin/sshd" if os.path.exists("/usr/sbin/sshd") else None)
HAVE_SSH = bool(SSHD and shutil.which("ssh") and shutil.which("ssh-keygen") and shutil.which("ssh-keyscan"))
USER = getpass.getuser()                      # from the password database: $USER is not set on every CI runner


class Home(unittest.TestCase):
    FEATURES = "multi_server"

    def setUp(self):
        self.td = tempfile.mkdtemp()
        p = mock.patch.dict(os.environ, {"GISSTACK_HOME": self.td, "GISSTACK_FEATURES": self.FEATURES})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.td, True)


class StoreTests(Home):
    P = {"id": "prod1", "name": "Production", "env": "production", "host": "gis.example.org", "port": 2222, "user": "deploy", "key": "/home/u/.ssh/id_ed25519"}

    def test_save_get_list_delete(self):
        s = store.save(self.P)
        self.assertEqual((s["id"], s["env"], s["port"], s["pinned"]), ("prod1", "production", 2222, False))
        store.save({**self.P, "id": "dev1", "env": "development", "host": "dev.example.org"})
        self.assertEqual([x["id"] for x in store.list_servers()], ["prod1", "dev1"])       # production first
        store.delete("prod1")
        with self.assertRaises(store.ServerNotFound):
            store.get("prod1")

    def test_validation(self):
        for bad in ({"id": "Bad Id"}, {"env": "prod"}, {"host": "a b"}, {"host": ""}, {"user": "R00T!"}, {"port": 0}, {"port": "x"}):
            with self.assertRaises((store.ServerError, DeployError), msg=bad):
                store.save({**self.P, **bad})

    def test_key_material_is_refused_only_a_path_is_kept(self):
        for bad in ("-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----", "relative/key", "/k\nx", "/a/PRIVATE KEY"):
            with self.assertRaises(store.ServerError, msg=bad):
                store.save({**self.P, "key": bad})
        store.save({**self.P, "key": ""})                                   # empty = ssh-agent
        self.assertEqual(store.get("prod1")["key"], "")

    def test_files_are_owner_only_and_hold_no_key_material(self):
        store.save(self.P)
        store.set_pin("prod1", ["gis.example.org ssh-ed25519 " + "A" * 68], ["SHA256:" + "A" * 43])
        for f in (store.file(), store.known_hosts_file("prod1")):
            self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600, f)
            self.assertNotIn("PRIVATE KEY", f.read_text())

    def test_unpinned_server_is_refused_and_pin_enables_it(self):
        store.save(self.P)
        with self.assertRaises(store.ServerError):
            store.ssh_for("prod1")
        store.set_pin("prod1", ["[gis.example.org]:2222 ssh-ed25519 " + "A" * 68], ["SHA256:" + "A" * 43])
        ssh = store.ssh_for("prod1")
        self.assertEqual((ssh["host"], ssh["port"], ssh["user"], ssh["key"]), ("gis.example.org", 2222, "deploy", self.P["key"]))
        self.assertTrue(Path(ssh["known_hosts"]).is_file())

    def test_changing_the_address_drops_the_pin(self):
        store.save(self.P)
        store.set_pin("prod1", ["gis.example.org ssh-ed25519 " + "A" * 68], ["SHA256:" + "A" * 43])
        store.save({**self.P, "name": "renamed"})
        self.assertTrue(store.get("prod1")["pinned"])                       # same address: keeps its pin
        store.save({**self.P, "host": "other.example.org"})
        self.assertFalse(store.get("prod1")["pinned"])
        self.assertFalse(store.known_hosts_file("prod1").exists())

    def test_profiles_are_independent(self):
        store.save(self.P)
        store.save({**self.P, "id": "dev1", "env": "development", "host": "dev.example.org", "user": "dev", "key": "/k/dev"})
        for sid, h in (("prod1", "gis.example.org"), ("dev1", "dev.example.org")):
            store.set_pin(sid, [f"{h} ssh-ed25519 " + "A" * 68], ["SHA256:" + "A" * 43])
        a, b = store.ssh_for("prod1"), store.ssh_for("dev1")
        self.assertNotEqual((a["host"], a["user"], a["key"], a["known_hosts"]), (b["host"], b["user"], b["key"], b["known_hosts"]))

    def test_corrupt_file_is_an_error_not_a_crash(self):
        store.file().write_text("{nope")
        with self.assertRaises(store.ServerError):
            store.list_servers()


class TransportTests(Home):
    def test_pinned_profile_uses_strict_checking_and_only_its_own_file(self):
        kh = Path(self.td) / "kh"
        kh.write_text("x\n")
        cmd = transport.ssh_base({"host": "h.example", "user": "u", "known_hosts": str(kh)})
        self.assertIn("StrictHostKeyChecking=yes", cmd)
        self.assertIn(f"UserKnownHostsFile={kh}", cmd)
        self.assertIn("GlobalKnownHostsFile=/dev/null", cmd)
        self.assertNotIn("StrictHostKeyChecking=accept-new", cmd)

    def test_without_a_profile_an_unknown_host_is_refused_before_any_connection(self):
        """Default policy: strict.  Nothing is sent to a host whose key was never trusted; the error says how to establish trust."""
        with self.assertRaises(HostKeyUnknown) as cm:
            transport.ssh_base({"host": "h.example", "user": "u", "port": 2222})
        msg = str(cm.exception)
        self.assertIn("[h.example]:2222", msg)
        self.assertIn("gisstack ssh-trust h.example --port 2222", msg)
        self.assertIn("GISSTACK_SSH_TOFU=1", msg)

    def test_strict_options_and_own_known_hosts_file(self):
        own = Path(self.td) / "known_hosts_adhoc"
        pub = subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(Path(self.td) / "k")], capture_output=True)
        self.assertEqual(pub.returncode, 0)
        key = (Path(self.td) / "k.pub").read_text().split()
        own.write_text(f"h.example {key[0]} {key[1]}\n")
        cmd = transport.ssh_base({"host": "h.example", "user": "u"})                                # trusted now: allowed
        self.assertIn("StrictHostKeyChecking=yes", cmd)
        opt = [c for c in cmd if c.startswith("UserKnownHostsFile=")]
        self.assertEqual(len(opt), 1)
        self.assertTrue(opt[0].startswith(f"UserKnownHostsFile={own}"))
        self.assertNotIn("StrictHostKeyChecking=accept-new", cmd)

    def test_trust_on_first_use_is_an_explicit_opt_in_and_strict_wins(self):
        with mock.patch.dict(os.environ, {"GISSTACK_SSH_TOFU": "1"}):
            cmd = transport.ssh_base({"host": "h.example", "user": "u"})
            self.assertIn("StrictHostKeyChecking=accept-new", cmd)
            self.assertEqual(transport.adhoc_policy(), "tofu")
            with mock.patch.dict(os.environ, {"GISSTACK_SSH_STRICT": "1"}):
                self.assertEqual(transport.adhoc_policy(), "strict")
                with self.assertRaises(HostKeyUnknown):
                    transport.ssh_base({"host": "h.example", "user": "u"})
        self.assertEqual(transport.adhoc_policy(), "strict")                                        # the default

    def test_server_profiles_do_not_need_prior_trust_to_be_saved_or_validated(self):
        transport.ssh_base({"host": "h.example", "user": "u"}, check_trust=False)                   # syntax only

    def test_a_home_path_that_cannot_be_quoted_falls_back_to_ssh_defaults(self):
        with mock.patch.dict(os.environ, {"GISSTACK_HOME": str(Path(self.td) / "has space"), "GISSTACK_SSH_TOFU": "1"}):
            cmd = transport.ssh_base({"host": "h.example", "user": "u"})
        self.assertIn("StrictHostKeyChecking=accept-new", cmd)
        self.assertFalse([c for c in cmd if c.startswith("UserKnownHostsFile")])

    def test_ssh_trust_errors_are_translated_from_sshs_own_messages(self):
        changed = "@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@\nHost key verification failed."
        e = transport.diagnose(changed, {"host": "h.example", "port": 2222})
        self.assertIsInstance(e, HostKeyChanged)
        self.assertIn("CHANGED", str(e))
        self.assertIn("--replace", str(e))
        unknown = "No ED25519 host key is known for [h.example]:22 and you have requested strict checking.\nHost key verification failed."
        self.assertIsInstance(transport.diagnose(unknown, {"host": "h.example"}), HostKeyUnknown)
        self.assertIsNone(transport.diagnose("Permission denied (publickey).", {"host": "h.example"}))             # other failures keep ssh's own text

    def test_missing_known_hosts_file_fails_closed(self):
        with self.assertRaises(DeployError):
            transport.ssh_base({"host": "h.example", "user": "u", "known_hosts": "/nonexistent/kh"})


class HostKeyTests(Home):
    def test_fingerprint_matches_openssh_format(self):
        b64 = base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519" + b"\x01" * 36).decode()
        fp = hostkey.fingerprint(b64)
        self.assertRegex(fp, r"^SHA256:[A-Za-z0-9+/]{43}$")
        with self.assertRaises(store.ServerError):
            hostkey.fingerprint("not base64!!")

    def test_parse_builds_known_hosts_lines_for_the_exact_port(self):
        key = base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519" + b"\x02" * 36).decode()
        out = f"# comment\nh ssh-ed25519 {key}\nh garbage line\n"
        self.assertEqual(hostkey.parse(out, "h", 22)[0]["line"], f"h ssh-ed25519 {key}")
        self.assertEqual(hostkey.parse(out, "h", 2222)[0]["line"], f"[h]:2222 ssh-ed25519 {key}")
        self.assertEqual(hostkey.parse("<html>", "h", 22), [])


class ServiceTests(Home):
    def setUp(self):
        super().setUp()
        for sid, host in (("a", "a.example"), ("b", "b.example"), ("c", "c.example")):
            store.save({"id": sid, "host": host, "env": "testing"})
            store.set_pin(sid, [f"{host} ssh-ed25519 " + "A" * 68], ["SHA256:" + "A" * 43])
        self.out = {"a.example": "os=Ubuntu 24.04\nkernel=6.8\nver.postgres=psql 16.4\nsvc.caddy.service=active\nsha.mapproxy_yaml=" + "1" * 64 + "\nroot=yes\nevil key=1\n",
                    "b.example": "os=Ubuntu 24.04\nkernel=6.8\nver.postgres=psql 17.0\nsvc.caddy.service=active\nsha.mapproxy_yaml=" + "1" * 64 + "\nroot=yes\n"}

    def run_fake(self, mode, ssh, script, **kw):
        self.assertEqual(mode, "ssh")
        if ssh["host"] not in self.out:
            raise DeployError("Connection refused")
        return self.out[ssh["host"]]

    def test_parse_keeps_only_known_keys(self):
        f = service.parse(self.out["a.example"])
        self.assertEqual(f["os"], "Ubuntu 24.04")
        self.assertNotIn("evil key", f)

    def test_probe_reports_unreachable_instead_of_raising(self):
        r = service.probe("c", self.run_fake)
        self.assertFalse(r["reachable"])
        self.assertIn("refused", r["error"])
        self.assertTrue(service.probe("a", self.run_fake)["reachable"])

    def test_compare_marks_drift_and_incomplete_results(self):
        r = service.compare(["a", "b"], self.run_fake)
        rows = {x["fact"]: x for x in r["rows"]}
        self.assertTrue(rows["ver.postgres"]["differs"])
        self.assertFalse(rows["os"]["differs"])
        self.assertFalse(rows["sha.mapproxy_yaml"]["differs"])
        self.assertEqual((r["drift"], r["complete"]), (1, True))
        r = service.compare(["a", "b", "c"], self.run_fake)
        self.assertFalse(r["complete"])
        self.assertEqual([s["reachable"] for s in r["servers"]], [True, True, False])

    def test_compare_needs_two_distinct_servers(self):
        for ids in (["a"], ["a", "a"], list("abcdefg")):
            with self.assertRaises(store.ServerError):
                service.compare(ids, self.run_fake)

    def test_history_is_the_journal_of_that_host(self):
        with journal.Operation("deploy", host="a.example", project="p"):
            pass
        with journal.Operation("deploy", host="b.example", project="p"):
            pass
        rows = service.history("a")
        self.assertEqual([r["host"] for r in rows], ["a.example"])


@unittest.skipUnless(HAVE_SSH, "sshd / ssh-keygen / ssh-keyscan needed")
class RealSshTests(Home):
    """Two real sshd instances on 127.0.0.1 = two servers with independent host keys."""

    def setUp(self):
        super().setUp()
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.client_key = self.dir / "client"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.client_key)], check=True)
        self.procs: list[subprocess.Popen] = []
        self.addCleanup(self.stop_all)
        self.ports = [self.start_sshd(f"s{i}") for i in range(2)]

    def stop_all(self):
        for p in self.procs:
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()

    def start_sshd(self, name, regen=False):
        d = self.dir / name
        d.mkdir(exist_ok=True)
        hk = d / "host_ed25519"
        if regen and hk.exists():
            hk.unlink()
            (d / "host_ed25519.pub").unlink()
        if not hk.exists():
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(hk)], check=True)
        auth = d / "authorized_keys"
        auth.write_text((self.client_key.with_suffix(".pub")).read_text())
        auth.chmod(0o600)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        cfg = d / "sshd_config"
        cfg.write_text(f"Port {port}\nListenAddress 127.0.0.1\nHostKey {hk}\nAuthorizedKeysFile {auth}\nPidFile {d}/pid\nPasswordAuthentication no\n"
                       f"KbdInteractiveAuthentication no\nUsePAM no\nStrictModes no\nPubkeyAuthentication yes\nMaxStartups 100:30:200\nLoginGraceTime 30\n")
        # OpenSSH 9.8+ penalises a source that connects without authenticating (ssh-keyscan does exactly that) and then drops its next connections
        # for seconds ("Connection reset"): switch that off for this throw-away server; older sshd does not know the option.
        if "persourcepenalties" in subprocess.run([SSHD, "-T", "-f", str(cfg)], capture_output=True, text=True).stdout.lower():
            cfg.write_text(cfg.read_text() + "PerSourcePenalties no\n")
        log = open(d / "sshd.log", "wb")  # noqa: SIM115 - lives as long as the sshd process; closed by addCleanup
        self.addCleanup(log.close)
        p = subprocess.Popen([SSHD, "-D", "-e", "-f", str(cfg)], stdout=subprocess.DEVNULL, stderr=log)
        self.procs.append(p)
        for _ in range(60):
            if p.poll() is not None:
                self.skipTest("sshd cannot run unprivileged here: " + (d / "sshd.log").read_text(errors="replace")[-300:])
            with socket.socket() as s:
                s.settimeout(0.2)
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    return port
            time.sleep(0.1)
        self.skipTest("sshd did not start")

    def reachable(self, sid, tries=4):
        """probe() until it connects: right after another ssh/ssh-keyscan on a busy CI runner sshd may reset one pre-auth connection (seen once, 'Connection reset')."""
        for _ in range(tries):
            r = service.probe(sid)
            if r["reachable"]:
                return r
            time.sleep(0.5)
        logs = "\n".join(f"--- {f.parent.name}/sshd.log\n{f.read_text(errors='replace')[-600:]}" for f in sorted(self.dir.glob("*/sshd.log")))
        self.fail(f"{sid} not reachable after {tries} tries: {r}\n{logs}")

    def profile(self, sid, port, env="testing"):
        return store.save({"id": sid, "env": env, "host": "127.0.0.1", "port": port, "user": USER, "key": str(self.client_key)})

    def fp(self, name):
        out = subprocess.run(["ssh-keygen", "-lf", str(self.dir / name / "host_ed25519.pub")], capture_output=True, text=True, check=True).stdout.split()[1]
        return out

    def pubkey(self, n):
        return (self.dir / f"s{n}" / "host_ed25519.pub").read_text().split()[1]

    def adhoc(self, port):
        return {"host": "127.0.0.1", "port": port, "user": USER, "key": str(self.client_key)}

    def own(self):
        return Path(self.td) / "known_hosts_adhoc"

    def test_adhoc_unknown_host_is_refused_with_a_clear_message_and_nothing_runs(self):
        marker = Path(self.td) / "should-not-exist"
        with self.assertRaises(HostKeyUnknown) as cm:
            transport.run("ssh", self.adhoc(self.ports[0]), f"touch {marker}", timeout=60)
        self.assertIn("ssh-trust", str(cm.exception))
        self.assertFalse(marker.exists())
        self.assertFalse(self.own().exists())                                                       # and nothing was recorded

    def test_adhoc_explicit_trust_with_the_right_fingerprint_then_connection_works(self):
        from gisstack import sshtrust
        port = self.ports[0]
        shown = sshtrust.scan("127.0.0.1", port)
        self.assertEqual(shown[0]["fingerprint"], self.fp("s0"))
        with self.assertRaises(DeployError) as cm:                                                  # a wrong fingerprint is NOT trusted
            sshtrust.trust("127.0.0.1", port, expected=self.fp("s1"))
        self.assertIn("NOT trusted", str(cm.exception))
        with self.assertRaises(DeployError):
            sshtrust.trust("127.0.0.1", port, expected="abc")                                       # not even in the SHA256: form
        self.assertFalse(self.own().exists())
        res = sshtrust.trust("127.0.0.1", port, expected=self.fp("s0"))
        self.assertFalse(res["already"])
        self.assertEqual(stat.S_IMODE(self.own().stat().st_mode), 0o600)
        self.assertEqual(transport.run("ssh", self.adhoc(port), "echo trusted-ok", timeout=60).strip(), "trusted-ok")
        self.assertTrue(sshtrust.trust("127.0.0.1", port, expected=self.fp("s0"))["already"])      # idempotent
        # the other server (different key) is still unknown
        with self.assertRaises(HostKeyUnknown):
            transport.run("ssh", self.adhoc(self.ports[1]), "echo no", timeout=60)

    def test_adhoc_changed_host_key_is_refused_with_a_clear_message_and_replace_is_deliberate(self):
        from gisstack import sshtrust
        port = self.ports[0]
        other = (self.dir / "s1" / "host_ed25519.pub").read_text().split()                          # a DIFFERENT key claimed for the same address
        self.own().write_text(f"[127.0.0.1]:{port} {other[0]} {other[1]}\n")
        marker = Path(self.td) / "should-not-exist"
        with self.assertRaises(HostKeyChanged) as cm:
            transport.run("ssh", self.adhoc(port), f"touch {marker}", timeout=60)
        self.assertIn("CHANGED", str(cm.exception))
        self.assertFalse(marker.exists())
        with self.assertRaises(HostKeyChanged):                                                     # trust() will not overwrite it unless told to
            sshtrust.trust("127.0.0.1", port, expected=self.fp("s0"))
        res = sshtrust.trust("127.0.0.1", port, expected=self.fp("s0"), replace=True)
        self.assertTrue(res["replaced"])
        self.assertEqual(transport.run("ssh", self.adhoc(port), "echo replaced-ok", timeout=60).strip(), "replaced-ok")

    def test_adhoc_trust_on_first_use_is_an_explicit_opt_in_and_is_recorded_in_our_file(self):
        users = Path.home() / ".ssh" / "known_hosts"
        before = users.stat().st_mtime_ns if users.exists() else None
        with mock.patch.dict(os.environ, {"GISSTACK_SSH_TOFU": "1"}):
            out = transport.run("ssh", self.adhoc(self.ports[0]), "echo tofu-ok", timeout=60)
        self.assertEqual(out.strip(), "tofu-ok")
        found = subprocess.run(["ssh-keygen", "-F", f"[127.0.0.1]:{self.ports[0]}", "-f", str(self.own())], capture_output=True, text=True)
        self.assertEqual(found.returncode, 0, found.stderr)                                         # hashed entries are looked up with ssh-keygen
        self.assertIn(self.pubkey(0), found.stdout)
        self.assertEqual(users.stat().st_mtime_ns if users.exists() else None, before)             # the user's own known_hosts was not touched

    def test_ssh_trust_command_scan_trust_and_refusals(self):
        import contextlib
        import io

        from gisstack import cli
        port = str(self.ports[0])

        def run(*argv):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = cli.main(["ssh-trust", "127.0.0.1", "--port", port, *argv])
            return rc, out.getvalue(), err.getvalue()
        rc, out, _ = run("--scan-only")
        self.assertEqual(rc, 0)
        self.assertIn(self.fp("s0"), out)
        self.assertFalse(self.own().exists())                                                       # scanning trusts nothing
        rc, _, err = run()                                                                           # no fingerprint and no terminal
        self.assertEqual(rc, 2)
        self.assertIn("--fingerprint", err)
        rc, _, err = run("--fingerprint", self.fp("s1"))
        self.assertEqual(rc, 1)
        self.assertIn("NOT trusted", err)
        rc, out, _ = run("--fingerprint", self.fp("s0"))
        self.assertEqual(rc, 0)
        self.assertIn("trusted", out)
        self.assertEqual(transport.run("ssh", self.adhoc(self.ports[0]), "echo cli-ok", timeout=60).strip(), "cli-ok")

    def test_scan_pin_probe(self):
        self.profile("one", self.ports[0])
        with self.assertRaises(store.ServerError):
            service.probe("one")                                            # unpinned: refused before any connection
        keys = hostkey.scan("one")
        self.assertEqual(keys[0]["fingerprint"], self.fp("s0"))             # our computation equals OpenSSH's
        with self.assertRaises(store.ServerError):
            hostkey.pin("one", "SHA256:" + "A" * 43)                        # a fingerprint nobody presented
        self.assertFalse(store.get("one")["pinned"])
        hostkey.pin("one", self.fp("s0"))
        r = self.reachable("one")
        self.assertIn("os", r["facts"])

    def test_two_servers_with_independent_pins_and_compare(self):
        for i, sid in enumerate(("one", "two")):
            self.profile(sid, self.ports[i])
            hostkey.pin(sid, self.fp(f"s{i}"))
        self.assertNotEqual(store.ssh_for("one")["known_hosts"], store.ssh_for("two")["known_hosts"])
        self.reachable("one")
        self.reachable("two")
        r = service.compare(["one", "two"])
        self.assertTrue(r["complete"], r)
        self.assertEqual([s["reachable"] for s in r["servers"]], [True, True])

    def test_a_pin_for_one_server_does_not_open_the_other(self):
        self.profile("one", self.ports[0])
        self.profile("two", self.ports[1])
        hostkey.pin("one", self.fp("s0"))
        # copy server one's pin onto server two: the key differs, so strict checking must refuse
        store.set_pin("two", [store.known_hosts_file("one").read_text().strip().replace(str(self.ports[0]), str(self.ports[1]))], [self.fp("s0")])
        r = service.probe("two")
        self.assertFalse(r["reachable"], r)
        self.assertIn("host key", r["error"].lower(), r)                    # refused BECAUSE of the key, not for some other reason

    def test_changed_host_key_is_refused_until_repinned(self):
        self.profile("one", self.ports[0])
        hostkey.pin("one", self.fp("s0"))
        self.reachable("one")
        self.procs[0].terminate()
        self.procs[0].wait(5)
        # same address, NEW host key (rebuilt server or man in the middle)
        sd = self.dir / "s0"
        (sd / "host_ed25519").unlink()
        (sd / "host_ed25519.pub").unlink()
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(sd / "host_ed25519")], check=True)
        cfg = (sd / "sshd_config").read_text()
        p = subprocess.Popen([SSHD, "-D", "-e", "-f", str(sd / "sshd_config")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(p)
        self.assertIn(str(self.ports[0]), cfg)
        for _ in range(50):
            with socket.socket() as s:
                s.settimeout(0.2)
                if s.connect_ex(("127.0.0.1", self.ports[0])) == 0:
                    break
            time.sleep(0.1)
        r = service.probe("one")
        self.assertFalse(r["reachable"], r)                                 # no silent trust of the new key
        self.assertIn("host key", r["error"].lower(), r)
        with self.assertRaises(store.ServerError):
            hostkey.pin("one", self.fp("s0"))                               # replacing a pin must be explicit
        hostkey.pin("one", self.fp("s0"), replace=True)
        self.reachable("one")

    def test_no_key_material_in_any_stored_file(self):
        self.profile("one", self.ports[0])
        hostkey.pin("one", self.fp("s0"))
        service.probe("one")
        secret = self.client_key.read_text().splitlines()[1]
        for f in Path(self.td).rglob("*"):
            if f.is_file():
                self.assertNotIn(secret, f.read_text(errors="replace"), f)
                self.assertNotIn("PRIVATE KEY", f.read_text(errors="replace"), f)


@unittest.skipUnless(HAVE_FLASK, "flask needed")
class ApiTests(Home):
    FEATURES = "multi_server,mapproxy_manager"
    P = {"host": "gis.example.org", "env": "production", "user": "deploy", "key": "/k/id"}
    NATIVE = {"deploy_target": "native", "targets": ["native"], "project": "mp"}

    def setUp(self):
        super().setUp()
        from gisstack.api import auth
        from gisstack.webgui.app import create_app
        self.c = create_app("tok").test_client()
        self.tok = {s: auth.TokenStore().create(s, s)[0] for s in ("read", "operate", "admin")}

    def call(self, method, path, body=None, token="tok"):
        r = self.c.open("/api/v1" + path, method=method, json=body, headers={"Authorization": f"Bearer {token}"})
        self.addCleanup(r.close)
        return r

    def audit(self):
        p = Path(self.td) / "audit.log"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def test_flag_off_hides_everything(self):
        with mock.patch.dict(os.environ, {"GISSTACK_FEATURES": ""}):
            self.assertEqual(self.call("GET", "/servers").status_code, 404)
            r = self.call("POST", "/mapproxy/health", {"mode": "ssh", "server": "x", "config": self.NATIVE})
            self.assertIn(r.status_code, (404, 400))

    def test_scopes(self):
        T = self.tok
        self.assertEqual(self.call("GET", "/servers", token=T["read"]).status_code, 200)
        for scope in ("read", "operate"):
            self.assertEqual(self.call("PUT", "/servers/prod1", self.P, T[scope]).status_code, 403)
            self.assertEqual(self.call("DELETE", "/servers/prod1", token=T[scope]).status_code, 403)
            self.assertEqual(self.call("POST", "/servers/prod1/hostkey/scan", {}, T[scope]).status_code, 403)
            self.assertEqual(self.call("POST", "/servers/prod1/hostkey/pin", {"fingerprint": "SHA256:" + "A" * 43}, T[scope]).status_code, 403)

    def test_crud_audit_and_contract(self):
        from gisstack.api import schema
        from gisstack.api.registry import registry
        r = self.call("PUT", "/servers/prod1", self.P)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(schema.validate(r.get_json()["data"], registry.find("PUT", "/servers/{id}").response, registry.components), [])
        self.assertEqual(self.call("GET", "/servers").get_json()["data"][0]["id"], "prod1")
        self.assertEqual([e["event"] for e in self.audit() if e["event"].startswith("servers.")], ["servers.save"])
        self.assertEqual(self.call("DELETE", "/servers/prod1").status_code, 200)
        self.assertEqual(self.call("DELETE", "/servers/prod1").status_code, 404)

    def test_key_content_is_a_400_and_never_stored(self):
        r = self.call("PUT", "/servers/prod1", {**self.P, "key": "-----BEGIN OPENSSH PRIVATE KEY-----\nAAA\n-----END OPENSSH PRIVATE KEY-----"})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(store.file().exists() and "PRIVATE" in store.file().read_text())

    def test_machine_routes_use_the_profile_credentials(self):
        self.call("PUT", "/servers/prod1", self.P)
        seen = []
        from gisstack.mapproxy.server import ScriptGateway

        def fake(mode, ssh):
            seen.append((mode, dict(ssh)))
            return mock.Mock(spec=ScriptGateway)

        with mock.patch("gisstack.api.routes_mapproxy.ScriptGateway", fake), mock.patch("gisstack.api.routes_mapproxy.mpm.validate", lambda gw: {"ok": True, "errors": []}):
            body = {"mode": "ssh", "server": "prod1", "config": self.NATIVE}
            r = self.call("POST", "/mapproxy/validate", body)
            self.assertEqual(r.status_code, 400)                            # unpinned: refused, nothing connected
            self.assertEqual(seen, [])
            store.set_pin("prod1", ["gis.example.org ssh-ed25519 " + "A" * 68], ["SHA256:" + "A" * 43])
            r = self.call("POST", "/mapproxy/validate", {**body, "ssh": {"host": "evil.example", "user": "root"}})
            self.assertEqual(r.status_code, 200, r.get_json())
            mode, ssh = seen[0]
            self.assertEqual((mode, ssh["host"], ssh["user"], ssh["key"]), ("ssh", "gis.example.org", "deploy", "/k/id"))   # the body's own ssh is ignored
            self.assertTrue(ssh["known_hosts"].endswith("prod1"))
            r = self.call("POST", "/mapproxy/validate", {**body, "mode": "local"})
            self.assertEqual(r.status_code, 400)                            # a profile means ssh

    def test_client_cannot_inject_a_known_hosts_file(self):
        r = self.call("POST", "/mapproxy/validate", {"mode": "ssh", "ssh": {"host": "h.example", "known_hosts": "/etc/passwd"}, "config": self.NATIVE})
        self.assertEqual(r.status_code, 400)

    def test_compare_and_history_routes(self):
        for sid, h in (("a", "a.example"), ("b", "b.example")):
            self.call("PUT", f"/servers/{sid}", {"host": h, "env": "testing"})
            store.set_pin(sid, [f"{h} ssh-ed25519 " + "A" * 68], ["SHA256:" + "A" * 43])
        with mock.patch.object(transport, "run", lambda mode, ssh, script, **kw: "os=X\nver.postgres=" + ssh["host"] + "\n"):
            d = self.call("POST", "/servers/compare", {"ids": ["a", "b"]}).get_json()["data"]
        self.assertEqual((d["drift"], d["complete"]), (1, True))
        self.assertEqual(self.call("GET", "/servers/a/history").get_json()["data"], [])
        self.assertEqual(self.call("POST", "/servers/compare", {"ids": ["a"]}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
