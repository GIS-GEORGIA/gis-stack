"""Phase 1b / R-02, R-05, S-08: operation journal, locks and the residue of an SSH deployment."""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gisstack import deploy, journal, runner


class Base(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.td, True)
        env = mock.patch.dict(os.environ, {"GISSTACK_HOME": str(self.td / "home")})
        env.start()
        self.addCleanup(env.stop)


class JournalTests(Base):
    def test_operation_writes_start_and_end_with_private_mode(self):
        with journal.Operation("deploy", target="native", host="srv"):
            pass
        ev = journal.tail()
        self.assertEqual([e["event"] for e in ev], ["start", "end"])
        self.assertEqual(ev[1]["status"], "ok")
        self.assertEqual(ev[0]["id"], ev[1]["id"])
        self.assertEqual(stat.S_IMODE(journal.path().stat().st_mode), 0o600)

    def test_failure_is_recorded_and_secrets_are_masked(self):
        mask = runner.redactor(["Sup3rSecret"])
        with self.assertRaises(RuntimeError), journal.Operation("deploy", redact=mask):
            raise RuntimeError("login failed for Sup3rSecret")
        end = journal.tail()[-1]
        self.assertEqual(end["status"], "error")
        self.assertNotIn("Sup3rSecret", json.dumps(journal.tail()))
        self.assertIn(runner.MASK, end["error"])

    def test_abandoned_generator_is_recorded_as_aborted(self):
        def gen():
            with journal.Operation("deploy"):
                yield 1
                yield 2
        g = gen()
        next(g)
        g.close()
        self.assertEqual(journal.tail()[-1]["status"], "aborted")

    def test_file_is_rotated_when_large(self):
        with mock.patch.object(journal, "MAX_BYTES", 50):
            for _ in range(5):
                journal.record({"event": "x", "pad": "y" * 40})
        self.assertTrue(journal.path().with_suffix(".jsonl.1").exists())


class LockTests(Base):
    def test_second_holder_is_refused_and_the_lock_is_released_afterwards(self):
        with journal.exclusive():
            with self.assertRaises(journal.Busy), journal.exclusive():
                pass
        with journal.exclusive():       # released
            pass

    def test_local_deploy_is_refused_while_another_operation_runs(self):
        b = self.td / "bundle"
        (b / "compose").mkdir(parents=True)
        (b / "compose/up.sh").write_text("echo hi\n")
        cfg = {"project": "p", "runtime": "docker"}
        with mock.patch.object(deploy, "preflight", return_value=[]), journal.exclusive(), self.assertRaises(deploy.DeployError):
            list(deploy.deploy(cfg, b, "compose", "local"))
        self.assertEqual(journal.tail()[-1]["status"], "error")

    @unittest.skipUnless(shutil.which("flock"), "util-linux flock needed")
    def test_remote_lock_fragment_returns_75_when_busy(self):
        lock = self.td / "op.lock"
        frag = deploy.REMOTE_LOCK.replace("/tmp/gisstack-op.lock", str(lock))
        holder = subprocess.Popen(["flock", str(lock), "sleep", "5"], start_new_session=True)
        try:
            for _ in range(50):
                if lock.exists():
                    break
                subprocess.run(["sleep", "0.1"])
            r = subprocess.run(["bash", "-c", f"{frag} true"], capture_output=True)
            self.assertEqual(r.returncode, deploy.LOCK_EXIT)
        finally:
            os.killpg(holder.pid, 9)         # the sleep child holds the lock too
            holder.wait()
        self.assertEqual(subprocess.run(["bash", "-c", f"{frag} true"]).returncode, 0)


@unittest.skipUnless(os.name == "posix", "POSIX")
class SshResidueTests(Base):
    def setUp(self):
        super().setUp()
        self.log = self.td / "ssh.log"
        stub = self.td / "bin"
        stub.mkdir()
        ssh = stub / "ssh"
        ssh.write_text(f'#!/bin/bash\necho "ssh $*" >> "{self.log}"\ncase "$*" in *"tar xzf"*) cat > /dev/null ;; *) echo remote-output ;; esac\n')
        ssh.chmod(0o755)
        p = mock.patch.dict(os.environ, {"PATH": f"{stub}:{os.environ['PATH']}"})
        p.start()
        self.addCleanup(p.stop)
        self.b = self.td / "bundle"
        for t in ("native", "compose"):
            (self.b / t).mkdir(parents=True)
            (self.b / t / ("install.sh" if t == "native" else "up.sh")).write_text("echo hi\n")

    def test_native_bundle_with_passwords_is_removed_from_the_server_afterwards(self):
        cfg = {"project": "p1", "runtime": "docker", "postgres": {"password": "Sup3rSecret1"}}
        list(deploy.deploy(cfg, self.b, "native", "ssh", {"host": "srv", "user": "root", "port": 22}))
        self.assertIn("rm -rf -- gisstack-p1/native", self.log.read_text())

    def test_native_bundle_is_removed_even_when_the_install_fails(self):
        (self.td / "bin/ssh").write_text(f'#!/bin/bash\necho "ssh $*" >> "{self.log}"\ncase "$*" in *"tar xzf"*) cat >/dev/null ;; *"install.sh"*) echo boom; exit 1 ;; esac\n')
        with self.assertRaises(deploy.DeployError):
            list(deploy.deploy({"project": "p1", "runtime": "docker"}, self.b, "native", "ssh", {"host": "srv", "user": "root", "port": 22}))
        self.assertIn("rm -rf -- gisstack-p1/native", self.log.read_text())

    def test_compose_bundle_is_kept_because_the_running_stack_mounts_it(self):
        list(deploy.deploy({"project": "p1", "runtime": "docker"}, self.b, "compose", "ssh", {"host": "srv", "user": "root", "port": 22}))
        self.assertNotIn("rm -rf", self.log.read_text())

    def test_server_busy_exit_code_becomes_a_clear_error(self):
        (self.td / "bin/ssh").write_text(f'#!/bin/bash\ncase "$*" in *"tar xzf"*) cat >/dev/null ;; *"install.sh"*) exit {deploy.LOCK_EXIT} ;; esac\n')
        with self.assertRaises(deploy.DeployError) as cm:
            list(deploy.deploy({"project": "p1", "runtime": "docker"}, self.b, "native", "ssh", {"host": "srv", "user": "root", "port": 22}))
        self.assertIn("უკვე მიმდინარეობს", str(cm.exception))

    def test_password_in_remote_output_is_masked_in_the_log(self):
        (self.td / "bin/ssh").write_text('#!/bin/bash\ncase "$*" in *"tar xzf"*) cat >/dev/null ;; *"install.sh"*) echo "pw=Sup3rSecret1" ;; esac\n')
        out = list(deploy.deploy({"project": "p1", "runtime": "docker", "postgres": {"password": "Sup3rSecret1"}}, self.b, "native", "ssh",
                                 {"host": "srv", "user": "root", "port": 22}))
        self.assertTrue(any(runner.MASK in x for x in out))
        self.assertFalse(any("Sup3rSecret1" in x for x in out))


if __name__ == "__main__":
    unittest.main()
