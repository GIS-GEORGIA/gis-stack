"""Alert rules manager: store (validation, atomicity, concurrency) and API (permissions, audit, contract).  Unit tested only."""
import json
import os
import shutil
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from gisstack.monitoring import alerts, rulestore
from tests.test_api import ApiBase

GOOD = {"metric": "host_cpu_percent", "op": ">", "threshold": 50, "for": 2, "level": "warn", "text": "cpu {value:.0f}% on {subject}", "description": "my rule"}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.td, True)
        self.p = Path(self.td) / "monitor" / "rules.json"

    def rev(self):
        return rulestore.view(self.p)["revision"]

    def row(self, v, rid):
        return next(r for r in v["rules"] if r["id"] == rid)

    def test_view_defaults_and_never_creates_file(self):
        v = rulestore.view(self.p)
        self.assertEqual(v["revision"], rulestore.NO_FILE)
        self.assertIsNone(v["modified"])
        self.assertEqual({r["id"] for r in v["rules"]}, set(alerts.DEFAULT_RULES))
        self.assertTrue(all(r["builtin"] and not r["modified"] for r in v["rules"]))
        self.assertFalse(self.p.exists())

    def test_builtin_change_stores_only_differences_and_resets(self):
        v = rulestore.update("cpu_high", {"threshold": 70, "level": "crit", "for": 5}, self.rev(), self.p)
        self.assertEqual(json.loads(self.p.read_text())["rules"], {"cpu_high": {"level": "crit", "threshold": 70}})     # for=5 is the default
        self.assertEqual(stat.S_IMODE(self.p.stat().st_mode), 0o600)
        self.assertTrue(self.row(v, "cpu_high")["modified"])
        self.assertIsNotNone(v["modified"])
        v = rulestore.reset("cpu_high", v["revision"], self.p)
        self.assertFalse(self.row(v, "cpu_high")["modified"])
        self.assertEqual(json.loads(self.p.read_text())["rules"], {})

    def test_disable_builtin_and_cannot_delete_it(self):
        v = rulestore.update("swap_high", {"enabled": False}, self.rev(), self.p)
        self.assertFalse(self.row(v, "swap_high")["enabled"])
        self.assertTrue(alerts.load_rules(self.p)["swap_high"]["enabled"] is False)
        with self.assertRaises(alerts.RulesError):
            rulestore.delete("swap_high", v["revision"], self.p)
        with self.assertRaises(alerts.RulesError):
            rulestore.update("cpu_high", {"metric": "host_load1"}, v["revision"], self.p)            # not editable on a built-in rule

    def test_custom_lifecycle_and_monitor_sees_it(self):
        v = rulestore.create("my_cpu", GOOD, self.rev(), self.p)
        r = self.row(v, "my_cpu")
        self.assertFalse(r["builtin"])
        self.assertEqual((r["threshold"], r["for"], r["description"]), (50.0, 2, "my rule"))
        self.assertIn("my_cpu", alerts.load_rules(self.p))                                            # the real monitor loader
        v = rulestore.update("my_cpu", {"threshold": 60, "enabled": False}, v["revision"], self.p)
        self.assertEqual(self.row(v, "my_cpu")["threshold"], 60.0)
        self.assertFalse(self.row(v, "my_cpu")["enabled"])
        self.assertEqual(self.row(v, "my_cpu")["metric"], "host_cpu_percent")                         # untouched fields survive
        v = rulestore.delete("my_cpu", v["revision"], self.p)
        self.assertNotIn("my_cpu", [r["id"] for r in v["rules"]])

    def test_duplicates_are_rejected(self):
        v = rulestore.create("my_cpu", GOOD, self.rev(), self.p)
        with self.assertRaises(rulestore.RulesConflict):
            rulestore.create("my_cpu", GOOD, v["revision"], self.p)
        with self.assertRaises(rulestore.RulesConflict):
            rulestore.create("cpu_high", GOOD, v["revision"], self.p)                                 # built-in id

    def test_invalid_rules_write_nothing(self):
        rev = self.rev()
        bads = [dict(GOOD, metric="nope"), dict(GOOD, op="!"), dict(GOOD, level="info"), dict(GOOD, **{"for": 0}), dict(GOOD, threshold="x"),
                dict(GOOD, threshold=True), dict(GOOD, text="{value.__class__}"), dict(GOOD, text="{0}"), dict(GOOD, text="{value!r}"), dict(GOOD, text=""),
                dict(GOOD, text="x" * 201), dict(GOOD, enabled="yes"), dict(GOOD, renotify=-1), dict(GOOD, renotify=10 ** 9), dict(GOOD, extra=1),
                dict(GOOD, description="a\nb"), {k: v for k, v in GOOD.items() if k != "text"}]
        for bad in bads:
            with self.assertRaises(alerts.RulesError, msg=bad):
                rulestore.create("my_cpu", bad, rev, self.p)
        for rid in ("A", "1ab", "ab", "has space", "x" * 50, "../etc"):
            with self.assertRaises(alerts.RulesError, msg=rid):
                rulestore.create(rid, GOOD, rev, self.p)
        self.assertFalse(self.p.exists())

    def test_unknown_rule(self):
        with self.assertRaises(rulestore.RulesNotFound):
            rulestore.update("nope_rule", {"threshold": 1}, self.rev(), self.p)
        with self.assertRaises(rulestore.RulesNotFound):
            rulestore.delete("nope_rule", self.rev(), self.p)

    def test_stale_revision_conflicts(self):
        rev = self.rev()
        rulestore.update("cpu_high", {"threshold": 70}, rev, self.p)
        before = self.p.read_text()
        with self.assertRaises(rulestore.RulesConflict):
            rulestore.update("cpu_high", {"threshold": 80}, rev, self.p)
        self.assertEqual(self.p.read_text(), before)

    def test_concurrent_writers_exactly_one_wins(self):
        rev = self.rev()
        results = []

        def go(n):
            try:
                rulestore.update("cpu_high", {"threshold": 60 + n}, rev, self.p)
                results.append("ok")
            except rulestore.RulesConflict:
                results.append("conflict")
        ts = [threading.Thread(target=go, args=(i,)) for i in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sorted(results), ["conflict"] * 7 + ["ok"])
        json.loads(self.p.read_text())                                                                # never a torn file

    def test_failed_write_keeps_old_file(self):
        v = rulestore.update("cpu_high", {"threshold": 70}, self.rev(), self.p)
        before = self.p.read_text()
        with mock.patch("os.replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
            rulestore.update("cpu_high", {"threshold": 80}, v["revision"], self.p)
        self.assertEqual(self.p.read_text(), before)
        self.assertEqual([f.name for f in self.p.parent.iterdir() if f.name.endswith(".tmp")], [])      # no temp residue

    def test_damaged_file_is_reported_not_overwritten(self):
        self.p.parent.mkdir(parents=True)
        self.p.write_text("{broken")
        with self.assertRaises(alerts.RulesError):
            rulestore.view(self.p)
        with self.assertRaises(alerts.RulesError):
            rulestore.update("cpu_high", {"threshold": 1}, "0", self.p)
        self.assertEqual(self.p.read_text(), "{broken")

    def test_old_hand_written_files_still_load(self):
        self.p.parent.mkdir(parents=True)
        self.p.write_text(json.dumps({"rules": {"cpu_high": {"op": ">=", "renotify": 60, "threshold": 80}}}))
        v = rulestore.update("cpu_high", {"threshold": 70}, self.rev(), self.p)
        self.assertEqual(json.loads(self.p.read_text())["rules"]["cpu_high"], {"op": ">=", "renotify": 60, "threshold": 70})
        self.assertEqual(self.row(v, "cpu_high")["op"], ">=")

    def test_known_metrics_cover_defaults_and_collector(self):
        self.assertTrue({r["metric"] for r in alerts.DEFAULT_RULES.values()} <= set(alerts.KNOWN_METRICS))


class RulesApiTests(ApiBase):
    def rev(self):
        return self.data(self.call("GET", "/monitor/rules"))["revision"]

    def audit_lines(self):
        p = Path(self.td) / "audit.log"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def test_permissions(self):
        self.assertEqual(self.call("GET", "/monitor/rules", self.tokens["read"]).status_code, 200)
        rev = self.rev()
        body = {"revision": rev, "id": "my_cpu", **GOOD}
        for scope in ("read", "operate"):
            t = self.tokens[scope]
            self.assertEqual(self.call("POST", "/monitor/rules", t, body=body).status_code, 403, scope)
            self.assertEqual(self.call("PUT", "/monitor/rules/cpu_high", t, body={"revision": rev, "threshold": 1}).status_code, 403, scope)
            self.assertEqual(self.call("DELETE", f"/monitor/rules/my_cpu?revision={rev}", t).status_code, 403, scope)
        self.assertEqual(self.call("GET", "/monitor/rules", None).status_code, 401)
        self.assertEqual(self.call("POST", "/monitor/rules", self.tokens["admin"], body=body).status_code, 200)

    def test_lifecycle_contract_and_audit(self):
        v = self.data(self.call("POST", "/monitor/rules", body={"revision": self.rev(), "id": "my_cpu", **GOOD}))
        self.conforms("/monitor/rules", "POST", v)
        v = self.data(self.call("PUT", "/monitor/rules/my_cpu", body={"revision": v["revision"], "threshold": 65}))
        v = self.data(self.call("PUT", "/monitor/rules/cpu_high", body={"revision": v["revision"], "threshold": 70}))
        v = self.data(self.call("PUT", "/monitor/rules/cpu_high", body={"revision": v["revision"], "reset": True}))
        v = self.data(self.call("DELETE", f"/monitor/rules/my_cpu?revision={v['revision']}"))
        self.conforms("/monitor/rules/{id}", "DELETE", v)
        got = self.data(self.call("GET", "/monitor/rules"))
        self.conforms("/monitor/rules", "GET", got)
        self.assertEqual(got["revision"], v["revision"])
        events = [a["event"] for a in self.audit_lines() if a["event"].startswith("monitor.rule")]
        self.assertEqual(events, ["monitor.rule.create", "monitor.rule.update", "monitor.rule.update", "monitor.rule.reset", "monitor.rule.delete"])
        last = [a for a in self.audit_lines() if a["event"] == "monitor.rule.delete"][0]
        self.assertTrue(last["ok"])
        self.assertEqual((last["rule"], last["scope"]), ("my_cpu", "admin"))

    def test_errors_are_mapped_and_audited(self):
        rev = self.rev()
        r = self.call("POST", "/monitor/rules", body={"revision": rev, "id": "cpu_high", **GOOD})
        self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (409, "conflict"))
        r = self.call("PUT", "/monitor/rules/cpu_high", body={"revision": "deadbeef", "threshold": 1})
        self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (409, "conflict"))
        r = self.call("PUT", "/monitor/rules/zzz_rule", body={"revision": rev, "threshold": 1})
        self.assertEqual(r.status_code, 404)
        r = self.call("DELETE", f"/monitor/rules/cpu_high?revision={rev}")
        self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (400, "rules"))
        for bad in ({"revision": rev, "id": "x_y", **dict(GOOD, op="!")}, {"revision": rev, "id": "x_y", **dict(GOOD, text="{value.__class__}")},
                    {"revision": rev, "id": "X", **GOOD}, {"id": "x_y", **GOOD}, {"revision": rev, "id": "x_y", **dict(GOOD, bogus=1)}):
            self.assertEqual(self.call("POST", "/monitor/rules", body=bad).status_code, 400, bad)
        failed = [a for a in self.audit_lines() if a["event"].startswith("monitor.rule") and a.get("ok") is False]
        self.assertGreaterEqual(len(failed), 4)
        self.assertFalse(os.path.exists(os.path.join(self.td, "monitor", "rules.json")))
        r = self.call("POST", "/monitor/rules", body={"revision": rev, "id": "x_y", **dict(GOOD, text="{value.__class__}")})
        self.assertNotIn(self.td, r.get_data(as_text=True))                               # no server paths in error messages


if __name__ == "__main__":
    unittest.main()
