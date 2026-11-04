"""Phase 1b: credential rotation - the secret travels on stdin only; failures change nothing."""
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from gisstack import cli, config, deploy, ops, rotation, runner

PW = "Ne3w-Passw0rd_x"


def cfg(**kw):
    return config.resolve({"targets": ["native"], "deploy_target": "native", "project": "rot", **kw})


class Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        p = mock.patch.dict(os.environ, {"GISSTACK_HOME": self.td.name})
        p.start()
        self.addCleanup(p.stop)


class ScriptTests(unittest.TestCase):
    def test_new_password_is_read_from_stdin_never_embedded(self):
        for what in rotation.WHAT:
            sc = rotation.script(what)
            self.assertIn("read -r NEWPW", sc)
            self.assertNotIn(PW, sc)
            self.assertRegex(sc, r"case \"\$NEWPW\" in ''\|\*\[!A-Za-z0-9_\.-\]\*\)")        # the alphabet is enforced server-side too
            self.assertNotRegex(sc, r"(psql|curl)[^\n]*\$NEWPW")                                 # never on a command line
            self.assertNotIn("-v dbpass", sc)

    def test_postgres_script_verifies_before_it_writes_and_can_roll_back(self):
        sc = rotation.script("postgres")
        self.assertLess(sc.index("login_ok \"$NEWPW\""), sc.index("setkv /etc/gisstack/stack.env"))
        self.assertIn('alter "$DB_PASSWORD"', sc)

    def test_geoserver_script_verifies_before_it_writes_and_can_roll_back(self):
        sc = rotation.script("geoserver")
        self.assertLess(sc.index('verified=1; break'), sc.index("setkv /etc/gisstack/stack.env"))
        self.assertIn("sleep 1", sc)                                           # the change is applied a moment after GeoServer answers 200
        self.assertIn("oldbody", sc)

    def test_helper_functions_edit_files_correctly_in_real_bash(self):
        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / "stack.env"
            env.write_text('DB_NAME="${DB_NAME:-gis}"\nDB_PASSWORD="${DB_PASSWORD:-old.pw_1234}"\nDB_PASSWORDX="keep"\n')
            cred = Path(td) / "credentials.txt"
            cred.write_text("Domain: x\nPostgreSQL:        host=127.0.0.1 port=5432 db=gis user=gis password=old.pw_1234\nGeoServer admin:   admin / oldgs_1234\n")
            helpers = rotation._PRELUDE.split("hexof()")[1]
            code = "hexof()" + helpers + f'\nNEWPW="{PW}"; export NEWPW\nsetkv "{env}" DB_PASSWORD\nsetcred "{cred}" pg\nsetcred "{cred}" gs\n'
            r = subprocess.run(["bash", "-c", "set -eu\n" + code], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(f'DB_PASSWORD="${{DB_PASSWORD:-{PW}}}"', env.read_text())
            self.assertIn('DB_PASSWORDX="keep"', env.read_text())                                # other lines untouched
            self.assertIn(f"password={PW}", cred.read_text())
            self.assertIn(f"admin / {PW}", cred.read_text())
            out = subprocess.run(["bash", "-c", f'. "{env}"; echo "$DB_PASSWORD"'], capture_output=True, text=True).stdout.strip()
            self.assertEqual(out, PW)                                                              # the file still sources correctly

    def test_script_text_passes_shellcheck_when_available(self):
        if not shutil.which("shellcheck"):
            self.skipTest("shellcheck not installed")
        for what in rotation.WHAT:
            r = subprocess.run(["shellcheck", "-s", "bash", "-S", "warning", "-"], input=rotation.script(what), capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout)


class RotateTests(Base):
    def test_success_passes_the_password_on_stdin_and_updates_the_local_store(self):
        seen = {}

        def fake(mode, ssh, script, *, root=False, timeout=40, input_text=None):
            seen.update(script=script, input=input_text, root=root)
            return "ROTATED=postgres\n"
        with mock.patch.object(ops, "_run", fake):
            res = rotation.rotate(cfg(), "postgres", "local", None, PW)
        self.assertEqual(seen["input"], PW + "\n")
        self.assertTrue(seen["root"])
        self.assertNotIn(PW, seen["script"])
        self.assertEqual(res["what"], "postgres")
        store = json.loads((Path(self.td.name) / "secrets/rot.json").read_text())
        self.assertEqual(store["postgres.password"], PW)

    def test_generated_password_is_valid_and_distinct(self):
        pws = {rotation.new_password() for _ in range(20)}
        self.assertEqual(len(pws), 20)
        self.assertTrue(all(rotation._PW.match(p) for p in pws))

    def test_failure_markers_become_clear_errors_and_leave_the_store_alone(self):
        for marker, text in (("verify-failed", "ძველი აღდგენილია"), ("change-failed", "არაფერი შეცვლილა"), ("not-installed", "არ არის დაყენებული")):
            with mock.patch.object(ops, "_run", return_value=f"ROTATE_ERROR={marker}\n"), self.assertRaises(deploy.DeployError) as cm:
                rotation.rotate(cfg(), "geoserver", "local", None, PW)
            self.assertIn(text, str(cm.exception))
        self.assertFalse((Path(self.td.name) / "secrets/rot.json").exists())

    def test_unconfirmed_run_is_not_success(self):
        with mock.patch.object(ops, "_run", return_value="all fine?\n"), self.assertRaises(deploy.DeployError):
            rotation.rotate(cfg(), "postgres", "local", None, PW)

    def test_bad_passwords_and_non_native_targets_are_refused_before_anything_runs(self):
        with mock.patch.object(ops, "_run") as run:
            for bad in ("short", "has space 12345", "semi;colon_1234", "x" * 70):
                with self.assertRaises(deploy.DeployError):
                    rotation.rotate(cfg(), "postgres", "local", None, bad)
            with self.assertRaises(deploy.DeployError):
                rotation.rotate(cfg(deploy_target="compose", targets=["compose"]), "postgres", "local", None, PW)
            with self.assertRaises(deploy.DeployError):
                rotation.script("tomcat")
            run.assert_not_called()

    def test_error_text_never_contains_the_password_and_the_journal_records_the_attempt(self):
        from gisstack import journal
        with mock.patch.object(ops, "_run", side_effect=deploy.DeployError(f"ssh said {PW}")), self.assertRaises(deploy.DeployError) as cm:
            rotation.rotate(cfg(), "postgres", "local", None, PW)
        self.assertNotIn(PW, str(cm.exception))
        self.assertNotIn(PW, json.dumps(journal.tail()))
        self.assertEqual(journal.tail()[-1]["status"], "error")

    def test_runner_hands_input_over_stdin_not_argv(self):
        p = runner.run(["bash", "-c", 'read -r X; echo "got=$X"; echo "args=$*"'], timeout=10, input_text="s3cr3tvalue\n")
        self.assertIn("got=s3cr3tvalue", p.stdout)
        self.assertNotIn("s3cr3tvalue", p.stdout.split("args=")[1])


class CliTests(Base):
    def run_cli(self, *a):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(list(a))
        return rc, out.getvalue(), err.getvalue()

    def test_cli_generates_a_password_and_never_prints_it(self):
        captured = {}

        def fake(mode, ssh, script, *, root=False, timeout=40, input_text=None):
            captured["pw"] = input_text.strip()
            return "ROTATED=geoserver\n"
        f = Path(self.td.name) / "c.json"
        f.write_text(json.dumps({"project": "rot", "deploy_target": "native", "targets": ["native"]}))
        with mock.patch.object(ops, "_run", fake):
            rc, out, err = self.run_cli("rotate-credentials", "geoserver", "-c", str(f))
        self.assertEqual(rc, 0, err)
        self.assertNotIn(captured["pw"], out + err)
        self.assertTrue(re.fullmatch(r"[A-Za-z0-9_\-]{20,}", captured["pw"]))

    def test_password_file_must_be_private_and_is_used(self):
        pwf = Path(self.td.name) / "pw"
        pwf.write_text(PW + "\n")
        f = Path(self.td.name) / "c.json"
        f.write_text(json.dumps({"project": "rot", "deploy_target": "native", "targets": ["native"]}))
        os.chmod(pwf, 0o644)
        rc, _, err = self.run_cli("rotate-credentials", "postgres", "-c", str(f), "--password-file", str(pwf))
        self.assertEqual(rc, 2)
        os.chmod(pwf, 0o600)
        got = {}

        def fake(mode, ssh, script, *, root=False, timeout=40, input_text=None):
            got["pw"] = input_text
            return "ROTATED=postgres\n"
        with mock.patch.object(ops, "_run", fake):
            rc, _, _ = self.run_cli("rotate-credentials", "postgres", "-c", str(f), "--password-file", str(pwf))
        self.assertEqual((rc, got["pw"]), (0, PW + "\n"))


if __name__ == "__main__":
    unittest.main()
