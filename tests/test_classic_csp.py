"""The classic page `/` must run under the strict CSP (no 'unsafe-inline'): no inline script, style, style attribute or event-handler attribute in the
HTML, and none produced by its scripts.  The browser-level proof (zero CSP violations while using the page) is tests/e2e/test_browser_csp.py."""
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import flask  # noqa: F401
    HAVE_FLASK = True
except ImportError:
    HAVE_FLASK = False

WEB = Path(__file__).resolve().parents[1] / "gisstack" / "webgui"
HTML = (WEB / "templates" / "index.html").read_text(encoding="utf-8")
JS = (WEB / "static" / "classic" / "app.js").read_text(encoding="utf-8")
BOOT = (WEB / "static" / "classic" / "boot.js").read_text(encoding="utf-8")
CSS = (WEB / "static" / "classic" / "app.css").read_text(encoding="utf-8")


class SourceTests(unittest.TestCase):
    def test_html_has_no_inline_script_style_or_handler(self):
        self.assertEqual(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", HTML), [])                  # every script is an external file
        self.assertNotIn("<style", HTML)
        self.assertNotRegex(HTML, r"<[a-zA-Z][^<>]*\sstyle=")
        self.assertNotRegex(HTML, r"<[a-zA-Z][^<>]*\son[a-z]+=")
        self.assertNotIn("javascript:", HTML)

    def test_scripts_do_not_build_inline_styles_or_handlers(self):
        for name, text in (("app.js", JS), ("boot.js", BOOT)):
            self.assertNotRegex(text, r"\sstyle=[\"']", name)                                   # an HTML string with a style attribute would be blocked
            self.assertNotRegex(text, r"\son(click|change|submit|input|load|error)=[\"']", name)
            self.assertNotRegex(text, r"setAttribute\(\s*[\"']style[\"']", name)
            self.assertNotRegex(text, r"\beval\(|new Function\(|document\.write\(", name)
            self.assertNotIn("javascript:", text)

    def test_the_stylesheet_has_the_classes_that_replaced_the_style_attributes(self):
        for cls in (".sm", ".mt6", ".mt4", ".ml10", ".w60", ".ox", ".nw", ".nodisp", "#updbox a.dl.bggray"):
            self.assertIn(cls, CSS)

    def test_esc_escapes_all_five_html_specials_and_no_attribute_is_single_quoted(self):
        self.assertRegex(JS, r'const esc = .*replace\(/\[&<>"\x27\]/g')
        self.assertIn("&#39;", JS)
        self.assertNotRegex(JS, r"=\x27\$\{")                                                  # esc() output is only ever placed in double-quoted attributes or text

    def test_page_references_its_three_files(self):
        for ref in ('href="/static/classic/app.css"', 'src="/static/classic/boot.js"', 'src="/static/classic/app.js"'):
            self.assertIn(ref, HTML)


@unittest.skipUnless(HAVE_FLASK, "flask needed")
class ServedTests(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        p = mock.patch.dict(os.environ, {"GISSTACK_HOME": td.name})
        p.start()
        self.addCleanup(p.stop)
        from gisstack.webgui.app import create_app
        self.c = create_app("tok").test_client()
        self.h = {"Authorization": "Bearer tok"}

    def test_page_and_files_are_served_under_the_strict_policy(self):
        page = self.c.get("/", headers=self.h)
        self.assertEqual(page.status_code, 200)
        csp = page.headers["Content-Security-Policy"]
        self.assertNotIn("unsafe-inline", csp)
        self.assertNotIn("unsafe-eval", csp)
        for want in ("default-src 'none'", "script-src 'self'", "style-src 'self'", "frame-ancestors 'none'"):
            self.assertIn(want, csp)
        for path, ctype in (("/static/classic/boot.js", "javascript"), ("/static/classic/app.js", "javascript"), ("/static/classic/app.css", "text/css")):
            r = self.c.get(path, headers=self.h)
            self.addCleanup(r.close)
            self.assertEqual(r.status_code, 200, path)
            self.assertIn(ctype, r.content_type)
            self.assertEqual(r.headers["Content-Security-Policy"], csp)
        self.assertEqual(self.c.get("/api/v1/openapi.json", headers=self.h).headers["Content-Security-Policy"], csp)


if __name__ == "__main__":
    unittest.main()
