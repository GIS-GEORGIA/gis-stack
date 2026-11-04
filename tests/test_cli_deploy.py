"""Characterisation tests for the command line (cli.py) and the SSH/local runner (deploy.py).

They pin today's behaviour with stubbed tools (ssh, tar, docker, kubectl) and patched collaborators, so the
modules can be refactored later (ROADMAP phase 2) without changing what users see.  No network, no real servers."""
import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gisstack import cli, config, deploy, deps, discover, existing, ops, projects, providers, settings, transport, updater  # noqa: E402


def make_cfg(**over):
    return config.resolve(over, generate_secrets=False)


class CliBase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        patcher = mock.patch.dict(os.environ, {"GISSTACK_HOME": self.home.name, "GISSTACK_LANG": "en"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = Path(self.home.name)

    def run_cli(self, *argv, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        rc = None
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                if stdin is not None:
                    with mock.patch("builtins.input", return_value=stdin):
                        rc = cli.main(list(argv))
                else:
                    rc = cli.main(list(argv))
            except SystemExit as e:             # argparse errors
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def config_file(self, **over):
        p = self.tmp / "stack.json"
        p.write_text(json.dumps(over))
        return str(p)


class CliBasicsTests(CliBase):
    def test_version_defaults_and_projects(self):
        rc, out, _ = self.run_cli("--version")
        self.assertEqual(rc, 0)
        self.assertIn("gisstack", out)
        rc, out, _ = self.run_cli("defaults")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["postgres"]["db"], "gis")
        rc, out, _ = self.run_cli("projects")
        self.assertEqual((rc, out.strip()), (0, ""))
        projects.save(make_cfg(project="demo"))
        self.assertEqual(self.run_cli("projects")[1].strip(), "demo")

    def test_generate_writes_a_bundle_and_reports_notes(self):
        out_dir = self.tmp / "bundle"
        rc, out, _ = self.run_cli("generate", "-c", self.config_file(targets=["native", "compose"]), "-o", str(out_dir))
        self.assertEqual(rc, 0)
        self.assertIn("bundle written", out)
        self.assertTrue((out_dir / "native/install.sh").is_file())
        self.assertTrue((out_dir / "compose/docker-compose.yml").is_file())

    def test_configuration_errors_exit_2_with_a_clear_message(self):
        rc, _, err = self.run_cli("generate", "-c", self.config_file(postgres={"port": 99999}), "-o", str(self.tmp / "o"))
        self.assertEqual(rc, 2)
        self.assertIn("configuration error", err)
        self.assertIn("postgres.port", err)
        bad = self.tmp / "bad.json"
        bad.write_text("{nope")
        rc, _, err = self.run_cli("generate", "-c", str(bad), "-o", str(self.tmp / "o"))
        self.assertEqual(rc, 2)
        self.assertNotIn("Traceback", err)
        rc, _, err = self.run_cli("generate", "-c", str(self.tmp / "missing.json"), "-o", str(self.tmp / "o"))
        self.assertEqual(rc, 2)
        big = self.tmp / "big.json"
        big.write_text(" " * (cli.MAX_CONFIG_BYTES + 1))
        rc, _, err = self.run_cli("generate", "-c", str(big), "-o", str(self.tmp / "o"))
        self.assertEqual(rc, 2)
        arr = self.tmp / "arr.json"
        arr.write_text("[1,2]")
        self.assertEqual(self.run_cli("generate", "-c", str(arr), "-o", str(self.tmp / "o"))[0], 2)

    def test_argparse_rejects_unknown_commands_and_components(self):
        self.assertEqual(self.run_cli("explode")[0], 2)
        self.assertEqual(self.run_cli("apply", "nonsense")[0], 2)
        self.assertEqual(self.run_cli("logs", "nonsense")[0], 2)

    def test_settings_show_and_change(self):
        rc, out, _ = self.run_cli("settings")
        self.assertIn("lang=auto", out)
        rc, out, _ = self.run_cli("settings", "--lang", "en", "--theme", "dark")
        self.assertIn("theme=dark", out)
        self.assertEqual(settings.load(), {"lang": "en", "theme": "dark"})
        self.assertEqual(self.run_cli("settings", "--theme", "blue")[0], 2)

    def test_save_whole_project_and_one_component(self):
        cfgf = self.config_file(project="saved1", postgres={"db": "mydb"})
        rc, out, _ = self.run_cli("save", "-c", cfgf)
        self.assertEqual(rc, 0)
        self.assertIn("saved:", out)
        rc, out, _ = self.run_cli("save", "-c", self.config_file(project="saved2", postgres={"db": "d2"}), "--component", "postgis")
        self.assertEqual(rc, 0)
        self.assertEqual(projects.load("saved2")["postgres"]["db"], "d2")
        self.assertNotIn("mapproxy", projects.load("saved2"))

    def test_ssh_mode_needs_a_host(self):
        rc, _, err = self.run_cli("status", "-c", self.config_file(), "--mode", "ssh")
        self.assertEqual(rc, 2)
        self.assertIn("--ssh", err)

    def test_desktop_stops_when_tkinter_is_declined(self):
        with mock.patch.object(deps, "ensure_tk", return_value=False):
            self.assertEqual(self.run_cli("desktop")[0], 1)

    def test_web_command_starts_the_server_function(self):
        try:
            import gisstack.webgui.app  # noqa: F401  (must be imported before it can be patched)
        except ImportError:
            self.skipTest("flask not installed")
        with mock.patch("gisstack.webgui.app.main") as m:
            rc, _, _ = self.run_cli("web", "--port", "9999")
        self.assertEqual(rc, 0)
        m.assert_called_once_with("127.0.0.1", 9999, "werkzeug", (), False)


class CliOperationsTests(CliBase):
    """Commands that talk to servers, with the collaborators replaced."""

    def setUp(self):
        super().setUp()
        p = mock.patch("gisstack.preflight.run", return_value={"ok": True, "checks": [], "target": "native", "facts": {}})   # no network here
        p.start()
        self.addCleanup(p.stop)

    def rows(self, ok=True):
        row = {"id": "caddy", "name": "Caddy", "state": "running" if ok else "failed", "detail": "", "enabled": True, "pid": 1,
               "since": "", "restarts": 0, "listen": ["0.0.0.0:443"], "urls": [], "checks": [], "ok": ok, "unit": "caddy.service"}
        return {"target": "native", "project": "p", "domain": "d", "time": "00:00:00", "took": 0.1, "rows": [row]}

    def test_status_table_json_and_exit_codes(self):
        cf = self.config_file()
        with mock.patch.object(ops, "status", return_value=self.rows(True)) as st:
            rc, out, _ = self.run_cli("status", "-c", cf)
            self.assertEqual(rc, 0)
            self.assertIn("caddy", out)
            self.assertEqual(st.call_args[0][1], "local")
            rc, out, _ = self.run_cli("status", "-c", cf, "--json")
            self.assertEqual(json.loads(out)["rows"][0]["id"], "caddy")
        with mock.patch.object(ops, "status", return_value=self.rows(False)):
            self.assertEqual(self.run_cli("status", "-c", cf)[0], 1)         # unhealthy -> non-zero (monitoring friendly)
        with mock.patch.object(ops, "status", side_effect=deploy.DeployError("boom")):
            rc, _, err = self.run_cli("status", "-c", cf)
            self.assertEqual(rc, 1)
            self.assertIn("boom", err)

    def test_start_stop_restart_enable_disable_and_logs(self):
        cf = self.config_file()
        calls = []
        with mock.patch.object(ops, "control", side_effect=lambda *a, **k: iter(calls.append(a) or ["== ok"])):
            for action in ("start", "stop", "restart", "enable", "disable"):
                rc, out, _ = self.run_cli(action, "-c", cf, "caddy")
                self.assertEqual(rc, 0, action)
                self.assertIn("== ok", out)
        self.assertEqual([c[3] for c in calls], ["start", "stop", "restart", "enable", "disable"])
        with mock.patch.object(ops, "logs", return_value=iter(["line1", "line2"])):
            rc, out, _ = self.run_cli("logs", "caddy", "-c", cf, "-n", "5")
        self.assertEqual((rc, out.split()), (0, ["line1", "line2"]))
        with mock.patch.object(ops, "control", side_effect=deploy.DeployError("nope")):
            self.assertEqual(self.run_cli("start", "-c", cf)[0], 1)

    def test_scan_exit_code_reflects_conflicts(self):
        cf = self.config_file()
        rep = {"os": "x", "target": "native", "components": {}, "web_servers": [], "conflicts": []}
        with mock.patch.object(discover, "scan", return_value=rep):
            self.assertEqual(self.run_cli("scan", "-c", cf)[0], 0)
            self.assertIn("x", self.run_cli("scan", "-c", cf, "--json")[1])
        rep2 = dict(rep, conflicts=[{"component": "caddy", "port": 80, "addr": "0.0.0.0:80", "process": "nginx", "pid": 1}])
        with mock.patch.object(discover, "scan", return_value=rep2):
            self.assertEqual(self.run_cli("scan", "-c", cf)[0], 3)
        with mock.patch.object(discover, "scan", side_effect=deploy.DeployError("down")):
            self.assertEqual(self.run_cli("scan", "-c", cf)[0], 1)

    def test_clean_asks_for_confirmation_and_passes_options(self):
        cf = self.config_file(deploy_target="native", targets=["native"])
        with mock.patch.object(deploy, "clean", return_value=iter(["cleaned"])) as cl:
            rc, out, _ = self.run_cli("clean", "-c", cf, "--only", "caddy,tomcat", "--purge", "-y")
        self.assertEqual(rc, 0)
        self.assertIn("cleaned", out)
        args = cl.call_args[0]
        self.assertEqual((args[5], args[6]), (["caddy", "tomcat"], True))
        with mock.patch.object(deploy, "clean", return_value=iter([])) as cl:
            rc, out, _ = self.run_cli("clean", "-c", cf, "--purge", stdin="no")
        self.assertEqual(rc, 1)
        cl.assert_not_called()
        with mock.patch.object(deploy, "clean", side_effect=deploy.DeployError("bad")):
            self.assertEqual(self.run_cli("clean", "-c", cf, "-y")[0], 1)

    def test_apply_and_save_flag(self):
        cf = self.config_file(project="appl", deploy_target="native", targets=["native"])
        with mock.patch.object(deploy, "apply_component", return_value=iter(["applied"])) as ap:
            rc, out, _ = self.run_cli("apply", "mapproxy", "-c", cf, "--save")
        self.assertEqual(rc, 0)
        self.assertIn("applied", out)
        self.assertEqual(ap.call_args[0][5], "mapproxy")
        self.assertIn("mapproxy", projects.load("appl"))
        with mock.patch.object(deploy, "apply_component", side_effect=deploy.DeployError("x")):
            self.assertEqual(self.run_cli("apply", "caddy", "-c", cf)[0], 1)

    def test_deploy_without_a_plan_refuses_on_port_conflicts_and_runs_otherwise(self):
        cf = self.config_file(deploy_target="native", targets=["native"])
        conflict = {"os": "x", "target": "native", "components": {}, "web_servers": [],
                    "conflicts": [{"component": "caddy", "port": 80, "addr": "a", "process": "nginx", "pid": 1}]}
        with mock.patch.object(discover, "scan", return_value=conflict), mock.patch.object(deploy, "deploy") as d:
            rc, _, err = self.run_cli("deploy", "-c", cf)
        self.assertEqual(rc, 3)
        self.assertIn("--existing", err)
        d.assert_not_called()
        with mock.patch.object(discover, "scan", return_value=dict(conflict, conflicts=[])), \
                mock.patch.object(deploy, "deploy", return_value=iter(["DEPLOYED"])):
            rc, out, _ = self.run_cli("deploy", "-c", cf, "-o", str(self.tmp / "b"))
        self.assertEqual(rc, 0)
        self.assertIn("DEPLOYED", out)
        self.assertTrue((self.tmp / "b/native/install.sh").is_file())
        # non-native targets are not scanned
        cf2 = self.config_file(deploy_target="compose", targets=["compose"])
        with mock.patch.object(discover, "scan", side_effect=AssertionError("must not scan")), \
                mock.patch.object(deploy, "deploy", return_value=iter(["OK"])):
            self.assertEqual(self.run_cli("deploy", "-c", cf2, "-o", str(self.tmp / "b2"))[0], 0)

    def test_deploy_with_a_plan_uses_the_existing_flow(self):
        cf = self.config_file(deploy_target="native", targets=["native"])
        seen = {}

        def fake_run(cfg, target, mode, ssh, plan, make, **kw):
            seen.update(plan=plan, kw=kw)
            make(cfg)
            return iter(["PLANNED"])
        with mock.patch.object(existing, "run", side_effect=fake_run):
            rc, out, _ = self.run_cli("deploy", "-c", cf, "-o", str(self.tmp / "bp"), "--existing", "postgis=adopt,nginx=disable",
                                      "--with-data", "--confirm-data-loss")
        self.assertEqual(rc, 0)
        self.assertEqual(seen["plan"]["postgis"]["action"], "adopt")
        self.assertTrue(seen["kw"]["confirm_data_loss"] and seen["kw"]["with_data"])
        self.assertTrue((self.tmp / "bp/native/install.sh").is_file())
        rc, _, err = self.run_cli("deploy", "-c", cf, "--existing", "tomcat=explode")
        self.assertEqual(rc, 1)

    def test_existing_backup_and_remove(self):
        cf = self.config_file(deploy_target="native", targets=["native"])
        rep = {"components": {"postgis": {"databases": []}}, "family": "deb", "pkgs_all": []}
        with mock.patch.object(discover, "scan", return_value=rep), \
                mock.patch.object(transport, "run", return_value="BACKUP_VERIFIED=/x\n"), \
                mock.patch.object(existing, "fetch_backup", return_value=Path("/tmp/x.tar.gz")), \
                mock.patch.object(existing, "verify_backup_archive", return_value=3):
            rc, out, _ = self.run_cli("existing", "backup", "postgis", "-c", cf)
        self.assertEqual(rc, 0)
        self.assertIn("SHA-256", out)
        with mock.patch.object(discover, "scan", return_value=rep), mock.patch.object(transport, "run", return_value="BACKUP_FAILED\n"):
            rc, _, err = self.run_cli("existing", "backup", "postgis", "-c", cf)
        self.assertEqual(rc, 1)
        with mock.patch.object(existing, "run", return_value=iter(["REMOVED"])) as run:
            rc, out, _ = self.run_cli("existing", "remove", "tomcat", "-c", cf, "-y")
        self.assertEqual(rc, 0)
        self.assertTrue(run.call_args[1]["confirm_data_loss"])
        with mock.patch.object(existing, "run") as run:
            rc, _, _ = self.run_cli("existing", "remove", "tomcat", "--purge", "-c", cf, stdin="no")
        self.assertEqual(rc, 1)
        run.assert_not_called()

    def test_update_command_paths(self):
        info = {"current": "0.1.0", "latest": "0.2.0", "published": "2026-01-01", "install_kind": "venv", "newer": True,
                "name": "v0.2.0", "url": "https://example.invalid", "notes": "- a\n- b", "assets": [], "sdist": {"name": "x.tar.gz", "url": "u"},
                "package": None, "sums": {"name": "SHA256SUMS", "url": "u"}, "can_auto": True}
        with mock.patch.object(updater, "check", return_value=dict(info, newer=False)):
            rc, out, _ = self.run_cli("update")
            self.assertEqual(rc, 0)
            self.assertIn("0.1.0", out)
        with mock.patch.object(updater, "check", return_value=info):
            self.assertEqual(self.run_cli("update", "--check")[0], 10)
            with mock.patch.object(updater, "download", return_value=Path("/tmp/x.tar.gz")):
                rc, out, _ = self.run_cli("update", "--download", str(self.tmp))
            self.assertEqual(rc, 0)
            with mock.patch.object(updater, "apply", return_value=iter(["upgraded"])):
                rc, out, _ = self.run_cli("update", "-y")
            self.assertEqual(rc, 0)
            self.assertIn("upgraded", out)
            self.assertEqual(self.run_cli("update", stdin="no")[0], 1)
        with mock.patch.object(updater, "check", return_value=dict(info, can_auto=False, install_kind="package")):
            self.assertEqual(self.run_cli("update", "-y")[0], 1)
        with mock.patch.object(updater, "check", side_effect=updater.UpdateError("offline")):
            rc, _, err = self.run_cli("update")
            self.assertEqual(rc, 1)
            self.assertIn("offline", err)


class RunnerTests(unittest.TestCase):
    """deploy.py: SSH option validation, preflight, streaming and local/SSH execution with stub tools."""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.td, ignore_errors=True))
        self.bin = self.td / "bin"
        self.bin.mkdir()
        self.log = self.td / "calls.log"
        env = mock.patch.dict(os.environ, {"PATH": f"{self.bin}:{os.environ['PATH']}", "GISSTACK_HOME": str(self.td / "home")})
        env.start()
        self.addCleanup(env.stop)
        for tool in ("docker", "podman", "kubectl"):       # preflight wants a container runtime; none is really used
            self.stub(tool, "exit 0")

    def stub(self, name, body):
        f = self.bin / name
        f.write_text("#!/bin/bash\n" + body + "\n")
        f.chmod(0o755)

    def bundle(self, target="compose", script="echo \"ARGS: $*\"; echo \"RUNTIME=${RUNTIME:-}\""):
        b = self.td / "bundle"
        (b / target).mkdir(parents=True, exist_ok=True)
        entry = providers.get(target).entry
        (b / target / entry).write_text("#!/bin/bash\n" + script + "\n")
        (b / target / "uninstall.sh").write_text("#!/bin/bash\necho \"UNINSTALL: $*\"\n")
        return b

    def test_ssh_option_validation(self):
        good = {"host": "srv.example.ge", "port": 2222, "user": "tester", "key": ""}
        base = deploy._ssh_base(good)
        self.assertIn("ServerAliveInterval=15", base)               # S-08: a dead connection must not hang an operation forever
        self.assertEqual(base[-1], "tester@srv.example.ge")
        self.assertIn("2222", base)
        self.assertIn("BatchMode=yes", base)
        for bad in ({"host": "a b"}, {"host": "x;rm"}, {"host": ""}, {"host": "h", "user": "Bad User"}, {"host": "h", "port": 70000},
                    {"host": "h", "port": 0}, {"host": "h", "port": "abc"}, {"host": "h", "key": "/no/such/key"}):
            with self.assertRaises(deploy.DeployError, msg=str(bad)):
                deploy._ssh_base({"user": "root", "port": 22, **bad})
        key = self.td / "id"
        key.write_text("k")
        self.assertIn(str(key), deploy._ssh_base({**good, "key": str(key)}))
        self.assertEqual(deploy._ssh_base({"host": "[::1]", "user": "root"})[-1], "root@[::1]")

    def test_preflight_by_target_and_mode(self):
        with mock.patch.object(os, "geteuid", return_value=1000):
            self.assertTrue(deploy.preflight("native", "local"))
        with mock.patch.object(os, "geteuid", return_value=0):
            self.assertEqual(deploy.preflight("native", "local"), [])
        with mock.patch.object(deploy.shutil, "which", return_value=None):
            self.assertTrue(deploy.preflight("k8s", "local"))
            self.assertTrue(deploy.preflight("compose", "local"))
            self.assertTrue(deploy.preflight("native", "ssh"))
        with mock.patch.object(deploy.shutil, "which", return_value="/bin/x"):
            self.assertEqual(deploy.preflight("k8s", "local"), [])
            self.assertEqual(deploy.preflight("compose", "ssh"), [])

    def test_stream_yields_lines_and_reports_failures(self):
        lines = list(deploy._stream(["bash", "-c", "echo one; echo two"]))
        self.assertEqual(lines[1:], ["one", "two"])
        with self.assertRaises(deploy.DeployError) as cm:
            list(deploy._stream(["bash", "-c", "echo partial; exit 7"]))
        self.assertIn("exit 7", str(cm.exception))
        with self.assertRaises(deploy.DeployError):
            list(deploy._stream(["definitely-not-a-command-xyz"]))

    def test_check_bundle_rejects_bad_targets_and_missing_dirs(self):
        with self.assertRaises(deploy.DeployError):
            deploy._check_bundle(self.td, "bogus", "local")
        with self.assertRaises(deploy.DeployError):
            deploy._check_bundle(self.td, "k8s", "local")

    def test_local_deploy_clean_and_apply_run_the_entry_points(self):
        b = self.bundle("compose")
        cfg = make_cfg(runtime="podman")
        out = list(deploy.deploy(cfg, b, "compose", "local"))
        self.assertTrue(any("RUNTIME=podman" in x for x in out))
        out = list(deploy.apply_component(cfg, b, "compose", "local", None, "mapproxy"))
        self.assertTrue(any("ARGS: only mapproxy" in x for x in out))
        out = list(deploy.clean(cfg, b, "compose", "local", None, ["caddy"], purge=True))
        self.assertTrue(any("ARGS: clean --purge caddy" in x for x in out))
        nb = self.bundle("native", "echo \"INSTALL: $*\"")
        out = list(deploy.clean(cfg, nb, "native", "local", None, ["tomcat"], purge=False, remove_postgres=False)) if os.geteuid() == 0 else None
        if out is not None:
            self.assertTrue(any("UNINSTALL" in x for x in out) or any("ARGS" in x for x in out))

    def test_deploy_refuses_unknown_modes(self):
        b = self.bundle("compose")
        with self.assertRaises(deploy.DeployError):
            list(deploy.deploy(make_cfg(), b, "compose", "carrier-pigeon"))

    def test_ssh_deploy_uploads_the_bundle_then_runs_it_with_a_private_umask(self):
        # ssh stub: records its arguments; for the upload it unpacks the tar stream so we can check what was sent
        self.stub("ssh", f'echo "ssh $*" >> "{self.log}"\ncase "$*" in *"tar xzf"*) mkdir -p "{self.td}/remote" && tar xzf - -C "{self.td}/remote" ;; '
                         f'*) echo "REMOTE-OUTPUT" ;; esac')
        b = self.bundle("compose")
        cfg = make_cfg(project="remoteproj", runtime="docker")
        key = self.td / "key"
        key.write_text("k")
        ssh = {"host": "srv", "port": 22, "user": "deployer", "key": str(key)}
        out = list(deploy.deploy(cfg, b, "compose", "ssh", ssh))
        text = self.log.read_text()
        self.assertIn("umask 077 && mkdir -p gisstack-remoteproj && tar xzf - -C gisstack-remoteproj", text)
        self.assertRegex(text, r"cd gisstack-remoteproj/compose && chmod \+x up\.sh && RUNTIME=docker \$\(command -v flock[^\n]*gisstack-op\.lock'\) \./up\.sh")
        self.assertNotIn("sudo", text.split("RUNTIME=docker")[1].split("./up.sh")[0])                 # compose: no sudo wrapper, but locked
        self.assertIn("REMOTE-OUTPUT", "\n".join(out))
        self.assertTrue((self.td / "remote/compose/up.sh").is_file())
        self.log.write_text("")
        list(deploy.clean(cfg, self.bundle("native", "echo hi"), "native", "ssh", ssh, ["caddy"], purge=True))
        self.assertRegex(self.log.read_text(), r"sudo -n \$\(command -v flock[^\n]*\) \./uninstall\.sh --only caddy --purge")             # non-root user -> sudo -n

    def test_ssh_upload_failure_is_reported(self):
        self.stub("ssh", 'cat >/dev/null; echo "Permission denied" >&2; exit 255')
        with self.assertRaises(deploy.DeployError) as cm:
            list(deploy.deploy(make_cfg(), self.bundle("compose"), "compose", "ssh", {"host": "srv", "port": 22, "user": "root"}))
        self.assertIn("Permission denied", str(cm.exception))

    def test_home_dir_is_private_and_honours_the_environment(self):
        h = deploy.home_dir()
        self.assertEqual(stat.S_IMODE(h.stat().st_mode), 0o700)
        self.assertEqual(str(h), str(self.td / "home"))

    def test_argument_builders(self):
        self.assertEqual(deploy.clean_args("native", ["qgis"], purge=True), ["--only", "qgis", "--purge"])
        self.assertEqual(deploy.apply_args("k8s", "webmap"), ["only", "caddy"])
        self.assertEqual(providers.get("compose").clean_entry, "up.sh")
        self.assertEqual(providers.get("native").clean_entry, "uninstall.sh")


if __name__ == "__main__":
    unittest.main()
