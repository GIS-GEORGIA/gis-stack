"""Browser end-to-end test with Playwright (Phase 11b).  Opt-in: GISSTACK_TEST_E2E=1 and `pip install playwright && playwright install chromium`.

Real `gisstack web` process, real Chromium, temp GISSTACK_HOME.  Flow: authentication (no token / wrong token / right token) -> configure -> preview the
generated files -> download the bundle (the "deploy to a file" mode; a real deployment is covered by tests/integration) -> dashboard pages for services and
backups -> language switch.  Nothing here installs software or touches a server."""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import threading
import zipfile
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
    HAVE_PW = True
except ImportError:
    HAVE_PW = False

ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(os.environ.get("GISSTACK_TEST_E2E") == "1" and HAVE_PW, "GISSTACK_TEST_E2E=1 and playwright needed")
class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp()
        cls.port = _free_port()
        env = {**os.environ, "GISSTACK_HOME": cls.home, "GISSTACK_FEATURES": "dashboard,monitoring", "PYTHONPATH": str(ROOT)}
        cls.proc = subprocess.Popen([sys.executable, "-m", "gisstack", "web", "--port", str(cls.port)], cwd=ROOT, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        cls.url = ""
        deadline = time.time() + 30
        while time.time() < deadline and not cls.url:
            line = cls.proc.stdout.readline()
            m = re.search(r"open:\s+(http://\S+)", line)
            if m:
                cls.url = m.group(1)
        if not cls.url:
            cls.proc.kill()
            raise RuntimeError("gisstack web did not print its URL")
        cls.base = f"http://127.0.0.1:{cls.port}"
        for _ in range(50):                                   # wait until it answers
            try:
                urllib.request.urlopen(cls.base + "/", timeout=1)
            except urllib.error.HTTPError:
                break
            except OSError:
                time.sleep(0.2)
            else:
                break
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.proc.terminate()
        try:
            cls.proc.wait(5)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        if cls.proc.stdout:
            cls.proc.stdout.close()
        shutil.rmtree(cls.home, ignore_errors=True)

    def page(self):
        ctx = self.browser.new_context(accept_downloads=True, locale="en-US")
        self.addCleanup(ctx.close)
        return ctx.new_page()

    def watched(self):
        """A page whose console errors and uncaught exceptions are collected (a clean page has none)."""
        page = self.page()
        self.errors = []
        page.on("console", lambda m: self.errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        return page

    def dashboard(self, page, route):
        """Open a dashboard page; the first visit asks where to look (this computer is the default): accept it."""
        page.goto(self.base + "/dashboard#/" + route)
        page.wait_for_selector("#dlg[open]")
        page.click("#dlg button.primary")
        page.wait_for_selector("#dlg:not([open])", state="attached")
        page.wait_for_load_state("networkidle")
        page.wait_for_selector("h1")
        page.click("button[data-lang='en']")                  # the language is stored on the server: never depend on what an earlier test chose
        page.wait_for_timeout(400)

    def test_1_authentication(self):
        page = self.page()
        r = page.goto(self.base + "/api/v1/meta")
        self.assertEqual(r.status, 401)                       # no token, no data
        r = page.goto(self.base + "/?token=wrong")
        self.assertIn(r.status, (401, 403))
        r = page.goto(self.url)
        self.assertEqual(r.status, 200)
        page.wait_for_selector("#project")

    def test_2_configure_preview_download(self):
        page = self.page()
        page.goto(self.url)
        page.wait_for_selector("#project")
        page.fill("#project", "e2e_demo")
        page.fill("#domain", "gis.example.org")
        page.click("#btn-preview")
        page.wait_for_selector("#filesel option", state="attached")
        names = page.eval_on_selector_all("#filesel option", "els => els.map(e => e.textContent)")
        self.assertTrue(any(n.endswith("install.sh") or n.endswith("docker-compose.yml") or n.endswith("stack.env") for n in names), names)
        self.assertNotEqual(page.inner_text("#view").strip(), "")
        self.assertNotIn("PRIVATE KEY", page.inner_text("#view"))
        page.select_option("#mode", "download")
        with page.expect_download() as dl:
            page.click("#btn-deploy")
        path = dl.value.path()
        with zipfile.ZipFile(path) as z:
            self.assertTrue(any(n.endswith("install.sh") or n.endswith("docker-compose.yml") for n in z.namelist()), z.namelist()[:10])

    def test_3_dashboard_services_and_backups(self):
        page = self.page()
        page.goto(self.url)
        page.wait_for_selector("#project")
        self.dashboard(page, "infra")
        self.assertEqual(page.locator("h1").first.inner_text().strip(), "Infrastructure")
        page.goto(self.base + "/dashboard#/backups")
        page.wait_for_selector("h1")
        self.assertEqual(page.locator("h1").first.inner_text().strip(), "Backups")
        self.assertEqual(page.locator("#toasts .toast.bad").count(), 0)

    def test_4_language_switch(self):
        page = self.page()
        page.goto(self.url)
        page.wait_for_selector("#project")
        self.dashboard(page, "settings")
        page.click("button[data-lang='ka']")
        page.wait_for_selector("h1:not(:has-text('Settings'))")
        self.assertRegex(page.locator("h1").first.inner_text(), r"[\u10A0-\u10FF]")


    def test_5_security_page(self):
        page = self.watched()
        page.goto(self.url)
        page.wait_for_selector("#project")
        self.dashboard(page, "security")
        self.assertEqual(page.locator("h1").first.inner_text().strip(), "Security")
        page.wait_for_selector("table tbody tr")
        text = page.inner_text("#main")
        self.assertIn("gis-stack data directory", text)
        self.assertIn("In order", text)                                   # the temp GISSTACK_HOME is owner-only
        self.assertNotIn("PRIVATE KEY", text)
        page.select_option("#sec-filter", "servers.")                    # the filter reloads the audit table without errors
        page.wait_for_load_state("networkidle")
        page.click("button[data-lang='ka']")
        page.wait_for_selector("h1:not(:has-text('Security'))")
        self.assertRegex(page.locator("h1").first.inner_text(), r"[\u10A0-\u10FF]")
        self.assertEqual(self.errors, [])

    def rules_row(self, page, rid):
        return page.locator("#main tbody tr", has_text=rid)

    def dialog_save(self, page):
        page.click("#dlg button:has-text('Save')")

    def test_6_alert_rules_manager(self):
        page = self.watched()
        page.goto(self.url)
        page.wait_for_selector("#project")
        self.dashboard(page, "rules")
        self.assertEqual(page.locator("h1").first.inner_text().strip(), "Alert rules")
        page.wait_for_selector("#main tbody tr")
        self.assertGreaterEqual(self.rules_row(page, "cpu_high").count(), 1)       # built-in rules are listed
        self.assertEqual(self.rules_row(page, "cpu_high").locator("button:has-text('Delete')").count(), 0)   # ...and cannot be deleted

        # create: a refused rule keeps the dialog open with the reason, a valid one appears in the table
        page.click("button:has-text('New rule')")
        page.fill("#rl-id", "e2e_cpu")
        page.fill("#rl-th", "55")
        page.fill("#rl-text", "{value.__class__}")
        self.dialog_save(page)
        page.wait_for_selector("#dlg .error-box")
        self.assertIn("text may use only", page.inner_text("#dlg .error-box"))
        page.fill("#rl-text", "cpu {value:.0f}% on {subject}")
        self.dialog_save(page)
        page.wait_for_selector("#main tbody tr:has-text('e2e_cpu')")
        row = self.rules_row(page, "e2e_cpu")
        self.assertIn("Custom", row.inner_text())
        self.assertIn("55", row.inner_text())
        self.assertIn("Enabled", row.inner_text())

        # disable / enable
        row.locator("button:has-text('Disable')").click()
        page.wait_for_selector("#main tbody tr:has-text('e2e_cpu') >> text=Disabled")
        self.rules_row(page, "e2e_cpu").locator("button:has-text('Enable')").click()
        page.wait_for_selector("#main tbody tr:has-text('e2e_cpu') >> text=Enabled")

        # edit a built-in rule, then reset it to the default
        self.rules_row(page, "cpu_high").locator("button:has-text('Edit')").click()
        page.fill("#rl-th", "70")
        self.dialog_save(page)
        page.wait_for_selector("#main tbody tr:has-text('cpu_high') >> text=Changed")
        self.assertIn("70", self.rules_row(page, "cpu_high").inner_text())
        self.rules_row(page, "cpu_high").locator("button:has-text('Default')").click()
        page.click("#dlg button:has-text('Roll back'), #dlg button.primary")
        page.wait_for_selector("#main tbody tr:has-text('cpu_high') >> text=Changed", state="detached")      # the table was re-rendered
        self.assertNotIn("Changed", self.rules_row(page, "cpu_high").inner_text())

        # language switch keeps the page working
        page.click("button[data-lang='ka']")
        page.wait_for_selector("h1:not(:has-text('Alert rules'))")
        self.assertRegex(page.locator("h1").first.inner_text(), r"[\u10A0-\u10FF]")
        page.click("button[data-lang='en']")
        page.wait_for_selector("h1:has-text('Alert rules')")

        # responsive: no horizontal page scroll on a phone-sized viewport
        page.set_viewport_size({"width": 375, "height": 812})
        page.wait_for_timeout(300)
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 376)
        page.set_viewport_size({"width": 1280, "height": 800})

        # delete needs a confirmation: Cancel keeps the rule, Delete removes it
        self.rules_row(page, "e2e_cpu").locator("button:has-text('Delete')").click()
        page.click("#dlg button:has-text('Cancel')")
        self.assertEqual(self.rules_row(page, "e2e_cpu").count(), 1)
        self.rules_row(page, "e2e_cpu").locator("button:has-text('Delete')").click()
        page.click("#dlg button.danger")
        page.wait_for_selector("#main tbody tr:has-text('e2e_cpu')", state="detached")
        refused = [e for e in self.errors if "status of 400" in e]                # the one deliberately invalid rule above, logged by the browser
        self.assertEqual(len(refused), 1, self.errors)
        self.assertEqual([e for e in self.errors if e not in refused], [])


    def serve_webmap(self):
        """The generated web map, served as a static site (tiles come from MapProxy/GeoServer, which are not running: tile requests 404)."""
        from gisstack import config, generate
        home = tempfile.mkdtemp()
        out = Path(home) / "bundle"
        cfg = config.resolve({"project": "e2emap", "targets": ["compose"], "deploy_target": "compose"}, persist_secrets=False, generate_secrets=True)
        generate.generate_bundle(cfg, out)
        class Quiet(SimpleHTTPRequestHandler):
            def log_message(self, *a, **k):
                pass
        handler = partial(Quiet, directory=str(out / "webmap"))
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close(), shutil.rmtree(home, ignore_errors=True)))
        return f"http://127.0.0.1:{srv.server_address[1]}/"

    def test_7_web_map(self):
        url = self.serve_webmap()
        page = self.watched()
        page.goto(url)
        page.wait_for_selector(".leaflet-container")
        self.assertEqual(page.locator(".leaflet-control-scale").count(), 1)               # scale bar
        self.assertEqual(page.locator(".leaflet-control-layers").count(), 1)              # base map / layer control
        self.assertEqual(page.locator(".leaflet-control-zoom").count(), 1)
        self.assertEqual(page.locator(".leaflet-control-reset").count(), 1)
        self.assertEqual(page.locator("#map .leaflet-tile-pane").count(), 1)
        box = page.locator("#map").bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)        # coordinates follow the pointer
        page.wait_for_timeout(200)
        self.assertRegex(page.inner_text("#coords"), r"-?\d+\.\d{5}, -?\d+\.\d{5}")
        page.click("#lang button[data-lang='en']")                                         # language switch relabels the page and the layer control
        page.wait_for_timeout(200)
        self.assertEqual(page.evaluate("document.documentElement.lang"), "en")
        page.hover(".leaflet-control-layers")
        self.assertIn("Base map", page.inner_text(".leaflet-control-layers"))      # the control is relabelled in English
        page.click("#lang button[data-lang='ka']")
        page.wait_for_timeout(200)
        self.assertEqual(page.evaluate("document.documentElement.lang"), "ka")
        page.set_viewport_size({"width": 375, "height": 812})
        page.wait_for_timeout(300)
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 376)
        # only failed tile loads (nothing serves /mapproxy here) may be logged; no script error
        self.assertEqual([e for e in self.errors if "Failed to load resource" not in e], [])


if __name__ == "__main__":
    unittest.main()
