"""Phase 8: SQLite ring-buffer history."""
import os
import sqlite3
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from gisstack import config
from gisstack.core import paths
from gisstack.monitoring import collect, store

NATIVE = (Path(__file__).parent / "fixtures/monitor/native_ubuntu2404_pg18.txt").read_text()
HOUR, DAY = 3600.0, 86400.0


class Base(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.td, True))
        self.s = store.Store(self.td / "m.sqlite")
        self.addCleanup(self.s.close)


class WriteReadTests(Base):
    def test_a_real_sample_roundtrips(self):
        cfg = config.resolve({"targets": ["native"], "deploy_target": "native", "postgres": {"version": "18"}})
        sample = collect.build_sample(cfg, "native", NATIVE, [{"id": "caddy", "state": "running", "restarts": 0, "enabled": True}], 1.0)
        flat = collect.flatten(sample)
        self.s.add_sample(sample["ts"], flat, sample["errors"])
        latest = self.s.latest()
        self.assertEqual(set(latest), {n for n, _, _ in flat})
        self.assertEqual(latest["service_up"][0]["labels"], {"service": "caddy"})
        self.assertEqual(self.s.count(), len(flat))
        self.assertEqual(self.s.latest_ts(), sample["ts"])

    def test_series_filters_labels_range_and_buckets(self):
        for i in range(10):
            self.s.add_sample(1000.0 + i * 10, [("cpu", {"core": "0"}, float(i)), ("cpu", {"core": "1"}, 100.0 + i), ("other", {}, 1.0)])
        pts = self.s.series("cpu", {"core": "0"})
        self.assertEqual([p["value"] for p in pts], [float(i) for i in range(10)])
        self.assertEqual(len(self.s.series("cpu")), 20)
        self.assertEqual([p["ts"] for p in self.s.series("cpu", {"core": "0"}, since=1030, until=1050)], [1030.0, 1040.0, 1050.0])
        b = self.s.series("cpu", {"core": "0"}, step=50)
        self.assertEqual([(p["ts"], p["value"]) for p in b], [(1000.0, 2.0), (1050.0, 7.0)])           # averages of 0..4 and 5..9
        self.assertEqual(len(self.s.series("cpu", {"core": "0"}, limit=3)), 3)
        self.assertEqual(self.s.series("cpu", {"core": "0"}, limit=3)[-1]["value"], 9.0)               # the newest points win
        self.assertEqual(self.s.series("missing"), [])

    def test_nan_and_infinity_are_never_stored(self):
        self.s.add_sample(1.0, [("a", {}, float("nan")), ("b", {}, float("inf")), ("c", {}, 2.0)])
        self.assertEqual(set(self.s.latest()), {"c"})

    def test_hostile_names_and_labels_are_data_not_sql(self):
        evil = "x'); DROP TABLE metrics; --"
        self.s.add_sample(1.0, [(evil, {"l": evil}, 1.0)])
        self.s.add_event(1.0, "warn", "k", evil, evil)
        self.assertEqual(self.s.series(evil, {"l": evil})[0]["value"], 1.0)
        self.assertEqual(self.s.events()[0]["message"], evil)
        self.assertEqual(self.s.count(), 1)

    def test_events_filter_and_order(self):
        for i, lv in enumerate(("info", "warn", "crit", "warn")):
            self.s.add_event(100.0 + i, lv, "alert", "svc", f"m{i}")
        self.assertEqual([e["message"] for e in self.s.events()], ["m3", "m2", "m1", "m0"])
        self.assertEqual([e["message"] for e in self.s.events(level="warn")], ["m3", "m1"])
        self.assertEqual([e["message"] for e in self.s.events(since=102.0, limit=1)], ["m3"])

    def test_availability_is_the_real_share_of_up_samples(self):
        for i, up in enumerate((1, 1, 0, 1)):
            self.s.add_sample(10.0 + i, [("service_up", {"service": "caddy"}, float(up)), ("service_up", {"service": "tomcat"}, 1.0)])
        self.assertEqual(self.s.availability(), {"caddy": 75.0, "tomcat": 100.0})

    def test_alert_state_roundtrip(self):
        self.s.set_alert("disk", "/", "firing", 5.0, 97.0, "disk / is 97 % full", 3, None)
        self.s.set_alert("disk", "/", "ok", 9.0, 40.0, "recovered", 0, 7.0)
        (a,) = self.s.alert_state()
        self.assertEqual((a["state"], a["since"], a["notified"]), ("ok", 9.0, 7.0))


class RetentionTests(Base):
    def test_old_data_is_dropped_but_the_newest_sample_always_survives(self):
        now = 1_000_000_000.0
        for i in range(5):
            self.s.add_sample(now - 30 * DAY + i, [("m", {}, float(i))])
        self.s.add_sample(now - 1 * HOUR, [("m", {}, 99.0)])
        deleted = self.s.prune(now)
        self.assertEqual(deleted, 5)
        self.assertEqual([p["value"] for p in self.s.series("m")], [99.0])
        # even when *everything* is older than the retention window the last sample is kept
        self.s.prune(now + 400 * DAY)
        self.assertEqual([p["value"] for p in self.s.series("m")], [99.0])

    def test_row_cap_is_a_ring_buffer_oldest_first(self):
        s = store.Store(self.td / "cap.sqlite", max_rows=30)
        self.addCleanup(s.close)
        for i in range(20):
            s.add_sample(1000.0 + i, [("a", {}, float(i)), ("b", {}, float(i))])                       # 40 rows
        s.prune(now=1020.0)
        self.assertLessEqual(s.count(), 30)
        vals = [p["value"] for p in s.series("a")]
        self.assertEqual(vals[-1], 19.0)
        self.assertEqual(vals, sorted(vals))
        self.assertGreaterEqual(vals[0], 5.0)                                                          # oldest samples went first
        self.assertEqual(len(s.series("a")), len(s.series("b")))                                       # samples stay whole

    def test_health_history_outlives_metrics(self):
        now = 1_000_000_000.0
        self.s.add_event(now - 20 * DAY, "crit", "alert", "x", "old but kept")
        self.s.add_event(now - 100 * DAY, "crit", "alert", "x", "too old")
        self.s.add_sample(now, [("m", {}, 1.0)])
        self.s.prune(now)
        self.assertEqual([e["message"] for e in self.s.events()], ["old but kept"])

    def test_environment_overrides_the_limits(self):
        with mock.patch.dict(os.environ, {"GISSTACK_MONITOR_RETENTION_DAYS": "3", "GISSTACK_MONITOR_MAX_ROWS": "50"}):
            s = store.Store(self.td / "env.sqlite")
        self.addCleanup(s.close)
        self.assertEqual((s.days, s.max_rows), (3, 50))


class FileTests(unittest.TestCase):
    def test_file_and_directory_are_owner_only(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"GISSTACK_HOME": td}):
            p = paths.monitor_db("proj", "srv.example.ge")
            s = store.Store(p)
            s.add_sample(1.0, [("a", {}, 1.0)])
            s.close()
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(p.parent.stat().st_mode), 0o700)
            self.assertEqual(p.name, "proj-srv.example.ge.sqlite")
            self.assertEqual(paths.monitor_db("p", "../../etc/x").parent, p.parent)             # host names cannot escape the folder
            self.assertNotIn("/", paths.monitor_db("p", "../../etc/x").name)

    def test_concurrent_writers_do_not_lose_rows(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "c.sqlite"
            stores = [store.Store(p) for _ in range(4)]

            def work(s, k):
                for i in range(40):
                    s.add_sample(float(k * 1000 + i), [("m", {"w": str(k)}, 1.0)])
            ts = [threading.Thread(target=work, args=(s, k)) for k, s in enumerate(stores)]
            [t.start() for t in ts]
            [t.join() for t in ts]
            self.assertEqual(stores[0].count(), 160)
            [s.close() for s in stores]

    def test_newer_schema_and_corrupt_files_give_clear_errors(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "n.sqlite"
            store.Store(p).close()
            db = sqlite3.connect(p)
            db.execute("PRAGMA user_version=99")
            db.close()
            with self.assertRaises(store.StoreError) as cm:
                store.Store(p)
            self.assertIn("newer gisstack", str(cm.exception))
            bad = Path(td) / "bad.sqlite"
            bad.write_bytes(b"this is not a database" * 100)
            with self.assertRaises(store.StoreError):
                store.Store(bad)


if __name__ == "__main__":
    unittest.main()
