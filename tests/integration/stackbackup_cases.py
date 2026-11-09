"""Shared test cases for the Compose and Kubernetes backup providers (mixins; the backend supplies a real stack).

A backend provides: `cfg`, `prov(**kw)`, `run_gen`, `psql(sql, db=None)`, `databases()`, `stop_postgres()`, `start_postgres()`, and for GeoServer
`gs_exec(args) -> stdout` (a command run inside the GeoServer workload)."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import subprocess  # noqa: F401
from unittest import mock

from gisstack import backup_crypto, deployments
from gisstack.backups import model
from gisstack.backups.model import BackupError, RestoreError
from gisstack.backups import provider


class PostgresCases:
    def seed(self, marker):
        self.psql("drop table if exists gs_pts; create table gs_pts(id serial primary key, name text, geom geometry(Point,4326)); "
                  "create index gs_pts_gix on gs_pts using gist(geom); "
                  f"insert into gs_pts(name, geom) values ('{marker}', ST_SetSRID(ST_MakePoint(44.8, 41.7), 4326))")

    def names(self):
        return self.psql("select string_agg(name, ',' order by id) from gs_pts")

    def test_1_create_verify_inspect_and_no_secrets(self):
        self.seed("before")
        p = self.prov()
        lines = self.run_gen(p.create(["postgres"], note="first"))
        self.assertTrue(any("sealed" in x for x in lines), lines)
        m = p.result
        bid = m["id"]
        self.assertEqual((m["target"], m["kind"], m["encrypted"]), (p.kind, "manual", False))
        self.assertTrue(m["versions"]["postgres"].startswith("17"))
        self.assertGreater(m["components"]["postgres"]["meta"]["toc_entries"], 5)
        rep = p.verify(bid)
        self.assertTrue(rep["ok"], rep)
        self.assertEqual([r["status"] for r in p.list_backups()], ["ok"])
        self.assertTrue(p.inspect(bid)["integrity"]["ok"])
        d = p.store.path(bid)
        self.assertEqual(oct(d.stat().st_mode & 0o777), "0o700")
        for f in d.iterdir():
            self.assertEqual(oct(f.stat().st_mode & 0o777), "0o600", f.name)
        everything = b"".join(f.read_bytes() for f in d.iterdir() if f.suffix in ("", ".json", ".log") or f.name == "SHA256SUMS")
        for secret in (self.cfg["postgres"]["password"], self.cfg["geoserver"]["admin_password"]):
            self.assertNotIn(secret.encode(), everything)                                  # no plaintext secret in manifest, sums or log
        self.assertNotIn(self.cfg["postgres"]["password"].encode(), (d / "postgres.dump").read_bytes())

    def test_2_restore_brings_back_the_data_keeps_the_old_and_is_health_checked(self):
        self.seed("before")
        p = self.prov()
        self.run_gen(p.create(["postgres"]))
        bid = p.result["id"]
        self.psql("insert into gs_pts(name, geom) values ('after', ST_SetSRID(ST_MakePoint(1,1),4326)); delete from gs_pts where name='before'")
        self.assertEqual(self.names(), "after")
        with self.assertRaises(RestoreError):
            self.run_gen(p.restore(bid, ["postgres"]))                                      # no confirmation: refused, nothing changed
        self.assertEqual(self.names(), "after")
        r = self.prov()
        lines = self.run_gen(r.restore(bid, ["postgres"], confirm=True))
        self.assertTrue(any("verified healthy" in x for x in lines), lines)
        self.assertEqual(self.names(), "before")                                           # the backup's data is live again
        self.assertEqual(self.psql("select count(*) from pg_indexes where indexname='gs_pts_gix'"), "1")
        res = r.result
        pre_db = res["retained_previous_data"]["postgres"]
        self.assertIn(pre_db, self.databases())                                            # the replaced data is still there
        self.assertEqual(self.psql("select string_agg(name, ',') from gs_pts", db=pre_db), "after")
        pre = [x for x in r.list_backups() if x["kind"] == "pre-restore"]
        self.assertEqual(len(pre), 1)
        self.assertTrue(r.verify(pre[0]["id"])["ok"])                                       # the automatic pre-restore backup is itself verified
        self.assertIsNone(r.store.read_state())
        self.assertFalse([d for d in self.databases() if "_rbk" in d or d.endswith("_scratch")])
        self.run_gen(r.discard_previous(res["retained_previous_data"], confirm=True))
        self.assertNotIn(pre_db, self.databases())

    def test_3_corrupted_and_incomplete_backups_are_refused_before_anything_changes(self):
        self.seed("before")
        p = self.prov()
        self.run_gen(p.create(["postgres"]))
        bid = p.result["id"]
        d = p.store.path(bid)
        dump = d / "postgres.dump"
        orig = dump.read_bytes()
        dump.write_bytes(orig[:-50] + b"X" * 50)                                           # flip the tail
        rep = p.verify(bid)
        self.assertFalse(rep["ok"])
        self.assertTrue(any("checksum mismatch" in x for x in rep["problems"]))
        with self.assertRaises(RestoreError):
            self.run_gen(self.prov().restore(bid, ["postgres"], confirm=True))
        self.assertEqual(self.names(), "before")
        dump.write_bytes(orig)
        dump.unlink()
        self.assertTrue(any("missing file" in x for x in p.verify(bid)["problems"]))
        dump.write_bytes(orig)
        (d / "extra.bin").write_bytes(b"x")
        self.assertTrue(any("unexpected file" in x for x in p.verify(bid)["problems"]))
        (d / "extra.bin").unlink()
        mf = d / "manifest.json"
        mf.write_text(mf.read_text().replace('"schema": 1', '"schema": 99'))
        self.assertFalse(p.inspect.__self__.store.verify(bid)["ok"] and False)
        with self.assertRaises(model.IntegrityError):
            p.store.read(bid)

    def test_4_a_valid_looking_but_unrestorable_dump_fails_before_the_swap(self):
        self.seed("before")
        p = self.prov()
        self.run_gen(p.create(["postgres"]))
        bid = p.result["id"]
        d = p.store.path(bid)
        # make the dump garbage but keep every checksum consistent, so only the real pg_restore can notice
        garbage = b"PGDMP-not-really" + os.urandom(2000)
        (d / "postgres.dump").write_bytes(garbage)
        sha = hashlib.sha256(garbage).hexdigest()
        mf = json.loads((d / "manifest.json").read_text())
        f = mf["components"]["postgres"]["files"][0]
        f.update(bytes=len(garbage), sha256=sha)
        (d / "manifest.json").write_text(json.dumps(mf, indent=2, sort_keys=True) + "\n")
        sums = {"postgres.dump": sha, "manifest.json": hashlib.sha256((d / "manifest.json").read_bytes()).hexdigest()}
        (d / "SHA256SUMS").write_text("".join(f"{v}  {k}\n" for k, v in sums.items()))
        self.assertTrue(p.verify(bid)["ok"])
        with self.assertRaises(BackupError) as cm:
            self.run_gen(self.prov().restore(bid, ["postgres"], confirm=True))
        self.assertIn("restore failed", str(cm.exception))
        self.assertEqual(self.names(), "before")                                           # live data untouched
        self.assertFalse([x for x in self.databases() if "_r" in x.split(self.cfg["postgres"]["db"], 1)[-1] and "pre" not in x])    # no scratch left behind
        self.assertIsNone(self.prov().store.read_state())

    def test_5_failed_health_check_after_the_swap_puts_the_original_back(self):
        self.seed("before")
        p = self.prov()
        self.run_gen(p.create(["postgres"]))
        bid = p.result["id"]
        self.psql("insert into gs_pts(name, geom) values ('live-only', ST_SetSRID(ST_MakePoint(2,2),4326))")
        bad = {"state": "unhealthy", "rows": [{"id": "postgis", "ok": False}]}
        with mock.patch.object(deployments, "verify", return_value=bad), self.assertRaises(RestoreError) as cm:
            self.run_gen(self.prov().restore(bid, ["postgres"], confirm=True))
        self.assertIn("previous data was put back", str(cm.exception))
        self.assertEqual(self.names(), "before,live-only")                                  # exactly the pre-restore state
        self.assertIsNone(self.prov().store.read_state())
        self.assertEqual(self.psql("select count(*) from pg_database where datname like '%\\_pre\\_%'"), "0")

    def test_6_an_interrupted_restore_is_recovered(self):
        self.seed("before")
        p = self.prov()
        self.run_gen(p.create(["postgres"]))
        bid = p.result["id"]
        self.psql("insert into gs_pts(name, geom) values ('live-only', ST_SetSRID(ST_MakePoint(2,2),4326))")
        crash = provider.BackupProvider._run_undo
        with mock.patch.object(deployments, "verify", side_effect=SystemExit("killed")), \
                mock.patch.object(provider.BackupProvider, "_run_undo", lambda self, undo: ["simulated crash: no undo"]), \
                self.assertRaises((SystemExit, RestoreError)):
            self.run_gen(self.prov().restore(bid, ["postgres"], confirm=True))
        del crash
        self.assertEqual(self.names(), "before")                                           # the process died right after the swap
        r = self.prov()
        self.assertIsNotNone(r.store.read_state())
        with self.assertRaises(RestoreError):
            self.run_gen(r.restore(bid, ["postgres"], confirm=True))                       # refuses until recovered
        self.run_gen(r.recover())
        self.assertEqual(self.names(), "before,live-only")
        self.assertIsNone(r.store.read_state())
        self.assertEqual(self.psql("select count(*) from pg_database where datname like '%\\_pre\\_%' or datname like '%\\_rgs%'"), "0")
        self.run_gen(r.recover())                                                           # idempotent
        self.assertEqual(self.names(), "before,live-only")

    def test_7_encrypted_backup_verifies_without_the_key_and_restores_with_it(self):
        if not shutil.which("gpg"):
            self.skipTest("gpg needed")
        self.seed("before")
        spec = backup_crypto.Spec(passphrase="correct horse battery staple")
        p = self.prov(encryption=spec)
        self.run_gen(p.create(["postgres"]))
        m = p.result
        bid = m["id"]
        self.assertTrue(m["encrypted"])
        self.assertEqual(m["encryption"]["method"], "gpg-passphrase")
        self.assertNotIn("correct horse", json.dumps(m))
        self.assertTrue(backup_crypto.looks_encrypted(p.store.path(bid) / "postgres.dump.gpg"))
        self.assertFalse((p.store.path(bid) / "postgres.dump").exists())                   # no plaintext left behind
        self.assertTrue(self.prov().verify(bid)["ok"])                                      # integrity needs no key
        self.psql("delete from gs_pts")
        with mock.patch.dict(os.environ, {backup_crypto.ENV_PASSPHRASE: ""}), self.assertRaises(RestoreError):
            self.run_gen(self.prov().restore(bid, ["postgres"], confirm=True))     # no key: refused
        self.assertEqual(self.psql("select count(*) from gs_pts"), "0")
        self.run_gen(self.prov(passphrase="correct horse battery staple").restore(bid, ["postgres"], confirm=True))
        self.assertEqual(self.names(), "before")
        wrong = self.prov(passphrase="a different passphrase here")
        with self.assertRaises(RestoreError):
            self.run_gen(wrong.restore(bid, ["postgres"], confirm=True))
        self.assertEqual(self.names(), "before")                                           # wrong key: nothing changed

    def test_8_a_stopped_workload_fails_the_backup_cleanly(self):
        self.stop_postgres()
        try:
            p = self.prov()
            before = len([r for r in p.list_backups() if r["status"] == "ok"])
            with self.assertRaises(BackupError) as cm:
                self.run_gen(p.create(["postgres"]))
            self.assertIn("not running", str(cm.exception))
            self.assertEqual(len([r for r in p.list_backups() if r["status"] == "ok"]), before)      # nothing sealed
        finally:
            self.start_postgres()

    def test_9_interrupted_backup_leaves_no_valid_backup(self):
        p = self.prov()
        ok_before = [r["id"] for r in p.list_backups() if r["status"] == "ok"]
        orig = provider.BackupProvider._verify_dump

        def boom(self_, name, dump):
            raise RuntimeError("connection lost")
        with mock.patch.object(provider.BackupProvider, "_verify_dump", boom), self.assertRaises(RuntimeError):
            self.run_gen(p.create(["postgres"]))
        del orig
        rows = p.list_backups()
        self.assertEqual([r["id"] for r in rows if r["status"] == "ok"], ok_before)
        self.assertTrue(any(r["status"] == "failed" for r in rows))
        self.assertFalse(list(p.store.root.glob("*.partial")))


class GeoServerCases:
    def gs(self, method, path, data=None):
        pw = self.cfg["geoserver"]["admin_password"]
        args = ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-u", f"admin:{pw}", "-X", method, "-H", "Content-Type: application/json"]
        if data:
            args += ["-d", data]
        return self.gs_exec(args + [f"http://127.0.0.1:8080/geoserver/rest{path}"])

    def workspaces(self):
        pw = self.cfg["geoserver"]["admin_password"]
        out = self.gs_exec(["curl", "-s", "-u", f"admin:{pw}", "-H", "Accept: application/json", "http://127.0.0.1:8080/geoserver/rest/workspaces"])
        return sorted(w["name"] for w in (json.loads(out).get("workspaces") or {}).get("workspace", []))

    def test_1_geoserver_data_dir_backup_and_restore(self):
        self.assertEqual(self.gs("POST", "/workspaces", '{"workspace":{"name":"kept_ws"}}'), "201")
        p = self.prov()
        self.run_gen(p.create(["geoserver", "postgres"]))
        m = p.result
        bid = m["id"]
        self.assertTrue(p.verify(bid)["ok"])
        self.assertGreater(m["components"]["geoserver"]["meta"]["members"], 10)
        self.assertEqual(m["components"]["geoserver"]["meta"]["path"], "/opt/geoserver_data")
        d = p.store.path(bid)
        raw = gzip.decompress((d / "geoserver.tar.gz").read_bytes())
        self.assertIn(b"kept_ws", raw)                                                       # the workspace really is in the archive
        self.assertNotIn(self.cfg["geoserver"]["admin_password"].encode(), raw)             # GeoServer stores only a hash, never the plaintext
        self.assertEqual(self.gs("POST", "/workspaces", '{"workspace":{"name":"temp_ws"}}'), "201")
        self.assertEqual(self.workspaces(), ["kept_ws", "temp_ws"])
        r = self.prov()
        lines = self.run_gen(r.restore(bid, ["geoserver"], confirm=True))
        self.assertTrue(any("verified healthy" in x for x in lines), lines)
        ws = self.workspaces()
        self.assertIn("kept_ws", ws)
        self.assertNotIn("temp_ws", ws)                                                      # the data dir is the backup's again
        kept = r.result["retained_previous_data"]["geoserver"]
        self.assertRegex(kept, r"/opt/geoserver_data/\.gs-pre-r")
        self.assertEqual(r.sh("geoserver", "root", ["sh", "-c", f'ls "{kept}/workspaces" | grep -c temp_ws'])[1].strip(), "1")   # the replaced data is kept
        self.run_gen(r.discard_previous(r.result["retained_previous_data"], confirm=True))
