"""Backups page for a Compose/Podman/Kubernetes stack in a REAL browser (Playwright).  Opt-in: GISSTACK_TEST_E2E=1.

The web server runs in this process with its provider swapped for the local stand-in (a directory instead of containers), so the whole UI flow -
create, details, verify, export, restore (typed confirmation, job log, health check), delete, KA/EN, phone layout, console errors - is exercised
against the real API and a real browser.  The container wiring itself is covered by tests/integration (real Compose and kind)."""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

try:
    from playwright.sync_api import sync_playwright
    HAVE_PW = True
except ImportError:
    HAVE_PW = False

from gisstack import deployments
from gisstack.backups import provider
from gisstack.backups.store import Store
from tests.test_stack_backup import LocalProvider


@unittest.skipUnless(os.environ.get("GISSTACK_TEST_E2E") == "1" and HAVE_PW, "GISSTACK_TEST_E2E=1 and playwright needed")
class StackBackupBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp()
        cls.vol = Path(tempfile.mkdtemp())
        (cls.vol / "global.xml").write_text("<global>v1</global>")
        cls.store = Store(Path(cls.home) / "sb")
        cls.patches = [
            mock.patch.dict(os.environ, {"GISSTACK_HOME": cls.home, "GISSTACK_FEATURES": "dashboard"}),
            mock.patch.object(provider, "provider_for", lambda cfg, mode, ssh, **kw: LocalProvider(cfg, cls.vol, store=cls.store, **kw)),
            mock.patch.object(deployments, "verify", return_value={"state": "healthy", "rows": [{"id": "geoserver", "ok": True}]}),
            mock.patch("gisstack.api.routes_stackbackup.local_problems", lambda *a, **k: None),
        ]
        for p in cls.patches:
            p.start()
        from werkzeug.serving import make_server

        from gisstack.webgui.app import create_app
        cls.app = create_app("e2etoken")
        cls.srv = make_server("127.0.0.1", 0, cls.app, threaded=True)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.srv.shutdown()
        for p in reversed(cls.patches):
            p.stop()
        shutil.rmtree(cls.home, ignore_errors=True)
        shutil.rmtree(cls.vol, ignore_errors=True)

    def open_backups(self):
        ctx = self.browser.new_context(accept_downloads=True, locale="en-US")
        self.addCleanup(ctx.close)
        page = ctx.new_page()
        self.errors = []
        page.on("console", lambda m: self.errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.goto(f"{self.base}/?token=e2etoken")
        page.wait_for_selector("#project")
        page.goto(self.base + "/dashboard#/backups")
        page.wait_for_selector("#dlg[open]")
        page.select_option("#c-target", "compose")                      # the stack under test is a Compose stack
        page.click("#dlg button.primary")
        page.wait_for_selector("#dlg:not([open])", state="attached")
        page.click("button[data-lang='en']")
        page.wait_for_selector("h1:has-text('Backups')")
        return page

    def test_backup_page_end_to_end(self):
        page = self.open_backups()
        page.wait_for_selector("text=There is no backup yet")
        # create: only the GeoServer data (the stand-in workload has no database)
        page.click("button:has-text('New backup') >> nth=0")
        page.wait_for_selector("#dlg[open]")
        for cb in page.locator("#dlg input[type=checkbox]").all():
            if cb.get_attribute("value") != "geoserver":
                cb.uncheck()
        page.fill("#sb-note", "browser test")
        page.click("#dlg button.primary")
        page.wait_for_selector("pre.log:has-text('sealed')")
        page.wait_for_selector("tbody tr")
        row = page.locator("tbody tr").first
        self.assertIn("Verified", row.inner_text())
        self.assertIn("geoserver", row.inner_text())
        bid = row.locator("th").inner_text().strip()
        self.assertRegex(bid, r"^\d{8}T\d{6}Z-[0-9a-f]{4}$")

        # details and verify
        row.locator("button:has-text('Details')").click()
        page.wait_for_selector("#dlg[open] >> text=browser test")
        self.assertIn("compose", page.inner_text("#dlg"))
        page.click("#dlg button.primary")
        page.wait_for_selector("#dlg:not([open])", state="attached")
        row.locator("button:has-text('Check')").click()
        page.wait_for_selector("#toasts .toast.ok")

        # export downloads a real tar of the backup
        with page.expect_download() as dl:
            row.locator("button:has-text('Download')").click()
        import tarfile
        with tarfile.open(dl.value.path()) as tf:
            self.assertIn(f"{bid}/manifest.json", tf.getnames())

        # restore: needs the typed confirmation; Cancel changes nothing
        (self.vol / "global.xml").write_text("<global>v2</global>")
        row.locator("button.danger:has-text('Restore')").click()
        page.wait_for_selector("#dlg[open] >> text=Type")
        page.click("#dlg button:has-text('Cancel')")
        self.assertEqual((self.vol / "global.xml").read_text(), "<global>v2</global>")
        row.locator("button.danger:has-text('Restore')").click()
        page.wait_for_selector("#dlg[open] #tc")
        okbtn = page.locator("#dlg button.danger")
        self.assertTrue(okbtn.is_disabled())                              # cannot be triggered by a stray Enter
        page.fill("#tc", "RESTORE")
        okbtn.click()
        page.wait_for_selector("pre.log:has-text('verified healthy')")
        self.assertEqual((self.vol / "global.xml").read_text(), "<global>v1</global>")
        page.wait_for_selector("tbody tr:has-text('Before restore')")     # the automatic pre-restore backup appears

        # language switch and phone layout
        page.click("button[data-lang='ka']")
        page.wait_for_selector("h1:not(:has-text('Backups'))")
        self.assertTrue(re.search(r"[Ⴀ-ჿ]", page.locator("h1").first.inner_text()))
        page.click("button[data-lang='en']")
        page.set_viewport_size({"width": 375, "height": 812})
        page.wait_for_timeout(300)
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 376)
        page.set_viewport_size({"width": 1280, "height": 800})

        # delete needs a typed confirmation, too
        page.locator("tbody tr", has_text="manual").locator("button.danger:has-text('Delete')").click()
        page.wait_for_selector("#dlg[open] #tc")
        page.fill("#tc", "DELETE")
        page.click("#dlg button.danger")
        page.wait_for_selector(f"tbody th:text-is('{bid}')", state="detached")
        self.assertFalse((Path(self.store.root) / bid).exists())
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
