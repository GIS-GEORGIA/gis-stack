"""Phase 7: gisstack-backup (create / verify / list / prune / restore) exercised with real bash, real tar/sha256sum and -
when Docker and a PostGIS image are available - a real PostgreSQL round trip.

No root needed: the tool reads GS_* overrides for the env file, backup root, config paths, systemctl and PostgreSQL wrapper."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "gisstack/templates/native/gisstack-backup.sh"
IMAGE = os.environ.get("GISSTACK_TEST_POSTGIS_IMAGE", "postgis/postgis:17-3.5")


class Box:
    """A fake server: directories for GeoServer data, QGIS projects, config files, backup root and a stub systemctl."""

    def __init__(self, td: Path, pgrun: str = "false", health_url: str = ""):
        self.td = td
        self.gs, self.qgis, self.root, self.etc = td / "var/geoserver_data", td / "srv/qgis_projects", td / "backups", td / "etc/gisstack"
        for d in (self.gs / "workspaces/w", self.qgis, self.root, self.etc, td / "bin"):
            d.mkdir(parents=True, exist_ok=True)
        (self.gs / "global.xml").write_text("<global>original</global>\n")
        (self.gs / "workspaces/w/layer.xml").write_text("<layer>roads</layer>\n")
        (self.qgis / "demo.qgs").write_text("<qgis>project</qgis>\n")
        (self.etc / "stack.env").write_text(f'DB_NAME="gis"\nDB_USER="gis"\nDB_PASSWORD="Pw_secret_1"\nPG_PORT="5432"\n'
                                            f'GS_DATA_DIR="{self.gs}"\nQGIS_PROJECTS_DIR="{self.qgis}"\nTOMCAT_PORT="1"\n')
        (self.etc / "credentials.txt").write_text("PostgreSQL: password=Pw_secret_1\n")
        self.active = td / "active-units"
        self.active.write_text("gisstack-tomcat.service\n")
        self.calls = td / "systemctl.log"
        (td / "bin/systemctl").write_text(f'#!/bin/bash\necho "$*" >> "{self.calls}"\n'
                                          f'if [ "$1" = is-active ]; then grep -qx "$3" "{self.active}"; exit $?; fi\nexit 0\n')
        (td / "bin/systemctl").chmod(0o755)
        self.env = {**os.environ, "GS_NO_ROOT": "1", "GS_ENV_FILE": str(self.etc / "stack.env"), "GS_BACKUP_ROOT": str(self.root),
                    "GS_PGRUN": pgrun, "GS_SYSTEMCTL": str(td / "bin/systemctl"), "GS_CONFIG_PATHS": str(self.etc),
                    "GS_RESTORE_ALLOW": "^" + re.escape(str(self.etc).lstrip("/")) + "/", "GS_CACHE_DIR": str(td / "cache_nope"),
                    "GS_WEBMAP_DIR": str(td / "webmap_nope"), "GS_HEALTH_WAIT": "2", "GS_HEALTH_URL": health_url or "http://127.0.0.1:9/x"}

    def run(self, *args, check=False, **env):
        r = subprocess.run(["bash", str(TOOL), *args], capture_output=True, text=True, env={**self.env, **env}, cwd=str(self.td))
        if check and r.returncode != 0:
            raise AssertionError(f"{args}: rc={r.returncode}\n{r.stdout}\n{r.stderr}")
        return r

    def create(self, comps="config,geoserver,qgis", **kw):
        r = self.run("create", "--components", comps, *kw.get("extra", []), check=True)
        return re.search(r"BACKUP_VERIFIED=(\S+)", r.stdout).group(1)

    def tree_state(self):
        return {str(p.relative_to(self.td)): p.read_bytes() for d in (self.gs, self.qgis, self.etc) for p in sorted(d.rglob("*")) if p.is_file()}


DEAD = "http://127.0.0.1:9/x"


def health_server():
    """A GeoServer stand-in that answers 200 - restores validate against it."""
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Base(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.td, True)
        self.srv = health_server()
        self.addCleanup(lambda: (self.srv.shutdown(), self.srv.server_close()))
        self.box = Box(self.td, health_url=f"http://127.0.0.1:{self.srv.server_port}/geoserver/web/")


@unittest.skipUnless(shutil.which("bash") and shutil.which("tar") and shutil.which("sha256sum") and shutil.which("flock"), "GNU userland needed")
class CreateVerifyListTests(Base):
    def test_create_produces_a_sealed_verified_backup(self):
        bid = self.box.create()
        d = self.box.root / bid
        self.assertTrue(re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{8}", bid))
        self.assertEqual(sorted(p.name for p in d.iterdir()), ["MANIFEST.json", "SHA256SUMS", "config.tar.gz", "geoserver-data.tar.gz", "qgis-projects.tar.gz"])
        m = json.loads((d / "MANIFEST.json").read_text())
        self.assertEqual((m["schema"], m["status"], m["kind"], m["id"]), (1, "complete", "manual", bid))
        self.assertEqual(sorted(f["name"] for f in m["files"]), ["config.tar.gz", "geoserver-data.tar.gz", "qgis-projects.tar.gz"])
        self.assertFalse(list(self.box.root.glob("*.partial")))
        self.assertEqual(oct(d.stat().st_mode & 0o777), "0o700")
        self.assertTrue(all(oct(p.stat().st_mode & 0o777) == "0o600" for p in d.iterdir()))
        self.assertIn(f"VERIFY_OK={bid}", self.box.run("verify", bid).stdout)

    def test_list_reports_state_kind_components(self):
        bid = self.box.create()
        rows = [l.split("\t") for l in self.box.run("list").stdout.splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][:3], [bid, "complete", "manual"])
        self.assertEqual(rows[0][5], "config geoserver qgis")

    def test_bad_arguments_and_duplicate_ids_are_refused(self):
        self.assertEqual(self.box.run("create", "--components", "nope").returncode, 2)
        self.assertEqual(self.box.run("create", "--id", "../../etc").returncode, 2)
        bid = self.box.create()
        self.assertEqual(self.box.run("create", "--components", "config", "--id", bid).returncode, 1)
        self.assertEqual(self.box.run("verify", "20200101-000000-deadbeef").returncode, 3)
        self.assertEqual(self.box.run("bogus").returncode, 2)

    def test_failed_component_never_becomes_a_backup(self):
        shutil.rmtree(self.box.qgis)
        r = self.box.run("create", "--components", "config,qgis")
        self.assertEqual(r.returncode, 1)
        self.assertIn("BACKUP_FAILED", r.stdout)
        failed = list(self.box.root.glob("*.FAILED"))
        self.assertEqual(len(failed), 1)
        rows = [l.split("\t") for l in self.box.run("list").stdout.splitlines()]
        self.assertEqual(rows[0][1], "failed")
        self.assertFalse((self.box.root / failed[0].name.replace(".FAILED", "")).exists())

    def test_verify_detects_corruption_truncation_missing_files_and_tampered_manifest(self):
        for how in ("flip", "truncate", "delete", "manifest", "extra-status"):
            with self.subTest(how):
                bid = self.box.create("config,geoserver")
                d = self.box.root / bid
                f = d / "geoserver-data.tar.gz"
                if how == "flip":
                    b = bytearray(f.read_bytes())
                    b[len(b) // 2] ^= 0xFF
                    f.write_bytes(bytes(b))
                elif how == "truncate":
                    f.write_bytes(f.read_bytes()[:50])
                elif how == "delete":
                    f.unlink()
                elif how == "manifest":
                    (d / "MANIFEST.json").write_text((d / "MANIFEST.json").read_text().replace('"sha256": "', '"sha256": "0', 1))
                else:
                    (d / "MANIFEST.json").write_text((d / "MANIFEST.json").read_text().replace('"complete"', '"partial"'))
                r = self.box.run("verify", bid)
                self.assertEqual(r.returncode, 1, (how, r.stdout))
                self.assertIn("VERIFY_FAILED", r.stdout)
                shutil.rmtree(d)

    def test_concurrent_operation_is_refused(self):
        holder = subprocess.Popen(["flock", str(self.box.root / ".lock"), "sleep", "6"], start_new_session=True)
        try:
            time.sleep(0.5)
            self.assertEqual(self.box.run("create", "--components", "config").returncode, 75)
        finally:
            os.killpg(holder.pid, 9)
            holder.wait()

    def test_not_root_is_refused_without_the_test_override(self):
        if os.geteuid() == 0:
            self.skipTest("running as root")
        r = self.box.run("list", GS_NO_ROOT="0")
        self.assertEqual(r.returncode, 2)
        self.assertIn("run as root", r.stderr)

    def test_quiesce_stops_and_restarts_tomcat_around_the_geoserver_copy(self):
        self.box.run("create", "--components", "geoserver", GS_BACKUP_QUIESCE="1", check=True)
        calls = self.box.calls.read_text().splitlines()
        self.assertEqual([c for c in calls if c.startswith(("stop", "start"))], ["stop gisstack-tomcat.service", "start gisstack-tomcat.service"])


@unittest.skipUnless(shutil.which("bash") and shutil.which("tar") and shutil.which("flock"), "GNU userland needed")
class PruneTests(Base):
    def make(self, ids):
        for i in ids:
            self.box.run("create", "--components", "config", "--id", i, check=True)

    def ids(self):
        return sorted(l.split("\t")[0] for l in self.box.run("list").stdout.splitlines())

    def test_keep_n_newest(self):
        self.make(["20260101-000000-aaaaaaa1", "20260102-000000-aaaaaaa2", "20260103-000000-aaaaaaa3", "20260104-000000-aaaaaaa4"])
        out = self.box.run("prune", "--keep", "2", "--dry-run").stdout
        self.assertEqual(sorted(re.findall(r"WOULD_DELETE=(\S+)", out)), ["20260101-000000-aaaaaaa1", "20260102-000000-aaaaaaa2"])
        self.assertEqual(len(self.ids()), 4)                                  # dry run changes nothing
        self.box.run("prune", "--keep", "2", check=True)
        self.assertEqual(self.ids(), ["20260103-000000-aaaaaaa3", "20260104-000000-aaaaaaa4"])

    def test_days_only_removes_old_backups_beyond_keep(self):
        recent = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        self.make(["20200101-000000-bbbbbbb1", "20200102-000000-bbbbbbb2", recent + "-bbbbbbb3"])
        self.box.run("prune", "--keep", "1", "--days", "30", check=True)
        self.assertEqual(self.ids(), [recent + "-bbbbbbb3"])
        self.make(["20260101-000000-ccccccc1"])                              # younger than --days: kept although beyond --keep
        self.box.run("prune", "--keep", "1", "--days", "36500", check=True)
        self.assertEqual(len(self.ids()), 2)

    def test_the_newest_backup_is_never_deleted_and_keep_zero_is_rejected(self):
        self.make(["20260101-000000-ddddddd1"])
        self.assertEqual(self.box.run("prune", "--keep", "0").returncode, 2)
        self.box.run("prune", "--keep", "1", check=True)
        self.assertEqual(len(self.ids()), 1)

    def test_pre_restore_backups_have_their_own_limit(self):
        self.make(["20260101-000000-eeeeeee1"])
        for i in range(1, 5):
            self.box.run("create", "--components", "config", "--kind", "pre-restore", "--id", f"pre-2026020{i}-000000-eeeeeee{i}", check=True)
        self.box.run("prune", "--keep", "5", "--keep-pre", "2", check=True)
        names = self.ids()
        self.assertEqual([n for n in names if n.startswith("pre-")], ["pre-20260203-000000-eeeeeee3", "pre-20260204-000000-eeeeeee4"])
        self.assertIn("20260101-000000-eeeeeee1", names)

    def test_stale_partial_and_failed_leftovers_are_cleaned_but_fresh_ones_are_kept(self):
        self.make(["20260101-000000-fffffff1"])
        old_partial, fresh_partial, old_failed = (self.box.root / n for n in ("20260101-000001-fffffff2.partial", "20260101-000002-fffffff3.partial", "20260101-000003-fffffff4.FAILED"))
        for d in (old_partial, fresh_partial, old_failed):
            d.mkdir()
        past = time.time() - 10 * 86400
        for d in (old_partial, old_failed):
            os.utime(d, (past, past))
        self.box.run("prune", "--keep", "5", check=True)
        self.assertFalse(old_partial.exists() or old_failed.exists())
        self.assertTrue(fresh_partial.exists())


@unittest.skipUnless(shutil.which("bash") and shutil.which("tar") and shutil.which("flock"), "GNU userland needed")
class RestoreTests(Base):
    def mutate(self):
        (self.box.gs / "global.xml").write_text("<global>BROKEN</global>\n")
        (self.box.gs / "workspaces/w/extra.xml").write_text("new file\n")
        (self.box.qgis / "demo.qgs").write_text("<qgis>changed</qgis>\n")
        (self.box.etc / "stack.env").write_text((self.box.etc / "stack.env").read_text() + "# edited\n")

    def test_dry_run_and_missing_confirmation_change_nothing(self):
        bid = self.box.create()
        self.mutate()
        before = self.box.tree_state()
        r = self.box.run("restore", bid, "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("RESTORE_DRY_RUN_OK", r.stdout)
        self.assertIn("RESTORE_PLAN", r.stdout)
        r = self.box.run("restore", bid)
        self.assertEqual(r.returncode, 2)
        self.assertIn("--yes", r.stderr)
        self.assertEqual(self.box.tree_state(), before)
        self.assertEqual(len(list(self.box.root.glob("pre-*"))), 0)           # no pre-restore backup for a refused/dry run

    def test_restore_in_place_reproduces_the_original_state_and_keeps_the_old_data(self):
        original = self.box.tree_state()
        bid = self.box.create()
        self.mutate()
        r = self.box.run("restore", bid, "--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"RESTORE_OK={bid}", r.stdout)
        self.assertEqual(self.box.tree_state(), dict(original))
        pre = list(self.box.root.glob("pre-*"))
        self.assertEqual(len(pre), 1)                                          # automatic pre-restore backup, verified
        self.assertIn("VERIFY_OK", self.box.run("verify", pre[0].name).stdout)
        kept = list(self.box.gs.parent.glob("geoserver_data.pre-restore-*"))
        self.assertEqual(len(kept), 1)
        self.assertEqual((kept[0] / "global.xml").read_text(), "<global>BROKEN</global>\n")          # nothing was thrown away
        calls = self.box.calls.read_text().splitlines()
        self.assertEqual([c for c in calls if c.startswith(("stop", "start"))][:2], ["stop gisstack-tomcat.service", "start gisstack-tomcat.service"])

    def test_the_pre_restore_backup_can_undo_the_restore(self):
        bid = self.box.create()
        self.mutate()
        mutated = self.box.tree_state()
        self.box.run("restore", bid, "--yes", check=True)
        pre = next(self.box.root.glob("pre-*")).name
        self.box.run("restore", pre, "--yes", "--no-pre-backup", check=True)
        self.assertEqual(self.box.tree_state(), mutated)

    def test_partial_restore_touches_only_the_named_components(self):
        bid = self.box.create()
        self.mutate()
        r = self.box.run("restore", bid, "--only", "qgis", "--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.box.qgis / "demo.qgs").read_text(), "<qgis>project</qgis>\n")
        self.assertEqual((self.box.gs / "global.xml").read_text(), "<global>BROKEN</global>\n")
        self.assertEqual(self.box.run("restore", bid, "--only", "cache", "--dry-run").returncode, 1)     # not in the backup

    def test_damaged_backup_is_refused_before_anything_changes(self):
        bid = self.box.create()
        self.mutate()
        before = self.box.tree_state()
        f = self.box.root / bid / "geoserver-data.tar.gz"
        f.write_bytes(f.read_bytes()[:-20])
        r = self.box.run("restore", bid, "--yes")
        self.assertEqual(r.returncode, 1)
        self.assertIn("damaged", r.stderr)
        self.assertEqual(self.box.tree_state(), before)
        self.assertFalse(list(self.box.root.glob("pre-*")))

    def test_failed_recovery_validation_rolls_everything_back(self):
        bid = self.box.create()
        self.mutate()
        before = self.box.tree_state()
        r = self.box.run("restore", bid, "--yes", GS_HEALTH_URL=DEAD)           # GeoServer never answers -> validation fails
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("RESTORE_FAILED_ROLLED_BACK", r.stdout)
        self.assertEqual(self.box.tree_state(), before)                          # live data is back exactly as it was
        self.assertFalse(list(self.box.gs.parent.glob("*.pre-restore-*")))
        self.assertFalse(list(self.box.gs.parent.glob(".gs-restore-*")))

    def test_no_rollback_flag_keeps_the_old_data_and_says_where(self):
        bid = self.box.create("geoserver,qgis")
        self.mutate()
        r = self.box.run("restore", bid, "--yes", "--no-rollback", GS_HEALTH_URL=DEAD)
        self.assertEqual(r.returncode, 1)
        self.assertIn("RESTORE_FAILED_NO_ROLLBACK", r.stdout)
        self.assertTrue(list(self.box.gs.parent.glob("geoserver_data.pre-restore-*")))

    def test_pre_restore_backup_failure_refuses_the_restore(self):
        bid = self.box.create()
        self.mutate()
        before = self.box.tree_state()
        shutil.rmtree(self.box.qgis)                                             # current state cannot be backed up (component missing)
        before.pop(str((self.box.qgis / "demo.qgs").relative_to(self.td)), None)
        r = self.box.run("restore", bid, "--yes")
        self.assertEqual(r.returncode, 1)
        self.assertIn("pre-restore backup failed", r.stderr)
        self.assertFalse((self.box.qgis / "demo.qgs").exists())                  # nothing was restored

    def test_staging_restore_leaves_live_data_alone(self):
        bid = self.box.create()
        self.mutate()
        before = self.box.tree_state()
        r = self.box.run("restore", bid, "--staging")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.box.tree_state(), before)
        stage = self.box.root / "staging" / bid
        self.assertEqual((stage / "geoserver_data/global.xml").read_text(), "<global>original</global>\n")
        self.assertEqual(self.box.run("restore", bid, "--staging").returncode, 1)         # does not overwrite an earlier staging dir

    def _forge(self, bid, archive, build):
        """Replace an archive by a hostile one and re-seal the backup so that checksums and manifest are consistent."""
        d = self.box.root / bid
        evil = self.td / "evil"
        shutil.rmtree(evil, ignore_errors=True)
        evil.mkdir()
        build(evil)
        subprocess.run(["tar", "czf", str(d / archive), "-C", str(evil), "."], check=True)
        sums = subprocess.run("sha256sum *.tar.gz", shell=True, cwd=d, capture_output=True, text=True).stdout
        (d / "SHA256SUMS").write_text(sums)
        m = (d / "MANIFEST.json").read_text()
        for line in sums.splitlines():
            h, n = line.split()
            size = (d / n).stat().st_size
            pattern = r'("name": "' + re.escape(n) + r'", "bytes": )\d+(, "sha256": ")[0-9a-f]+'
            m = re.sub(pattern, lambda mo, size=size, h=h: f"{mo.group(1)}{size}{mo.group(2)}{h}", m)
        (d / "MANIFEST.json").write_text(m)
        self.assertIn("VERIFY_OK", self.box.run("verify", bid).stdout)             # the forgery is internally consistent

    def test_archives_that_could_escape_are_refused(self):
        outside = self.td / "outside"
        cases = {
            "traversal": lambda e: (e / "geoserver_data").mkdir() or (e / "geoserver_data/x").write_text("x") or os.symlink("../../../outside", e / "geoserver_data/link"),
            "wrong-root": lambda e: (e / "etc_other").mkdir() or (e / "etc_other/passwd").write_text("pwned\n"),
        }
        for name, build in cases.items():
            with self.subTest(name):
                bid = self.box.create("geoserver")
                self._forge(bid, "geoserver-data.tar.gz", build)
                before = self.box.tree_state()
                r = self.box.run("restore", bid, "--yes")
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertIn("refused", r.stdout)
                self.assertEqual(self.box.tree_state(), before)
                self.assertFalse(outside.exists())
                shutil.rmtree(self.box.root / bid)

    def test_config_archive_outside_the_allowed_directories_is_refused(self):
        bid = self.box.create("config")
        self._forge(bid, "config.tar.gz", lambda e: (e / "usr/bin").mkdir(parents=True) or (e / "usr/bin/evil").write_text("#!/bin/sh\n"))
        r = self.box.run("restore", bid, "--yes")
        self.assertEqual(r.returncode, 1)
        self.assertIn("refused", r.stdout)


def _docker_pg_available():
    if not shutil.which("docker"):
        return False
    if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode == 0:
        return True
    # pulling a ~400 MB image is opt-in (CI sets GISSTACK_TEST_DOCKER=1 on one matrix entry)
    return os.environ.get("GISSTACK_TEST_DOCKER") == "1" and subprocess.run(["docker", "pull", "-q", IMAGE], capture_output=True).returncode == 0


@unittest.skipUnless(_docker_pg_available(), f"Docker and {IMAGE} needed")
class PostgresRoundTripTests(unittest.TestCase):
    """Phase 7 exit criterion: a restore reproduces the original database in a real PostGIS server."""

    @classmethod
    def setUpClass(cls):
        cls.name = f"gs-backup-test-{os.getpid()}"
        subprocess.run(["docker", "run", "-d", "--rm", "--name", cls.name, "-e", "POSTGRES_PASSWORD=pw", IMAGE], check=True, capture_output=True)
        for _ in range(120):                                  # the image restarts the server once after initialising
            logs = subprocess.run(["docker", "logs", cls.name], capture_output=True, text=True)
            ready = (logs.stdout + logs.stderr).count("ready to accept connections")
            if ready >= 2 and subprocess.run(["docker", "exec", "-u", "postgres", cls.name, "pg_isready", "-q"], capture_output=True).returncode == 0:
                break
            time.sleep(1)
        cls.sql("CREATE ROLE gis LOGIN PASSWORD 'Pw_secret_1'")

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["docker", "rm", "-f", cls.name], capture_output=True)

    @classmethod
    def sql(cls, q, db="postgres"):
        r = subprocess.run(["docker", "exec", "-u", "postgres", cls.name, "psql", "-X", "-At", "-v", "ON_ERROR_STOP=1", "-d", db, "-c", q], capture_output=True, text=True)
        if r.returncode:
            raise AssertionError(r.stderr)
        return r.stdout.strip()

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.td, True)
        self.db = "gis"
        self.sql(f'DROP DATABASE IF EXISTS "{self.db}" WITH (FORCE)')
        for d in self.sql("select datname from pg_database where datname like 'gis\\_%'").split():
            self.sql(f'DROP DATABASE "{d}" WITH (FORCE)')
        self.sql(f'CREATE DATABASE "{self.db}" OWNER gis')
        self.sql("CREATE EXTENSION postgis", self.db)
        self.sql("CREATE TABLE places (id serial primary key, name text, geom geometry(Point,4326)); ALTER TABLE places OWNER TO gis", self.db)
        self.sql("INSERT INTO places(name, geom) SELECT 'p'||g, ST_SetSRID(ST_MakePoint(g, g/2.0), 4326) FROM generate_series(1,500) g", self.db)
        self.box = Box(self.td, pgrun=f"docker exec -i -u postgres {self.name}")
        self.box.env["GS_HEALTH_URL"] = ""
        (self.box.td / "bin/curl").write_text("#!/bin/bash\nexit 0\n")                  # GeoServer is not part of this test: health = ok
        (self.box.td / "bin/curl").chmod(0o755)
        self.box.env["PATH"] = f"{self.box.td / 'bin'}:{os.environ['PATH']}"

    def fingerprint(self, db=None):
        return self.sql("select count(*), sum(id), md5(string_agg(name || ST_AsText(geom), ',' order by id)) from places", db or self.db)

    def test_backup_restore_reproduces_the_database(self):
        want = self.fingerprint()
        bid = self.box.create("postgres,config")
        self.assertIn("VERIFY_OK", self.box.run("verify", bid).stdout)
        self.sql("DELETE FROM places WHERE id % 2 = 0; UPDATE places SET name = 'x'", self.db)
        self.assertNotEqual(self.fingerprint(), want)
        r = self.box.run("restore", bid, "--only", "postgres", "--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.fingerprint(), want)                                      # byte-for-byte the same data
        old = self.sql("select datname from pg_database where datname like 'gis\\_old\\_%'").split()
        self.assertEqual(len(old), 1)                                                   # the previous state is still there
        self.assertEqual(self.sql("select count(*) from places", old[0]), "250")
        self.assertEqual(self.sql("select count(*) from pg_extension where extname='postgis'", self.db), "1")
        self.assertEqual(self.sql("select pg_catalog.pg_get_userbyid(datdba) from pg_database where datname='gis'"), "gis")

    def test_staging_restore_leaves_the_live_database_alone(self):
        bid = self.box.create("postgres")
        self.sql("TRUNCATE places", self.db)
        r = self.box.run("restore", bid, "--only", "postgres", "--staging")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        staged = self.sql("select datname from pg_database where datname like 'gis\\_staging\\_%'")
        self.assertEqual(self.sql("select count(*) from places", staged), "500")
        self.assertEqual(self.sql("select count(*) from places", self.db), "0")

    def test_failed_validation_swaps_the_old_database_back(self):
        bid = self.box.create("postgres,geoserver")
        self.sql("DELETE FROM places WHERE id > 100", self.db)
        before = self.fingerprint()
        (self.box.td / "bin/curl").write_text("#!/bin/bash\nexit 22\n")                 # GeoServer never becomes healthy
        r = self.box.run("restore", bid, "--yes")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("RESTORE_FAILED_ROLLED_BACK", r.stdout)
        self.assertEqual(self.fingerprint(), before)                                    # live database is the pre-restore one again
        self.assertEqual(self.sql("select count(*) from pg_database where datname like 'gis\\_restore\\_%' or datname like 'gis\\_old\\_%'"), "0")

    def test_corrupted_dump_is_detected(self):
        bid = self.box.create("postgres")
        f = self.box.root / bid / f"postgres-{self.db}.dump"
        b = bytearray(f.read_bytes())
        b[len(b) // 2] ^= 0xFF
        f.write_bytes(bytes(b))
        self.assertEqual(self.box.run("verify", bid).returncode, 1)
        before = self.fingerprint()
        self.assertEqual(self.box.run("restore", bid, "--only", "postgres", "--yes").returncode, 1)
        self.assertEqual(self.fingerprint(), before)


if __name__ == "__main__":
    unittest.main()
