"""Monitoring on Compose / Podman / Kubernetes: PostgreSQL health and the state of the stack backups, and the alert that follows a failed backup.
The real-engine proof is tests/integration/test_podman_reliability.py (and the Compose run); these tests pin the parsing and the alert lifecycle."""
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
from test_monitoring_service import Base, ROWS  # noqa: E402

from gisstack import config  # noqa: E402
from gisstack.monitoring import collect, service  # noqa: E402

NOW = 1_800_000_000.0
STAMP = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 3600))


def ccfg():
    return config.resolve({"project": "monc", "runtime": "docker", "targets": ["compose"], "deploy_target": "compose", "domain": "localhost", "tls": "none",
                           "components": {"postgis": True, "geoserver": False, "tomcat": False, "qgis_server": False, "mapproxy": False, "caddy": False, "webmap": False}},
                          generate_secrets=False, persist_secrets=False)


def row(bid, status="ok", kind="manual", created=STAMP):
    return {"id": bid, "status": status, "kind": kind, "created": created if status == "ok" else ""}


class StatsTests(unittest.TestCase):
    def test_age_count_and_failures_since_the_newest_good_backup(self):
        rows = [row("20261110T100000Z-aaaa"), row("20261109T100000Z-bbbb")]
        st = collect.stack_backup_stats(rows, NOW)
        self.assertEqual((st["count"], st["failed"]), (2.0, 0.0))
        self.assertAlmostEqual(st["last_age_seconds"], 3600.0, delta=1)

    def test_a_failed_attempt_after_the_last_good_backup_counts_and_a_later_good_one_clears_it(self):
        failed = row("20261110T120000Z-ffff", "failed")
        self.assertEqual(collect.stack_backup_stats([failed, row("20261110T100000Z-aaaa")], NOW)["failed"], 1.0)
        self.assertEqual(collect.stack_backup_stats([row("20261110T130000Z-cccc"), failed, row("20261110T100000Z-aaaa")], NOW)["failed"], 0.0)

    def test_a_failure_in_the_same_second_as_the_newest_good_backup_counts(self):
        """Found on GitHub: the random id suffix decided the order of two entries made within one second, and the failed attempt sometimes lost."""
        for suffix in ("0000", "zzzz"):
            rows = [row("20261110T100000Z-aaaa"), row(f"20261110T100000Z-{suffix}", "failed")]
            self.assertEqual(collect.stack_backup_stats(rows, NOW)["failed"], 1.0, suffix)
        self.assertEqual(collect.stack_backup_stats([row("20261110T100001Z-aaaa"), row("20261110T100000Z-zzzz", "failed")], NOW)["failed"], 0.0)   # an earlier second is older

    def test_partial_and_damaged_count_pre_restore_backups_do_not_count_as_the_regular_backup(self):
        st = collect.stack_backup_stats([row("20261110T120000Z-pppp", "partial"), row("20261110T110000Z-dddd", "damaged"),
                                         row("pre-20261110T100000Z-eeee", kind="pre-restore")], NOW)
        self.assertEqual((st["count"], st["failed"]), (0.0, 2.0))
        self.assertNotIn("last_age_seconds", st)                                          # no regular backup at all: no age (the rule needs a number to fire)


class PodmanStatsParsing(unittest.TestCase):
    def test_podman_stats_lines_with_podman_compose_and_docker_compose_names(self):
        text = ("@@stats\nstat monc_postgis_1|0.52%|45.3MB / 31.2GB\nstat monc_geoserver_1|12.00%|1.2GB / 31.2GB\nstat other_postgis_1|9%|1MB / 2GB\n"
                "stat monc-qgis-1|0.00%|812.5kB / 31.2GB\n@@end\n")
        u = collect.compose_usage(collect.parse(text)["sections"]["stats"], "monc")
        self.assertEqual(sorted(u), ["geoserver", "postgis", "qgis_server"])               # another project's container is ignored
        self.assertEqual(u["postgis"]["cpu_percent"], 0.52)
        self.assertAlmostEqual(u["postgis"]["rss_bytes"], 45.3e6, delta=2e6)
        self.assertGreater(u["geoserver"]["rss_bytes"], 1e9)

    def test_an_engine_that_returns_no_rows_gives_no_usage_and_no_invented_numbers(self):
        self.assertEqual(collect.compose_usage(collect.parse("@@stats\n@@end\n")["sections"].get("stats", []), "monc"), {})


class NativeBackupTests(unittest.TestCase):
    def test_native_list_lines_give_age_and_failures_since_the_newest_complete_backup(self):
        c = "20261110T100000Z-aaaa"
        rows = [["backup", c, "complete", "manual", STAMP, "10", "postgres"], ["backup", "20261110T120000Z-bbbb", "failed", "", "", "0", ""],
                ["backup", "20261109T120000Z-cccc", "failed", "", "", "0", ""]]
        m = collect.backup_metrics(rows, NOW)
        self.assertEqual((m["count"], m["failed"]), (1, 1))                               # the older failure is behind a good backup
        self.assertAlmostEqual(m["last_age_seconds"], 3600.0, delta=1)
        self.assertEqual(collect.backup_metrics([], NOW), {"count": 0})
        same = [["backup", c, "complete", "manual", STAMP, "10", "postgres"], ["backup", "20261110T100000Z-0000", "failed", "", "", "0", ""]]
        self.assertEqual(collect.backup_metrics(same, NOW)["failed"], 1)                    # same second: counted, whatever the random suffix says
        self.assertEqual(collect.backup_metrics([rows[1]], NOW)["failed"], 1)             # only failures: still reported, and no age to hide behind


class FakeProv:
    db = "gis"

    def __init__(self, rows, psql):
        self.store = mock.Mock()
        self.store.list.return_value = rows
        self._psql = psql

    def psql(self, sql, **kw):
        if isinstance(self._psql, Exception):
            raise self._psql
        return self._psql


class ExtrasTests(unittest.TestCase):
    def extras(self, prov):
        with mock.patch("gisstack.backups.provider.provider_for", return_value=prov):
            flat, errors = collect.container_extras(ccfg(), "local", None, NOW)
        return {n: v for n, _, v in flat}, errors

    def test_healthy_database_and_backups_are_reported(self):
        m, errors = self.extras(FakeProv([row("20261110T100000Z-aaaa")], "7|100|12.5|0"))
        self.assertEqual(errors, [])
        self.assertEqual((m["pg_up"], m["pg_connections_total"], m["pg_max_connections"], m["pg_connections_percent"]), (1.0, 7.0, 100.0, 7.0))
        self.assertEqual((m["pg_longest_query_seconds"], m["pg_waiting_locks"], m["backup_count"], m["backup_failed_count"]), (12.5, 0.0, 1.0, 0.0))

    def test_a_database_that_does_not_answer_is_pg_up_zero_not_a_gap(self):
        m, errors = self.extras(FakeProv([], RuntimeError("connection refused")))
        self.assertEqual(m["pg_up"], 0.0)
        self.assertEqual(errors, ["postgres: no answer (RuntimeError)"])
        self.assertNotIn("backup_last_age_seconds", m)

    def test_an_unreadable_backup_store_is_an_error_but_the_database_is_still_checked(self):
        prov = FakeProv([], "1|100|0|0")
        prov.store.list.side_effect = OSError("disk gone")
        m, errors = self.extras(prov)
        self.assertEqual(m["pg_up"], 1.0)
        self.assertTrue(errors and errors[0].startswith("backup store: OSError"))


class AlertLifecycle(Base):
    def collect_with(self, rows, psql="3|100|0|0"):
        def f(c, mode, ssh, target=None):
            s = collect.build_sample(c, "compose", f"@@meta ts={int(NOW)}\n@@end\n", [{**r} for r in ROWS[:1]], 1.0)
            return s
        return mock.patch.object(collect, "collect", side_effect=f), mock.patch("gisstack.backups.provider.provider_for", return_value=FakeProv(rows, psql))

    def test_failed_backup_fires_a_critical_alert_and_a_good_backup_resolves_it(self):
        mon = service.Monitor(ccfg(), "local", None, providers=[])
        self.addCleanup(mon.store.close)
        t = [NOW]
        mon.clock = lambda: t[0]
        good = row("20261110T100000Z-aaaa")
        c1, p1 = self.collect_with([row("20261110T120000Z-ffff", "failed"), good])
        with c1, p1:
            res = mon.tick()
        fired = [n for n in res["notifications"] if n["rule"] == "backup_failed"]
        self.assertEqual([(n["state"], n["level"]) for n in fired], [("firing", "crit")], res["notifications"])
        self.assertIn("1 backup attempt(s) failed", fired[0]["message"])
        self.assertEqual([a["rule"] for a in service.snapshot(mon.store, NOW)["alerts"] if a["rule"] == "backup_failed"], ["backup_failed"])
        t[0] += 60
        c2, p2 = self.collect_with([row("20261110T130000Z-cccc"), row("20261110T120000Z-ffff", "failed"), good])
        with c2, p2:
            res = mon.tick()
        self.assertEqual([(n["rule"], n["state"]) for n in res["notifications"] if n["rule"] == "backup_failed"], [("backup_failed", "resolved")])
        self.assertEqual([a for a in service.snapshot(mon.store, NOW + 60)["alerts"] if a["rule"] == "backup_failed"], [])

    def test_postgres_down_is_reported_through_the_container_path(self):
        mon = service.Monitor(ccfg(), "local", None, providers=[])
        self.addCleanup(mon.store.close)
        t = [NOW]
        mon.clock = lambda: t[0]
        fired = []
        for _ in range(2):                                                                # the rule needs two consecutive rounds
            c, p = self.collect_with([row("20261110T100000Z-aaaa")], psql=RuntimeError("down"))
            with c, p:
                fired += mon.tick()["notifications"]
            t[0] += 60
        self.assertEqual([n["rule"] for n in fired], ["postgres_down"])


if __name__ == "__main__":
    unittest.main()
