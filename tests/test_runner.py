"""Phase 1b / R-01: the central runner bounds, cancels and cleans up commands and hides secrets."""
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from gisstack import deploy, runner, transport


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:                                              # a zombie still "exists"; treat it as gone
        return Path(f"/proc/{pid}/stat").read_text().split(")")[1].split()[0] != "Z"
    except OSError:
        return True


def wait_dead(pid: int, seconds: float = 6.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if not alive(pid):
            return True
        time.sleep(0.1)
    return False


@unittest.skipUnless(os.name == "posix", "POSIX process groups")
class RunnerTests(unittest.TestCase):
    def test_collects_output_and_raises_on_failure(self):
        self.assertEqual(list(runner.stream(["bash", "-c", "echo a; echo b"], echo=False)), ["a", "b"])
        with self.assertRaises(runner.RunnerError):
            list(runner.stream(["bash", "-c", "echo x; exit 3"]))
        with self.assertRaises(deploy.DeployError):
            list(runner.stream(["definitely-not-a-command-xyz"], error=deploy.DeployError))

    def test_deadline_kills_the_command(self):
        t0 = time.time()
        with self.assertRaises(runner.Timeout):
            list(runner.stream(["sleep", "60"], deadline=1, grace=1))
        self.assertLess(time.time() - t0, 8)

    def test_idle_timeout_when_no_output(self):
        with self.assertRaises(runner.Timeout) as cm:
            list(runner.stream(["bash", "-c", "echo start; sleep 60"], idle=1, grace=1))
        self.assertIn("no output", str(cm.exception))

    def test_cancel_returns_normally_and_kills_children(self):
        with tempfile.TemporaryDirectory() as td:
            pidfile = Path(td) / "child.pid"
            stop = threading.Event()
            script = f"sleep 120 & echo $! > {pidfile}; echo ready; wait"
            seen = []
            for line in runner.stream(["bash", "-c", script], stop=stop, grace=1, echo=False):
                seen.append(line)
                if line == "ready":
                    threading.Timer(0.3, stop.set).start()
            self.assertEqual(seen, ["ready"])
            self.assertTrue(wait_dead(int(pidfile.read_text())), "the grandchild survived the cancellation")

    def test_stubborn_child_that_ignores_term_is_killed(self):
        with tempfile.TemporaryDirectory() as td:
            pidfile = Path(td) / "pid"
            script = f"trap '' TERM; echo $$ > {pidfile}; echo up; while :; do sleep 1; done"
            stop = threading.Event()
            for line in runner.stream(["bash", "-c", script], stop=stop, grace=1, echo=False):
                if line == "up":
                    stop.set()
            self.assertTrue(wait_dead(int(pidfile.read_text())))

    def test_closing_the_generator_early_leaves_nothing_behind(self):
        with tempfile.TemporaryDirectory() as td:
            pidfile = Path(td) / "pid"
            g = runner.stream(["bash", "-c", f"echo $$ > {pidfile}; echo first; sleep 120"], grace=1, echo=False)
            self.assertEqual(next(g), "first")
            g.close()
            self.assertTrue(wait_dead(int(pidfile.read_text())))

    def test_secrets_are_masked_in_output_and_in_the_echoed_command(self):
        mask = runner.redactor(["S3cretValue", "S3cret"])
        out = list(runner.stream(["bash", "-c", "echo token=S3cretValue; echo S3cret"], redact=mask))
        self.assertTrue(all("S3cret" not in line for line in out), out)
        self.assertIn(runner.MASK, out[1])

    def test_secrets_of_finds_passwords_in_a_config(self):
        cfg = {"postgres": {"password": "pw-one-1"}, "geoserver": {"admin_password": "pw-two-2", "version": "3"}, "tomcat": {"port": 1}}
        self.assertEqual(sorted(runner.secrets_of(cfg)), ["pw-one-1", "pw-two-2"])

    def test_run_timeout_kills_a_hung_command(self):
        t0 = time.time()
        with self.assertRaises(deploy.DeployError):
            runner.run(["sleep", "60"], timeout=1, error=deploy.DeployError, grace=1)
        self.assertLess(time.time() - t0, 8)

    def test_environment_overrides_the_default_limits(self):
        with mock.patch.dict(os.environ, {runner.DEADLINE_ENV: "12", runner.IDLE_ENV: "0"}):
            self.assertEqual(runner.default_deadline(), 12.0)
            self.assertIsNone(runner.default_idle())
        with mock.patch.dict(os.environ, {runner.DEADLINE_ENV: "junk"}):
            self.assertEqual(runner.default_deadline(), runner.DEFAULT_DEADLINE)


@unittest.skipUnless(os.name == "posix", "POSIX")
class HungSshTests(unittest.TestCase):
    """A fake `ssh` that never answers must not hang an operation."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        ssh = Path(self.td.name) / "ssh"
        ssh.write_text("#!/bin/bash\necho connecting >&2\nexec sleep 300\n")
        ssh.chmod(0o755)
        self.env = mock.patch.dict(os.environ, {"PATH": f"{self.td.name}:{os.environ['PATH']}"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.td.cleanup)
        self.ssh = {"host": "srv.example.ge", "user": "tester", "port": 22}

    def test_status_style_call_times_out(self):
        t0 = time.time()
        with mock.patch.object(runner, "GRACE_SECONDS", 1.0), self.assertRaises(deploy.DeployError):
            transport.run("ssh", self.ssh, "true", timeout=1)
        self.assertLess(time.time() - t0, 10)

    def test_deploy_stream_honours_the_deadline_variable(self):
        with mock.patch.dict(os.environ, {runner.DEADLINE_ENV: "1"}), mock.patch.object(runner, "GRACE_SECONDS", 1.0):
            t0 = time.time()
            with self.assertRaises(deploy.DeployError) as cm:
                list(deploy._stream(deploy._ssh_base(self.ssh) + ["true"]))
            self.assertIn("deadline", str(cm.exception))
            self.assertLess(time.time() - t0, 10)

    def test_log_following_can_be_cancelled(self):
        stop = threading.Event()
        threading.Timer(0.8, stop.set).start()
        t0 = time.time()
        with mock.patch.object(runner, "GRACE_SECONDS", 1.0):
            list(transport.stream("ssh", self.ssh, "tail -f /dev/null", stop=stop))
        self.assertLess(time.time() - t0, 10)


if __name__ == "__main__":
    unittest.main()
