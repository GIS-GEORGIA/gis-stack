"""The managers (PostGIS ...) against a real Compose stack: gis-stack's OWN code, driven at containers through `docker exec` (Phase: managers on Compose/Kubernetes).
Opt-in: GISSTACK_TEST_INTEGRATION=1 (builds/pulls several images and takes minutes, so the ordinary CI job that sets GISSTACK_TEST_DOCKER=1 does not run it)."""
from __future__ import annotations

import contextlib
import os
import shlex
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from gisstack import config, generate
from gisstack.core.errors import DeployError
from gisstack.geoserver import gateway as gs_gateway
from gisstack.geoserver import manager as gsm
from gisstack.mapproxy import manager as mpm
from gisstack.mapproxy.server import PlaceGateway
from gisstack import targets, transport
from gisstack.postgis import manager as pgm
from gisstack.postgis.server import ScriptGateway

IMAGE = "postgis/postgis:17-3.5"
GS_IMAGE = "docker.osgeo.org/geoserver:2.28.6"


def available() -> bool:
    if os.environ.get("GISSTACK_TEST_INTEGRATION") != "1" or not shutil.which("docker"):
        return False
    if subprocess.run(["docker", "compose", "version"], capture_output=True).returncode != 0:
        return False
    return subprocess.run(["docker", "pull", "-q", IMAGE], capture_output=True).returncode == 0


GS_AVAILABLE = os.environ.get("GISSTACK_TEST_INTEGRATION") == "1" and shutil.which("docker") is not None and subprocess.run(["docker", "pull", "-q", GS_IMAGE], capture_output=True).returncode == 0


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Stack:
    """A throw-away PostGIS-only Compose project."""

    def __init__(self, tc: unittest.TestCase, **components: bool) -> None:
        self.project = "gsmg" + uuid.uuid4().hex[:6]
        comps = {"postgis": True, "geoserver": False, "tomcat": False, "qgis_server": False, "mapproxy": False, "caddy": False, "webmap": False, **components}
        self.cfg = config.resolve({"project": self.project, "postgres": {"port": free_port()}, "targets": ["compose"], "deploy_target": "compose", "components": comps},
                                  generate_secrets=True)
        out = Path(tempfile.mkdtemp())
        tc.addCleanup(shutil.rmtree, out, True)
        generate.generate_bundle(self.cfg, out)
        d = out / "compose"
        self.base = ["docker", "compose", "-p", self.project, "-f", str(d / "docker-compose.yml"), "--env-file", str(d / ".env")]
        tc.addCleanup(lambda: subprocess.run([*self.base, "down", "-v", "--remove-orphans"], capture_output=True, timeout=180))
        up = subprocess.run([*self.base, "up", "-d", "--wait", "--wait-timeout", "240"], capture_output=True, text=True, timeout=300)
        tc.assertEqual(up.returncode, 0, up.stdout + up.stderr)


SHIM = r"""#!/bin/bash
# fake kubectl for tests: `-n NS get TYPE/NAME -o name` and `-n NS exec -i TYPE/NAME -- CMD...` -> the real container behind $GS_FAKE_MAP
# ("TYPE/NAME=container;..."), as the image's own user ($GS_FAKE_USER_<TYPE>_<NAME>, default: the image default)
[ "$1" = -n ] && { [ "$2" = "$GS_FAKE_NS" ] || { echo "namespace $2 not found" >&2; exit 1; }; shift 2; }
lookup() { echo "$GS_FAKE_MAP" | tr ';' '\n' | sed -n "s#^$1=##p" | head -1; }
case "$1" in
  get) [ -n "$(lookup "$2")" ] && { echo "$2"; exit 0; } || exit 1 ;;
  exec) shift; [ "$1" = -i ] && shift; w="$1"; c="$(lookup "$w")"; [ -n "$c" ] || { echo "no such workload" >&2; exit 1; }; shift
        [ "$1" = -- ] && shift
        u=""; [ "$w" = "statefulset/postgis" ] && u="-u postgres"
        exec docker exec -i $u "$c" "$@" ;;
esac
exit 2
"""


def ctr(project: str, service: str) -> str:
    return subprocess.run(["docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={project}", "--filter", f"label=com.docker.compose.service={service}"],
                          capture_output=True, text=True).stdout.split()[0]


def install_shim(home: str, project: str, namespace: str, workloads: dict[str, str]) -> dict[str, str]:
    """Environment that makes `kubectl` (a shim in $home/bin) forward to the Compose containers of `project`."""
    shim = Path(home) / "bin"
    shim.mkdir(exist_ok=True)
    (shim / "kubectl").write_text(SHIM)
    (shim / "kubectl").chmod(0o755)
    return {"PATH": f"{shim}:{os.environ['PATH']}", "GS_FAKE_NS": namespace,
            "GS_FAKE_MAP": ";".join(f"{w}={ctr(project, svc)}" for w, svc in workloads.items())}


class PostgisBase:
    """The same manager behaviour must hold whether the database is reached through docker exec or (through a shim of) kubectl exec."""
    target = "compose"

    @classmethod
    def setUpClass(cls):
        cls.t = unittest.TestCase()
        cls.cleanups = []
        cls.t.addCleanup = lambda fn, *a, **k: cls.cleanups.append((fn, a, k))        # class-level fixture: run the cleanups in tearDownClass
        cls.stack = Stack(cls.t)
        cls.home = tempfile.mkdtemp()
        env = {"HOME": cls.home}                                                      # the read-only role's password file goes to $HOME/.gisstack
        cls.cfg = dict(cls.stack.cfg, deploy_target=cls.target)
        if cls.target == "k8s":
            env.update(install_shim(cls.home, cls.stack.project, cls.cfg["k8s"]["namespace"], {"statefulset/postgis": "postgis"}))
        cls.p = mock.patch.dict(os.environ, env)
        cls.p.start()
        cls.gw = ScriptGateway(cls.cfg, "local", None)
        cls.db = cls.cfg["postgres"]["db"]

    @classmethod
    def container(cls):
        return ctr(cls.stack.project, "postgis")

    @classmethod
    def tearDownClass(cls):
        cls.p.stop()
        shutil.rmtree(cls.home, ignore_errors=True)
        for fn, a, k in reversed(cls.cleanups):
            fn(*a, **k)

    def test_1_inventory_and_health_run_inside_the_container(self):
        inv = pgm.inventory(self.gw, self.db)
        self.assertTrue(any(e["name"] == "postgis" for e in inv["extensions"]))
        self.assertTrue(pgm.health(inv)["ok"], pgm.health(inv))
        self.assertFalse(inv["errors"], inv["errors"])

    def test_2_readonly_editor_role_boundary(self):
        self.gw.setup(self.db, self.cfg["postgres"]["user"], 15)
        pw = Path(self.home) / ".gisstack" / f"postgis-ro-{self.stack.project}.pw"
        self.assertTrue(pw.is_file() and oct(pw.stat().st_mode & 0o777) == "0o600", "password file must be owner-only, outside the container")
        res = pgm.query(self.gw, self.db, "select postgis_version() as v, current_user as u")
        self.assertEqual(res["rows"][0][1], "gisstack_ro")
        with self.assertRaises(DeployError):
            pgm.query(self.gw, self.db, "create table must_not_exist(a int)")                  # read-only role + transaction
        with self.assertRaises(DeployError):
            pgm.query(self.gw, self.db, "\\! touch /tmp/pwned")                              # a psql meta-command never runs a shell
        out = subprocess.run(["docker", "exec", self.container(), "test", "-e", "/tmp/pwned"], capture_output=True)
        self.assertNotEqual(out.returncode, 0)

    def test_3_elevated_write_uses_the_stacks_own_role(self):
        self.gw.setup(self.db, self.cfg["postgres"]["user"], 15)
        pgm.execute(self.gw, self.db, "create table public.gs_probe(id int primary key)")
        pgm.execute(self.gw, self.db, "insert into public.gs_probe values (1)")
        self.assertEqual(pgm.query(self.gw, self.db, "select count(*) from public.gs_probe")["rows"][0][0], "1")

    def test_4_missing_workload_is_a_clear_error(self):
        cfg = dict(self.cfg, project="gs_no_such_project")
        if self.target == "k8s":
            cfg["k8s"] = dict(self.cfg["k8s"], namespace="nowhere")
        gw = ScriptGateway(cfg, "local", None)
        with self.assertRaises(DeployError) as cm:
            pgm.inventory(gw, self.db)
        self.assertRegex(str(cm.exception), "not running|not found")

    def test_5_hostile_owner_name_is_refused_before_any_script_runs(self):
        cfg = dict(self.cfg, postgres=dict(self.cfg["postgres"], user="gis; touch /tmp/x"))
        with self.assertRaises(DeployError):
            ScriptGateway(cfg, "local", None)


@unittest.skipUnless(available(), "GISSTACK_TEST_INTEGRATION=1 and docker compose needed")
class PostgisOnCompose(PostgisBase, unittest.TestCase):
    target = "compose"


@unittest.skipUnless(available(), "GISSTACK_TEST_INTEGRATION=1 and docker compose needed")
class PostgisOnKubernetesShim(PostgisBase, unittest.TestCase):
    """kubectl is a shim that forwards to the same container: it proves the commands we build are well-formed kubectl-exec invocations (no real cluster here)."""
    target = "k8s"


def port_busy(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


class GeoServerBase:
    target = "compose"

    @classmethod
    def setUpClass(cls):
        if port_busy(8080):                                           # the generated Compose file publishes GeoServer on host port 8080 when there is no Caddy
            raise unittest.SkipTest("host port 8080 is in use")
        cls.t = unittest.TestCase()
        cls.cleanups = []
        cls.t.addCleanup = lambda fn, *a, **k: cls.cleanups.append((fn, a, k))
        cls.stack = Stack(cls.t, geoserver=True)
        cls.home = tempfile.mkdtemp()
        env = {"HOME": cls.home}
        cls.cfg = dict(cls.stack.cfg, deploy_target=cls.target)
        if cls.target == "k8s":
            env.update(install_shim(cls.home, cls.stack.project, cls.cfg["k8s"]["namespace"], {"deployment/geoserver": "geoserver"}))
        cls.p = mock.patch.dict(os.environ, env)
        cls.p.start()
        cls.gw = gs_gateway.for_config(cls.cfg, "local", None)

    @classmethod
    def tearDownClass(cls):
        cls.p.stop()
        shutil.rmtree(cls.home, ignore_errors=True)
        for fn, a, k in reversed(cls.cleanups):
            fn(*a, **k)

    def test_1_health_and_inventory_inside_the_container(self):
        h = gsm.health(self.gw)
        self.assertTrue(h["ok"], h)
        inv = gsm.inventory(self.gw)
        self.assertIn("version", inv)
        self.assertFalse(inv["errors"], inv["errors"])

    def test_2_wrong_credentials_are_reported_not_leaked(self):
        bad = gs_gateway.CurlGateway("local", None, 8080, "admin", "definitely-wrong-password", self.gw.place)
        h = gsm.health(bad)
        self.assertFalse(h["ok"])
        self.assertNotIn("definitely-wrong-password", repr(h))

    def test_3_create_and_delete_a_workspace(self):
        name = "gsit" + uuid.uuid4().hex[:6]
        gsm.create_workspace(self.gw, name)
        self.assertIn(name, [w["name"] for w in gsm.inventory(self.gw)["workspaces"]])
        gsm.delete_workspace(self.gw, name, recurse=False)
        self.assertNotIn(name, [w["name"] for w in gsm.inventory(self.gw)["workspaces"]])


@unittest.skipUnless(available() and GS_AVAILABLE, "GISSTACK_TEST_INTEGRATION=1, docker compose and the GeoServer image needed")
class GeoServerOnCompose(GeoServerBase, unittest.TestCase):
    target = "compose"


@unittest.skipUnless(available() and GS_AVAILABLE, "GISSTACK_TEST_INTEGRATION=1, docker compose and the GeoServer image needed")
class GeoServerOnKubernetesShim(GeoServerBase, unittest.TestCase):
    target = "k8s"


class MapproxyBase:
    """MapProxy built from the generated Dockerfile (python:3.12-slim + pip install mapproxy): needs network once; the image is then cached."""
    target = "compose"

    @classmethod
    def setUpClass(cls):
        if port_busy(8081):                                           # published on host port 8081 when there is no Caddy
            raise unittest.SkipTest("host port 8081 is in use")
        cls.t = unittest.TestCase()
        cls.cleanups = []
        cls.t.addCleanup = lambda fn, *a, **k: cls.cleanups.append((fn, a, k))
        cls.stack = Stack(cls.t, postgis=False, mapproxy=True)
        cls.home = tempfile.mkdtemp()
        env = {"HOME": cls.home}
        cls.cfg = dict(cls.stack.cfg, deploy_target=cls.target)
        if cls.target == "k8s":
            env.update(install_shim(cls.home, cls.stack.project, cls.cfg["k8s"]["namespace"], {"deployment/mapproxy": "mapproxy"}))
        cls.p = mock.patch.dict(os.environ, env)
        cls.p.start()
        cls.gw = PlaceGateway(targets.place_of(cls.cfg, "mapproxy"), "local", None)
        for _ in range(60):                                            # gunicorn needs a few seconds to import MapProxy
            if cls.gw.probe()["http"] == 200:
                break
            time.sleep(2)

    @classmethod
    def tearDownClass(cls):
        cls.p.stop()
        shutil.rmtree(cls.home, ignore_errors=True)
        for fn, a, k in reversed(cls.cleanups):
            fn(*a, **k)

    def test_1_health_config_and_cache_come_from_inside_the_container(self):
        h = mpm.health(self.gw)
        self.assertTrue(h["ok"], h)
        self.assertEqual(h["http"], 200)
        self.assertTrue(h["config_valid"])
        snap = mpm.snapshot(self.gw)
        self.assertTrue(snap["config"]["layers"], "the generated configuration has layers")
        self.assertTrue(mpm.cache_stats(self.gw)["available"])

    def test_2_a_dry_run_edit_is_validated_by_mapproxy_in_the_container(self):
        res = mpm.edit(self.gw, "add_layer", {"name": "dryrun1", "source": "wms", "url": "http://127.0.0.1:1/wms", "layers": "a"}, dry_run=True)
        self.assertEqual((res["dry_run"], res["backup"]), (True, ""))
        with self.assertRaises(DeployError):
            mpm.edit(self.gw, "add_layer", {"name": "bad", "source": "wms", "url": "file:///etc/passwd", "layers": "a"}, dry_run=True)

    def test_3_seeding_estimate_runs_the_seeder_in_the_container(self):
        cache = next(iter(mpm.snapshot(self.gw)["conf"]["caches"]))
        lines = list(mpm.seed(self.gw, {"caches": [cache], "level_from": 0, "level_to": 3, "bbox": [39, 41, 46, 43.5], "dry_run": True}))
        self.assertIsNotNone(mpm.estimate_tiles(lines), lines[-3:])

    def test_4_cleanup_dry_run_without_pgrep_installed(self):
        cache = next(iter(mpm.snapshot(self.gw)["conf"]["caches"]))
        res = mpm.cleanup(self.gw, cache, None, dry_run=True)
        self.assertTrue(res["dry_run"])

    def test_5_cancelling_a_seed_kills_it_inside_the_container(self):
        """Killing the docker/kubectl CLIENT does not stop a process in the container, so the gateway kills the seeder there.  No tiles are fetched:
        a stand-in process with a mapproxy-seed command line is started inside the container and must be gone after a cancelled run."""
        gs_in_start = self.gw.place.prelude() + "gs_in '' bash -c " + shlex.quote("""bash -c 'exec -a "mapproxy-seed -f /nonexistent" sleep 300' >/dev/null 2>&1 &
sleep 1; echo started""")
        self.assertIn("started", transport.run("local", None, gs_in_start))
        self.assertTrue(self.gw._sh("pgrep -f '[m]approxy-seed -f' | wc -l").strip() != "0", "the stand-in is not running")
        stop = threading.Event()
        stop.set()                                                    # a run that was cancelled
        seed_text = "seeds: {}\n"
        with contextlib.suppress(Exception):
            list(self.gw.seed(seed_text, 1, True, stop))
        self.assertEqual(self.gw._sh("pgrep -f '[m]approxy-seed -f' | wc -l").strip(), "0", "the cancelled seeder is still running in the container")


@unittest.skipUnless(available(), "GISSTACK_TEST_INTEGRATION=1 and docker compose needed")
class MapproxyOnCompose(MapproxyBase, unittest.TestCase):
    target = "compose"

    def test_6_edit_writes_to_the_project_dir_with_a_backup_and_mapproxy_reloads(self):
        wd = Path(subprocess.run(["docker", "inspect", "-f", '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}', ctr(self.stack.project, "mapproxy")],
                                 capture_output=True, text=True).stdout.strip())
        before = (wd / "mapproxy" / "mapproxy.yaml").read_text()
        res = mpm.edit(self.gw, "add_layer", {"name": "gs_new", "source": "wms", "url": "http://127.0.0.1:1/wms", "layers": "a"}, dry_run=False)
        self.assertTrue(res["backup"])
        after = (wd / "mapproxy" / "mapproxy.yaml").read_text()
        self.assertIn("gs_new", after)
        self.assertEqual(len(mpm.backups(self.gw)), 1)
        for _ in range(30):                                            # MapProxy's reloader notices the new file by itself
            if "gs_new" in " ".join(mpm.parse_capabilities(self.gw.probe()["caps"])):
                break
            time.sleep(1)
        else:
            self.fail("MapProxy did not reload the changed configuration")
        mpm.restore(self.gw, res["backup"])
        self.assertEqual((wd / "mapproxy" / "mapproxy.yaml").read_text(), before)


@unittest.skipUnless(available(), "GISSTACK_TEST_INTEGRATION=1 and docker compose needed")
class MapproxyOnKubernetesShim(MapproxyBase, unittest.TestCase):
    target = "k8s"

    def test_6_configuration_edits_are_refused_with_an_explanation(self):
        with self.assertRaises(DeployError) as cm:
            mpm.edit(self.gw, "add_layer", {"name": "gs_new", "source": "wms", "url": "http://127.0.0.1:1/wms", "layers": "a"}, dry_run=False)
        self.assertIn("ConfigMap", str(cm.exception))
        self.assertEqual(mpm.backups(self.gw), [])


if __name__ == "__main__":
    unittest.main()
