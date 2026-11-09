"""/api/v1/stack-backups/*: permissions, validation, audit, contract, jobs, export.  The workload is a local directory (see test_stack_backup.LocalProvider):
unit tested, NOT against real containers (tests/integration covers that)."""
import io
import json
import os
import shutil
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from gisstack import deployments
from gisstack.backups import provider as prov_mod
from gisstack.backups.store import Store
from tests.test_api import ApiBase
from tests.test_stack_backup import LocalProvider

REAL_PROVIDER_FOR = prov_mod.provider_for
BODY = {"mode": "local", "config": {"deploy_target": "compose", "targets": ["compose"], "project": "apisb",
                                    "components": {"postgis": False, "geoserver": True, "tomcat": False, "qgis_server": False, "mapproxy": False, "caddy": False, "webmap": False}}}


class StackBackupApiTests(ApiBase):
    def setUp(self):
        super().setUp()
        self.vol = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.vol, True)
        (self.vol / "global.xml").write_text("<global>v1</global>")
        (self.vol / "ws").mkdir()
        (self.vol / "ws" / "a.xml").write_text("a")
        self.store = Store(Path(self.td) / "sb")
        self.local = None

        def make(cfg, mode, ssh, **kw):
            self.local = LocalProvider(cfg, self.vol, store=self.store, **kw)
            return self.local
        for p in (mock.patch.object(prov_mod, "provider_for", make), mock.patch("gisstack.api.routes_stackbackup.provider.provider_for", make),
                  mock.patch("gisstack.api.routes_stackbackup.local_problems", lambda *a, **k: None),
                  mock.patch.object(deployments, "verify", return_value={"state": "healthy", "rows": [{"id": "geoserver", "ok": True}]})):
            p.start()
            self.addCleanup(p.stop)

    def job(self, path, body, token=None):
        r = self.call("POST", path, token or self.tokens["admin"], body=body)
        d = self.data(r)
        for _ in range(400):
            j = self.data(self.call("GET", f"/jobs/{d['job']}"))
            if j["done"]:
                return j
            time.sleep(0.02)
        self.fail("job did not finish")

    def audit(self):
        p = Path(self.td) / "audit.log"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def make(self):
        j = self.job("/stack-backups/create", {**BODY, "components": ["geoserver"], "note": "api"})
        self.assertTrue(j["ok"], j["lines"])
        return self.data(self.call("POST", "/stack-backups/list", body=BODY))["backups"][0]["id"]

    def test_permissions_for_every_route(self):
        bid = self.make()
        reads = [("/stack-backups/list", BODY), ("/stack-backups/inspect", {**BODY, "id": bid}), ("/stack-backups/verify", {**BODY, "id": bid})]
        operates = [("/stack-backups/check", {**BODY, "id": bid}), ("/stack-backups/create", {**BODY, "components": ["geoserver"]})]
        admins = [("/stack-backups/restore", {**BODY, "id": bid, "confirm": True}), ("/stack-backups/recover", BODY), ("/stack-backups/delete", {**BODY, "id": bid, "confirm": True}),
                  ("/stack-backups/prune", BODY), ("/stack-backups/export", {**BODY, "id": bid})]
        for path, body in reads:
            self.assertEqual(self.call("POST", path, None, body=body).status_code, 401, path)
            self.assertEqual(self.call("POST", path, self.tokens["read"], body=body).status_code, 200, path)
        for path, body in operates:
            self.assertEqual(self.call("POST", path, self.tokens["read"], body=body).status_code, 403, path)
        for path, body in admins:
            for scope in ("read", "operate"):
                self.assertEqual(self.call("POST", path, self.tokens[scope], body=body).status_code, 403, (path, scope))
        self.assertEqual(self.call("POST", "/stack-backups/check", self.tokens["operate"], body={**BODY, "id": bid}).status_code, 200)

    def test_create_list_inspect_verify_contract_and_no_secrets(self):
        bid = self.make()
        lst = self.data(self.call("POST", "/stack-backups/list", self.tokens["read"], body=BODY))
        self.conforms("/stack-backups/list", "POST", lst)
        self.assertEqual((lst["backups"][0]["status"], lst["backups"][0]["note"], lst["restore_pending"]), ("ok", "api", False))
        self.assertTrue(any(c["id"] == "geoserver" and c["restorable"] for c in lst["components"]))
        self.assertFalse(next(c for c in lst["components"] if c["id"] == "mapproxy_config")["restorable"])
        ins = self.data(self.call("POST", "/stack-backups/inspect", body={**BODY, "id": bid}))
        self.assertEqual((ins["id"], ins["target"], ins["integrity"]["ok"]), (bid, "compose", True))
        rep = self.data(self.call("POST", "/stack-backups/verify", body={**BODY, "id": bid}))
        self.conforms("/stack-backups/verify", "POST", rep)
        self.assertTrue(rep["ok"])
        text = json.dumps([lst, ins, rep]) + (Path(self.td) / "audit.log").read_text()
        self.assertNotIn(str(self.store.root), text)                                              # no server paths in responses
        for secret in ("password", "passwd"):
            self.assertNotIn(secret, json.dumps(ins).lower())
        self.assertIn("stack_backup.create", [a["event"] for a in self.audit()])

    def test_bad_input_is_rejected(self):
        for path, body in (("/stack-backups/inspect", {**BODY, "id": "../../etc"}), ("/stack-backups/verify", {**BODY, "id": "nope"}),
                           ("/stack-backups/create", {**BODY, "components": ["../x"]}), ("/stack-backups/create", {**BODY, "components": []}),
                           ("/stack-backups/create", {**BODY, "note": "x" * 500}), ("/stack-backups/prune", {**BODY, "keep": 0}),
                           ("/stack-backups/delete", {**BODY, "id": "20260101T000000Z-aaaa"}), ("/stack-backups/restore", {**BODY, "id": "20260101T000000Z-aaaa"})):
            self.assertEqual(self.call("POST", path, body=body).status_code, 400, path)
        r = self.call("POST", "/stack-backups/inspect", body={**BODY, "id": "20260101T000000Z-aaaa"})
        self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (404, "not_found"))
        native = {"mode": "local", "config": {"deploy_target": "native", "targets": ["native"], "project": "apisb"}}
        with mock.patch("gisstack.api.routes_stackbackup.provider.provider_for", REAL_PROVIDER_FOR):
            r = self.call("POST", "/stack-backups/list", body=native)
        self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (400, "deploy"))             # native has its own backup tool

    def test_restore_needs_confirm_is_refused_when_unsafe_and_works_when_safe(self):
        bid = self.make()
        (self.vol / "global.xml").write_text("<global>v2</global>")
        self.assertEqual(self.call("POST", "/stack-backups/restore", body={**BODY, "id": bid, "confirm": False}).status_code, 400)
        self.assertEqual((self.vol / "global.xml").read_text(), "<global>v2</global>")
        d = self.store.path(bid)
        (d / "geoserver.tar.gz").write_bytes(b"corrupt")
        r = self.call("POST", "/stack-backups/restore", body={**BODY, "id": bid, "confirm": True})
        self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (409, "restore"))
        self.assertFalse(r.get_json()["error"]["details"]["ok"])
        self.assertEqual((self.vol / "global.xml").read_text(), "<global>v2</global>")
        refused = [a for a in self.audit() if a["event"] == "stack_backup.restore"]
        self.assertEqual((refused[-1]["ok"], refused[-1]["error"]), (False, "refused"))
        self.store.delete(bid)
        bid = self.make()
        j = self.job("/stack-backups/restore", {**BODY, "id": bid, "confirm": True})
        self.assertTrue(j["ok"], j["lines"])
        self.assertTrue(any("verified healthy" in x for x in j["lines"]))
        self.assertEqual((self.vol / "global.xml").read_text(), "<global>v2</global>")            # the backup was taken when the volume held v2
        ev = [a for a in self.audit() if a["event"] == "stack_backup.restore" and a.get("phase") == "start"]
        self.assertEqual((ev[-1]["backup"], ev[-1]["scope"]), (bid, "admin"))

    def test_failed_restore_job_reports_and_restores_the_previous_data(self):
        bid = self.make()
        (self.vol / "global.xml").write_text("<global>live</global>")
        with mock.patch.object(deployments, "verify", return_value={"state": "unhealthy", "rows": [{"id": "geoserver", "ok": False}]}):
            j = self.job("/stack-backups/restore", {**BODY, "id": bid, "confirm": True})
        self.assertFalse(j["ok"])
        self.assertTrue(any("previous data was put back" in x for x in j["lines"]), j["lines"])
        self.assertEqual((self.vol / "global.xml").read_text(), "<global>live</global>")

    def test_delete_prune_recover(self):
        a = self.make()
        time.sleep(1.1)
        b = self.make()
        self.assertEqual(self.call("POST", "/stack-backups/delete", body={**BODY, "id": a, "confirm": False}).status_code, 400)
        dry = self.data(self.call("POST", "/stack-backups/prune", body={**BODY, "keep": 1}))
        self.assertTrue(dry["dry_run"])
        self.assertEqual(dry["removed"], [a])
        self.assertTrue(self.store.path(a).exists())
        done = self.data(self.call("POST", "/stack-backups/prune", body={**BODY, "keep": 1, "dry_run": False}))
        self.assertEqual(done["removed"], [a])
        self.assertFalse(self.store.path(a).exists())
        self.assertTrue(self.store.path(b).exists())                                              # the newest intact backup survives
        self.assertEqual(self.data(self.call("POST", "/stack-backups/delete", body={**BODY, "id": b, "confirm": True}))["deleted"], b)
        j = self.job("/stack-backups/recover", BODY)
        self.assertTrue(j["ok"])
        self.assertIn("nothing to recover", j["lines"])
        events = [a_["event"] for a_ in self.audit()]
        for e in ("stack_backup.prune", "stack_backup.delete", "stack_backup.recover"):
            self.assertIn(e, events)

    def test_export_streams_a_verified_backup_and_refuses_a_damaged_one(self):
        bid = self.make()
        r = self.call("POST", "/stack-backups/export", body={**BODY, "id": bid})
        self.assertEqual((r.status_code, r.mimetype), (200, "application/x-tar"))
        with tarfile.open(fileobj=io.BytesIO(r.data)) as tf:
            self.assertIn(f"{bid}/manifest.json", tf.getnames())
            self.assertTrue(all(n.startswith(bid) for n in tf.getnames()))
        self.assertEqual([f for f in os.listdir(tempfile.gettempdir()) if f.startswith("gsexport")], [])    # the temp file is gone
        (self.store.path(bid) / "geoserver.tar.gz").write_bytes(b"corrupt")
        r = self.call("POST", "/stack-backups/export", body={**BODY, "id": bid})
        self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (409, "integrity"))
        self.assertIn("stack_backup.export", [a["event"] for a in self.audit()])


if __name__ == "__main__":
    unittest.main()
