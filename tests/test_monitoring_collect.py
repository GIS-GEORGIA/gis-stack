"""Phase 8: collector parsers run against output captured from a real Ubuntu 24.04 / PostgreSQL 18 / GeoServer / MapProxy / Caddy
server (tests/fixtures/monitor/) and real `docker stats` output; no synthetic data path exists."""
import math
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from gisstack import config
from gisstack.core import features
from gisstack.monitoring import collect

FIX = Path(__file__).parent / "fixtures/monitor"
NATIVE = (FIX / "native_ubuntu2404_pg18.txt").read_text()
COMPOSE_STATS = (FIX / "compose_docker_stats.txt").read_text()


def cfg(target="native", **kw):
    return config.resolve({"targets": [target], "deploy_target": target, "domain": "localhost", "tls": "none", "project": "e2e",
                           "postgres": {"version": "18"}, **kw})


ROWS = [{"id": c, "state": "running", "restarts": 0, "enabled": True} for c in ("postgis", "tomcat", "geoserver", "mapproxy", "caddy", "webmap")]


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.sample = collect.build_sample(cfg(), "native", NATIVE, ROWS, 1.5)

    def test_host_numbers_are_derived_exactly_from_the_proc_readings(self):
        h = self.sample["host"]
        self.assertEqual((h["load1"], h["load5"], h["load15"]), (1.35, 0.63, 0.24))
        self.assertEqual((h["uptime_seconds"], h["cpu_count"]), (91, 4))
        a = [11828, 0, 1671, 20530, 248, 0, 37, 2086, 0, 0]
        b = [11846, 0, 1706, 21040, 248, 0, 37, 2089, 0, 0]
        busy = sum(b) - sum(a) - ((b[3] + b[4]) - (a[3] + a[4]))
        self.assertAlmostEqual(h["cpu_percent"], round(100 * busy / (sum(b) - sum(a)), 2), places=2)
        self.assertEqual(h["mem_total_bytes"], 6067380 * 1024)
        self.assertAlmostEqual(h["mem_used_percent"], round(100 * (6067380 - 4628476) / 6067380, 2), places=2)
        self.assertNotIn("swap_used_percent", h)                                    # no swap -> no invented swap figure

    def test_disk(self):
        (d,) = self.sample["disks"]
        self.assertEqual(d["mount"], "/")
        self.assertEqual(d["total_bytes"], 29378688 * 1024)
        self.assertAlmostEqual(d["used_percent"], round(100 * 5446304 / (5446304 + 23916000), 2), places=2)

    def test_service_cpu_and_memory_come_from_cgroup_processes(self):
        by = {s["id"]: s for s in self.sample["services"]}
        self.assertEqual(by["tomcat"]["rss_bytes"], 755152 * 1024)
        self.assertAlmostEqual(by["tomcat"]["cpu_percent"], (11704 - 11702) / 100 / 1.0 * 100, places=2)      # 2 ticks in 1 s = 2 % of one core
        self.assertEqual(by["postgis"]["processes"], 9)
        self.assertEqual(by["postgis"]["cpu_percent"], 0.0)
        self.assertTrue(all(s["up"] for s in self.sample["services"]))

    def test_postgres(self):
        pg = self.sample["postgres"]
        self.assertEqual((pg["version"].split()[0], pg["max_connections"], pg["connections_total"]), ("18.6", 100, 1))
        self.assertEqual(pg["connections_percent"], 1.0)
        dbs = {d["name"]: d for d in pg["databases"]}
        self.assertEqual(set(dbs), {"gis", "gis_failed_20261104172113", "gis_old_20261104172021", "legacy", "postgres"})
        self.assertEqual(dbs["gis"]["size_bytes"], 17323711)
        self.assertEqual(dbs["gis"]["cache_hit_ratio"], round(155336 / (155336 + 103), 4))
        self.assertEqual((pg["longest_query_seconds"], pg["waiting_locks"]), (0, 0))

    def test_probes_use_each_checks_own_notion_of_up(self):
        p = {x["name"]: x for x in self.sample["probes"]}
        self.assertTrue(p["postgis:tcp_5432"]["up"])
        self.assertEqual(p["tomcat:http_8080"]["code"], "404")
        self.assertTrue(p["tomcat:http_8080"]["up"])                                 # kind "alive": any real answer
        self.assertTrue(p["geoserver:public"]["up"])                                 # 302 counts for kind "http"
        self.assertGreater(p["geoserver:wms_8080"]["seconds"], 0)

    def test_backup_age_is_measured_against_the_servers_own_clock(self):
        b = self.sample["backup"]
        self.assertEqual(b["count"], 3)                                              # pre-restore backups are not counted
        import calendar
        newest = 1793815406 - calendar.timegm((2026, 11, 4, 17, 21, 33))             # fixture ts minus 2026-11-04T17:21:33Z
        self.assertAlmostEqual(b["last_age_seconds"], newest, delta=2)

    def test_a_clean_sample_has_no_errors(self):
        self.assertEqual(self.sample["errors"], [])
        self.assertEqual(self.sample["took"], 1.5)

    def test_flatten_yields_finite_numbers_with_stable_names(self):
        flat = collect.flatten(self.sample)
        names = {n for n, _, _ in flat}
        for want in ("host_cpu_percent", "host_mem_used_percent", "disk_used_percent", "service_up", "service_rss_bytes", "pg_connections_percent",
                     "pg_database_size_bytes", "probe_up", "probe_seconds", "backup_last_age_seconds", "collect_seconds"):
            self.assertIn(want, names)
        self.assertTrue(all(math.isfinite(v) for _, _, v in flat))
        self.assertEqual({tuple(sorted(l.items())) for n, l, _ in flat if n == "service_up"} >= {(("service", "caddy"),)}, True)
        self.assertFalse([n for n in names if n.startswith("host_swap")])


class HonestyTests(unittest.TestCase):
    def test_missing_sources_are_reported_not_invented(self):
        s = collect.build_sample(cfg(), "native", "@@meta ts=1000\n@@host\nload 0.1 0.1 0.1\n@@end\n", ROWS, 0.1)
        self.assertEqual(s["host"], {"load1": 0.1, "load5": 0.1, "load15": 0.1})
        self.assertNotIn("cpu_percent", s["host"])
        self.assertNotIn("postgres", s)
        self.assertEqual(s["disks"], [])
        for want in ("host: cpu sample missing", "host: /proc/meminfo unreadable", "disk: df returned nothing", "postgres: no answer from psql"):
            self.assertIn(want, s["errors"])
        names = {n for n, _, _ in collect.flatten(s)}
        self.assertNotIn("host_cpu_percent", names)
        self.assertNotIn("disk_used_percent", names)
        self.assertEqual(next(v for n, _, v in collect.flatten(s) if n == "pg_up"), 0.0)     # pg_up=0 is a fact (it did not answer)

    def test_garbage_and_empty_input_never_raise(self):
        for text in ("", "\n\n", "@@", "@@host\nload x y z\ncpu0 a b c\ncpu1 1 2 3\n", "random noise\n" * 5, "@@postgres\nup|1\ndb|x|y\n"):
            s = collect.build_sample(cfg(), "native", text, ROWS, 0.0)
            self.assertIsInstance(s["errors"], list)
            collect.flatten(s)

    def test_cpu_percent_edge_cases(self):
        self.assertIsNone(collect._cpu_percent(["1", "2", "3", "4"], ["1", "2", "3", "4"]))        # no time elapsed
        self.assertIsNone(collect._cpu_percent(["1", "2"], ["1", "2"]))
        self.assertIsNone(collect._cpu_percent(["a", "b", "c", "d"], ["1", "2", "3", "4"]))
        self.assertEqual(collect._cpu_percent(["0", "0", "0", "0"], ["0", "0", "0", "100"]), 0.0)       # all idle
        self.assertEqual(collect._cpu_percent(["0", "0", "0", "0"], ["100", "0", "0", "0"]), 100.0)     # all busy

    def test_unknown_service_rows_without_usage_just_have_no_usage_keys(self):
        s = collect.build_sample(cfg(), "native", NATIVE, [{"id": "ghost", "state": "missing", "restarts": None, "enabled": None}], 0.0)
        ghost = s["services"][0]
        self.assertFalse(ghost["up"])
        self.assertNotIn("cpu_percent", ghost)
        self.assertNotIn("rss_bytes", ghost)


class ComposeTests(unittest.TestCase):
    def test_docker_stats_fixture(self):
        usage = collect.compose_usage(collect.parse(COMPOSE_STATS)["sections"]["stats"], "demo")
        self.assertEqual(set(usage), {"postgis", "caddy"})
        self.assertAlmostEqual(usage["postgis"]["rss_bytes"], 23.57 * 1024 ** 2, delta=1)
        self.assertEqual(usage["postgis"]["cpu_percent"], 0.06)
        self.assertAlmostEqual(usage["caddy"]["rss_bytes"], 1.477 * 1024 ** 2, delta=1)

    def test_other_projects_are_ignored_and_units_convert(self):
        rows = [["stat", "other-postgis-1|1.0%|1GiB", "/", "2GiB"], ["stat", "demo_qgis_2|5%|10MB", "/", "1GB"]]
        usage = collect.compose_usage(rows, "demo")
        self.assertEqual(set(usage), {"qgis_server"})
        self.assertEqual(usage["qgis_server"]["rss_bytes"], 10e6)
        self.assertEqual(collect._bytes("1.5KiB"), 1536)
        self.assertEqual(collect._bytes("garbage"), 0.0)

    def test_compose_sample_has_host_metrics_but_no_postgres_section(self):
        text = "@@meta ts=5\n@@host\nload 1 1 1\ncpus 2\nmem MemTotal: 1000\nmem MemAvailable: 500\ncpu0 0 0 0 0\ncpu1 10 0 10 80\n@@disk\ndisk / 1000 400 600\n" + COMPOSE_STATS
        s = collect.build_sample(cfg("compose", project="demo"), "compose", text, [{"id": "postgis", "state": "running", "restarts": None, "enabled": None}], 0.0)
        self.assertEqual(s["host"]["cpu_percent"], 20.0)
        self.assertEqual(s["services"][0]["cpu_percent"], 0.06)
        self.assertNotIn("postgres", s)
        self.assertNotIn("postgres: no answer from psql", s["errors"])

    def test_k8s_has_no_host_section_because_the_host_is_the_cluster(self):
        s = collect.build_sample(cfg("k8s"), "k8s", "@@meta ts=9\n", [{"id": "caddy", "state": "running", "restarts": 2, "enabled": None}], 0.0)
        self.assertNotIn("host", s)
        self.assertEqual(s["errors"], [])
        self.assertIn(("service_restarts", {"service": "caddy"}, 2.0), collect.flatten(s))


class ScriptTests(unittest.TestCase):
    def test_scripts_are_read_only(self):
        for target in ("native", "compose", "k8s"):
            sc = collect.script(cfg(target), target)
            for bad in ("rm -", "systemctl stop", "systemctl start", "systemctl restart", "apt", "dnf ", " > /etc", "kill ", "DROP ", "DELETE ", "UPDATE ", "INSERT "):
                self.assertNotIn(bad, sc, (target, bad))
            self.assertTrue(sc.rstrip().endswith("echo \"@@end\"") or target == "k8s")

    def test_native_script_names_the_postgres_unit_of_the_configured_version_and_only_selected_components(self):
        sc = collect.script(cfg(postgres={"version": "16"}, components={"mapproxy": False, "caddy": False, "geoserver": False, "tomcat": False}), "native")
        self.assertIn("postgresql@16-main.service", sc)
        self.assertNotIn("mapproxy", sc)

    @unittest.skipUnless(shutil.which("bash"), "bash needed")
    def test_the_native_script_really_runs_here_and_parses(self):
        """Host section against this very machine's /proc (services/postgres are simply absent here)."""
        sc = collect.script(cfg(components={"postgis": False, "tomcat": False, "geoserver": False, "mapproxy": False, "caddy": False, "webmap": False}), "native")
        r = subprocess.run(["bash", "-c", sc], capture_output=True, text=True, timeout=60)
        s = collect.build_sample(cfg(components={"postgis": False, "tomcat": False, "geoserver": False, "mapproxy": False, "caddy": False, "webmap": False}),
                                 "native", r.stdout, [], 1.0)
        self.assertGreater(s["host"]["mem_total_bytes"], 1e8)
        self.assertGreaterEqual(s["host"]["cpu_percent"], 0)
        self.assertTrue(s["disks"])
        self.assertNotIn("postgres", s)

    def test_collect_end_to_end_with_a_fake_transport(self):
        from gisstack import transport
        with mock.patch.object(transport, "run", side_effect=lambda mode, ssh, script, **kw: NATIVE if "@@host" in script else ""), \
                mock.patch("gisstack.providers.native.fetch_status", return_value=ROWS):
            s = collect.collect(cfg(components={"qgis_server": False}), "local", None)
        self.assertEqual(s["target"], "native")
        self.assertIn("host", s)
        with mock.patch.object(transport, "run", return_value=""), mock.patch("gisstack.providers.native.fetch_status", return_value=ROWS):
            from gisstack.core.errors import DeployError
            with self.assertRaises(DeployError):
                collect.collect(cfg(), "local", None)


class FeatureTests(unittest.TestCase):
    def test_flags(self):
        with mock.patch.dict("os.environ", {"GISSTACK_FEATURES": "monitoring, Prometheus ,bogus"}):
            self.assertTrue(features.enabled("monitoring"))
            self.assertTrue(features.enabled("prometheus"))
            self.assertFalse(features.enabled("dashboard"))
        with mock.patch.dict("os.environ", {"GISSTACK_FEATURES": "all"}):
            self.assertTrue(all(features.enabled(f) for f in features.KNOWN))
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(features.enabled("monitoring"))
        with self.assertRaises(KeyError):
            features.enabled("nope")


if __name__ == "__main__":
    unittest.main()
