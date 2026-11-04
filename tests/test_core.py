"""Phase 2: core package - error taxonomy, paths, events, logging."""
import io
import logging
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gisstack import config, deploy, existing, journal, projects, runner, secretsrc, updater
from gisstack.core import errors, events, log, paths


class ErrorTests(unittest.TestCase):
    def test_old_names_are_the_new_classes(self):
        self.assertIs(deploy.DeployError, errors.DeployError)
        self.assertIs(config.ConfigError, errors.ConfigError)
        self.assertIs(existing.PlanError, errors.PlanError)
        self.assertIs(updater.UpdateError, errors.UpdateError)
        self.assertIs(projects.ProjectError, errors.ProjectError)
        self.assertIs(runner.RunnerError, errors.RunnerError)
        self.assertIs(runner.Timeout, errors.Timeout)
        self.assertIs(journal.Busy, errors.Busy)
        self.assertIs(secretsrc.SecretError, errors.SecretError)

    def test_hierarchy_keeps_existing_except_clauses_working(self):
        self.assertTrue(issubclass(errors.PlanError, errors.DeployError))
        self.assertTrue(issubclass(errors.Timeout, errors.DeployError))       # a timeout is caught where DeployError is caught
        self.assertTrue(issubclass(errors.ConfigError, ValueError))
        self.assertTrue(issubclass(errors.ProjectError, ValueError))
        self.assertTrue(issubclass(errors.SecretError, errors.ConfigError))
        self.assertTrue(all(issubclass(c, errors.GisStackError) for c in
                            (errors.DeployError, errors.ConfigError, errors.UpdateError, errors.Busy, errors.ProjectError)))

    def test_every_error_has_a_distinct_stable_code(self):
        classes = [c for c in vars(errors).values() if isinstance(c, type) and issubclass(c, errors.GisStackError)]
        codes = [c.code for c in classes]
        self.assertEqual(len(codes), len(set(codes)), codes)
        self.assertEqual(errors.code_of(errors.Timeout("x")), "timeout")
        self.assertEqual(errors.code_of(KeyError("x")), "internal")


class PathTests(unittest.TestCase):
    def test_everything_hangs_off_one_private_home(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"GISSTACK_HOME": td}):
            h = paths.home()
            self.assertEqual(h, Path(td))
            self.assertEqual(stat.S_IMODE(h.stat().st_mode), 0o700)
            for p in (paths.secrets_file("p"), paths.projects_dir(), paths.bundles_dir(), paths.backups_dir(), paths.settings_file(),
                      paths.token_file(), paths.audit_log(), paths.journal_file(), paths.log_file(), paths.lock_dir()):
                self.assertEqual(p.parent if p.suffix or "secrets" in p.parts else p.parent, p.parent)
                self.assertTrue(str(p).startswith(td), p)
            self.assertEqual(deploy.home_dir(), h)                                  # old entry point still works
            self.assertEqual(config._secrets_path("p"), paths.secrets_file("p"))

    def test_default_is_dot_gisstack_in_the_user_home(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"HOME": td}, clear=False):
            os.environ.pop("GISSTACK_HOME", None)
            self.assertEqual(paths.home(), Path(td) / ".gisstack")


class EventTests(unittest.TestCase):
    def test_subscribe_emit_unsubscribe(self):
        seen = []
        off = events.subscribe(seen.append)
        ev = events.emit("x.y", "hello", a=1)
        off()
        events.emit("x.z")
        self.assertEqual([e.kind for e in seen], ["x.y"])
        self.assertEqual((ev.message, ev.data), ("hello", {"a": 1}))

    def test_a_failing_listener_does_not_break_the_emitter_or_other_listeners(self):
        seen = []
        offs = [events.subscribe(lambda e: 1 / 0), events.subscribe(seen.append)]
        self.addCleanup(lambda: [o() for o in offs])
        events.emit("k")
        self.assertEqual(len(seen), 1)

    def test_operations_announce_themselves(self):
        seen = []
        off = events.subscribe(seen.append)
        self.addCleanup(off)
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"GISSTACK_HOME": td}):
            with journal.Operation("deploy", host="srv"):
                pass
            with self.assertRaises(RuntimeError), journal.Operation("clean"):
                raise RuntimeError("boom")
        kinds = [(e.kind, e.message, e.data.get("status")) for e in seen if e.kind.startswith("operation")]
        self.assertEqual(kinds, [("operation.start", "deploy", None), ("operation.end", "deploy", "ok"),
                                 ("operation.start", "clean", None), ("operation.end", "clean", "error")])


class LogTests(unittest.TestCase):
    def tearDown(self):
        log.configure(None)
        log._secrets.clear()

    def test_silent_by_default_and_redacts_when_enabled(self):
        buf = io.StringIO()
        log.configure("debug")
        h = [x for x in logging.getLogger("gisstack").handlers if getattr(x, "_gisstack", False)][0]
        h.setStream(buf)
        log.add_secrets(["Sup3rSecret99"])
        log.get("test").info("password is Sup3rSecret99 ok")
        self.assertIn("••••••••", buf.getvalue())
        self.assertNotIn("Sup3rSecret99", buf.getvalue())

    def test_runner_logs_commands_with_secrets_masked(self):
        buf = io.StringIO()
        log.configure("debug")
        [x for x in logging.getLogger("gisstack").handlers if getattr(x, "_gisstack", False)][0].setStream(buf)
        list(runner.stream(["echo", "Sup3rSecret99"], redact=runner.redactor(["Sup3rSecret99"])))
        self.assertIn("run: echo", buf.getvalue())
        self.assertNotIn("Sup3rSecret99", buf.getvalue())

    def test_file_log_is_owner_only_and_bad_level_is_rejected(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"GISSTACK_HOME": td}):
            log.configure("info", to_file=True)
            self.assertEqual(stat.S_IMODE(paths.log_file().stat().st_mode), 0o600)
            with self.assertRaises(ValueError):
                log.configure("loud")
            log.configure(None)


if __name__ == "__main__":
    unittest.main()


class CliLogTests(unittest.TestCase):
    def test_log_level_flag_turns_logging_on_and_off(self):
        from contextlib import redirect_stderr, redirect_stdout

        from gisstack import cli
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["--log-level", "debug", "defaults"]), 0)
            self.assertTrue(any(getattr(h, "_gisstack", False) for h in logging.getLogger("gisstack").handlers))
            self.assertEqual(cli.main(["defaults"]), 0)
        self.assertFalse(any(getattr(h, "_gisstack", False) for h in logging.getLogger("gisstack").handlers))
