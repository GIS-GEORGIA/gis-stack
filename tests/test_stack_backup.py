"""Stack backup and restore (Compose / Podman / Kubernetes providers): store, format, archive safety, retention, and the whole create/restore/undo/recover
logic for *file* components.  The workload is stood in for by a local directory and local `tar`/`sh` (a fake `_argv`), so every failure path runs for real
without containers.  PostgreSQL and the actual docker/kubectl wiring are covered by tests/integration (real Compose and kind) - NOT here.  Unit tested."""
import io
import json
import os
import shutil
import stat
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from gisstack import backup_crypto, config
from gisstack.backups import model, provider
from gisstack.backups.model import BackupError, IntegrityError, RestoreError
from gisstack.backups.store import Store
from gisstack.core.errors import DeployError
from tests.test_cli_deploy import CliBase

REAL_PROVIDER_FOR = provider.provider_for
HEALTHY = {"state": "healthy", "rows": [{"id": "geoserver", "ok": True}]}


def cfg_for(target="compose", **over):
    c = config.resolve({"project": "demo", "targets": [target], "deploy_target": target, "components": {"postgis": False, "geoserver": True, "tomcat": False,
                       "qgis_server": False, "mapproxy": False, "caddy": False, "webmap": False}, **over}, persist_secrets=False, generate_secrets=False)
    c["postgres"]["password"] = "pg-secret-value-1"
    c["geoserver"]["admin_password"] = "gs-secret-value-1"
    return c


class LocalProvider(provider.ComposeBackupProvider):
    """The "container" is this machine: commands run locally and the volume is a temp directory."""

    def __init__(self, cfg, volume, **kw):
        super().__init__(cfg, **kw)
        self.paths["geoserver"] = str(volume)
        self.up = True
        self.restarts = []

    def _argv(self, component, user, cmd):
        return list(cmd)

    def running(self, component):
        return self.up

    def restart(self, component):
        self.restarts.append(component)


class Env(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.td, True)
        p = mock.patch.dict(os.environ, {"GISSTACK_HOME": str(self.td / "home")})
        p.start()
        self.addCleanup(p.stop)
        self.vol = self.td / "vol"
        self.vol.mkdir()
        (self.vol / "global.xml").write_text("<global>v1</global>")
        (self.vol / "workspaces").mkdir()
        (self.vol / "workspaces" / "a.xml").write_text("ws-a")
        self.store = Store(self.td / "store")
        self.cfg = cfg_for()

    def prov(self, **kw):
        return LocalProvider(self.cfg, self.vol, store=self.store, **kw)

    def make(self, **kw):
        p = self.prov(**kw)
        list(p.create(["geoserver"], note="t"))
        return p, p.result["id"]

    def restore(self, p, bid, verify=HEALTHY, **kw):
        with mock.patch.object(provider.deployments, "verify", return_value=verify):
            return list(p.restore(bid, ["geoserver"], confirm=True, **kw))

    def tree(self):
        return {str(f.relative_to(self.vol)): (f.read_text() if f.is_file() else None) for f in sorted(self.vol.rglob("*")) if ".gs-" not in str(f)}


class ModelTests(unittest.TestCase):
    def test_ids_and_components_and_notes(self):
        for good in (model.new_id(), model.new_id(pre=True)):
            self.assertEqual(model.valid_id(good), good)
        for bad in ("../x", "2026", "20261109T101010Z-zzzz", "x" * 80, "", "pre-", "20261109T101010Z-abcd/../.."):
            with self.assertRaises(BackupError, msg=bad):
                model.valid_id(bad)
        self.assertEqual(model.check_components(["postgres", "geoserver"]), ["postgres", "geoserver"])
        with self.assertRaises(BackupError):
            model.check_components(["postgres", "../etc"])
        with self.assertRaises(BackupError):
            model.check_note("a\nb")
        with self.assertRaises(BackupError):
            model.check_note("x" * 201)

    def tar(self, build):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        p = Path(d) / "x.tar.gz"
        with tarfile.open(p, "w:gz") as tf:
            build(tf)
        return p

    @staticmethod
    def add(tf, name, data=b"x", type_=tarfile.REGTYPE, linkname=""):
        ti = tarfile.TarInfo(name)
        ti.type, ti.linkname = type_, linkname
        ti.size = len(data) if type_ == tarfile.REGTYPE else 0
        tf.addfile(ti, io.BytesIO(data) if type_ == tarfile.REGTYPE else None)

    def test_scan_accepts_normal_archives_including_absolute_symlinks(self):
        p = self.tar(lambda tf: (self.add(tf, "./a/b.txt", b"hello"), self.add(tf, "a/link", type_=tarfile.SYMTYPE, linkname="/etc/hosts"), self.add(tf, "a/rel", type_=tarfile.SYMTYPE, linkname="../b")))
        info = model.scan_tar(p)
        self.assertGreaterEqual(info["members"], 3)

    def test_scan_rejects_every_escape(self):
        cases = {
            "absolute name": lambda tf: self.add(tf, "/etc/passwd"),
            "parent dir": lambda tf: self.add(tf, "a/../../evil"),
            "device": lambda tf: self.add(tf, "dev", type_=tarfile.CHRTYPE),
            "fifo": lambda tf: self.add(tf, "ff", type_=tarfile.FIFOTYPE),
            "below symlink": lambda tf: (self.add(tf, "d", type_=tarfile.SYMTYPE, linkname="/tmp"), self.add(tf, "d/x", b"payload")),
            "hardlink outside": lambda tf: (self.add(tf, "f"), self.add(tf, "h", type_=tarfile.LNKTYPE, linkname="../../etc/passwd")),
        }
        for what, build in cases.items():
            with self.assertRaises(IntegrityError, msg=what):
                model.scan_tar(self.tar(build))

    def test_scan_rejects_truncated_and_garbage(self):
        p = self.tar(lambda tf: self.add(tf, "a", b"x" * 100000))
        data = p.read_bytes()
        p.write_bytes(data[: len(data) // 2])
        with self.assertRaises(IntegrityError):
            model.scan_tar(p)
        p.write_bytes(b"not a tarball at all")
        with self.assertRaises(IntegrityError):
            model.scan_tar(p)


class StoreTests(Env):
    def sealed(self, bid=None, kind="manual", created=None):
        bid = bid or model.new_id(pre=kind == "pre-restore")
        part = self.store.begin(bid)
        (part / "geoserver.tar.gz").write_bytes(b"data-" + bid.encode())
        from gisstack.backups.store import sha256_file
        f = part / "geoserver.tar.gz"
        self.store.seal(bid, {"schema": 1, "id": bid, "created": created or "2026-11-09T10:00:00Z", "kind": kind, "components": {"geoserver": {"files": [
            {"name": "geoserver.tar.gz", "bytes": f.stat().st_size, "sha256": sha256_file(f)}], "meta": {}}}})
        return bid

    def test_seal_is_atomic_and_private(self):
        bid = model.new_id()
        part = self.store.begin(bid)
        self.assertTrue(part.name.endswith(".partial"))
        self.assertEqual([r["status"] for r in self.store.list()], ["partial"])                  # never listed as a backup
        (part / "geoserver.tar.gz").write_bytes(b"x")
        with self.assertRaises(IntegrityError):
            self.store.seal(bid, {"schema": 1, "components": {"geoserver": {"files": [{"name": "missing.tar.gz", "bytes": 1, "sha256": "0" * 64}]}}})
        self.assertTrue(part.exists())
        self.store.delete(bid)
        b2 = self.sealed()
        d = self.store.path(b2)
        self.assertEqual(stat.S_IMODE(d.stat().st_mode), 0o700)
        for f in d.iterdir():
            self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600, f.name)
        self.assertTrue(self.store.verify(b2, structure=False)["ok"])

    def test_verify_finds_every_kind_of_damage(self):
        bid = self.sealed()
        d = self.store.path(bid)
        orig = (d / "geoserver.tar.gz").read_bytes()
        (d / "geoserver.tar.gz").write_bytes(orig + b"!")
        self.assertTrue(any("checksum mismatch" in x or "size mismatch" in x for x in self.store.verify(bid, structure=False)["problems"]))
        (d / "geoserver.tar.gz").unlink()
        self.assertTrue(any("missing file" in x for x in self.store.verify(bid, structure=False)["problems"]))
        (d / "geoserver.tar.gz").write_bytes(orig)
        (d / "stray").write_text("x")
        self.assertTrue(any("unexpected file" in x for x in self.store.verify(bid, structure=False)["problems"]))
        (d / "stray").unlink()
        sums = d / "SHA256SUMS"
        sums.write_text(sums.read_text().replace(sums.read_text()[:4], "ffff", 1))
        self.assertFalse(self.store.verify(bid, structure=False)["ok"])
        sums.unlink()
        self.assertTrue(any("SHA256SUMS is missing" in x for x in self.store.verify(bid, structure=False)["problems"]))
        with self.assertRaises(model.BackupNotFound):
            self.store.verify(model.new_id())

    def test_unknown_manifest_schema_is_refused(self):
        bid = self.sealed()
        mf = self.store.path(bid) / "manifest.json"
        mf.write_text(mf.read_text().replace('"schema": 1', '"schema": 99'))
        with self.assertRaises(IntegrityError):
            self.store.read(bid)
        self.assertEqual(self.store.list()[0]["status"], "damaged")

    def test_delete_cannot_leave_the_store(self):
        bid = self.sealed()
        outside = self.td / "precious"
        outside.mkdir()
        with self.assertRaises(BackupError):
            self.store.delete("../precious")
        link = self.store.root / "20200101T000000Z-aaaa"
        link.symlink_to(outside)
        with self.assertRaises(BackupError):
            self.store.delete("20200101T000000Z-aaaa")
        self.assertTrue(outside.exists())
        self.store.delete(bid)
        self.assertEqual([r for r in self.store.list() if r["status"] == "ok"], [])

    def test_retention_policy(self):
        ids = [self.sealed(f"2026110{i}T100000Z-000{i}") for i in range(1, 9)]
        pre = [self.sealed(f"pre-2026110{i}T100000Z-100{i}", kind="pre-restore") for i in range(1, 5)]
        dry = self.store.prune(keep=3, keep_pre=2, dry_run=True)
        self.assertEqual(sorted(dry["removed"]), sorted(ids[:5] + pre[:2]))
        self.assertEqual(len(self.store.list()), 12)                                              # dry run removed nothing
        res = self.store.prune(keep=3, keep_pre=2)
        self.assertEqual(sorted(res["removed"]), sorted(ids[:5] + pre[:2]))
        self.assertEqual([r["id"] for r in self.store.list() if r["kind"] != "pre-restore"], ids[:4:-1])
        self.assertEqual(len([r for r in self.store.list() if r["kind"] == "pre-restore"]), 2)

    def test_retention_by_age_and_the_newest_intact_backup_is_never_removed(self):
        ids = [self.sealed(f"2026110{i}T100000Z-000{i}") for i in range(1, 4)]
        now = time.mktime(time.strptime("2026-12-31", "%Y-%m-%d"))
        res = self.store.prune(keep=0, days=30, now=now)                                          # everything is older than 30 days...
        self.assertEqual(sorted(res["removed"]), sorted(ids[:2]))                                 # ...but the newest survives
        self.assertEqual([r["id"] for r in self.store.list()], [ids[2]])
        young = self.sealed("20261230T100000Z-0009")
        res = self.store.prune(keep=0, days=30, now=now)
        self.assertIn(young, [r["id"] for r in self.store.list()])                                 # younger than the window: kept

    def test_old_leftovers_are_cleaned_but_young_ones_are_kept(self):
        bid = model.new_id()
        part = self.store.begin(bid)
        self.assertEqual(self.store.prune(keep=5)["removed_leftovers"], [])
        old = time.time() - 3 * 86400
        os.utime(part, (old, old))
        self.assertEqual(self.store.prune(keep=5)["removed_leftovers"], [bid])
        self.assertFalse(part.exists())

    def test_unwritable_store_fails_cleanly(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        self.store.ensure()
        os.chmod(self.store.root, 0o500)
        self.addCleanup(os.chmod, self.store.root, 0o700)
        p = self.prov()
        with self.assertRaises(OSError):
            list(p.create(["geoserver"]))
        self.assertEqual([r for r in self.store.list() if r["status"] == "ok"], [])


class CreateTests(Env):
    def test_create_verifies_and_contains_no_secrets(self):
        p, bid = self.make()
        m = p.result
        self.assertEqual((m["target"], m["family"], m["kind"], m["encrypted"], m["note"]), ("compose", "compose", "manual", False, "t"))
        meta = m["components"]["geoserver"]["meta"]
        self.assertEqual(meta["path"], str(self.vol))
        self.assertGreaterEqual(meta["members"], 3)
        self.assertTrue(p.verify(bid)["ok"])
        d = self.store.path(bid)
        blob = b"".join(f.read_bytes() for f in d.iterdir() if f.suffix != ".gz")
        for s in ("pg-secret-value-1", "gs-secret-value-1"):
            self.assertNotIn(s.encode(), blob)
        self.assertEqual(stat.S_IMODE((d / "operation.log").stat().st_mode), 0o600)
        self.assertIn("sealed", (d / "operation.log").read_text())
        with tarfile.open(d / "geoserver.tar.gz") as tf:
            names = tf.getnames()
        self.assertIn("./global.xml", names)

    def test_podman_and_compose_share_the_format_and_k8s_does_not(self):
        c = provider.provider_for(cfg_for(runtime="podman"))
        self.assertIsInstance(c, provider.PodmanBackupProvider)
        self.assertEqual((c.kind, c.family, c.runtime), ("podman", "compose", "podman"))
        self.assertIs(type(provider.provider_for(cfg_for())), provider.ComposeBackupProvider)
        k = provider.provider_for(cfg_for("k8s"))
        self.assertEqual((k.kind, k.family), ("k8s", "k8s"))
        with self.assertRaises(DeployError):
            provider.provider_for(cfg_for("native"))

    def test_a_workload_that_is_not_running_fails_the_whole_backup(self):
        p = self.prov()
        p.up = False
        with self.assertRaises(BackupError) as cm:
            list(p.create(["geoserver"]))
        self.assertIn("not running", str(cm.exception))
        self.assertEqual([r["status"] for r in self.store.list()], ["failed"])

    def test_a_failure_while_streaming_leaves_no_backup(self):
        p = self.prov()
        with mock.patch.object(provider.BackupProvider, "_verify_tar", side_effect=RuntimeError("connection lost")), self.assertRaises(RuntimeError):
            list(p.create(["geoserver"]))
        rows = self.store.list()
        self.assertEqual([r["status"] for r in rows], ["failed"])
        self.assertEqual(list(self.store.root.glob("*.partial")), [])

    def test_running_is_false_when_the_command_hangs_and_kubernetes_checks_ready_replicas_first(self):
        """Regression (found on kind): a workload scaled to zero made `kubectl exec` wait for a pod and the backup died with TimeoutExpired."""
        import subprocess
        c = provider.ComposeBackupProvider(cfg_for(), store=self.store)
        with mock.patch.object(provider.subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 45)):
            self.assertFalse(c.running("geoserver"))
        k = provider.KubernetesBackupProvider(cfg_for("k8s"), store=self.store)
        for ready, expect in (("", False), ("0", False), ("1", True)):
            with mock.patch.object(provider.KubernetesBackupProvider, "host", return_value=ready), \
                    mock.patch.object(provider.BackupProvider, "sh", return_value=(0, "", "")):
                self.assertEqual(k.running("geoserver"), expect, ready)
        with mock.patch.object(provider.KubernetesBackupProvider, "host", side_effect=BackupError("kubectl failed")):
            self.assertFalse(k.running("geoserver"))

    def test_a_failing_command_inside_the_workload_is_reported_with_its_error(self):
        p = self.prov()
        p.paths["geoserver"] = str(self.td / "does-not-exist")
        with self.assertRaises(BackupError) as cm:
            list(p.create(["geoserver"]))
        self.assertIn("command failed", str(cm.exception))
        self.assertEqual([r["status"] for r in self.store.list() if r["status"] == "ok"], [])

    def test_an_empty_volume_is_not_a_backup(self):
        shutil.rmtree(self.vol)
        self.vol.mkdir()
        p = self.prov()
        with self.assertRaises(IntegrityError):
            list(p.create(["geoserver"]))

    def test_unknown_component_kind_and_note_are_rejected_before_anything_is_written(self):
        p = self.prov()
        for kw in ({"components": ["../x"]}, {"kind": "weird"}, {"note": "a\nb"}):
            with self.assertRaises(BackupError):
                list(p.create(**{"components": ["geoserver"], **kw}))
        self.assertEqual(self.store.list(), [])

    @unittest.skipUnless(__import__("shutil").which("gpg"), "gpg needed")
    def test_encrypted_backup_verifies_without_the_key(self):
        spec = backup_crypto.Spec(passphrase="correct horse battery staple")
        p, bid = self.make(encryption=spec)
        d = self.store.path(bid)
        self.assertTrue(p.result["encrypted"])
        self.assertEqual(p.result["encryption"]["method"], "gpg-passphrase")
        self.assertNotIn("correct horse", (d / "manifest.json").read_text() + (d / "operation.log").read_text())
        self.assertEqual(sorted(f.name for f in d.iterdir()), ["SHA256SUMS", "geoserver.tar.gz.gpg", "manifest.json", "operation.log"])      # no plaintext archive
        self.assertTrue(backup_crypto.looks_encrypted(d / "geoserver.tar.gz.gpg"))
        self.assertTrue(self.prov().verify(bid)["ok"])
        data = (d / "geoserver.tar.gz.gpg").read_bytes()
        (d / "geoserver.tar.gz.gpg").write_bytes(data[:-5] + b"XXXXX")
        self.assertFalse(self.prov().verify(bid)["ok"])


class RestoreTests(Env):
    def test_restore_replaces_keeps_the_old_data_and_protects_first(self):
        p, bid = self.make()
        (self.vol / "global.xml").write_text("<global>v2</global>")
        (self.vol / "workspaces" / "b.xml").write_text("ws-b")
        r = self.prov()
        lines = self.restore(r, bid)
        self.assertTrue(any("verified healthy" in x for x in lines), lines)
        self.assertEqual(self.tree()["global.xml"], "<global>v1</global>")
        self.assertNotIn("workspaces/b.xml", self.tree())
        kept = Path(r.result["retained_previous_data"]["geoserver"])
        self.assertEqual((kept / "global.xml").read_text(), "<global>v2</global>")              # the replaced data is still there
        self.assertEqual((kept / "workspaces" / "b.xml").read_text(), "ws-b")
        self.assertEqual(r.restarts, ["geoserver"])
        pre = [x for x in r.list_backups() if x["kind"] == "pre-restore"]
        self.assertEqual(len(pre), 1)
        self.assertTrue(r.verify(pre[0]["id"])["ok"])
        with tarfile.open(self.store.path(pre[0]["id"]) / "geoserver.tar.gz") as pre_tar:
            self.assertEqual(pre_tar.extractfile("./global.xml").read(), b"<global>v2</global>")     # the pre-restore backup holds the live data
        self.assertIsNone(self.store.read_state())
        self.assertFalse(list(self.vol.glob(".gs-staging-*")))
        # the retained data can be discarded - but only explicitly and only what a restore kept
        with self.assertRaises(RestoreError):
            list(r.discard_previous(r.result["retained_previous_data"]))
        with self.assertRaises(RestoreError):
            list(r.discard_previous({"geoserver": str(self.td)}, confirm=True))
        list(r.discard_previous(r.result["retained_previous_data"], confirm=True))
        self.assertFalse(kept.exists())
        self.assertEqual(self.tree()["global.xml"], "<global>v1</global>")

    def test_restore_needs_confirmation_and_changes_nothing_without_it(self):
        p, bid = self.make()
        (self.vol / "global.xml").write_text("<global>v2</global>")
        with self.assertRaises(RestoreError):
            list(self.prov().restore(bid, ["geoserver"]))
        self.assertEqual(self.tree()["global.xml"], "<global>v2</global>")
        self.assertEqual([x for x in self.store.list() if x["kind"] == "pre-restore"], [])

    def test_corrupted_missing_and_incompatible_backups_are_refused_before_any_change(self):
        p, bid = self.make()
        (self.vol / "global.xml").write_text("<global>v2</global>")
        d = self.store.path(bid)
        orig = (d / "geoserver.tar.gz").read_bytes()
        (d / "geoserver.tar.gz").write_bytes(orig[:-20] + b"0" * 20)
        with self.assertRaises(RestoreError) as cm:
            self.restore(self.prov(), bid)
        self.assertIn("checksum", str(cm.exception))
        (d / "geoserver.tar.gz").write_bytes(orig)
        for wanted, why in ((["mapproxy_config"], "does not contain"), (["postgres"], "does not contain")):
            with self.assertRaises(RestoreError) as cm2, mock.patch.object(provider.deployments, "verify", return_value=HEALTHY):
                list(self.prov().restore(bid, wanted, confirm=True))
            self.assertIn(why, str(cm2.exception))
        self.assertEqual(self.tree()["global.xml"], "<global>v2</global>")
        p2 = self.prov()
        p2.up = False
        with self.assertRaises(RestoreError) as cm3:
            self.restore(p2, bid)
        self.assertIn("not running", str(cm3.exception))

    def test_configuration_mounts_cannot_be_restored_in_place(self):
        p = self.prov()
        p.paths["mapproxy_config"] = str(self.vol)
        list(p.create(["mapproxy_config"]))
        with self.assertRaises(RestoreError) as cm:
            list(p.restore(p.result["id"], ["mapproxy_config"], confirm=True))
        self.assertIn("redeploy", str(cm.exception))

    def test_a_backup_from_another_family_is_incompatible(self):
        p, bid = self.make()
        k = provider.KubernetesBackupProvider(cfg_for("k8s"), store=self.store)
        rep = k.check_restore(bid, None)
        self.assertFalse(rep["ok"])
        self.assertTrue(any("not compatible" in x for x in rep["problems"]))

    def test_unhealthy_after_the_swap_puts_the_previous_files_back(self):
        p, bid = self.make()
        (self.vol / "global.xml").write_text("<global>v2</global>")
        (self.vol / "workspaces" / "b.xml").write_text("ws-b")
        before = self.tree()
        bad = {"state": "unhealthy", "rows": [{"id": "geoserver", "ok": False}]}
        r = self.prov()
        with self.assertRaises(RestoreError) as cm:
            self.restore(r, bid, verify=bad)
        self.assertIn("previous data was put back", str(cm.exception))
        self.assertEqual(self.tree(), before)                                                    # exactly the state before the restore
        self.assertIsNone(self.store.read_state())
        self.assertEqual(r.restarts, ["geoserver", "geoserver"])                                  # restarted again after the undo
        self.assertTrue(list(self.vol.glob(".gs-failed-*")))                                      # the rejected restored copy is kept, not deleted

    def test_staging_validation_failure_leaves_the_volume_untouched(self):
        p = self.prov()
        (self.vol / "global.xml").unlink()                                                        # an archive without the GeoServer marker file
        list(p.create(["geoserver"]))
        bid = p.result["id"]
        (self.vol / "global.xml").write_text("<global>live</global>")
        with self.assertRaises(RestoreError) as cm:
            self.restore(self.prov(), bid)
        self.assertIn("expected file global.xml is missing", str(cm.exception) + str(cm.exception.__cause__))
        self.assertEqual(self.tree()["global.xml"], "<global>live</global>")
        self.assertFalse(list(self.vol.glob(".gs-staging-*")))
        self.assertFalse(list(self.vol.glob(".gs-pre-*")))

    def test_an_interrupted_restore_is_recovered_and_recovery_is_idempotent(self):
        p, bid = self.make()
        (self.vol / "global.xml").write_text("<global>v2</global>")
        before = self.tree()
        r = self.prov()
        with mock.patch.object(provider.BackupProvider, "_run_undo", lambda self_, undo: ["simulated crash: no undo"]), \
                mock.patch.object(provider.deployments, "verify", side_effect=SystemExit("killed")), self.assertRaises((SystemExit, RestoreError)):
            list(r.restore(bid, ["geoserver"], confirm=True))
        self.assertEqual(self.tree()["global.xml"], "<global>v1</global>")                        # died right after the swap
        self.assertIsNotNone(self.store.read_state())
        with self.assertRaises(RestoreError) as cm:
            self.restore(self.prov(), bid)
        self.assertIn("recover", str(cm.exception))
        rec = self.prov()
        list(rec.recover())
        self.assertEqual(self.tree(), before)
        self.assertIsNone(self.store.read_state())
        self.assertEqual(list(rec.recover()), ["nothing to recover"])
        self.restore(self.prov(), bid)                                                            # and a new restore works again
        self.assertEqual(self.tree()["global.xml"], "<global>v1</global>")

    def test_an_unsafe_archive_is_refused_at_extraction_time_even_if_verification_was_bypassed(self):
        """Defence in depth: the archive is scanned again right before it is extracted (decrypted archives cannot be scanned earlier)."""
        p, bid = self.make()
        d = self.store.path(bid)
        evil = d / "geoserver.tar.gz"
        with tarfile.open(evil, "w:gz") as tf:
            ti = tarfile.TarInfo("../../escape.txt")
            ti.size = 1
            tf.addfile(ti, io.BytesIO(b"x"))
        (self.vol / "global.xml").write_text("<global>live</global>")
        with mock.patch.object(Store, "verify", return_value={"ok": True, "problems": [], "checked": 1}), \
                mock.patch.object(provider.deployments, "verify", return_value=HEALTHY), self.assertRaises(RestoreError) as cm:
            list(self.prov().restore(bid, ["geoserver"], confirm=True))
        self.assertIn("unsafe member name", str(cm.exception))
        self.assertEqual((self.vol / "global.xml").read_text(), "<global>live</global>")
        self.assertFalse((self.vol.parent / "escape.txt").exists())
        self.assertEqual([x for x in self.store.list() if x["kind"] == "pre-restore"], [])      # refused before even the pre-restore backup

    def test_cancelled_restore_job_puts_everything_back(self):
        p, bid = self.make()
        (self.vol / "global.xml").write_text("<global>v2</global>")
        before = self.tree()
        gen = self.prov().restore(bid, ["geoserver"], confirm=True)
        with mock.patch.object(provider.deployments, "verify", return_value=HEALTHY):
            for line in gen:
                if "swapped" in line:
                    break                                                                         # the user cancels the job mid-restore
            gen.close()
        self.assertEqual(self.tree(), before)
        self.assertIsNone(self.store.read_state())


if __name__ == "__main__":
    unittest.main()


class CliStackBackupTests(CliBase):
    """`gisstack stack-backup ...` against the local stand-in workload.  Unit tested."""

    def setUp(self):
        super().setUp()
        self.vol = self.tmp / "vol"
        self.vol.mkdir()
        (self.vol / "global.xml").write_text("<global>v1</global>")
        self.store = Store(self.tmp / "store")
        self.conf = self.config_file(deploy_target="compose", targets=["compose"], project="clisb",
                                     components={"postgis": False, "geoserver": True, "tomcat": False, "qgis_server": False, "mapproxy": False, "caddy": False, "webmap": False})

        def make(cfg, mode, ssh, **kw):
            return LocalProvider(cfg, self.vol, store=self.store, **kw)
        for p in (mock.patch.object(provider, "provider_for", make), mock.patch.object(provider.deployments, "verify", return_value=HEALTHY)):
            p.start()
            self.addCleanup(p.stop)

    def cli(self, *a, stdin=None):
        return self.run_cli("stack-backup", *a, "-c", self.conf, stdin=stdin)

    def test_full_cycle(self):
        rc, out, err = self.cli("create", "--components", "geoserver", "--note", "cli")
        self.assertEqual(rc, 0, err)
        bid = [ln.split("=")[1] for ln in out.splitlines() if ln.startswith("BACKUP_VERIFIED=")][0]
        rc, out, _ = self.cli("list")
        self.assertIn(bid, out)
        self.assertIn("cli", out)
        rc, out, _ = self.cli("verify", bid)
        self.assertEqual((rc, out.strip()), (0, "OK"))
        rc, out, _ = self.cli("inspect", bid)
        self.assertEqual(json.loads(out)["id"], bid)
        rc, out, _ = self.cli("check", bid, "--json")
        self.assertTrue(json.loads(out)["ok"])
        (self.vol / "global.xml").write_text("<global>v2</global>")
        rc, out, _ = self.cli("restore", bid, stdin="no")
        self.assertEqual(rc, 1)
        self.assertIn("cancelled", out)
        self.assertEqual((self.vol / "global.xml").read_text(), "<global>v2</global>")              # nothing changed without the typed confirmation
        rc, out, err = self.cli("restore", bid, stdin="RESTORE")
        self.assertEqual(rc, 0, err)
        self.assertIn("verified healthy", out)
        self.assertEqual((self.vol / "global.xml").read_text(), "<global>v1</global>")
        rc, out, _ = self.cli("prune", "--keep", "1", "--dry-run")
        self.assertTrue(json.loads(out)["dry_run"])
        rc, out, _ = self.cli("recover")
        self.assertEqual((rc, out.strip()), (0, "nothing to recover"))
        rc, out, _ = self.cli("delete", bid, "--yes")
        self.assertEqual(rc, 0)
        self.assertFalse([ln for ln in self.cli("list")[1].splitlines() if ln.startswith(bid)])      # gone (the pre-restore backup still mentions it in its note)

    def test_damaged_backup_is_reported_with_a_failing_exit_code_and_restore_is_refused(self):
        self.cli("create", "--components", "geoserver")
        bid = self.store.list()[0]["id"]
        (self.store.path(bid) / "geoserver.tar.gz").write_bytes(b"corrupt")
        rc, out, _ = self.cli("verify", bid)
        self.assertEqual(rc, 1)
        self.assertIn("checksum mismatch", out)
        rc, out, err = self.cli("restore", bid, "--yes")
        self.assertEqual(rc, 1)
        self.assertIn("nothing was changed", err)

    def test_native_target_is_pointed_at_its_own_tool(self):
        with mock.patch.object(provider, "provider_for", REAL_PROVIDER_FOR):
            rc, _, err = self.run_cli("stack-backup", "list", "-c", self.config_file(deploy_target="native", targets=["native"], project="clin"))
        self.assertEqual(rc, 1)
        self.assertIn("native", err)


if __name__ == "__main__":
    unittest.main()
