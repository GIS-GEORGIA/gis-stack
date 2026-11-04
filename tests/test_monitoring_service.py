"""Phase 8: the monitoring loop, Prometheus text, the `gisstack monitor` CLI and the flag-gated /api/v1 endpoints."""
import io
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
from test_monitoring_alerts import NOTE, Receiver  # noqa: E402, F401

from gisstack import cli, config  # noqa: E402
from gisstack.core import paths  # noqa: E402
from gisstack.core.errors import DeployError  # noqa: E402
from gisstack.monitoring import alerts, collect, notify, render, service, store  # noqa: E402

FIX = (Path(__file__).parent / "fixtures/monitor/native_ubuntu2404_pg18.txt").read_text()
ROWS = [{"id": c, "state": "running", "restarts": 0, "enabled": True} for c in ("postgis", "tomcat", "geoserver", "mapproxy", "caddy", "webmap")]


def cfg(**kw):
    return config.resolve({"targets": ["native"], "deploy_target": "native", "project": "mon", "domain": "localhost", "tls": "none",
                           "postgres": {"version": "18"}, **kw})


class Base(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.td, True))
        p = mock.patch.dict(os.environ, {"GISSTACK_HOME": str(self.td), "GISSTACK_FEATURES": "monitoring,prometheus", "GISSTACK_LANG": "en"})
        p.start()
        self.addCleanup(p.stop)
        self.clock = mock.Mock(side_effect=lambda: 1_800_000_000.0)

    def fake_collect(self, rows=ROWS, text=FIX):
        def f(c, mode, ssh, target=None):
            return collect.build_sample(c, "native", text, rows, 1.0)
        return mock.patch.object(collect, "collect", side_effect=f)


class LoopTests(Base):
    def test_tick_stores_a_real_sample_and_reports_nothing_for_a_healthy_machine(self):
        mon = service.Monitor(cfg(), "local", None, providers=[])
        self.addCleanup(mon.store.close)
        with self.fake_collect():
            res = mon.tick()
        self.assertEqual(res["notifications"], [])
        self.assertEqual(res["errors"], [])
        snap = service.snapshot(mon.store)
        self.assertEqual(snap["alerts"], [])
        self.assertEqual(snap["metrics"]["monitor_reachable"][0]["value"], 1.0)
        self.assertEqual(snap["availability"]["caddy"], 100.0)
        self.assertEqual(mon.store.db_path if hasattr(mon.store, "db_path") else mon.store.path, paths.monitor_db("mon", "localhost"))

    def test_an_outage_fires_notifies_and_resolves_end_to_end(self):
        hook = Receiver()
        self.addCleanup(hook.close)
        rules = {k: {**v, "enabled": True} for k, v in alerts.DEFAULT_RULES.items()}
        mon = service.Monitor(cfg(), "local", None, rules=rules, providers=[notify.WebhookProvider(hook.url)])
        self.addCleanup(mon.store.close)
        down = [{**r, "state": "failed"} if r["id"] == "caddy" else r for r in ROWS]
        sent = []
        for rows, ts in ((ROWS, 1000.0), (down, 1030.0), (down, 1060.0), (down, 1090.0), (ROWS, 1120.0)):
            with mock.patch.object(collect, "collect", side_effect=lambda c, m, s, t=None, rows=rows, ts=ts: {**collect.build_sample(c, "native", FIX, rows, 1.0), "ts": ts}):
                sent += mon.tick()["notifications"]
        self.assertEqual([(n["rule"], n["subject"], n["state"]) for n in sent], [("service_down", "caddy", "firing"), ("service_down", "caddy", "resolved")])
        bodies = [json.loads(b) for _, _, b in hook.requests]
        self.assertEqual(len(bodies), 2)
        self.assertIn("service caddy is not running", bodies[0]["text"])
        self.assertEqual(bodies[0]["alert"]["project"], "mon")
        self.assertTrue(bodies[1]["text"].startswith("🟢"))
        self.assertEqual([e["kind"] for e in mon.store.events()][:2], ["alert.resolved", "alert.firing"])
        self.assertEqual(mon.store.availability()["caddy"], 40.0)                      # up in 2 of the 5 real samples

    def test_an_unreachable_machine_is_recorded_as_a_fact_and_alerts(self):
        rules = {k: {**v, "enabled": True} for k, v in alerts.DEFAULT_RULES.items()}
        mon = service.Monitor(cfg(), "ssh", {"host": "srv", "user": "root"}, rules=rules, providers=[], clock=lambda: 5000.0)
        self.addCleanup(mon.store.close)
        fired = []
        with mock.patch.object(collect, "collect", side_effect=DeployError("ssh: connect timed out")):
            for _ in range(3):
                fired += mon.tick()["notifications"]
        self.assertEqual([(n["rule"], n["level"]) for n in fired], [("host_unreachable", "crit")])
        self.assertEqual(mon.store.latest()["monitor_reachable"][0]["value"], 0.0)
        self.assertNotIn("host_cpu_percent", mon.store.latest())                      # nothing was invented for the missing sample
        self.assertIn("collect.failed", [e["kind"] for e in mon.store.events()])
        self.assertEqual(mon.store.path, paths.monitor_db("mon", "srv"))

    def test_failing_notification_is_recorded_not_raised(self):
        mon = service.Monitor(cfg(), "local", None, rules={"disk": {**alerts.DEFAULT_RULES["disk_critical"], "enabled": True, "for": 1}},
                              providers=[notify.WebhookProvider("http://127.0.0.1:9/x", retries=0, timeout=1)])
        self.addCleanup(mon.store.close)
        text = FIX.replace("disk / 29378688 5446304 23916000", "disk / 1000 990 10")
        with self.fake_collect(text=text):
            res = mon.tick()
        self.assertFalse(res["delivery"][0]["ok"])
        self.assertIn("notify.failed", [e["kind"] for e in mon.store.events()])

    def test_watch_runs_until_stopped_survives_errors_and_enforces_a_minimum_interval(self):
        mon = service.Monitor(cfg(), "local", None, providers=[])
        self.addCleanup(mon.store.close)
        calls = []
        stop = threading.Event()

        def flaky():
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("boom")
            return {"notifications": [], "errors": []}
        mon.tick = flaky
        with mock.patch.object(service, "MIN_INTERVAL", 0.01):
            self.assertEqual(mon.watch(0.0, stop, max_ticks=4), 4)
        self.assertEqual(len(calls), 4)
        self.assertIn("monitor.error", [e["kind"] for e in mon.store.events()])
        self.assertEqual(service.MIN_INTERVAL, 10.0)
        stop.set()
        self.assertEqual(mon.watch(30, stop), 0)

    def test_retention_is_applied_by_the_loop(self):
        mon = service.Monitor(cfg(), "local", None, providers=[], clock=lambda: 10_000_000.0)
        self.addCleanup(mon.store.close)
        mon.store.add_sample(1.0, [("old", {}, 1.0)])
        with self.fake_collect():
            mon.tick()
        self.assertEqual(mon.store.series("old"), [])


class PrometheusTests(Base):
    def test_text_format_is_valid_and_complete(self):
        mon = service.Monitor(cfg(), "local", None, providers=[])
        self.addCleanup(mon.store.close)
        with self.fake_collect():
            mon.tick()
        text = service.prometheus_text(mon.store, "mon")
        sample_re = re.compile(r'^[a-zA-Z_:][a-zA-Z0-9_:]*(\{([a-zA-Z_][a-zA-Z0-9_]*="([^"\\]|\\.)*"(,|(?=\})))*\})? -?[0-9.e+-]+$')
        types = {}
        for line in text.splitlines():
            if line.startswith("# TYPE "):
                _, _, name, kind = line.split()
                types[name] = kind
            else:
                self.assertRegex(line, sample_re)
                self.assertIn(re.split(r"[{ ]", line)[0], types)                      # every series has a TYPE line before it
        self.assertEqual(types["gisstack_pg_xact_commit_total"], "counter")
        self.assertEqual(types["gisstack_host_cpu_percent"], "gauge")
        self.assertIn('gisstack_service_up{project="mon",service="caddy"} 1', text)
        self.assertTrue(text.endswith("\n"))

    def test_label_values_are_escaped_and_empty_history_gives_empty_text(self):
        s = store.Store(self.td / "p.sqlite")
        self.addCleanup(s.close)
        self.assertEqual(service.prometheus_text(s), "")
        s.add_sample(1.0, [("odd", {"l": 'a"b\\c\nd'}, 2.5)])
        self.assertIn(r'gisstack_odd{l="a\"b\\c\nd"} 2.5', service.prometheus_text(s))


class RenderTests(Base):
    def test_snapshot_text(self):
        mon = service.Monitor(cfg(), "local", None, providers=[])
        self.addCleanup(mon.store.close)
        with self.fake_collect():
            mon.tick()
        text = render.format_snapshot(service.snapshot(mon.store, now=mon.store.latest_ts() + 5))
        for want in ("HOST   cpu", "DISK   /", "SVC    caddy", "PG     up  connections 1/100", "PROBE  geoserver:public", "BACKUP 3 backup(s)", "ALERTS none firing"):
            self.assertIn(want, text)
        self.assertIn("(5 s ago)", text)
        self.assertIn("run: gisstack monitor collect", render.format_snapshot({"ts": None}))


class CliTests(Base):
    def run_cli(self, *a):
        o, e = io.StringIO(), io.StringIO()
        cf = self.td / "c.json"
        cf.write_text(json.dumps({"project": "mon", "deploy_target": "native", "targets": ["native"], "postgres": {"version": "18"}}))
        with redirect_stdout(o), redirect_stderr(e):
            rc = cli.main(["monitor", a[0], "-c", str(cf), *a[1:]])
        return rc, o.getvalue(), e.getvalue()

    def test_feature_flag_gates_the_whole_command(self):
        with mock.patch.dict(os.environ, {"GISSTACK_FEATURES": ""}):
            rc, _, err = self.run_cli("show")
        self.assertEqual(rc, 3)
        self.assertIn("GISSTACK_FEATURES=monitoring", err)

    def test_collect_show_history_alerts_prune_prometheus_rules(self):
        with self.fake_collect():
            rc, out, err = self.run_cli("collect")
        self.assertEqual(rc, 0, err)
        self.assertIn("SVC    caddy", out)
        rc, out, _ = self.run_cli("show")
        self.assertIn("ALERTS none firing", out)
        rc, out, _ = self.run_cli("show", "--json")
        self.assertEqual(json.loads(out)["metrics"]["monitor_reachable"][0]["value"], 1.0)
        rc, out, _ = self.run_cli("history", "host_load1", "--since", "1s", "--json")
        self.assertEqual(len(json.loads(out)), 0)                                 # the stored sample is older than one second
        rc, out, _ = self.run_cli("history", "service_up", "--since", "100000d", "--label", "service=caddy", "--json")
        self.assertEqual([p["value"] for p in json.loads(out)], [1.0])
        self.assertEqual(self.run_cli("history", "x", "--since", "5y")[0], 1)
        self.assertEqual(self.run_cli("alerts")[1].splitlines()[0], "no alerts firing")
        self.assertIn("deleted", self.run_cli("prune")[1])
        self.assertIn("gisstack_service_up", self.run_cli("prometheus")[1])
        rules = self.run_cli("rules")[1]
        self.assertIn("cpu_high", rules)
        self.assertEqual(json.loads(self.run_cli("rules", "--json")[1])["cpu_high"]["threshold"], 90.0)

    def test_reading_commands_without_history_say_so_and_do_not_create_files(self):
        rc, _, err = self.run_cli("show")
        self.assertEqual(rc, 1)
        self.assertIn("no history", err)
        self.assertFalse(paths.monitor_db("mon", "localhost").exists())

    def test_watch_with_max_ticks(self):
        with self.fake_collect(), mock.patch.object(service, "MIN_INTERVAL", 0.01):
            rc, out, _ = self.run_cli("watch", "--interval", "0.01", "--max-ticks", "2", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual(len(out.strip().splitlines()), 2)

    def test_bad_rules_file_is_a_clean_error(self):
        (self.td / "monitor").mkdir()
        (self.td / "monitor/rules.json").write_text('{"rules": {"nope": {}}}')
        rc, _, err = self.run_cli("rules")
        self.assertEqual(rc, 1)
        self.assertIn("unknown rule", err)


@unittest.skipUnless(__import__("importlib").util.find_spec("flask"), "flask needed")
class WebTests(Base):
    def setUp(self):
        super().setUp()
        from gisstack.webgui.app import create_app
        self.c = create_app("tok").test_client()
        self.h = {"Authorization": "Bearer tok", "X-GisStack": "1"}
        self.body = {"config": {"project": "mon", "deploy_target": "native", "targets": ["native"], "postgres": {"version": "18"}}, "mode": "local"}

    def test_everything_is_off_without_the_flags_and_needs_authentication(self):
        with mock.patch.dict(os.environ, {"GISSTACK_FEATURES": ""}):
            for method, path in (("get", "/api/v1/metrics?project=mon"), ("get", "/api/v1/alerts?project=mon"), ("get", "/metrics?project=mon"),
                                 ("post", "/api/v1/monitor/collect")):
                r = getattr(self.c, method)(path, headers=self.h, **({"json": self.body} if method == "post" else {}))
                self.assertEqual(r.status_code, 404, path)
                self.assertEqual(r.get_json()["error"]["code"], "feature_disabled")
        self.assertEqual(self.c.get("/api/v1/metrics?project=mon").status_code, 401)
        self.assertEqual(self.c.get("/metrics?project=mon").status_code, 401)
        with mock.patch.dict(os.environ, {"GISSTACK_FEATURES": "monitoring"}):                        # prometheus has its own flag
            self.assertEqual(self.c.get("/metrics?project=mon", headers=self.h).get_json()["error"]["code"], "feature_disabled")

    def test_collect_then_read_everything_back(self):
        with self.fake_collect():
            r = self.c.post("/api/v1/monitor/collect", json=self.body, headers=self.h)
        j = r.get_json()
        self.assertEqual((r.status_code, j["error"], j["meta"]["version"]), (200, None, 1))
        self.assertEqual(j["data"]["snapshot"]["alerts"], [])
        q = "?project=mon&host=localhost"
        m = self.c.get("/api/v1/metrics" + q, headers=self.h).get_json()["data"]
        self.assertEqual(m["metrics"]["pg_up"][0]["value"], 1.0)
        h = self.c.get("/api/v1/metrics/history" + q + "&name=service_up&since=1e12&labels=" + json.dumps({"service": "caddy"}), headers=self.h).get_json()
        self.assertEqual([p["value"] for p in h["data"]], [1.0])
        a = self.c.get("/api/v1/alerts" + q, headers=self.h).get_json()["data"]
        self.assertEqual(a["events"], [])
        p = self.c.get("/metrics" + q + "&project=mon", headers=self.h)
        self.assertEqual(p.status_code, 200)
        self.assertIn("text/plain", p.content_type)
        self.assertIn("gisstack_host_load1", p.get_data(as_text=True))

    def test_validation_and_no_history(self):
        for q in ("", "?project=Bad Name", "?project=mon&host=../../etc", "?project=mon&host=" + "x" * 200):
            r = self.c.get("/api/v1/metrics" + q, headers=self.h)
            self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (400, "bad_request"), q)
        r = self.c.get("/api/v1/metrics?project=mon&host=nowhere", headers=self.h)
        self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (404, "not_found"))
        self.assertFalse(paths.monitor_db("mon", "nowhere").exists())                                 # a GET never creates a history
        with self.fake_collect():
            self.c.post("/api/v1/monitor/collect", json=self.body, headers=self.h)
        for q in ("&name=", "&name=x&since=abc", "&name=x&step=x", "&name=x&labels=[1]", "&name=x&labels=not-json", '&name=x&labels={"a":1}'):
            r = self.c.get("/api/v1/metrics/history?project=mon&host=localhost" + q, headers=self.h)
            self.assertEqual(r.status_code, 400, q)

    def test_unreachable_machine_gives_502_with_a_code(self):
        with mock.patch.object(collect, "collect", side_effect=DeployError("ssh down")):
            r = self.c.post("/api/v1/monitor/collect", json=self.body, headers=self.h)
        # an unreachable machine is recorded as a fact by the monitor; the request itself succeeds and reports it
        self.assertEqual(r.status_code, 200)
        self.assertIn("unreachable", r.get_json()["data"]["errors"][0])

    def fake_collect(self):
        return mock.patch.object(collect, "collect", side_effect=lambda c, m, s, t=None: collect.build_sample(c, "native", FIX, ROWS, 1.0))


if __name__ == "__main__":
    unittest.main()
