"""S-5: deployment snapshots must not keep credentials.  A rollback takes every password / token / secret from the CURRENT secret store, so a snapshot
has no use for them - and an old one would go stale (or leak) after a rotation."""
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gisstack import config, deployments, services
from gisstack.core import paths

PG, GS, TC, EXTRA = "pg-SNAPSHOT-secret-1", "gs-SNAPSHOT-secret-2", "tc-SNAPSHOT-secret-3", "tok-SNAPSHOT-secret-4"


def cfg_with_secrets(**over):
    c = config.resolve({"project": "snap", "deploy_target": "compose", "domain": "gis.example.org", **over}, persist_secrets=False, generate_secrets=False)
    c["postgres"]["password"], c["geoserver"]["admin_password"], c["tomcat"]["manager_password"] = PG, GS, TC
    c["webmap"]["api_token"] = EXTRA                                                           # a nested token-like key and a list holding one
    c["extras"] = [{"name": "x", "secret_key": EXTRA}]
    return c


class SnapshotSecretTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        p = mock.patch.dict(os.environ, {"GISSTACK_HOME": self.td, "GISSTACK_VERIFY_TIMEOUT": "0"})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.td, True)

    def everything(self):
        """The text of every file under the state directory."""
        return {str(f.relative_to(self.td)): f.read_text(errors="ignore") for f in Path(self.td).rglob("*") if f.is_file()}

    def test_a_new_snapshot_and_record_hold_no_credential_and_stay_private(self):
        rec = deployments.begin(cfg_with_secrets(), "compose", "local", "localhost")
        deployments.finish(rec, "ok")
        files = self.everything()
        self.assertTrue(files, "something was written")
        for name, text in files.items():
            for secret in (PG, GS, TC, EXTRA):
                self.assertNotIn(secret, text, f"{name} contains a credential")
        snap = paths.deployments_dir() / f"{rec['id']}.config"
        self.assertEqual(stat.S_IMODE(snap.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(snap.parent.stat().st_mode) & 0o077, 0)
        got = deployments.snapshot(rec["id"])
        self.assertEqual((got["postgres"]["password"], got["geoserver"]["admin_password"], got["tomcat"]["manager_password"]), ("", "", ""))
        self.assertEqual(got["webmap"]["api_token"], "")
        self.assertEqual(got["extras"][0]["secret_key"], "")                                   # lists are walked too
        self.assertEqual(got["domain"], "gis.example.org")                                     # everything else is kept
        self.assertEqual(got["postgres"]["version"], "17")

    def test_rollback_re_applies_the_earlier_settings_with_the_current_secrets(self):
        rec = deployments.begin(cfg_with_secrets(), "compose", "local", "localhost")
        deployments.finish(rec, "ok", after=[{"id": "postgis", "ok": True}])
        current = cfg_with_secrets()
        current["postgres"]["password"] = "rotated-CURRENT-password"                           # rotated after that deployment
        merged = deployments.with_current_secrets(deployments.snapshot(rec["id"]), current)
        self.assertEqual(merged["postgres"]["password"], "rotated-CURRENT-password")
        self.assertEqual(merged["geoserver"]["admin_password"], GS)
        self.assertEqual(merged["tomcat"]["manager_password"], TC)
        self.assertEqual(merged["domain"], current["domain"])

    def test_a_stripped_snapshot_is_still_a_valid_configuration(self):
        rec = deployments.begin(cfg_with_secrets(), "compose", "local", "localhost")
        stripped = deployments.snapshot(rec["id"])
        stripped.pop("_meta", None)
        again = config.resolve(stripped, generate_secrets=True, persist_secrets=False)         # empty secrets mean "use / generate the stored one"
        self.assertTrue(again["postgres"]["password"])
        self.assertNotEqual(again["postgres"]["password"], PG)                                 # a NEW secret, never the old one resurrected from disk

    def test_snapshots_written_by_earlier_versions_are_scrubbed_and_stay_readable(self):
        d = paths.deployments_dir()
        d.mkdir(parents=True, exist_ok=True)
        legacy = d / "legacy000001.config"
        legacy.write_text(json.dumps(cfg_with_secrets(), default=str))
        legacy.chmod(0o600)
        (d / "legacy000001.json").write_text(json.dumps({"id": "legacy000001", "ts": 1, "n": 1, "kind": "deploy", "project": "snap", "target": "compose", "mode": "local",
                                                          "host": "localhost", "digest": "x", "before": [], "after": [{"id": "postgis", "ok": True}], "outcome": "ok", "error": "",
                                                          "seconds": 1.0, "rollback_of": ""}))
        self.assertIn(PG, legacy.read_text())
        rec = deployments.begin(cfg_with_secrets(), "compose", "local", "localhost")           # any later deployment cleans the old files
        deployments.finish(rec, "ok")
        text = legacy.read_text()
        for secret in (PG, GS, TC, EXTRA):
            self.assertNotIn(secret, text)
        self.assertEqual(stat.S_IMODE(legacy.stat().st_mode), 0o600)
        self.assertEqual(deployments.snapshot("legacy000001")["domain"], "gis.example.org")  # still readable
        self.assertTrue([r for r in deployments.records("snap") if r["id"] == "legacy000001"])   # and listed
        self.assertEqual(deployments.scrub_snapshots(), 0)                                     # idempotent

    def test_a_damaged_snapshot_is_left_alone_not_crashed_on(self):
        paths.deployments_dir().mkdir(parents=True, exist_ok=True)
        bad = paths.deployments_dir() / "damaged00001.config"
        bad.write_text("{not json")
        self.assertEqual(deployments.scrub_snapshots(), 0)
        self.assertEqual(bad.read_text(), "{not json")

    def test_rollback_config_through_the_service_layer_uses_current_secrets_end_to_end(self):
        cfg = cfg_with_secrets()
        rec = deployments.begin(cfg, "compose", "local", "localhost")
        deployments.finish(rec, "ok", after=[{"id": "postgis", "ok": True}])
        newer = deployments.begin(cfg_with_secrets(domain="other.example.org"), "compose", "local", "localhost")
        deployments.finish(newer, "unhealthy", after=[{"id": "postgis", "ok": False}])
        current = cfg_with_secrets(domain="other.example.org")
        current["postgres"]["password"] = "rotated-CURRENT-password"
        snap, plan = services.rollback_config(current, "local", None, "compose", rec["id"])
        self.assertEqual(snap["domain"], "gis.example.org")                                    # the earlier settings
        self.assertEqual(snap["postgres"]["password"], "rotated-CURRENT-password")             # the current secrets
        self.assertTrue(plan["eligible"])


if __name__ == "__main__":
    unittest.main()
