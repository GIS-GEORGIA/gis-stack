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
import zipfile
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
        env = {**os.environ, "GISSTACK_HOME": cls.home, "GISSTACK_FEATURES": "dashboard", "PYTHONPATH": str(ROOT)}
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

    def dashboard(self, page, route):
        """Open a dashboard page; the first visit asks where to look (this computer is the default): accept it."""
        page.goto(self.base + "/dashboard#/" + route)
        page.wait_for_selector("#dlg[open]")
        page.click("#dlg button.primary")
        page.wait_for_selector("#dlg:not([open])", state="attached")
        page.wait_for_load_state("networkidle")
        page.wait_for_selector("h1")

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


if __name__ == "__main__":
    unittest.main()
