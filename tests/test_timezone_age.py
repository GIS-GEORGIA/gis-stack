"""Backup ids and `created` stamps are UTC ("...Z"): ages and retention must not depend on the machine's time zone or on DST
(found in the audit of 2026-11: `time.mktime(...) - time.timezone` is wrong by the UTC offset for retention and by one hour in summer time for the age metrics)."""
import calendar
import os
import tempfile
import time
import unittest
from pathlib import Path

from gisstack.backups import store as bstore
from gisstack.monitoring import collect


class TimezoneIndependence(unittest.TestCase):
    ZONES = ("UTC", "Asia/Tbilisi", "America/Los_Angeles", "Pacific/Kiritimati", "Europe/Berlin")

    def setUp(self):
        self.old = os.environ.get("TZ")
        self.addCleanup(self._restore)

    def _restore(self):
        if self.old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.old
        time.tzset()

    def zone(self, tz):
        os.environ["TZ"] = tz
        time.tzset()

    def test_backup_age_is_utc_in_every_zone_and_season(self):
        for created in ("2026-01-15T12:00:00Z", "2026-07-15T12:00:00Z"):       # winter and summer time
            epoch = calendar.timegm(time.strptime(created, "%Y-%m-%dT%H:%M:%SZ"))
            for tz in self.ZONES:
                self.zone(tz)
                rows = [["backup", "20260101T000000Z-aaaa", "complete", "manual", created, "10", "config"]]
                self.assertEqual(collect.backup_metrics(rows, epoch + 3600)["last_age_seconds"], 3600, (tz, created))
                store_rows = [{"status": "ok", "kind": "manual", "id": "20260101T000000Z-aaaa", "created": created}]
                self.assertEqual(collect.stack_backup_stats(store_rows, epoch + 3600)["last_age_seconds"], 3600, (tz, created))

    def test_retention_by_days_uses_utc(self):
        bid = "20260710T120000Z-aaaa"
        epoch = calendar.timegm(time.strptime("20260710T120000Z", "%Y%m%dT%H%M%SZ"))
        for tz in self.ZONES:
            self.zone(tz)
            with tempfile.TemporaryDirectory() as d:
                st = bstore.Store(Path(d))
                rows = [{"id": "20260711T120000Z-bbbb", "status": "ok", "kind": "manual"}, {"id": bid, "status": "ok", "kind": "manual"}]   # newest first
                st.list = lambda rows=rows: rows                                   # only the age rule is under test
                just_inside = st.prune(keep=1, days=2, dry_run=True, now=epoch + 2 * 86400 - 60)
                just_outside = st.prune(keep=1, days=2, dry_run=True, now=epoch + 2 * 86400 + 60)
                self.assertEqual(just_inside["removed"], [], tz)
                self.assertEqual(just_outside["removed"], [bid], tz)


if __name__ == "__main__":
    unittest.main()


class ConfigBackupIdsAreUtc(unittest.TestCase):
    """MapProxy configuration backups are named by time and pruned by name (`sort | head -n -30`).  A local-time id sorts wrongly after a
    DST fall-back (the newest backup looks older than the previous hour's and is pruned first), so every id generator must use `date -u`."""

    def test_every_id_generator_uses_utc(self):
        src = (Path(collect.__file__).parent.parent / "mapproxy" / "server.py").read_text(encoding="utf-8")
        gens = [ln for ln in src.splitlines() if 'id="$(date' in ln]
        self.assertGreaterEqual(len(gens), 2)
        for ln in gens:
            self.assertIn("date -u ", ln)
