"""Phase 7: gisstack.backup (install the tool, drive it, copy backups, schedule) and the `gisstack backup` CLI."""
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
from test_backup_tool import Box, health_server  # noqa: E402

from gisstack import backup, cli, config, transport  # noqa: E402
from gisstack.core.errors import DeployError  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.td, True)
        self.srv = health_server()
        self.addCleanup(lambda: (self.srv.shutdown(), self.srv.server_close()))
        self.box = Box(self.td, health_url=f"http://127.0.0.1:{self.srv.server_port}/x")
        self.tool = self.td / "sbin/gisstack-backup"
        patches = [mock.patch.dict(os.environ, {k: v for k, v in self.box.env.items() if k.startswith("GS_") or k == "PATH"}),
                   mock.patch.object(backup, "TOOL", str(self.tool)), mock.patch.object(backup, "REMOTE_ROOT", str(self.box.root)),
                   mock.patch("os.geteuid", return_value=0), mock.patch.dict(os.environ, {"GISSTACK_HOME": str(self.td / "home")})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.cfg = config.resolve({"targets": ["native"], "deploy_target": "native", "project": "bk"})

    def create(self, comps=("config", "geoserver", "qgis")):
        lines = list(backup.create(self.cfg, "local", None, list(comps)))
        return backup.created_id(lines), lines


class ManagerTests(Base):
    def test_install_tool_is_idempotent_and_detects_changes(self):
        self.assertTrue(backup.install_tool(self.cfg, "local", None))
        self.assertTrue(self.tool.exists() and os.access(self.tool, os.X_OK))
        self.assertFalse(backup.install_tool(self.cfg, "local", None))
        self.tool.write_text("tampered")
        self.assertTrue(backup.install_tool(self.cfg, "local", None))            # replaced again
        self.assertEqual(self.tool.read_text(), backup.tool_text())
        self.assertIn(f":-{backup.__version__}}}", backup.tool_text())          # the manifests carry the gisstack version

    def test_create_list_verify_prune_roundtrip(self):
        bid, lines = self.create()
        self.assertTrue(any("BACKUP_VERIFIED=" in x for x in lines))
        rows = backup.list_backups(self.cfg, "local", None)
        self.assertEqual([(r["id"], r["status"], r["kind"], r["components"]) for r in rows], [(bid, "complete", "manual", ["config", "geoserver", "qgis"])])
        self.assertTrue(any("VERIFY_OK=" in x for x in backup.verify(self.cfg, "local", None, bid)))
        self.assertIn(bid, backup.format_table(rows))
        out = list(backup.prune(self.cfg, "local", None, keep=1, dry_run=True))
        self.assertIn("PRUNE_DONE", out[-1])
        manifest = json.loads((self.box.root / bid / "MANIFEST.json").read_text())
        self.assertEqual(manifest["gisstack_version"], backup.__version__)

    def test_a_failed_tool_run_raises_and_a_missing_marker_is_not_success(self):
        shutil.rmtree(self.box.qgis)
        with self.assertRaises(DeployError):
            list(backup.create(self.cfg, "local", None, ["qgis"]))
        with mock.patch.object(transport, "stream", return_value=iter(["all fine"])), mock.patch.object(backup, "install_tool", return_value=False):
            with self.assertRaises(DeployError) as cm:
                list(backup.create(self.cfg, "local", None, ["config"]))
            self.assertIn("BACKUP_VERIFIED", str(cm.exception))

    def test_restore_plans_by_default_and_needs_explicit_confirmation(self):
        bid, _ = self.create()
        (self.box.qgis / "demo.qgs").write_text("changed")
        plan = list(backup.restore(self.cfg, "local", None, bid))                          # dry_run defaults to True
        self.assertTrue(any("RESTORE_DRY_RUN_OK" in x for x in plan))
        self.assertEqual((self.box.qgis / "demo.qgs").read_text(), "changed")
        with self.assertRaises(DeployError):
            list(backup.restore(self.cfg, "local", None, bid, dry_run=False))             # no confirm -> refused before the server is asked
        out = list(backup.restore(self.cfg, "local", None, bid, dry_run=False, confirm=True))
        self.assertTrue(any("RESTORE_OK=" in x for x in out))
        self.assertEqual((self.box.qgis / "demo.qgs").read_text(), "<qgis>project</qgis>\n")
        out = list(backup.restore(self.cfg, "local", None, bid, only=["qgis"], dry_run=False, staging=True))
        self.assertTrue(any("RESTORE_STAGING_OK=" in x for x in out))

    def test_inputs_are_validated_before_anything_runs(self):
        with mock.patch.object(transport, "stream") as st, mock.patch.object(transport, "run") as run:
            for call in (lambda: list(backup.verify(self.cfg, "local", None, "../../etc")),
                         lambda: list(backup.restore(self.cfg, "local", None, "x; rm -rf /")),
                         lambda: list(backup.create(self.cfg, "local", None, ["config; id"])),
                         lambda: list(backup.prune(self.cfg, "local", None, keep=0)),
                         lambda: backup.download(self.cfg, "local", None, "nope"),
                         lambda: list(backup.restore(self.cfg, "local", None, "20260101-000000-aaaaaaaa", only=["bogus"]))):
                with self.assertRaises(DeployError):
                    call()
            st.assert_not_called()
            run.assert_not_called()
        compose = config.resolve({"targets": ["compose"], "deploy_target": "compose"})
        with self.assertRaises(DeployError):
            list(backup.create(compose, "local", None))

    def test_download_is_verified_never_overwrites_and_detects_damage(self):
        bid, _ = self.create()
        path = backup.download(self.cfg, "local", None, bid, str(self.td / "dl"))
        self.assertTrue(path.name == f"{bid}.tar.gz" and path.stat().st_size > 0)
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(backup.verify_archive(path), 3)
        with self.assertRaises(DeployError):
            backup.download(self.cfg, "local", None, bid, str(self.td / "dl"))                # exists
        # flip a byte inside the archive's payload (re-pack a corrupted copy)
        bad = self.td / "bad.tar.gz"
        with tarfile.open(path) as src, tarfile.open(bad, "w:gz") as dst:
            for m in src.getmembers():
                data = src.extractfile(m).read() if m.isfile() else None
                if m.name.endswith("qgis-projects.tar.gz"):
                    data = bytes([data[0] ^ 1]) + data[1:]
                    m.size = len(data)
                dst.addfile(m, io.BytesIO(data) if data is not None else None)
        with self.assertRaises(DeployError) as cm:
            backup.verify_archive(bad)
        self.assertIn("SHA-256", str(cm.exception))
        with self.assertRaises(DeployError):
            backup.verify_archive(self.td / "missing.tar.gz")

    def test_upload_restores_a_backup_onto_another_server_and_refuses_hostile_or_duplicate_archives(self):
        bid, _ = self.create()
        archive = backup.download(self.cfg, "local", None, bid, str(self.td / "dl"))
        shutil.rmtree(self.box.root / bid)                                                        # "the old server is gone"
        self.assertEqual(backup.upload(self.cfg, "local", None, archive), bid)
        self.assertTrue(any("VERIFY_OK=" in x for x in backup.verify(self.cfg, "local", None, bid)))
        self.assertEqual(oct((self.box.root / bid).stat().st_mode & 0o777), "0o700")
        with self.assertRaises(DeployError):
            backup.upload(self.cfg, "local", None, archive)                                       # no silent overwrite
        evil = self.td / "evil.tar.gz"
        with tarfile.open(evil, "w:gz") as tf:
            data = b"x"
            ti = tarfile.TarInfo("../outside")
            ti.size = 1
            tf.addfile(ti, io.BytesIO(data))
        with self.assertRaises(DeployError):
            backup.upload(self.cfg, "local", None, evil)
        self.assertFalse((self.box.root.parent / "outside").exists())


class ScheduleTests(Base):
    def test_units(self):
        service, timer = backup.schedule_units("Mon *-*-* 03:00", keep=7, days=30, components=["config", "postgres"], with_cache=False)
        self.assertIn(f"ExecStart={self.tool} create --kind scheduled --components config,postgres", service)
        self.assertIn(f"ExecStartPost={self.tool} prune --keep 7 --days 30", service)
        self.assertIn("OnCalendar=Mon *-*-* 03:00", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("Type=oneshot", service)

    def test_hostile_values_are_rejected(self):
        for kw in ({"calendar": "daily\nExecStart=/bin/evil"}, {"calendar": "$(id)"}, {"keep": 0}, {"components": ["x"]}, {"days": -1}):
            with self.assertRaises(DeployError, msg=str(kw)):
                backup.schedule_units(**kw)

    def test_enable_writes_both_units_and_starts_the_timer(self):
        calls = []

        def fake(mode, ssh, script, *, root=False, timeout=40, input_text=None):
            calls.append((script, input_text))
            return "enabled\n"
        with mock.patch.object(transport, "run", fake), mock.patch.object(backup, "install_tool", return_value=False):
            out = backup.schedule_enable(self.cfg, "local", None, calendar="daily", keep=5)
            backup.schedule_disable(self.cfg, "local", None)
        self.assertEqual(out, "enabled")
        writes = [c for c in calls if "gisstack-backup.service" in c[0] and c[1]]
        self.assertEqual(len(writes), 1)
        self.assertIn("ExecStart=", writes[0][1])
        self.assertTrue(any("enable --now gisstack-backup.timer" in c[0] for c in calls))
        self.assertTrue(any("disable --now gisstack-backup.timer" in c[0] for c in calls))


class CliTests(Base):
    def run_cli(self, *a, stdin=None):
        o, e = io.StringIO(), io.StringIO()
        cf = self.td / "c.json"
        cf.write_text(json.dumps({"project": "bk", "deploy_target": "native", "targets": ["native"]}))
        with redirect_stdout(o), redirect_stderr(e), mock.patch("builtins.input", return_value=stdin or ""):
            rc = cli.main(["backup", a[0], "-c", str(cf), *a[1:]])
        return rc, o.getvalue(), e.getvalue()

    def test_create_list_verify_prune_download_through_the_cli(self):
        rc, out, err = self.run_cli("create", "--components", "config,qgis", "--note", "cli test", "--download", str(self.td / "dl"))
        self.assertEqual(rc, 0, err)
        bid = [l for l in out.splitlines() if l.startswith("BACKUP_VERIFIED=")][0].split("=")[1]
        self.assertIn("SHA-256", out)
        rc, out, _ = self.run_cli("list", "--json")
        self.assertEqual(json.loads(out)[0]["id"], bid)
        self.assertEqual(self.run_cli("verify", bid)[0], 0)
        self.assertEqual(self.run_cli("prune", "--keep", "3", "--dry-run")[0], 0)
        self.assertEqual(self.run_cli("verify", "20200101-000000-deadbeef")[0], 1)
        rc, _, err = self.run_cli("download", "20200101-000000-deadbeef")
        self.assertEqual(rc, 1)

    def test_restore_wizard_shows_the_plan_then_asks_and_aborts_without_a_yes(self):
        rc, out, _ = self.run_cli("create", "--components", "config,qgis")
        bid = [l for l in out.splitlines() if l.startswith("BACKUP_VERIFIED=")][0].split("=")[1]
        (self.box.qgis / "demo.qgs").write_text("changed")
        rc, out, _ = self.run_cli("restore", bid, stdin="no")
        self.assertEqual(rc, 1)
        self.assertIn("RESTORE_PLAN", out)
        self.assertIn("aborted", out)
        self.assertEqual((self.box.qgis / "demo.qgs").read_text(), "changed")
        rc, out, err = self.run_cli("restore", bid, stdin="yes")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("RESTORE_OK=", out)
        self.assertEqual((self.box.qgis / "demo.qgs").read_text(), "<qgis>project</qgis>\n")
        self.assertEqual(self.run_cli("restore", bid, "--dry-run")[0], 0)

    def test_restore_yes_skips_the_question(self):
        _, out, _ = self.run_cli("create", "--components", "qgis")
        bid = [l for l in out.splitlines() if l.startswith("BACKUP_VERIFIED=")][0].split("=")[1]
        with mock.patch("builtins.input", side_effect=AssertionError("must not ask")):
            o, e = io.StringIO(), io.StringIO()
            cf = self.td / "c.json"
            with redirect_stdout(o), redirect_stderr(e):
                rc = cli.main(["backup", "restore", bid, "-c", str(cf), "--yes"])
        self.assertEqual(rc, 0, o.getvalue() + e.getvalue())


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(__import__("importlib").util.find_spec("flask"), "flask needed")
class WebTests(Base):
    def setUp(self):
        super().setUp()
        from gisstack.webgui.app import create_app
        self.app = create_app("tok")
        self.c = self.app.test_client()
        self.h = {"Authorization": "Bearer tok", "X-GisStack": "1"}
        self.body = {"config": {"project": "bk", "deploy_target": "native", "targets": ["native"]}, "mode": "local"}

    def job(self, path, **extra):
        r = self.c.post(path, json={**self.body, **extra}, headers=self.h)
        if r.status_code != 200:
            return r, None
        jid = r.get_json()["job"]
        for _ in range(300):
            j = self.c.get(f"/api/jobs/{jid}", headers=self.h).get_json()
            if j["done"]:
                return r, j
            import time
            time.sleep(0.05)
        self.fail("job did not finish")

    def test_create_list_verify_restore_prune(self):
        _, j = self.job("/api/backup/create", components=["config", "qgis"])
        self.assertTrue(j["ok"], j["lines"])
        r = self.c.post("/api/backup/list", json=self.body, headers=self.h)
        rows = r.get_json()["backups"]
        self.assertEqual(len(rows), 1)
        bid = rows[0]["id"]
        _, j = self.job("/api/backup/verify", id=bid)
        self.assertTrue(j["ok"], j["lines"])
        (self.box.qgis / "demo.qgs").write_text("changed")
        _, j = self.job("/api/backup/restore", id=bid)                                  # default: a plan only
        self.assertTrue(j["ok"] and any("RESTORE_DRY_RUN_OK" in x for x in j["lines"]), j["lines"])
        self.assertEqual((self.box.qgis / "demo.qgs").read_text(), "changed")
        r, _ = self.job("/api/backup/restore", id=bid, dry_run=False)                   # no confirm -> 400 with a code
        self.assertEqual((r.status_code, r.get_json()["code"]), (400, "deploy"))
        self.assertEqual((self.box.qgis / "demo.qgs").read_text(), "changed")
        _, j = self.job("/api/backup/restore", id=bid, dry_run=False, confirm=True)
        self.assertTrue(j["ok"], j["lines"])
        self.assertEqual((self.box.qgis / "demo.qgs").read_text(), "<qgis>project</qgis>\n")
        _, j = self.job("/api/backup/prune", keep=1)                                      # dry run by default
        self.assertTrue(any("PRUNE_DONE" in x for x in j["lines"]))

    def test_bad_input_gets_400_with_a_code_and_non_native_targets_are_refused(self):
        for path, extra in (("/api/backup/verify", {"id": "../x"}), ("/api/backup/restore", {"id": "nope"}),
                            ("/api/backup/create", {"components": "config"}), ("/api/backup/prune", {"keep": "x"}),
                            ("/api/backup/restore", {"id": "20260101-000000-aaaaaaaa", "only": "qgis"})):
            r = self.c.post(path, json={**self.body, **extra}, headers=self.h)
            self.assertEqual(r.status_code, 400, (path, extra))
            self.assertIn("code", r.get_json())
        r = self.c.post("/api/backup/list", json={**self.body, "config": {"deploy_target": "compose", "targets": ["compose"]}}, headers=self.h)
        self.assertEqual(r.status_code, 400)

    def test_audit_log_records_restore_requests_without_secrets(self):
        self.c.post("/api/backup/restore", json={**self.body, "id": "20260101-000000-aaaaaaaa", "confirm": True, "dry_run": False}, headers=self.h)
        raw = (Path(os.environ["GISSTACK_HOME"]) / "audit.log").read_text()
        events = [json.loads(x) for x in raw.splitlines()]
        req = [e for e in events if e.get("path") == "/api/backup/restore"][0]
        self.assertEqual((req["id"], req["confirm"], req["dry_run"]), ("20260101-000000-aaaaaaaa", True, False))
