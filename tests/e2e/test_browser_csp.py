"""The classic page `/` in REAL Chromium under the strict CSP.  Opt-in: GISSTACK_TEST_E2E=1.

Proof that moving the page's code into /static/classic/ lost nothing: every `securitypolicyviolation` event, console error and uncaught exception is
collected while the page's flows run (theme boot, language, settings dialog, the SSH box, preview, the status table, the update box), and the styles
that used to be inline are checked as COMPUTED styles."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
    HAVE_PW = True
except ImportError:
    HAVE_PW = False

ROOT = Path(__file__).resolve().parents[2]
CSP_SPY = """
window.__csp = [];
document.addEventListener("securitypolicyviolation", (e) => window.__csp.push(e.violatedDirective + " " + (e.blockedURI || "inline") + " " + (e.sample || "")));
"""
ROW = {"id": "postgis", "name": "PostGIS", "unit": "postgis", "state": "active", "detail": "running", "since": "", "enabled": True, "pid": 1, "restarts": 0,
       "listen": ["127.0.0.1:5432"], "checks": [{"ok": True, "label": "tcp 5432", "code": "open"}], "urls": []}


@unittest.skipUnless(os.environ.get("GISSTACK_TEST_E2E") == "1" and HAVE_PW, "GISSTACK_TEST_E2E=1 and playwright needed")
class ClassicPageCspTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp()
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            cls.port = s.getsockname()[1]
        env = {**os.environ, "GISSTACK_HOME": cls.home, "GISSTACK_PRINT_TOKEN": "1", "PYTHONPATH": str(ROOT)}
        cls.proc = subprocess.Popen([sys.executable, "-m", "gisstack", "web", "--port", str(cls.port)], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        cls.url = ""
        deadline = time.time() + 30
        while time.time() < deadline and not cls.url:
            m = re.search(r"open:\s+(http://\S+)", cls.proc.stdout.readline())
            if m:
                cls.url = m.group(1)
        if not cls.url:
            cls.proc.kill()
            raise RuntimeError("gisstack web did not print its URL")
        cls.base = f"http://127.0.0.1:{cls.port}"
        for _ in range(50):
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

    def watched(self):
        ctx = self.browser.new_context(locale="en-US")
        self.addCleanup(ctx.close)
        ctx.add_init_script(CSP_SPY)
        page = ctx.new_page()
        self.errors, self.failed = [], []
        page.on("console", lambda m: self.errors.append(m.text) if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.on("requestfailed", lambda r: self.failed.append(f"{r.method} {r.url}"))
        page.on("response", lambda r: self.failed.append(f"{r.status} {r.url}") if r.status >= 400 else None)
        return page

    def violations(self, page):
        return page.evaluate("window.__csp")

    def test_page_and_every_flow_run_without_a_single_csp_violation(self):
        page = self.watched()
        resp = page.goto(self.url)
        csp = resp.headers.get("content-security-policy", "")
        self.assertNotIn("unsafe-inline", csp)
        page.wait_for_selector("#project")
        page.wait_for_load_state("networkidle")
        # boot.js applied a theme before first paint; app.css is in effect
        self.assertIn(page.evaluate("document.documentElement.getAttribute('data-theme')"), ("light", "dark"))
        self.assertGreater(page.evaluate("getComputedStyle(document.querySelector('header')).paddingLeft.length"), 0)
        # language + theme + settings dialog
        page.click("#langseg button[data-lang='en']")
        page.wait_for_timeout(500)
        page.click("#btn-theme")
        page.wait_for_timeout(400)
        page.click("#btn-set")
        page.wait_for_selector("#dlg-set[open]")
        page.click("#set-close")
        # the form no longer has an inline onsubmit: pressing Enter in a field must not reload the page
        page.fill("#project", "csp_demo")
        page.press("#project", "Enter")
        page.wait_for_timeout(300)
        self.assertEqual(page.input_value("#project"), "csp_demo")
        self.assertEqual(page.url.split("#")[0].rstrip("?"), self.base + "/")
        # classes that replaced style attributes are in effect (computed, not just present)
        self.assertEqual(page.evaluate("getComputedStyle(document.querySelector('#seed_from')).width"), "120px")   # `.f input {min-width:120px}` wins over width:60px, as it did inline
        self.assertEqual(page.evaluate("getComputedStyle(document.querySelector('#addlayer')).marginTop"), "6px")
        self.assertEqual(page.evaluate("getComputedStyle(document.querySelector('#btn-log-stop')).display"), "none")
        # SSH box is toggled by script (CSSOM, allowed)
        page.select_option("#mode", "ssh")
        self.assertEqual(page.evaluate("getComputedStyle(document.querySelector('#sshbox')).display"), "flex")
        page.select_option("#mode", "download")
        self.assertEqual(page.evaluate("getComputedStyle(document.querySelector('#sshbox')).display"), "none")
        # preview
        page.fill("#domain", "gis.example.org")
        page.click("#btn-preview")
        page.wait_for_selector("#filesel option", state="attached")
        # status table (HTML built by the script, with the nowrap class)
        page.route("**/api/ops/status", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(
            {"target": "compose", "domain": "gis.example.org", "time": "12:00:00", "took": 0.1, "rows": [ROW]})))
        page.select_option("#mode", "local")
        page.click("#btn-st-refresh")
        page.wait_for_selector("#st-body tr")
        self.assertEqual(page.evaluate("getComputedStyle(document.querySelector('#st-body td.nw')).whiteSpace"), "nowrap")
        # update box: every inline style the script used to emit is a class now
        page.route("**/api/update/check", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(
            {"newer": True, "current": "0.1.0", "latest": "0.2.0", "published": "2026-11-01", "notes": "n", "assets": [{"name": "a.deb", "url": "https://example.org/a.deb"}],
             "url": "https://example.org/r", "install_kind": "venv", "can_auto": True})))
        page.click("#btn-upd")
        page.wait_for_selector("#upd-apply")
        self.assertEqual(page.evaluate("getComputedStyle(document.querySelector('#upd-apply')).marginTop"), "4px")
        self.assertEqual(page.evaluate("getComputedStyle(document.querySelector('#upd-close')).marginLeft"), "10px")
        self.assertEqual(page.evaluate("getComputedStyle(document.querySelector('#updbox a.dl.bggray')).backgroundColor"), "rgb(91, 107, 122)")
        self.assertEqual(self.violations(page), [], "CSP violations")
        self.assertEqual([e for e in self.errors if "Content Security Policy" in e or "Refused to" in e], [])
        self.assertEqual(self.errors, [])
        self.assertEqual([f for f in self.failed if "/static/classic/" in f], [])

    def test_csp_really_blocks_injected_inline_code(self):
        """The policy is enforced, not merely declared: an inline script and an inline handler injected into the page do nothing and are reported."""
        page = self.watched()
        page.goto(self.url)
        page.wait_for_selector("#project")
        page.evaluate("""() => { window.__ran = false; const s = document.createElement('script'); s.textContent = 'window.__ran = true'; document.body.appendChild(s);
                         const d = document.createElement('div'); d.innerHTML = '<img src=x onerror="window.__ran = true">'; document.body.appendChild(d); }""")
        page.wait_for_timeout(500)
        self.assertFalse(page.evaluate("window.__ran"))
        self.assertTrue(any("script-src" in v for v in self.violations(page)), self.violations(page))


if __name__ == "__main__":
    unittest.main()
