"""Phase 2: the application services every UI shares - and the three UIs' use of them."""
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from gisstack import cli, config, deploy, discover, existing, preflight, services
from gisstack.core.errors import DeployError

GOOD = {"ok": True, "checks": [{"id": "os", "status": "ok", "message": "m", "detail": ""}], "target": "native", "facts": {}}
BAD = {"ok": False, "checks": [{"id": "arch", "status": "fail", "message": "m", "detail": "sparc"}], "target": "native", "facts": {}}
REPORT = {"conflicts": [{"port": 80}], "programs": []}


def cfg(target="native"):
    return config.resolve({"targets": [target], "deploy_target": target})


class ServiceTests(unittest.TestCase):
    def test_check_mode(self):
        self.assertEqual(services.check_mode("ssh"), "ssh")
        for bad in ("carrier-pigeon", None, ""):
            with self.assertRaises(DeployError):
                services.check_mode(bad)

    def test_ensure_ready(self):
        with mock.patch.object(preflight, "run", return_value=GOOD) as run:
            self.assertEqual(services.ensure_ready(cfg(), "local", None, "native"), GOOD)
            self.assertIsNone(services.ensure_ready(cfg(), "local", None, "native", skip_preflight=True))
            self.assertEqual(run.call_count, 1)
        with mock.patch.object(preflight, "run", return_value=BAD), self.assertRaises(services.PreflightFailed) as cm:
            services.ensure_ready(cfg(), "local", None, "native")
        self.assertEqual(cm.exception.result, BAD)
        self.assertEqual(cm.exception.code, "preflight")
        with mock.patch.object(preflight, "run", side_effect=DeployError("unreachable")), self.assertRaises(DeployError):
            services.ensure_ready(cfg(), "local", None, "native")

    def test_find_conflicts(self):
        with mock.patch.object(discover, "scan", return_value=REPORT) as scan:
            self.assertIsNone(services.find_conflicts(cfg("compose"), "local", None, "compose"))
            scan.assert_not_called()                                      # only native targets are scanned
            self.assertEqual(services.find_conflicts(cfg(), "local", None, "native"), REPORT)
        with mock.patch.object(discover, "scan", return_value={"conflicts": []}):
            self.assertIsNone(services.find_conflicts(cfg(), "local", None, "native"))
        with mock.patch.object(discover, "scan", side_effect=DeployError("no sudo")):
            self.assertIsNone(services.find_conflicts(cfg(), "local", None, "native"))        # cannot inspect: installer reports

    def test_deploy_steps_without_a_plan(self):
        with mock.patch.object(discover, "scan", return_value=REPORT), mock.patch.object(deploy, "deploy", return_value=iter(["ran"])) as dep:
            with self.assertRaises(services.Conflict) as cm:
                list(services.deploy_steps(cfg(), Path("/b"), "native", "local", None))
            self.assertEqual(cm.exception.report, REPORT)
            dep.assert_not_called()                                       # nothing is touched when a port is taken
            self.assertEqual(list(services.deploy_steps(cfg(), Path("/b"), "native", "local", None, refuse_conflicts=False)), ["ran"])
        with mock.patch.object(discover, "scan", return_value={"conflicts": []}), mock.patch.object(deploy, "deploy", return_value=iter(["ok"])):
            self.assertEqual(list(services.deploy_steps(cfg(), Path("/b"), "native", "local", None)), ["ok"])

    def test_deploy_steps_with_a_plan_uses_the_existing_flow(self):
        with mock.patch.object(existing, "run", return_value=iter(["a", "b"])) as run, mock.patch.object(discover, "scan") as scan:
            out = list(services.deploy_steps(cfg(), Path("/b"), "native", "ssh", {"host": "h"}, plan={"caddy": {"action": "remove", "purge": False}},
                                             with_data=True, confirm_data_loss=True))
        self.assertEqual(out, ["a", "b"])
        scan.assert_not_called()
        self.assertTrue(run.call_args.kwargs["with_data"] and run.call_args.kwargs["confirm_data_loss"])
        with mock.patch.object(existing, "run", return_value=iter([])) as run:
            list(services.deploy_steps(cfg(), Path("/b"), "native", "local", None, plan={}))            # an empty plan is still a plan
            run.assert_called_once()


class CliUsesServices(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        p = mock.patch.dict(os.environ, {"GISSTACK_HOME": self.td.name, "GISSTACK_LANG": "en"})
        p.start()
        self.addCleanup(p.stop)

    def run_cli(self, *a):
        o, e = io.StringIO(), io.StringIO()
        with redirect_stdout(o), redirect_stderr(e):
            return cli.main(list(a)), o.getvalue(), e.getvalue()

    def test_conflict_gives_exit_3_and_never_deploys(self):
        with mock.patch.object(preflight, "run", return_value=GOOD), mock.patch.object(discover, "scan", return_value={**REPORT, "found": [], "ports": []}), \
                mock.patch.object(discover, "format_report", return_value="REPORT"), mock.patch.object(deploy, "deploy") as dep:
            rc, _, err = self.run_cli("deploy", "--target", "native")
        self.assertEqual(rc, 3)
        self.assertIn("REPORT", err)
        dep.assert_not_called()

    def test_warnings_are_printed_but_do_not_stop_the_deploy(self):
        warn = {"ok": True, "checks": [{"id": "time", "status": "warn", "message": "clock off", "detail": ""}], "target": "compose", "facts": {}}
        with mock.patch.object(preflight, "run", return_value=warn), mock.patch.object(deploy, "deploy", return_value=iter(["ran"])):
            rc, out, err = self.run_cli("deploy", "--target", "compose")
        self.assertEqual(rc, 0)
        self.assertIn("clock off", err)
        self.assertIn("ran", out)


@unittest.skipUnless(__import__("importlib").util.find_spec("flask"), "flask needed")
class WebUsesServices(unittest.TestCase):
    def test_deploy_responses_carry_machine_readable_codes(self):
        from gisstack.webgui.app import create_app
        c = create_app("tok").test_client()
        h = {"Authorization": "Bearer tok", "X-GisStack": "1"}
        body = {"config": {"targets": ["native"], "deploy_target": "native"}, "mode": "local"}
        with mock.patch.object(deploy, "preflight", return_value=[]):
            with mock.patch.object(preflight, "run", return_value=BAD):
                r = c.post("/api/deploy", json=body, headers=h)
                self.assertEqual((r.status_code, r.get_json()["code"]), (409, "preflight"))
            with mock.patch.object(preflight, "run", side_effect=DeployError("down")):
                r = c.post("/api/deploy", json=body, headers=h)
                self.assertEqual((r.status_code, r.get_json()["code"]), (502, "deploy"))
            with mock.patch.object(preflight, "run", return_value=GOOD), mock.patch.object(discover, "scan", return_value=REPORT):
                r = c.post("/api/deploy", json=body, headers=h)
                self.assertEqual((r.status_code, r.get_json()["code"]), (409, "conflict"))
                self.assertTrue(r.get_json()["existing"]["conflicts"])


def _have_display():
    try:
        import tkinter
        tkinter.Tk().destroy()
        return True
    except Exception:  # noqa: BLE001
        return False


@unittest.skipUnless(_have_display(), "no display / tkinter")
class TkUsesServices(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        p = mock.patch.dict(os.environ, {"GISSTACK_HOME": self.td.name})
        p.start()
        self.addCleanup(p.stop)
        from gisstack import desktop, settings
        settings.save(lang="en", theme="light")
        desktop.LANG = settings.resolve_lang()
        self.app = desktop.App()
        self.addCleanup(self._dispose)
        self.app.after = lambda ms, fn=None, *a: fn() if fn else None          # run scheduled UI callbacks at once
        self.jobs = []
        self.app._run_job = lambda cfg, tag, steps: self.jobs.append(steps)
        self.app.resolved = lambda: cfg()
        self.app._exist_show = mock.Mock()
        self.app.nb = mock.Mock()
        self.app.vars["_mode"].set("local")

    def _dispose(self):
        self.app.destroy()
        self.app = None
        self.jobs = []
        __import__("gc").collect()                     # Tk variables must be finalised on the main thread, not by a GC run in some worker

    def start(self):
        with mock.patch("gisstack.desktop.messagebox.askyesno", return_value=True):
            self.app.do_deploy()
        self.assertEqual(len(self.jobs), 1)
        return self.jobs[0]

    def test_failed_preflight_is_shown_and_stops(self):
        steps = self.start()
        with mock.patch.object(preflight, "run", return_value=BAD), self.assertRaises(DeployError) as cm:
            got = []
            for line in steps(Path(self.td.name)):
                got.append(line)
        self.assertIn("sparc", "\n".join(got) + "")
        self.assertIn("მანქანა", str(cm.exception))

    def test_conflict_switches_to_the_existing_programs_tab(self):
        steps = self.start()
        with mock.patch.object(preflight, "run", return_value=GOOD), mock.patch.object(discover, "scan", return_value=REPORT), \
                mock.patch.object(deploy, "deploy") as dep, self.assertRaises(DeployError):
            list(steps(Path(self.td.name)))
        dep.assert_not_called()
        self.app._exist_show.assert_called_once()

    def test_clean_machine_deploys(self):
        steps = self.start()
        with mock.patch.object(preflight, "run", return_value=GOOD), mock.patch.object(discover, "scan", return_value={"conflicts": []}), \
                mock.patch.object(deploy, "deploy", return_value=iter(["installing"])):
            self.assertIn("installing", list(steps(Path(self.td.name))))


if __name__ == "__main__":
    unittest.main()


def _have_tk():
    try:
        import tkinter  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(_have_tk(), "tkinter needed")
class TkPatchingTests(unittest.TestCase):
    """Audit A-04: importing the desktop module changes nothing global; patching is explicit, idempotent and reversible."""

    def test_import_is_side_effect_free_and_install_uninstall_roundtrip(self):
        import tkinter as tk
        from tkinter import messagebox, ttk

        from gisstack import desktop
        desktop.uninstall_translation()
        stock = (ttk.Label, ttk.Notebook.add, tk.Wm.wm_title, messagebox.askyesno)
        self.assertTrue(desktop._wrap_text_widget(ttk.Label) is not ttk.Label)
        desktop.install_translation()
        patched = (ttk.Label, ttk.Notebook.add, tk.Wm.wm_title, messagebox.askyesno)
        self.assertTrue(all(a is not b for a, b in zip(stock, patched)))
        desktop.install_translation()                                                  # idempotent: no double wrapping
        self.assertEqual(patched, (ttk.Label, ttk.Notebook.add, tk.Wm.wm_title, messagebox.askyesno))
        desktop.uninstall_translation()
        self.assertEqual(stock, (ttk.Label, ttk.Notebook.add, tk.Wm.wm_title, messagebox.askyesno))
        desktop.install_translation()                                                  # leave the module as the other tests expect
