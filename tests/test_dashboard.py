"""Phase 3: the dashboard - feature flag, strict CSP, accessible structure, hygiene of the shipped JavaScript, and its backend endpoints."""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

from gisstack import config, journal, ops
from gisstack.core import features
from gisstack.monitoring import collect

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "gisstack/webgui"
STATIC = WEB / "static/dashboard"
JS_FILES = sorted(STATIC.rglob("*.js"))
FIX = (Path(__file__).parent / "fixtures/monitor/native_ubuntu2404_pg18.txt").read_text()
HAVE_FLASK = __import__("importlib").util.find_spec("flask") is not None


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.stack = [], []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs), None))
        self.stack.append(tag)

    def handle_data(self, data):
        if self.stack and self.stack[-1] in ("button", "label", "a", "h1", "title", "legend", "small"):
            self.tags.append(("#text:" + self.stack[-1], {}, data))

    def handle_endtag(self, tag):
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()


def parse_page():
    p = Page()
    p.feed((WEB / "templates/dashboard.html").read_text(encoding="utf-8"))
    return p


class StaticHygieneTests(unittest.TestCase):
    """The shipped page and scripts must be safe to serve under a CSP without 'unsafe-inline' and must not contain XSS foot-guns."""

    def test_no_inline_script_style_or_event_handler_in_the_html(self):
        p = parse_page()
        scripts = [a for t, a, _ in p.tags if t == "script"]
        self.assertTrue(scripts and all("src" in a for a in scripts))                     # only external scripts
        self.assertTrue(all(a.get("type") == "module" for a in scripts))
        for tag, attrs, _ in p.tags:
            self.assertNotIn("style", attrs, tag)
            self.assertFalse([k for k in attrs if k.startswith("on")], (tag, attrs))
            for k in ("src", "href"):
                v = attrs.get(k, "")
                self.assertFalse(v.startswith(("http:", "https:", "//")), (tag, v))       # nothing is loaded from other origins

    def test_html_landmarks_and_accessible_names(self):
        p = parse_page()
        tags = p.tags
        self.assertEqual([a for t, a, _ in tags if t == "html"][0].get("lang"), "ka")
        self.assertEqual(len([1 for t, _, _ in tags if t == "main"]), 1)
        self.assertTrue([a for t, a, _ in tags if t == "nav"][0].get("aria-label"))
        self.assertEqual([a for t, a, _ in tags if t == "header"][0].get("role"), "banner")
        first_link = next(a for t, a, _ in tags if t == "a")
        self.assertEqual(first_link.get("href"), "#main")                                 # skip link is the first focusable element
        self.assertIn("skip", first_link.get("class", ""))
        ids = {a["id"] for _, a, _ in tags if "id" in a}
        self.assertEqual(len(ids), len([1 for _, a, _ in tags if "id" in a]), "duplicate ids")
        labels_for = {a.get("for") for t, a, _ in tags if t == "label"}
        for t, a, _ in tags:
            if t in ("select", "input", "textarea"):
                self.assertTrue(a.get("aria-label") or a.get("id") in labels_for, f"{t} {a} has no label")
            if t == "button":
                self.assertTrue(a.get("aria-label") or a.get("id") or a.get("data-lang"), a)
        button_names = [a.get("aria-label") for t, a, _ in tags if t == "button" and a.get("aria-label")]
        self.assertIn("მენიუს გახსნა", button_names)
        self.assertTrue([a for t, a, _ in tags if t == "div" and a.get("id") == "toasts"][0].get("aria-live") == "polite")
        self.assertTrue(any(t == "meta" and a.get("name") == "viewport" for t, a, _ in tags))

    def test_css_has_tokens_for_both_themes_and_a_mobile_layout_and_visible_focus(self):
        css = (STATIC / "css/dashboard.css").read_text()
        self.assertIn('html[data-theme="dark"]', css)
        self.assertIn(":focus-visible", css)
        self.assertIn("@media (max-width: 860px)", css)
        self.assertIn("prefers-reduced-motion", css)
        self.assertNotRegex(css, r"@import|url\(\s*['\"]?https?:")

    def test_scripts_have_no_xss_or_eval_foot_guns(self):
        bad = [r"\binnerHTML\b", r"\bouterHTML\b", r"insertAdjacentHTML", r"document\.write", r"\beval\s*\(", r"new\s+Function", r"setTimeout\(\s*['\"`]",
               r"\.setAttribute\(\s*['\"]on", r"javascript:"]
        for f in JS_FILES:
            text = f.read_text(encoding="utf-8")
            for pat in bad:
                self.assertIsNone(re.search(pat, text), f"{f.name}: {pat}")

    def test_scripts_only_talk_to_this_server(self):
        for f in JS_FILES:
            text = f.read_text(encoding="utf-8")
            for m in re.finditer(r"fetch\(\s*([^,)]+)", text):
                self.assertNotRegex(m.group(1), r"https?://", f.name)
            self.assertNotRegex(re.sub(r"//[^\n]*", "", text), r"""['"]https?://""", f.name)       # no absolute URLs at all (comments aside)

    def test_state_changing_calls_carry_the_csrf_header(self):
        api = (STATIC / "js/api.js").read_text()
        self.assertIn('"X-GisStack": "1"', api)
        for f in JS_FILES:
            if f.name != "api.js":
                self.assertNotIn("fetch(", f.read_text(), f.name)                           # everything goes through api.js

    def test_every_import_resolves(self):
        for f in JS_FILES:
            for m in re.finditer(r"""from\s+["'](\.[^"']+)["']""", f.read_text(encoding="utf-8")):
                self.assertTrue((f.parent / m.group(1)).resolve().is_file(), f"{f.name}: {m.group(1)}")
        entry = re.search(r'src="/static/dashboard/([^"]+)"', (WEB / "templates/dashboard.html").read_text()).group(1)
        self.assertTrue((STATIC / entry).is_file())

    def test_all_views_follow_the_contract(self):
        for f in sorted((STATIC / "js/views").glob("*.js")):
            text = f.read_text(encoding="utf-8")
            self.assertRegex(text, r"export default \{", f.name)
            self.assertRegex(text, r"title:\s*\(", f.name)
            self.assertRegex(text, r"async mount\(", f.name)

    def test_javascript_syntax_and_pure_logic_with_node(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        for f in JS_FILES:
            r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f"{f.name}: {r.stderr}")
        r = subprocess.run([node, "--test", str(ROOT / "tests/js/pure.test.mjs")], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout[-3000:] + r.stderr[-1500:])


class JournalOperationsTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        p = mock.patch.dict(os.environ, {"GISSTACK_HOME": self.td})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.td, True)

    def test_start_and_end_are_folded_into_one_row_newest_first(self):
        with journal.Operation("deploy", project="a", target="native", host="srv", mode="ssh"):
            pass
        with self.assertRaises(RuntimeError), journal.Operation("clean", project="a"):
            raise RuntimeError("boom")
        with journal.Operation("deploy", project="b"):
            pass
        journal.record({"id": "orphan", "ts": 5, "op": "deploy", "event": "start", "project": "a"})        # a start without an end
        rows = journal.operations(10, "a")
        by = {r["id"]: r for r in rows}
        self.assertEqual(by["orphan"]["status"], "running")
        self.assertEqual(sorted(r["op"] for r in rows if r["id"] != "orphan"), ["clean", "deploy"])
        clean = next(r for r in rows if r["op"] == "clean")
        self.assertEqual((clean["status"], clean["error"]), ("error", "RuntimeError: boom"))
        self.assertEqual({r["project"] for r in rows}, {"a"})
        self.assertEqual(len(journal.operations(10)), 4)
        self.assertEqual(len(journal.operations(2)), 2)
        self.assertEqual([r["started"] for r in rows], sorted([r["started"] for r in rows], key=lambda x: x or 0, reverse=True))

    def test_empty_journal(self):
        self.assertEqual(journal.operations(), [])


class CacheMetricTests(unittest.TestCase):
    def test_mapproxy_cache_size_is_collected_when_present_and_reported_missing_otherwise(self):
        cfg = config.resolve({"targets": ["native"], "deploy_target": "native", "postgres": {"version": "18"}})
        rows = [{"id": "mapproxy", "state": "running", "restarts": 0, "enabled": True}]
        bare = re.sub(r"@@cache\ncache \d+\n", "", FIX)                                  # the captured server has an empty cache dir (0 bytes)
        self.assertNotIn("@@cache", bare)
        real = collect.build_sample(cfg, "native", FIX, rows, 1.0)
        self.assertEqual(real["mapproxy"]["cache_bytes"], 0)                              # real output: an empty directory is 0, reported as 0
        self.assertEqual(real["errors"], [])
        s = collect.build_sample(cfg, "native", bare.replace("@@probes", "@@cache\ncache 123456789\n@@probes"), rows, 1.0)
        self.assertEqual(s["mapproxy"]["cache_bytes"], 123456789)
        self.assertIn(("mapproxy_cache_bytes", {}, 123456789.0), collect.flatten(s))
        s2 = collect.build_sample(cfg, "native", bare, rows, 1.0)
        self.assertNotIn("mapproxy", s2)
        self.assertIn("mapproxy: cache size unavailable", s2["errors"])
        self.assertNotIn("mapproxy_cache_bytes", {n for n, _, _ in collect.flatten(s2)})
        self.assertIn("du -sb", collect.script(cfg, "native"))
        cfg2 = config.resolve({"targets": ["native"], "deploy_target": "native", "components": {"mapproxy": False}})
        self.assertNotIn("du -sb", collect.script(cfg2, "native"))


@unittest.skipUnless(HAVE_FLASK, "flask needed")
class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        p = mock.patch.dict(os.environ, {"GISSTACK_HOME": self.td, "GISSTACK_FEATURES": "dashboard,monitoring"})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.td, True)
        from gisstack.webgui.app import create_app
        self.c = create_app("tok").test_client()
        self.h = {"Authorization": "Bearer tok", "X-GisStack": "1"}
        self.body = {"config": {"project": "dash", "deploy_target": "native", "targets": ["native"], "postgres": {"version": "18"}}, "mode": "local"}

    def test_everything_is_off_without_the_flag_and_requires_login(self):
        with mock.patch.dict(os.environ, {"GISSTACK_FEATURES": ""}):
            for method, path in (("get", "/dashboard"), ("get", "/api/v1/dashboard/config"), ("post", "/api/v1/dashboard/summary"), ("get", "/api/v1/journal")):
                r = getattr(self.c, method)(path, headers=self.h, **({"json": self.body} if method == "post" else {}))
                self.assertEqual(r.status_code, 404, path)
                self.assertEqual(r.get_json()["error"]["code"], "feature_disabled")
        for path in ("/dashboard", "/static/dashboard/js/app.js", "/api/v1/dashboard/config", "/api/v1/journal"):
            self.assertEqual(self.c.get(path).status_code, 401, path)

    def test_page_and_assets_are_served_with_a_strict_csp_but_the_classic_page_is_untouched(self):
        r = self.c.get("/dashboard", headers=self.h)
        self.assertEqual(r.status_code, 200)
        csp = r.headers["Content-Security-Policy"]
        for want in ("default-src 'none'", "script-src 'self'", "style-src 'self'", "connect-src 'self'", "frame-ancestors 'none'", "base-uri 'none'"):
            self.assertIn(want, csp)
        self.assertNotIn("unsafe-inline", csp)
        self.assertNotIn("unsafe-eval", csp)
        self.assertEqual(r.headers["Referrer-Policy"], "no-referrer")
        js = self.c.get("/static/dashboard/js/app.js", headers=self.h)
        self.addCleanup(js.close)
        self.assertEqual(js.status_code, 200)
        self.assertIn("javascript", js.content_type)
        self.assertNotIn("unsafe-inline", js.headers["Content-Security-Policy"])
        css = self.c.get("/static/dashboard/css/dashboard.css", headers=self.h)
        self.addCleanup(css.close)
        self.assertIn("text/css", css.content_type)
        classic = self.c.get("/", headers=self.h)
        self.assertIn("unsafe-inline", classic.headers["Content-Security-Policy"])         # the classic page is unchanged
        self.assertEqual(self.c.get("/static/dashboard/js/nope.js", headers=self.h).status_code, 404)
        self.assertEqual(self.c.get("/static/dashboard/../../app.py", headers=self.h).status_code, 404)

    def test_every_served_script_exists_and_is_javascript(self):
        for f in JS_FILES:
            rel = f.relative_to(ROOT / "gisstack/webgui/static")
            r = self.c.get("/static/" + rel.as_posix(), headers=self.h)
            self.addCleanup(r.close)
            self.assertEqual(r.status_code, 200, rel)
            self.assertIn("javascript", r.content_type, rel)

    def test_config_endpoint(self):
        d = self.c.get("/api/v1/dashboard/config", headers=self.h).get_json()["data"]
        self.assertEqual(set(d["features"]), {"dashboard", "monitoring"})
        self.assertIn("version", d)
        self.assertTrue({"lang", "theme"} <= set(d["settings"]))
        self.assertIsInstance(d["projects"], list)

    def test_summary_combines_live_status_stored_metrics_and_operations_and_isolates_failures(self):
        from gisstack.monitoring import service
        rows = [{"id": "caddy", "name": "Caddy", "state": "running", "ok": True, "checks": [], "listen": [], "urls": [], "unit": "caddy.service", "detail": "", "enabled": True,
                 "pid": 1, "since": "", "restarts": 0}]
        status = {"target": "native", "project": "dash", "domain": "localhost", "time": "00:00:00", "took": 0.1, "rows": rows}
        with mock.patch.object(ops, "status", return_value=status):
            d = self.c.post("/api/v1/dashboard/summary", json=self.body, headers=self.h).get_json()["data"]
        self.assertEqual((d["project"], d["target"], d["errors"], d["monitoring"]), ("dash", "native", {}, None))     # no history yet: null, not invented
        self.assertEqual(d["status"]["rows"][0]["id"], "caddy")
        # now with a stored sample and a recorded operation
        cfg = config.resolve({**self.body["config"]})
        mon = service.Monitor(cfg, "local", None, providers=[])
        with mock.patch.object(collect, "collect", side_effect=lambda c, m, s, t=None: collect.build_sample(c, "native", FIX, rows, 1.0)):
            mon.tick()
        mon.store.close()
        with journal.Operation("deploy", project="dash", target="native", host="localhost", mode="local"):
            pass
        with mock.patch.object(ops, "status", return_value=status):
            d = self.c.post("/api/v1/dashboard/summary", json=self.body, headers=self.h).get_json()["data"]
        self.assertEqual(d["monitoring"]["metrics"]["pg_up"][0]["value"], 1.0)
        self.assertEqual(d["operations"][0]["op"], "deploy")
        # a failing machine does not blank the rest
        from gisstack.core.errors import DeployError
        with mock.patch.object(ops, "status", side_effect=DeployError("ssh: connection refused")):
            r = self.c.post("/api/v1/dashboard/summary", json=self.body, headers=self.h)
        d = r.get_json()["data"]
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(d["status"])
        self.assertEqual(d["errors"]["status"]["message"], "ssh: connection refused")
        self.assertIsNotNone(d["monitoring"])
        self.assertEqual(len(d["operations"]), 1)

    def test_summary_without_the_monitoring_flag_never_reads_a_history(self):
        with mock.patch.dict(os.environ, {"GISSTACK_FEATURES": "dashboard"}), mock.patch.object(ops, "status", return_value={"rows": []}):
            d = self.c.post("/api/v1/dashboard/summary", json=self.body, headers=self.h).get_json()["data"]
        self.assertIsNone(d["monitoring"])

    def test_journal_endpoint(self):
        with journal.Operation("deploy", project="p1"):
            pass
        with journal.Operation("clean", project="p2"):
            pass
        rows = self.c.get("/api/v1/journal?project=p1", headers=self.h).get_json()["data"]
        self.assertEqual([r["op"] for r in rows], ["deploy"])
        self.assertEqual(len(self.c.get("/api/v1/journal", headers=self.h).get_json()["data"]), 2)
        self.assertEqual(self.c.get("/api/v1/journal?limit=abc", headers=self.h).status_code, 400)
        self.assertEqual(len(self.c.get("/api/v1/journal?limit=1", headers=self.h).get_json()["data"]), 1)

    def test_state_changing_dashboard_calls_need_the_csrf_header(self):
        r = self.c.post("/api/v1/dashboard/summary", json=self.body, headers={"Authorization": "Bearer tok"})
        self.assertEqual(r.status_code, 403)


class FeatureFlagTests(unittest.TestCase):
    def test_dashboard_is_a_known_flag_and_off_by_default(self):
        self.assertIn("dashboard", features.KNOWN)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(features.enabled("dashboard"))


if __name__ == "__main__":
    unittest.main()
