"""Disk-capacity alert end to end on a REAL machine (a throw-away systemd container), without ever touching the host's disk.  Opt-in: GISSTACK_TEST_INSTALLER=1.

/var/lib/gisstack inside the container is a 24 MB tmpfs: it is filled to ~92 % with a file, the real collection script (df) reports it, the built-in
`disk_high` rule fires and the notice reaches a real HTTP receiver; removing the file resolves the alert and the receiver gets the resolved notice;
staying full sends no duplicate.  Only the container's own tmpfs is filled."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gisstack import config, transport  # noqa: E402
from gisstack.monitoring import notify, service  # noqa: E402

from . import sysd  # noqa: E402
from tests.test_monitoring_delivery import Receiver  # noqa: E402

MOUNT = "/var/lib/gisstack"


@unittest.skipUnless(sysd.enabled(), "set GISSTACK_TEST_INSTALLER=1 (needs docker)")
class DiskAlertTests(unittest.TestCase):
    def test_disk_warning_fires_notifies_resolves_and_is_not_repeated(self):
        rx = Receiver()
        rx.start()
        self.addCleanup(rx.stop)
        home = tempfile.mkdtemp()
        p = mock.patch.dict(os.environ, {"GISSTACK_HOME": home})
        p.start()
        self.addCleanup(p.stop)
        cfg = config.resolve({"project": "disk", "targets": ["native"], "deploy_target": "native", "components": {"postgis": False, "tomcat": False, "geoserver": False,
                              "qgis_server": False, "mapproxy": False, "caddy": False, "webmap": False}}, generate_secrets=False, persist_secrets=False)
        with sysd.Box("ubuntu-24.04") as box:
            box.sh(f"mkdir -p {MOUNT} && mount -t tmpfs -o size=24m tmpfs {MOUNT}")
            with mock.patch.object(transport, "argv", lambda mode, ssh, script, *, root: box.argv(script)):
                mon = service.Monitor(cfg, "local", None, providers=[notify.WebhookProvider(rx.url, timeout=5, retries=0)])
                self.addCleanup(mon.store.close)

                def used():
                    return {s["labels"]["mount"]: s["value"] for s in service.snapshot(mon.store)["metrics"].get("disk_used_percent", [])}
                mon.tick()
                self.assertLess(used().get(MOUNT, 100), 10, used())                             # the collector really sees this mount, empty
                self.assertEqual(rx.got, [])
                box.sh(f"dd if=/dev/zero of={MOUNT}/fill bs=1M count=22 status=none")           # ~92 % of the 24 MB tmpfs
                res = mon.tick()
                self.assertGreater(used()[MOUNT], 85, used())
                self.assertEqual([(n["rule"], n["state"]) for n in res["notifications"] if n["subject"] == MOUNT], [("disk_high", "firing")], res["notifications"])
                self.assertEqual([t for t in rx.texts() if t.startswith("disk_high")], [f"disk_high:{MOUNT}:firing"])
                for _ in range(3):
                    mon.tick()
                self.assertEqual(len([t for t in rx.texts() if t.startswith("disk_high")]), 1, "no duplicate while it stays full")
                box.sh(f"rm -f {MOUNT}/fill")
                res = mon.tick()
                self.assertEqual([(n["rule"], n["state"]) for n in res["notifications"] if n["subject"] == MOUNT], [("disk_high", "resolved")])
                self.assertEqual([t for t in rx.texts() if t.startswith("disk_high")], [f"disk_high:{MOUNT}:firing", f"disk_high:{MOUNT}:resolved"])
                self.assertEqual([a for a in service.snapshot(mon.store)["alerts"] if a["subject"] == MOUNT], [])


if __name__ == "__main__":
    unittest.main()
