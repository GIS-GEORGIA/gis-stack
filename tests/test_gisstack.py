import copy
import json
import os
import re
import stat
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gisstack.providers import common, compose, kubernetes, native  # noqa: E402
from gisstack import config, deploy, deps, discover, existing, fsutil, generate, i18n, ops, projects, settings, transport, updater, validation  # noqa: E402


def make(user=None):
    return config.resolve(user or {})


class ConfigTests(unittest.TestCase):
    def test_defaults_resolve_and_fill_secrets(self):
        c = make()
        self.assertGreaterEqual(len(c["postgres"]["password"]), 16)
        self.assertEqual(c["geoserver"]["admin_user"], "admin")

    def test_geoserver_channel_dictates_tomcat(self):
        self.assertEqual(make({"geoserver": {"channel": "3"}})["tomcat"]["major"], "11")
        self.assertEqual(make({"geoserver": {"channel": "2"}})["tomcat"]["major"], "9")

    def test_rejects_bad_input(self):
        for bad in ({"project": "Bad Name"}, {"domain": "a b"}, {"postgres": {"password": "x;y"}},
                    {"postgres": {"version": "9"}}, {"tls": "auto"},   # auto + localhost
                    {"geoserver": {"extensions": ["nope"]}}, {"postgres": {"extensions": ["nope"]}}):
            with self.assertRaises(config.ConfigError, msg=str(bad)):
                make(bad)

    def test_partial_components_merge_with_defaults(self):
        c = make({"components": {"qgis_server": True}})
        self.assertTrue(c["components"]["qgis_server"])
        self.assertTrue(c["components"]["postgis"])

    def test_webmap_forces_webmercator_grid(self):
        c = make({"mapproxy": {"grids": ["UTM38N"]}})
        self.assertEqual(c["mapproxy"]["grids"][0], "GLOBAL_WEBMERCATOR")


class GenerateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.cfg = make({
            "domain": "gis.example.ge", "tls": "internal", "targets": ["native", "compose", "k8s"],
            "components": {"qgis_server": True},
            "postgres": {"extensions": ["postgis", "pgrouting", "hstore", "pg_stat_statements"]},
            "geoserver": {"extensions": ["vectortiles"]},
            "mapproxy": {"grids": ["GLOBAL_WEBMERCATOR", "UTM38N"], "upstream_basemap": "osm",
                         "seed": {"enabled": True, "levels": [0, 8], "bbox": [39.9, 41.0, 46.8, 43.7]}},
        })
        cls.out = generate.generate_bundle(cls.cfg, Path(cls.tmp.name) / "b")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_all_yaml_parses(self):
        n = 0
        for p in list(self.out.rglob("*.yaml")) + list(self.out.rglob("*.yml")):
            list(yaml.safe_load_all(p.read_text(encoding="utf-8")))
            n += 1
        self.assertGreater(n, 10)

    def test_shell_syntax(self):
        for rel in ("native/install.sh", "native/uninstall.sh", "compose/up.sh", "k8s/apply.sh"):
            r = subprocess.run(["bash", "-n", str(self.out / rel)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, rel + r.stderr)

    def test_secret_files_are_private(self):
        for rel in ("native/stack.env", "compose/.env", "k8s/secret.yaml", "stack.json"):
            self.assertEqual((self.out / rel).stat().st_mode & 0o077, 0, rel)

    def test_native_pulls_in_tomcat_for_geoserver(self):
        self.assertTrue((self.out / "native/systemd/gisstack-tomcat.service").exists())
        self.assertIn("step_geoserver", (self.out / "native/install.sh").read_text())

    def test_compose_has_no_separate_tomcat_unless_requested(self):
        c = yaml.safe_load((self.out / "compose/docker-compose.yml").read_text())
        self.assertNotIn("tomcat", c["services"])
        self.assertIn("geoserver", c["services"])

    def test_compose_builds_custom_postgis_for_pgrouting(self):
        c = yaml.safe_load((self.out / "compose/docker-compose.yml").read_text())
        self.assertIn("build", c["services"]["postgis"])
        self.assertIn("postgresql-17-pgrouting", (self.out / "compose/postgis/Dockerfile").read_text())

    def test_extensions_sql(self):
        sql = (self.out / "native/config/init-extensions.sql").read_text()
        self.assertIn('"pgrouting"', sql)
        self.assertLess(sql.index('"postgis"'), sql.index('"pgrouting"'))

    def test_webmap_config_is_valid_js_json(self):
        js = (self.out / "webmap/config.js").read_text()
        data = json.loads(js.split("=", 1)[1].strip().rstrip(";"))
        self.assertEqual(data["basemap"]["url"], "/mapproxy/wmts/basemap/GLOBAL_WEBMERCATOR/{z}/{x}/{y}.png")

    def test_k8s_bundle_has_webmap_archive(self):
        self.assertTrue((self.out / "k8s/files/webmap.tgz").stat().st_size > 1000)

    def test_mapproxy_config_loads_in_real_mapproxy(self):
        try:
            from mapproxy.config.loader import load_configuration
        except ImportError:
            self.skipTest("mapproxy not installed")
        load_configuration(str(self.out / "native/config/mapproxy/mapproxy.yaml"), ignore_warnings=True)

    def test_caddyfile_routes(self):
        cf = (self.out / "native/config/Caddyfile").read_text()
        for needle in ("handle /geoserver*", "handle_path /mapproxy/*", "transport fastcgi", "tls internal"):
            self.assertIn(needle, cf)
        self.assertIn("respond \"Forbidden\" 403", cf)


class SecretPersistenceTests(unittest.TestCase):
    def test_generated_secrets_are_reused_per_project(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ["GISSTACK_HOME"] = td
            try:
                a = config.resolve({"project": "p1"}, persist_secrets=True)
                b = config.resolve({"project": "p1"}, persist_secrets=True)
                c = config.resolve({"project": "p2"}, persist_secrets=True)
                self.assertEqual(a["postgres"]["password"], b["postgres"]["password"])
                self.assertEqual(a["geoserver"]["admin_password"], b["geoserver"]["admin_password"])
                self.assertNotEqual(a["postgres"]["password"], c["postgres"]["password"])
                f = Path(td) / "secrets" / "p1.json"
                self.assertEqual(f.stat().st_mode & 0o077, 0)
                # an explicitly typed password always wins and is not stored
                d = config.resolve({"project": "p1", "postgres": {"password": "Explicit-123"}}, persist_secrets=True)
                self.assertEqual(d["postgres"]["password"], "Explicit-123")
                self.assertNotIn("Explicit-123", f.read_text())
            finally:
                os.environ.pop("GISSTACK_HOME", None)

    def test_default_resolve_stays_random(self):
        self.assertNotEqual(make()["postgres"]["password"], make()["postgres"]["password"])


class CleanTests(unittest.TestCase):
    def test_clean_args_per_target(self):
        self.assertEqual(deploy.clean_args("native", None), [])
        self.assertEqual(deploy.clean_args("native", ["qgis_server", "tomcat"], purge=True),
                         ["--only", "qgis,tomcat", "--purge"])
        self.assertEqual(deploy.clean_args("native", ["postgis"], True, True, True),
                         ["--only", "postgis", "--purge", "--remove-postgres", "--force-postgres"])
        self.assertEqual(deploy.clean_args("native", ["qgis"]), ["--only", "qgis"])
        self.assertEqual(deploy.clean_args("compose", None), ["clean"])
        self.assertEqual(deploy.clean_args("k8s", ["postgis", "caddy"], True), ["clean", "--purge", "postgis", "caddy"])

    def test_clean_args_rejects_bad_input(self):
        for bad in (("native", ["nope"]), ("native", ["tomcat; rm -rf /"]), ("bogus", None)):
            with self.assertRaises(deploy.DeployError):
                deploy.clean_args(*bad)
        with self.assertRaises(deploy.DeployError):
            deploy.clean_args("compose", None, remove_postgres=True)

    def test_scripts_with_stub_tools(self):
        """Run the generated clean entry points against logging stubs of docker / kubectl."""
        cfg = make({"targets": ["compose", "k8s"], "project": "demo"})
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            out = generate.generate_bundle(cfg, td / "b")
            bindir = td / "bin"
            bindir.mkdir()
            log = td / "calls.log"
            for tool in ("docker", "kubectl"):
                stub = bindir / tool
                stub.write_text(f'#!/bin/sh\necho "$(basename "$0") $*" >> "{log}"\n'
                                'case " $* " in *" config "*) printf "postgis\\ngeoserver\\nmapproxy\\ncaddy\\n" ;; esac\nexit 0\n')
                stub.chmod(0o755)
            env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")

            def run(rel, *args):
                log.write_text("")
                r = subprocess.run(["bash", str(out / rel), *args], cwd=out / rel.split("/")[0],
                                   env=env, capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                return log.read_text()

            c = run("compose/up.sh", "clean")
            self.assertIn("down --remove-orphans", c)
            self.assertNotIn(" -v", c)
            c = run("compose/up.sh", "clean", "--purge")
            self.assertIn("down -v --rmi local", c)
            c = run("compose/up.sh", "clean", "--purge", "postgis", "tomcat")
            self.assertIn("rm -sf postgis", c)
            self.assertIn("volume rm -f demo_pgdata", c)
            self.assertNotIn("volume rm -f demo_geoserver_data", c)
            self.assertNotIn("rm -sf postgis tomcat", c)   # tomcat is not part of this stack

            c = run("compose/up.sh", "only", "webmap", "qgis_server", "postgis")
            self.assertIn("up -d --build caddy postgis", c)       # webmap -> caddy; qgis is not part of this stack
            c = run("k8s/apply.sh", "only", "mapproxy", "postgis")
            self.assertIn("apply -k .", c)
            self.assertIn("rollout status deployment/mapproxy", c)
            self.assertIn("rollout status statefulset/postgis", c)

            c = run("k8s/apply.sh", "clean")                       # no purge: the namespace owns the volumes, so it must NOT be deleted (audit 2026-11, R-4)
            self.assertNotIn("delete -k .", c)
            self.assertIn("delete statefulset,deployment", c)
            self.assertNotIn("pvc", c)
            self.assertNotIn("namespace", c)
            c = run("k8s/apply.sh", "clean", "--purge")            # purge: everything, volumes included
            self.assertIn("delete -k . --ignore-not-found", c)
            c = run("k8s/apply.sh", "clean", "geoserver")
            self.assertIn("delete deployment/geoserver service/geoserver", c)
            self.assertNotIn("pvc", c)
            c = run("k8s/apply.sh", "clean", "--purge", "postgis")
            self.assertIn("statefulset/postgis", c)
            self.assertIn("pvc/data-postgis-0", c)


class WebCleanTests(unittest.TestCase):
    def setUp(self):
        try:
            from gisstack.webgui.app import create_app
        except ImportError:
            self.skipTest("flask not installed")
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        self.addCleanup(os.environ.pop, "GISSTACK_HOME", None)
        os.environ["GISSTACK_HOME"] = self._home.name
        self.c = create_app("tok").test_client()
        self.c.get("/?token=tok")
        self.h = {"X-GisStack": "1"}

    def test_requires_csrf_header(self):
        self.assertEqual(self.c.post("/api/clean", json={"mode": "local"}).status_code, 403)

    def test_validates_input(self):
        post = lambda body: self.c.post("/api/clean", json=body, headers=self.h)   # noqa: E731
        self.assertEqual(post({"mode": "download"}).status_code, 400)
        self.assertEqual(post({"mode": "local", "components": ["nope"]}).status_code, 400)
        self.assertEqual(post({"mode": "local", "components": []}).status_code, 400)
        self.assertEqual(post({"mode": "local", "config": {"deploy_target": "compose"},
                               "remove_postgres": True}).status_code, 400)


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        self.addCleanup(os.environ.pop, "GISSTACK_HOME", None)
        os.environ["GISSTACK_HOME"] = self._home.name

    def test_save_component_touches_only_its_sections(self):
        cfg = config.resolve({"project": "demo", "postgres": {"db": "d1"}, "tomcat": {"heap_mb": 777}},
                             generate_secrets=False)
        projects.save_component(cfg, "postgis")
        data = projects.load("demo")
        self.assertEqual(data["postgres"]["db"], "d1")
        self.assertNotIn("tomcat", data)
        self.assertTrue(data["components"]["postgis"])
        # a second program is added without disturbing the first
        projects.save_component(cfg, "tomcat")
        data = projects.load("demo")
        self.assertEqual(data["tomcat"]["heap_mb"], 777)
        self.assertEqual(data["postgres"]["db"], "d1")
        self.assertEqual(projects.list_projects(), ["demo"])
        self.assertEqual((Path(self._home.name) / "projects" / "demo.json").stat().st_mode & 0o077, 0)

    def test_saved_project_is_a_valid_config_file(self):
        projects.save(config.resolve({"project": "full", "domain": "gis.example.ge", "tls": "internal"},
                                     generate_secrets=False))
        full = projects.load_full("full")
        self.assertEqual(full["domain"], "gis.example.ge")
        self.assertIn("mapproxy", full)                     # defaults laid under the saved values
        self.assertEqual(config.resolve(projects.load("full"))["domain"], "gis.example.ge")

    def test_rejects_bad_names_components_and_values(self):
        cfg = config.resolve({"project": "demo"}, generate_secrets=False)
        with self.assertRaises(projects.ProjectError):
            projects.save_component(cfg, "nope")
        with self.assertRaises(projects.ProjectError):
            projects.load("../etc/passwd")
        bad = dict(cfg, domain="a b")
        with self.assertRaises(projects.ProjectError):
            projects.save(bad)
        self.assertEqual(projects.list_projects(), [])      # nothing half-written

    def test_apply_args(self):
        self.assertEqual(deploy.apply_args("native", "qgis_server"), ["--only", "qgis"])
        self.assertEqual(deploy.apply_args("native", "webmap"), ["--only", "webmap"])
        self.assertEqual(deploy.apply_args("compose", "webmap"), ["only", "caddy"])
        self.assertEqual(deploy.apply_args("k8s", "postgis"), ["only", "postgis"])
        for bad in (("native", "x; rm"), ("bogus", "postgis")):
            with self.assertRaises(deploy.DeployError):
                deploy.apply_args(*bad)

    def test_web_save_load_apply_validation(self):
        try:
            from gisstack.webgui.app import create_app
        except ImportError:
            self.skipTest("flask not installed")
        c = create_app("tok").test_client()
        c.get("/?token=tok")
        h = {"X-GisStack": "1"}
        self.assertEqual(c.post("/api/save", json={"config": {"project": "w1"}, "component": "mapproxy"}).status_code, 403)
        r = c.post("/api/save", json={"config": {"project": "w1", "mapproxy": {"workers": 2}}, "component": "mapproxy"}, headers=h)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(c.get("/api/projects").get_json()["projects"], ["w1"])
        self.assertEqual(c.get("/api/projects/w1").get_json()["config"]["mapproxy"]["workers"], 2)
        self.assertEqual(c.get("/api/projects/nope").status_code, 404)
        self.assertEqual(c.post("/api/save", json={"config": {"project": "w1"}, "component": "zzz"}, headers=h).status_code, 400)
        self.assertEqual(c.post("/api/save", json={"config": {"project": "BAD NAME"}}, headers=h).status_code, 400)
        self.assertEqual(c.post("/api/apply", json={"config": {"project": "w1"}, "component": "zzz", "mode": "local"},
                                headers=h).status_code, 400)
        self.assertEqual(c.post("/api/apply", json={"config": {"project": "w1"}, "component": "caddy", "mode": "download"},
                                headers=h).status_code, 400)


NATIVE_OUT = """@@unit postgis postgresql@17-main.service
LoadState=loaded
ActiveState=active
SubState=running
UnitFileState=enabled
MainPID=1201
ActiveEnterTimestamp=Wed 2026-11-04 08:00:00 UTC
NRestarts=0
@@unit tomcat gisstack-tomcat.service
LoadState=loaded
ActiveState=active
SubState=running
UnitFileState=enabled
MainPID=2301
ActiveEnterTimestamp=Wed 2026-11-04 08:01:00 UTC
NRestarts=2
@@unit qgis_server gisstack-qgis.service
LoadState=loaded
ActiveState=inactive
SubState=dead
UnitFileState=disabled
MainPID=0
@@unit qgis_socket gisstack-qgis.socket
LoadState=loaded
ActiveState=active
SubState=listening
Listen=127.0.0.1:9993 (Stream)
@@unit mapproxy gisstack-mapproxy.service
LoadState=not-found
ActiveState=inactive
SubState=dead
@@unit caddy caddy.service
LoadState=loaded
ActiveState=failed
SubState=failed
UnitFileState=enabled
MainPID=0
@@ss
LISTEN 0 244   127.0.0.1:5432  0.0.0.0:*
LISTEN 0 100   [::ffff:127.0.0.1]:8080  *:*
LISTEN 0 4096  127.0.0.1:9993  0.0.0.0:*
LISTEN 0 4096        *:443         *:*
@@file webmap present
"""


class OpsTests(unittest.TestCase):
    def cfg(self, **kw):
        base = {"targets": ["native", "compose", "k8s"], "domain": "gis.example.ge", "tls": "internal",
                "components": {"geoserver": True, "tomcat": True, "qgis_server": True, "mapproxy": True,
                               "caddy": True, "webmap": True}}
        base.update(kw)
        return config.resolve(base, generate_secrets=False)

    def test_native_status_parsing(self):
        cfg = self.cfg()
        ids = common.components(cfg, "native", None)
        checks = common.checks(cfg, "native", ids)
        # answer the probes: everything fine except the public QGIS check
        portish = ("tcp", "pgready")                                       # the probe script reports both as "@@tcp <i> open|closed"
        http = {i: ("open" if c["kind"] in portish else "200") for i, c in enumerate(checks)}
        out = NATIVE_OUT + "".join(f"@@{'tcp' if checks[i]['kind'] in portish else 'http'} {i} {v}\n" for i, v in http.items())
        rows = {r["id"]: r for r in native.rows(cfg, ids, ops.parse_native(out), checks)}
        self.assertEqual(rows["postgis"]["state"], "running")
        self.assertEqual(rows["postgis"]["listen"], ["127.0.0.1:5432"])
        self.assertEqual(rows["postgis"]["pid"], 1201)
        self.assertIs(rows["postgis"]["enabled"], True)
        self.assertEqual(rows["tomcat"]["restarts"], 2)
        self.assertEqual(rows["geoserver"]["listen"], ["127.0.0.1:8080"])     # lives in Tomcat
        self.assertEqual(rows["qgis_server"]["state"], "stopped")
        self.assertIn("socket active", rows["qgis_server"]["detail"])
        self.assertEqual(rows["qgis_server"]["listen"], ["127.0.0.1:9993"])
        self.assertEqual(rows["mapproxy"]["state"], "missing")
        self.assertEqual(rows["caddy"]["state"], "failed")
        self.assertEqual(rows["caddy"]["listen"], ["0.0.0.0:443"])             # '*:443' normalised
        self.assertEqual(rows["webmap"]["state"], "running")
        self.assertTrue(rows["postgis"]["ok"])
        self.assertFalse(rows["caddy"]["ok"])

    def test_check_rules(self):
        self.assertTrue(common.check_ok("http", "302"))
        self.assertFalse(common.check_ok("http", "500"))
        self.assertTrue(common.check_ok("alive", "500"))        # QGIS answers 500 without a project
        self.assertFalse(common.check_ok("alive", "502"))
        self.assertFalse(common.check_ok("http", "000"))
        self.assertTrue(common.check_ok("tcp", "open"))

    def test_compose_status_parsing(self):
        cfg = self.cfg(deploy_target="compose")
        ps = "\n".join(json.dumps(x) for x in [
            {"Service": "postgis", "State": "running", "Health": "healthy", "Status": "Up 3 minutes (healthy)",
             "Publishers": [{"URL": "127.0.0.1", "TargetPort": 5432, "PublishedPort": 5432, "Protocol": "tcp"}]},
            {"Service": "geoserver", "State": "exited", "Health": "", "Status": "Exited (1)", "Publishers": []},
            {"Service": "caddy", "State": "running", "Health": "", "Status": "Up 1 minute",
             "Publishers": [{"URL": "0.0.0.0", "TargetPort": 443, "PublishedPort": 443, "Protocol": "tcp"}]},
        ])
        out = ps + "\n@@inspect\nmap[com.docker.compose.service:caddy]|172.18.0.4 \nmap[com.docker.compose.service:geoserver]|\n"
        rows = {r["id"]: r for r in compose.rows(cfg, ["postgis", "geoserver", "caddy", "mapproxy"], ops.parse_compose(out), [])}
        self.assertEqual(rows["postgis"]["listen"], ["127.0.0.1:5432 -> 5432/tcp"])
        self.assertEqual(rows["geoserver"]["state"], "stopped")
        self.assertEqual(rows["geoserver"]["listen"], [])       # stopped: no IP
        self.assertEqual(rows["caddy"]["listen"], ["0.0.0.0:443 -> 443/tcp"])
        self.assertEqual(ops.parse_compose(out)["ips"]["caddy"], "172.18.0.4")
        self.assertEqual(rows["mapproxy"]["state"], "missing")
        # JSON array form (newer compose) parses too
        self.assertEqual(len(ops.parse_compose(json.dumps([{"Service": "a"}]) + "\n@@inspect\n")["items"]), 1)

    def test_k8s_status_parsing(self):
        cfg = self.cfg(deploy_target="k8s")
        pod = {"kind": "Pod", "metadata": {"name": "geoserver-1", "labels": {"app": "geoserver"}},
               "spec": {"containers": [{"ports": [{"containerPort": 8080}]}]},
               "status": {"phase": "Running", "podIP": "10.244.0.5", "startTime": "2026-11-04T08:00:00Z",
                          "containerStatuses": [{"ready": True, "restartCount": 1}]}}
        svc = {"kind": "Service", "metadata": {"name": "geoserver"},
               "spec": {"type": "ClusterIP", "clusterIP": "10.96.0.9", "ports": [{"port": 8080, "protocol": "TCP"}]}}
        gs = {"kind": "Deployment", "metadata": {"name": "geoserver"}, "spec": {"replicas": 1}}
        caddy = {"kind": "Deployment", "metadata": {"name": "caddy"}, "spec": {"replicas": 0}}
        rows = {r["id"]: r for r in kubernetes.rows(cfg, ["geoserver", "caddy", "postgis"], [pod, svc, gs, caddy])}
        self.assertEqual(rows["geoserver"]["state"], "running")
        self.assertIn("10.244.0.5:8080/tcp (pod)", rows["geoserver"]["listen"])
        self.assertIn("10.96.0.9:8080/tcp (ClusterIP)", rows["geoserver"]["listen"])
        self.assertEqual(rows["caddy"]["state"], "stopped")
        self.assertEqual(rows["postgis"]["state"], "missing")

    def test_status_end_to_end_with_fake_transport(self):
        cfg = self.cfg()
        calls = []

        def fake(mode, ssh, script, **kw):
            calls.append(script)
            checks = common.checks(cfg, "native", common.components(cfg, "native", None))
            return NATIVE_OUT + "".join(f"@@{'tcp' if c['kind'] == 'tcp' else 'http'} {i} {'open' if c['kind'] == 'tcp' else '200'}\n"
                                        for i, c in enumerate(checks))
        real = transport.run
        transport.run = fake
        try:
            res = ops.status(cfg, "local", None, "native")
        finally:
            transport.run = real
        self.assertEqual({r["id"] for r in res["rows"]}, {"postgis", "tomcat", "geoserver", "qgis_server", "mapproxy", "caddy", "webmap"})
        txt = ops.format_table(res)
        self.assertIn("127.0.0.1:5432", txt)
        self.assertIn("https://gis.example.ge/geoserver/web/", txt)
        self.assertEqual(len(calls), 1)                      # one round trip per refresh
        self.assertIn("ss -Hltn", calls[0])

    def test_control_and_logs_scripts(self):
        cfg = self.cfg()
        stop = ops.control_script(cfg, "native", "stop", ["postgis", "qgis_server", "caddy"])
        self.assertLess(stop.index("systemctl stop caddy.service"), stop.index("systemctl stop $PGU"))                       # reverse order on stop
        self.assertIn("systemctl stop gisstack-qgis.service gisstack-qgis.socket", stop)
        start = ops.control_script(cfg, "native", "start", ["qgis_server"])
        self.assertIn("systemctl start gisstack-qgis.socket gisstack-qgis.service", start)
        self.assertIn("$PGE", ops.control_script(cfg, "native", "enable", ["postgis"]))
        self.assertIn("compose -p gisstack stop caddy", ops.control_script(cfg, "compose", "stop", ["webmap"]))
        self.assertIn("scale statefulset/postgis --replicas=0", ops.control_script(cfg, "k8s", "stop", ["postgis"]))
        self.assertIn("rollout restart deployment/qgis", ops.control_script(cfg, "k8s", "restart", ["qgis_server"]))
        self.assertIn("journalctl --no-pager -n 50 -f -u gisstack-tomcat", ops.logs_script(cfg, "native", "geoserver", 50, True))
        self.assertIn("logs --no-color --tail 20 qgis", ops.logs_script(cfg, "compose", "qgis_server", 20))
        for bad in (("native", "explode", ["caddy"]), ("native", "stop", [])):
            with self.assertRaises(deploy.DeployError):
                ops.control_script(cfg, *bad)
        with self.assertRaises(deploy.DeployError):
            common.components(cfg, "native", ["nope; rm"])

    def test_local_transport_runs_and_cancels(self):
        import threading
        self.assertEqual(transport.run("local", None, "echo hello").strip(), "hello")
        stop = threading.Event()
        got = []
        for line in transport.stream("local", None, "while true; do echo tick; sleep 0.1; done", stop=stop):
            got.append(line)
            if len(got) == 3:
                stop.set()
        self.assertGreaterEqual(len(got), 3)                  # ended cleanly after cancel, no exception

    def test_web_ops_validation(self):
        try:
            from gisstack.webgui.app import create_app
        except ImportError:
            self.skipTest("flask not installed")
        os.environ["GISSTACK_HOME"] = tempfile.mkdtemp()
        self.addCleanup(os.environ.pop, "GISSTACK_HOME", None)
        c = create_app("tok").test_client()
        c.get("/?token=tok")
        h = {"X-GisStack": "1"}
        post = lambda url, body: c.post(url, json=body, headers=h)   # noqa: E731
        self.assertEqual(c.post("/api/ops/status", json={"mode": "local"}).status_code, 403)
        self.assertEqual(post("/api/ops/status", {"mode": "download"}).status_code, 400)
        self.assertEqual(post("/api/ops/control", {"mode": "local", "action": "explode"}).status_code, 400)
        self.assertEqual(post("/api/ops/control", {"mode": "local", "action": "stop", "components": ["x"]}).status_code, 400)
        self.assertEqual(post("/api/ops/logs", {"mode": "local", "component": "x"}).status_code, 400)
        self.assertEqual(post("/api/jobs/nope/cancel", {}).status_code, 404)


class _Resp:
    def __init__(self, data: bytes):
        self._d, self._pos = data, 0

    def read(self, n=-1):
        if n is None or n < 0:
            n = len(self._d)
        chunk = self._d[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class UpdaterTests(unittest.TestCase):
    TAG = "v9.9.9"
    SDIST = b"fake sdist bytes"

    def release(self, **kw):
        import hashlib
        base = updater.ASSET_PREFIX + self.TAG + "/"
        data = {"tag_name": self.TAG, "name": "Release 9.9.9", "body": "- new things", "published_at": "2026-12-01T10:00:00Z",
                "html_url": "https://github.com/GIS-GEORGIA/gis-stack/releases/tag/v9.9.9",
                "assets": [{"name": "gisstack_installer-9.9.9.tar.gz", "browser_download_url": base + "gisstack_installer-9.9.9.tar.gz", "size": 10},
                           {"name": "SHA256SUMS", "browser_download_url": base + "SHA256SUMS", "size": 100},
                           {"name": "evil", "browser_download_url": "https://evil.example/x", "size": 1}]}
        data.update(kw)
        sums = f"{hashlib.sha256(self.SDIST).hexdigest()}  gisstack_installer-9.9.9.tar.gz\n"
        return data, sums

    def opener(self, data, sums, sdist=None):
        def op(req, timeout=0):
            url = req.full_url
            if url == updater.API_LATEST:
                return _Resp(json.dumps(data).encode())
            if url.endswith("SHA256SUMS"):
                return _Resp(sums.encode())
            return _Resp(sdist if sdist is not None else self.SDIST)
        return op

    def test_version_compare(self):
        self.assertTrue(updater.is_newer("0.1.10", "0.1.9"))
        self.assertTrue(updater.is_newer("v1.0", "0.9.9"))
        self.assertFalse(updater.is_newer("0.1.2", "0.1.2"))
        self.assertFalse(updater.is_newer("0.1.1", "0.1.2"))
        self.assertFalse(updater.is_newer("1.0.0", "1.0"))
        for bad in ("latest", "1.0-beta", "", "1..2"):
            with self.assertRaises(updater.UpdateError):
                updater.parse_version(bad)

    def test_check_reports_newer_and_filters_foreign_assets(self):
        data, sums = self.release()
        info = updater.check(self.opener(data, sums))
        self.assertTrue(info["newer"])
        self.assertEqual(info["latest"], "9.9.9")
        self.assertEqual([a["name"] for a in info["assets"]], ["gisstack_installer-9.9.9.tar.gz", "SHA256SUMS"])   # evil host dropped
        self.assertEqual(info["sdist"]["name"], "gisstack_installer-9.9.9.tar.gz")
        self.assertIn(info["install_kind"], ("venv", "package", "source", "unknown"))

    def test_up_to_date(self):
        data, sums = self.release(tag_name="v" + updater.__version__)
        self.assertFalse(updater.check(self.opener(data, sums))["newer"])

    def test_check_errors_are_friendly(self):
        import urllib.error

        def http404(req, timeout=0):
            raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)

        def offline(req, timeout=0):
            raise urllib.error.URLError("no network")
        with self.assertRaises(updater.UpdateError) as cm:
            updater.check(http404)
        self.assertIn("release", str(cm.exception))
        with self.assertRaises(updater.UpdateError):
            updater.check(offline)
        data, sums = self.release(tag_name="nightly")
        with self.assertRaises(updater.UpdateError):
            updater.check(self.opener(data, sums))

    def test_download_verifies_checksum(self):
        data, sums = self.release()
        info = updater.check(self.opener(data, sums))
        with tempfile.TemporaryDirectory() as td:
            path = updater.download(info["sdist"], info["sums"], td, self.opener(data, sums))
            self.assertEqual(path.read_bytes(), self.SDIST)
            # tampered download: hash mismatch -> file removed, error raised
            with self.assertRaises(updater.UpdateError):
                updater.download(info["sdist"], info["sums"], td, self.opener(data, sums, sdist=b"tampered"))
            self.assertFalse((Path(td) / "gisstack_installer-9.9.9.tar.gz").exists())
            # asset missing from SHA256SUMS -> refuse
            with self.assertRaises(updater.UpdateError):
                updater.download(info["sdist"], info["sums"], td, self.opener(data, "deadbeef  other.tar.gz\n"))
            # untrusted URL / no checksum file -> refuse
            with self.assertRaises(updater.UpdateError):
                updater.download({"name": "x.tar.gz", "url": "https://evil.example/x.tar.gz"}, info["sums"], td, self.opener(data, sums))
            with self.assertRaises(updater.UpdateError):
                updater.download(info["sdist"], None, td, self.opener(data, sums))

    def test_apply_is_refused_where_unsafe(self):
        data, sums = self.release()
        info = updater.check(self.opener(data, sums))
        info = dict(info, can_auto=False, install_kind="package")
        with self.assertRaises(updater.UpdateError):
            list(updater.apply(info, tempfile.gettempdir(), self.opener(data, sums)))
        same = dict(info, newer=False)
        self.assertIn("უახლესი", next(iter(updater.apply(same, tempfile.gettempdir()))))

    def test_web_update_check_error_is_reported(self):
        try:
            from gisstack.webgui.app import create_app
        except ImportError:
            self.skipTest("flask not installed")
        os.environ["GISSTACK_HOME"] = tempfile.mkdtemp()
        self.addCleanup(os.environ.pop, "GISSTACK_HOME", None)
        real = updater.check
        updater.check = lambda *a, **k: (_ for _ in ()).throw(updater.UpdateError("offline"))
        self.addCleanup(setattr, updater, "check", real)
        c = create_app("tok").test_client()
        c.get("/?token=tok")
        self.assertEqual(c.post("/api/update/check", json={}).status_code, 403)          # CSRF header required
        r = c.post("/api/update/check", json={}, headers={"X-GisStack": "1"})
        self.assertEqual((r.status_code, r.get_json()["error"]), (502, "offline"))
        r = c.post("/api/update/apply", json={}, headers={"X-GisStack": "1"})
        self.assertEqual(r.status_code, 502)


SCAN_OUT = """@@os deb Ubuntu 24.04.5 LTS
@@root 0
@@ours no
@@pkgs
caddy 2.6.2-6
nginx 1.24.0
postgresql-18 18.6-1
postgresql-client-18 18.6-1
postgresql-common 293
postgis 3.4
qgis-server-bin 3.34.4
tomcat10 10.1.55
@@units
caddy.service active running
nginx.service active running
postgresql@18-main.service active running
postgresql@.service inactive dead
tomcat10.service active running
@@unitfiles
caddy.service enabled
nginx.service enabled
tomcat10.service enabled
@@ss
LISTEN 0 4096 127.0.0.1:5432 0.0.0.0:* users:(("postgres",pid=900,fd=5))
LISTEN 0 511 0.0.0.0:80 0.0.0.0:* users:(("nginx",pid=700,fd=5))
LISTEN 0 511 [::]:80 [::]:* users:(("nginx",pid=700,fd=6))
LISTEN 0 100 *:8080 *:* users:(("java",pid=800,fd=38))
@@paths
/etc/caddy
/etc/postgresql
/var/lib/postgresql
/var/lib/tomcat10
/var/lib/geoserver_data
/var/lib/tomcat10/webapps/geoserver
@@webapps
/var/lib/tomcat10/webapps/geoserver
@@caddy
bin=/usr/bin/caddy
version=v2.6.2
ours=no
@@mapproxy
bin=
@@qgis
fcgi=/usr/lib/cgi-bin/qgis_mapserv.fcgi
@@pg
ver|18.6
port|5432
datadir|/var/lib/postgresql/18/main
db|legacy|7340032
db|gis|16777216
@@end
"""


class ExistingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config.resolve({"components": {"geoserver": True, "qgis_server": True, "mapproxy": True,
                                                  "caddy": True, "webmap": True}}, generate_secrets=False)
        self.rep = discover.analyse(discover.parse(SCAN_OUT), self.cfg)
        self.rep["conflicts"] = discover.conflicts(self.rep, self.cfg)
        self.rep["target"] = "native"

    def test_scan_classification(self):
        c = self.rep["components"]
        self.assertTrue(c["postgis"]["foreign"] and not c["postgis"]["ours"])
        self.assertEqual(c["postgis"]["major"], "18")
        self.assertEqual(c["postgis"]["databases"], ["legacy", "gis"])
        self.assertTrue(c["tomcat"]["foreign"])
        self.assertTrue(c["geoserver"]["foreign"])            # a geoserver webapp inside somebody's Tomcat
        self.assertTrue(c["qgis_server"]["foreign"])
        self.assertTrue(c["caddy"]["foreign"])
        self.assertFalse(c["mapproxy"]["found"])
        self.assertEqual([w["name"] for w in self.rep["web_servers"]], ["nginx"])
        self.assertNotIn("postgresql@.service", [u["name"] for u in c["postgis"]["services"]])   # systemd template ignored

    def test_conflicts_are_per_port_and_name_the_owner(self):
        got = {(x["component"], x["port"]): x for x in self.rep["conflicts"]}
        self.assertEqual(set(got), {("postgis", 5432), ("tomcat", 8080), ("caddy", 80)})
        self.assertEqual(got[("caddy", 80)]["process"], "nginx")
        self.assertEqual(got[("caddy", 80)]["addr"], "0.0.0.0:80, [::]:80")      # IPv4 + IPv6 merged

    OURS_OUT = """@@os deb Ubuntu 24.04
@@root 0
@@ours yes
@@pkgs
postgresql-17 17.11
@@units
gisstack-tomcat.service active running
gisstack-mapproxy.service active running
caddy.service active running
@@unitfiles
gisstack-tomcat.service enabled
@@ss
LISTEN 0 100 127.0.0.1:8080 0.0.0.0:* users:(("java",pid=8,fd=1))
LISTEN 0 100 127.0.0.1:8081 0.0.0.0:* users:(("gunicorn",pid=9,fd=1))
LISTEN 0 100 0.0.0.0:443 0.0.0.0:* users:(("caddy",pid=10,fd=1))
@@paths
/opt/tomcat
/var/lib/tomcat10
/etc/caddy
@@webapps
/opt/tomcat/webapps/geoserver
/opt/apache-tomcat-11.0.27/webapps/geoserver
@@caddy
bin=/usr/local/bin/caddy
ours=yes
@@end
"""

    def test_our_own_install_is_not_a_conflict(self):
        rep = discover.analyse(discover.parse(self.OURS_OUT), self.cfg)
        c = rep["components"]
        self.assertTrue(c["tomcat"]["ours"] and not c["tomcat"]["foreign"])        # leftover /var/lib/tomcat10 is just files
        self.assertTrue(c["geoserver"]["ours"] and not c["geoserver"]["foreign"])
        self.assertTrue(c["mapproxy"]["ours"] and c["caddy"]["ours"] and not c["caddy"]["foreign"])
        self.assertEqual(discover.conflicts(rep, self.cfg), [])
        self.assertTrue(any("დარჩენილი ფაილები" in d for d in c["tomcat"]["details"]))

    def test_a_foreign_process_on_a_port_of_our_own_service_is_still_a_conflict(self):
        out = self.OURS_OUT.replace("LISTEN 0 100 0.0.0.0:443 0.0.0.0:* users:((\"caddy\",pid=10,fd=1))",
                                    "LISTEN 0 100 0.0.0.0:443 0.0.0.0:* users:((\"caddy\",pid=10,fd=1))\nLISTEN 0 100 0.0.0.0:80 0.0.0.0:* users:((\"nginx\",pid=77,fd=1))")
        rep = discover.analyse(discover.parse(out), self.cfg)
        got = discover.conflicts(rep, self.cfg)
        self.assertEqual([(x["component"], x["port"], x["process"]) for x in got], [("caddy", 80, "nginx")])
        # ...while our own caddy on 443 is not, and neither is our QGIS worker (its process name is cut to 15 characters)
        self.assertNotIn(443, [x["port"] for x in got])
        qgis = out.replace("LISTEN 0 4096 127.0.0.1:9993  0.0.0.0:* users:((\"qgis_mapserv.fc\",pid=1,fd=1))", "")
        qgis = qgis.replace("@@paths", "LISTEN 0 4096 127.0.0.1:9993 0.0.0.0:* users:((\"qgis_mapserv.fc\",pid=1,fd=1))\n@@paths").replace("gisstack-mapproxy.service active running", "gisstack-mapproxy.service active running\ngisstack-qgis.service active running")
        rep2 = discover.analyse(discover.parse(qgis), self.cfg)
        self.assertNotIn(9993, [x["port"] for x in discover.conflicts(rep2, self.cfg)])

    def test_plan_spec_and_validation(self):
        self.assertEqual(existing.parse_plan_spec("qgis=remove, caddy=reuse+purge")["qgis_server"], {"action": "remove", "purge": False})
        self.assertTrue(existing.parse_plan_spec("caddy=remove+purge")["caddy"]["purge"])
        for bad in ("tomcat=explode", "tomcat"):
            with self.assertRaises(existing.PlanError):
                existing.parse_plan_spec(bad)
        with self.assertRaises(existing.PlanError):
            existing._norm({"nonsense": "remove"})
        v = lambda spec: existing.validate(existing._norm(existing.parse_plan_spec(spec)), self.rep, self.cfg)   # noqa: E731
        self.assertEqual(len(v("")), 3)                                   # postgres, tomcat, nginx/caddy all unresolved
        self.assertEqual(v("postgis=adopt,tomcat=remove,nginx=disable"), [])
        self.assertEqual(v("postgis=reuse,tomcat=reuse,nginx=disable"), [])
        self.assertTrue(any("adopt" in x for x in v("tomcat=adopt,postgis=adopt,nginx=disable")))      # adopt is PostgreSQL only
        self.assertTrue(any("nginx" in x for x in v("postgis=adopt,tomcat=remove")))                    # port 80 needs nginx dealt with
        self.assertTrue(any("არ ნაპოვნია" in x for x in v("mapproxy=remove,postgis=adopt,tomcat=remove,nginx=disable")))

    def test_scripts_are_safe_and_complete(self):
        rep = dict(self.rep, pkgs_all=[{"name": "tomcat10", "version": "1"}, {"name": "evil; rm -rf /", "version": "1"},
                                       {"name": "postgresql-18", "version": "1"}, {"name": "postgresql-common", "version": "1"}])
        rem = existing.remove_script(rep, "tomcat", purge=False)
        self.assertIn("apt-get remove -y tomcat10", rem)               # remove, not purge: data and config stay
        self.assertNotIn("evil", rem)
        self.assertNotIn("rm -rf /var/lib/tomcat", rem)
        self.assertIn("rm -rf", existing.remove_script(rep, "tomcat", purge=True))
        self.assertIn("apt-get purge -y tomcat10", existing.remove_script(rep, "tomcat", purge=True))
        pg = existing.remove_script(rep, "postgis", purge=False)
        self.assertIn("postgresql-18", pg)
        self.assertNotIn("postgresql-common", pg)                      # shared with other tools
        self.assertIn("/etc/postgresql moved aside", pg)               # keeps Debian from reserving the old port
        self.assertIn("systemctl unmask", existing.remove_script(rep, "caddy", purge=False))
        bk = existing.backup_script(dict(rep), ["postgis", "caddy"], "20260101-000000", with_data=True)
        self.assertIn("pg_dump -Fc legacy", bk)
        self.assertIn("pg-globals.sql", bk)
        self.assertNotIn("pg_dump -Fc legacy", existing.backup_script(rep, ["postgis"], "20260101-000000", with_data=False))
        for comps in (["postgis", "geoserver", "qgis_server", "caddy", "tomcat", "mapproxy"],):
            for script in (bk, existing.import_script(rep, comps, "20260101-000000"), rem):
                r = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
                self.assertEqual(r.returncode, 0, r.stderr)
        imp = existing.import_script(rep, ["geoserver"], "20260101-000000")
        self.assertIn("security", imp)                                 # our admin password must survive the import
        # a hostile database name never reaches a shell
        evil = dict(rep, components=dict(rep["components"], postgis=dict(rep["components"]["postgis"], databases=["x; touch /tmp/pwned"])))
        self.assertNotIn("pwned", existing.backup_script(evil, ["postgis"], "20260101-000000", True))

    def test_caddyfile_imports_carried_over_sites_on_native_only(self):
        with tempfile.TemporaryDirectory() as td:
            out = generate.generate_bundle(config.resolve({"targets": ["native", "compose"]}), Path(td) / "b")
            self.assertIn("import /etc/gisstack/caddy.d/*", (out / "native/config/Caddyfile").read_text())
            self.assertNotIn("caddy.d", (out / "compose/caddy/Caddyfile").read_text())
            inst = (out / "native/install.sh").read_text()
            self.assertIn("--adopt-postgres", inst)
            self.assertIn("clear_mask", inst)

    def test_web_existing_validation(self):
        try:
            from gisstack.webgui.app import create_app
        except ImportError:
            self.skipTest("flask not installed")
        os.environ["GISSTACK_HOME"] = tempfile.mkdtemp()
        self.addCleanup(os.environ.pop, "GISSTACK_HOME", None)
        c = create_app("tok").test_client()
        c.get("/?token=tok")
        h = {"X-GisStack": "1"}
        post = lambda url, body: c.post(url, json=body, headers=h)   # noqa: E731
        self.assertEqual(c.post("/api/existing/scan", json={"mode": "local"}).status_code, 403)
        self.assertEqual(post("/api/existing/scan", {"mode": "local", "config": {"deploy_target": "compose"}}).status_code, 400)
        self.assertEqual(post("/api/existing/backup", {"mode": "local", "components": ["x"]}).status_code, 400)
        self.assertEqual(post("/api/existing/execute", {"mode": "local", "plan": {"tomcat": "explode"}}).status_code, 400)
        # Deploy refuses (409) when somebody else holds a port we need, and returns the report for the UI
        real = discover.scan
        discover.scan = lambda *a, **k: dict(self.rep)
        self.addCleanup(setattr, discover, "scan", real)
        real_pf = deploy.preflight
        deploy.preflight = lambda *a, **k: []
        self.addCleanup(setattr, deploy, "preflight", real_pf)
        r = post("/api/deploy", {"mode": "local", "skip_preflight": True, "config": {"deploy_target": "native"}})
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.get_json()["existing"]["conflicts"])


class I18nTests(unittest.TestCase):
    RUN = re.compile("[^\\n\"`<>{}\\\\$\\[\\]]*[\u10A0-\u10FF][^\\n\"`<>{}\\\\$\\[\\]]*")
    ALLOW = {"ჩემი რუკა", "Ⴀ-ჿ"}       # data default (web-map title) and the page's own Georgian regex

    def _texts(self):
        root = Path(__file__).resolve().parents[1] / "gisstack"
        html = (root / "webgui/templates/index.html").read_text()
        html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
        html = re.sub(r"<style>.*?</style>", "", html, flags=re.S)
        html = re.sub(r"(?<![:\"'])//[^\n]*", "", html).replace("\\n", "\n")
        yield "index.html", html
        dash = (root / "webgui/templates/dashboard.html").read_text()
        yield "dashboard.html", re.sub(r"<!--.*?-->", "", dash, flags=re.S)
        for js in sorted((root / "webgui/static/dashboard/js").rglob("*.js")):
            text = re.sub(r"/\*.*?\*/", "", js.read_text(), flags=re.S)
            yield "dashboard/" + js.name, re.sub(r"(?<![:\"'])//[^\n]*", "", text)
        import ast as _ast
        for f in ("desktop", "catalog", "config", "deploy", "ops", "existing", "discover", "updater", "projects", "webgui/app", "settings", "deps", "cli", "preflight", "journal", "runner", "rotation", "secretsrc", "backup", "backup_crypto", "monitoring/collect", "monitoring/alerts", "monitoring/notify", "monitoring/store", "monitoring/service", "monitoring/render"):
            tree = _ast.parse((root / f"{f}.py").read_text())
            docs = {id(n.body[0].value) for n in _ast.walk(tree)
                    if isinstance(n, (_ast.Module, _ast.FunctionDef, _ast.ClassDef)) and n.body
                    and isinstance(n.body[0], _ast.Expr) and isinstance(getattr(n.body[0], "value", None), _ast.Constant)}
            for n in _ast.walk(tree):
                if isinstance(n, _ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
                    yield f, n.value

    def test_every_interface_string_has_an_english_translation(self):
        """Add new Georgian UI text to gisstack/i18n.py (Georgian -> English) or this fails."""
        missing = {}
        for label, text in self._texts():
            for m in self.RUN.finditer(text):
                run = m.group(0).strip()
                if run not in self.ALLOW and i18n.untranslated(run):
                    missing.setdefault(label, set()).add(run[:100])
        self.assertEqual(missing, {})

    def test_translate_whole_strings_fragments_and_word_boundaries(self):
        self.assertEqual(i18n.tr("შენახვა", "en"), "Save")
        self.assertEqual(i18n.tr("შენახვა", "ka"), "შენახვა")                      # Georgian is the source language
        self.assertEqual(i18n.tr("  შენახვა\n", "en"), "  Save\n")                 # surrounding whitespace is kept
        self.assertEqual(i18n.tr("== ბექაფი: postgis, tomcat", "en"), "== backup: postgis, tomcat")   # fragment + data
        self.assertEqual(i18n.tr("ახალი ვერსია: 0.2.0 (შენი: 0.1.2)", "en"), "New version: 0.2.0 (yours: 0.1.2)")
        self.assertEqual(i18n.tr("no georgian here", "en"), "no georgian here")
        self.assertEqual(i18n.tr("შენახვები", "en"), "შენახვები")                  # unknown word containing a known one is left alone
        self.assertNotIn("\\n", "".join(i18n.EN))

    def test_settings_roundtrip_and_language_resolution(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ["GISSTACK_HOME"] = td
            old = {k: os.environ.pop(k, None) for k in ("GISSTACK_LANG", "LC_ALL", "LC_MESSAGES", "LANG")}
            try:
                self.assertEqual(settings.load(), {"lang": "auto", "theme": "auto"})
                os.environ["LANG"] = "ka_GE.UTF-8"
                self.assertEqual(settings.resolve_lang(), "ka")
                os.environ["LANG"] = "en_US.UTF-8"
                self.assertEqual(settings.resolve_lang(), "en")
                settings.save(lang="ka", theme="dark")
                self.assertEqual(settings.load(), {"lang": "ka", "theme": "dark"})
                self.assertEqual(settings.resolve_lang(), "ka")
                self.assertEqual((Path(td) / "settings.json").stat().st_mode & 0o077, 0)
                with self.assertRaises(ValueError):
                    settings.save(lang="fr")
                with self.assertRaises(ValueError):
                    settings.save(theme="blue")
            finally:
                os.environ.pop("GISSTACK_HOME", None)
                for k, v in old.items():
                    if v is not None:
                        os.environ[k] = v

    def test_web_settings_and_table_endpoints(self):
        try:
            from gisstack.webgui.app import create_app
        except ImportError:
            self.skipTest("flask not installed")
        os.environ["GISSTACK_HOME"] = tempfile.mkdtemp()
        self.addCleanup(os.environ.pop, "GISSTACK_HOME", None)
        c = create_app("tok").test_client()
        c.get("/?token=tok")
        h = {"X-GisStack": "1"}
        self.assertEqual(c.get("/api/i18n").get_json()["en"]["შენახვა"], "Save")
        self.assertEqual(c.get("/api/settings").get_json()["theme"], "auto")
        self.assertEqual(c.post("/api/settings", json={"theme": "dark"}).status_code, 403)       # CSRF header required
        r = c.post("/api/settings", json={"lang": "en", "theme": "dark"}, headers=h)
        self.assertEqual((r.get_json()["lang_resolved"], r.get_json()["theme"]), ("en", "dark"))
        self.assertEqual(c.post("/api/settings", json={"lang": "xx"}, headers=h).status_code, 400)
        page = c.get("/").get_data(as_text=True)
        for needle in ('id="btn-gh"', "https://github.com/GIS-GEORGIA/gis-stack", 'id="dlg-set"', 'data-lang="en"'):
            self.assertIn(needle, page)
        css = c.get("/static/classic/app.css").get_data(as_text=True)                        # the dark theme lives in the stylesheet (no inline <style>)
        self.assertIn('html[data-theme="dark"]', css)


class TkI18nTests(unittest.TestCase):
    def test_desktop_gui_follows_language_and_theme(self):
        try:
            import tkinter
            root = tkinter.Tk()
            root.destroy()
        except Exception:
            self.skipTest("no display / tkinter")
        old = os.environ.get("GISSTACK_HOME")
        try:
            for lang, theme, tab0, btn in (("en", "dark", "General", "⚙ Settings"), ("ka", "light", "ზოგადი", "⚙ პარამეტრები")):
                with tempfile.TemporaryDirectory() as td:
                    os.environ["GISSTACK_HOME"] = td
                    settings.save(lang=lang, theme=theme)
                    from gisstack import desktop
                    desktop.LANG = settings.resolve_lang()      # normally read once when the program starts
                    app = desktop.App()
                    try:
                        app.update()
                        self.assertEqual(app.theme, theme)
                        self.assertEqual(app.nb.tab(0, "text"), tab0)
                        top = [w.cget("text") for w in app.winfo_children()[0].winfo_children() if w.winfo_class() == "TButton"]
                        self.assertIn(btn, top)
                        self.assertIn("GitHub", top)
                    finally:
                        app.destroy()
                        del app
                        __import__("gc").collect()          # finalise Tk variables here, on the main thread (not in a worker thread later)
        finally:
            if old is None:
                os.environ.pop("GISSTACK_HOME", None)
            else:
                os.environ["GISSTACK_HOME"] = old


class DepsTests(unittest.TestCase):
    """Tkinter is installed only after the user says yes."""

    def setUp(self):
        self.msgs = []
        self.calls = []
        self._orig = (deps.tk_available, deps._family)
        self.state = {"tk": False}
        deps.tk_available = lambda: self.state["tk"]
        deps._family = lambda: "deb"
        self.addCleanup(lambda: (setattr(deps, "tk_available", self._orig[0]), setattr(deps, "_family", self._orig[1])))
        os.environ["GISSTACK_LANG"] = "en"
        self.addCleanup(os.environ.pop, "GISSTACK_LANG", None)

    def run_fn(self, rc=0, installs=True):
        def run(cmd, **kw):
            self.calls.append(cmd)
            if installs and rc == 0:
                self.state["tk"] = True
            return subprocess.CompletedProcess(cmd, rc)
        return run

    def test_nothing_happens_when_tkinter_is_there(self):
        self.state["tk"] = True
        self.assertTrue(deps.ensure_tk(ask=lambda q: self.fail("must not ask"), run=self.run_fn(), out=self.msgs.append))
        self.assertEqual((self.calls, self.msgs), ([], []))

    def test_asks_first_and_installs_on_yes(self):
        asked = []
        ok = deps.ensure_tk(ask=lambda q: asked.append(q) or "y", run=self.run_fn(), interactive=True, out=self.msgs.append)
        self.assertTrue(ok)
        self.assertEqual(len(asked), 1)
        self.assertIn("Install it now", asked[0])
        cmd = self.calls[0]
        self.assertEqual(cmd[-4:] if cmd[0] != "sudo" else cmd[-4:], ["apt-get", "install", "-y", "python3-tk"][-4:])
        self.assertTrue(any("python3-tk" in m for m in self.msgs))               # the user is told which package

    def test_no_means_no(self):
        for answer in ("", "n", "no", "maybe"):
            self.calls.clear()
            self.assertFalse(deps.ensure_tk(ask=lambda q, a=answer: a, run=self.run_fn(), interactive=True, out=self.msgs.append))
            self.assertEqual(self.calls, [])
        self.assertTrue(any("web GUI" in m for m in self.msgs))                   # points at the alternative

    def test_never_installs_without_a_terminal_or_on_eof(self):
        self.assertFalse(deps.ensure_tk(ask=lambda q: self.fail("no prompt"), run=self.run_fn(), interactive=False, out=self.msgs.append))
        def eof(q):
            raise EOFError
        self.assertFalse(deps.ensure_tk(ask=eof, run=self.run_fn(), interactive=True, out=self.msgs.append))
        self.assertEqual(self.calls, [])

    def test_failures_and_other_distributions(self):
        self.assertFalse(deps.ensure_tk(ask=lambda q: "y", run=self.run_fn(rc=100), interactive=True, out=self.msgs.append))
        self.assertTrue(any("failed" in m for m in self.msgs))
        self.state["tk"] = False
        self.assertFalse(deps.ensure_tk(ask=lambda q: "y", run=self.run_fn(installs=False), interactive=True, out=self.msgs.append))
        self.assertTrue(any("cannot see it" in m for m in self.msgs))               # installed but not importable (venv)
        deps._family = lambda: "rpm"
        self.assertEqual(deps.tk_package(), ("python3-tkinter", ["dnf", "install", "-y", "python3-tkinter"]))
        deps._family = lambda: ""
        self.calls.clear()
        self.assertFalse(deps.ensure_tk(ask=lambda q: "y", run=self.run_fn(), interactive=True, out=self.msgs.append))
        self.assertEqual(self.calls, [])

    def test_install_script_asks_before_installing(self):
        sh = (Path(__file__).resolve().parents[1] / "install.sh").read_text()
        self.assertIn("Install it now? [y/N]", sh)
        self.assertIn("[ -t 0 ]", sh)                     # only on a terminal
        self.assertIn("--no-tk", sh)


class ConfigValidationTests(unittest.TestCase):
    """Phase 1a / S-01: every field has a type, a range or a pattern; hostile and malformed input is rejected."""

    def rejects(self, **patch):
        cfg = copy.deepcopy(config.default_config())
        for path, value in patch.items():
            cur = cfg
            keys = path.split("__")
            for k in keys[:-1]:
                cur = cur[k]
            cur[keys[-1]] = value
        with self.assertRaises(config.ConfigError) as cm:
            config.resolve(cfg)
        return str(cm.exception)

    def test_defaults_and_numeric_strings_still_work(self):
        c = config.resolve({"postgres": {"port": "5433", "ram_gb": "16"}, "webmap": {"center": ["41.7", "44.8"]}})
        self.assertEqual((c["postgres"]["port"], c["postgres"]["ram_gb"]), (5433, 16))
        self.assertEqual(c["webmap"]["center"], [41.7, 44.8])
        self.assertEqual(c["_meta"]["warnings"], [])

    def test_ranges_and_types(self):
        for bad in ({"postgres__port": 99999}, {"postgres__port": 0}, {"postgres__port": "abc"}, {"postgres__port": True},
                    {"postgres__ram_gb": -1}, {"postgres__max_connections": 5}, {"tomcat__heap_mb": 1}, {"qgis__processes": 1000},
                    {"mapproxy__workers": 0}, {"webmap__zoom": 40}, {"webmap__center": [91, 0]}, {"postgres__remote_access": "yes"},
                    {"mapproxy__seed__levels": [10, 2]}, {"mapproxy__seed__bbox": [10, 0, 5, 1]}, {"mapproxy__meta_size": [0, 4]}):
            self.rejects(**bad)

    def test_patterns_for_values_that_reach_shell_caddy_pg_hba_and_yaml(self):
        self.assertIn("allowed_cidr", self.rejects(postgres__allowed_cidr="0.0.0.0/0 trust # "))
        self.assertIn("allowed_cidr", self.rejects(postgres__allowed_cidr="10.0.0.0/33"))
        self.assertIn("email", self.rejects(email="a@b.c\n\theader_up X-Evil 1"))
        self.assertIn("email", self.rejects(email="not-an-email"))
        self.assertIn("k8s.namespace", self.rejects(k8s__namespace="x; touch /tmp/pwn #"))
        self.assertIn("k8s.namespace", self.rejects(k8s__namespace="UPPER"))
        self.assertIn("projects_dir", self.rejects(qgis__projects_dir="/srv/q$(touch /tmp/pwn)"))
        self.assertIn("projects_dir", self.rejects(qgis__projects_dir="/srv/../etc"))
        self.assertIn("projects_dir", self.rejects(qgis__projects_dir="relative/path"))
        self.assertIn("image", self.rejects(geoserver__image="evil/image:1; rm -rf /"))
        self.assertIn("basemap_url", self.rejects(webmap__basemap_url="javascript:alert(1)"))
        self.assertIn("storage", self.rejects(k8s__pg_storage="20GB; x"))
        self.assertIn("service_type", self.rejects(k8s__service_type="Evil"))

    def test_control_characters_are_rejected_everywhere(self):
        for field in ("webmap__title_en", "webmap__title_ka", "domain", "geoserver__version"):
            self.rejects(**{field: "a\nb"})
        self.rejects(mapproxy__layers=[{"name": "a", "title": "x\u0000y", "source": "geoserver"}])

    def test_layer_items_are_validated(self):
        self.rejects(mapproxy__layers=[{"name": "bad name", "title": "t", "source": "geoserver"}])
        self.rejects(mapproxy__layers=[{"name": "ok", "title": "t", "source": "evil"}])
        self.rejects(mapproxy__layers=[{"name": "ok", "title": "t", "source": "wms", "url": "ftp://x"}])
        self.assertIn("unknown key", self.rejects(mapproxy__layers=[{"name": "ok", "title": "t", "source": "wms", "extra": 1}]))
        self.rejects(mapproxy__layers=[{"name": f"l{i}", "title": "t", "source": "geoserver"} for i in range(600)])

    def test_malformed_files_give_errors_not_tracebacks(self):
        for bad in ({"postgres": "x"}, {"components": []}, {"mapproxy": {"layers": "x"}}, {"webmap": {"center": 5}},
                    {"k8s": None}, {"postgres": {"port": [1]}}, {"targets": "native"}, {"geoserver": {"extensions": 3}}):
            with self.assertRaises(config.ConfigError, msg=str(bad)):
                config.resolve(bad)
        with self.assertRaises(config.ConfigError):
            config.resolve([1, 2])             # type: ignore[arg-type]

    def test_unknown_keys_warn_by_default_and_fail_in_strict_mode(self):
        c = config.resolve({"postgres": {"prot": 5432}, "typo_section": {"a": 1}})
        self.assertTrue(any("postgres.prot" in w and "typo_section" in w for w in c["_meta"]["warnings"]))
        os.environ[validation.STRICT_ENV] = "error"
        self.addCleanup(os.environ.pop, validation.STRICT_ENV, None)
        with self.assertRaises(config.ConfigError) as cm:
            config.resolve({"postgres": {"prot": 5432}})
        self.assertIn("postgres.prot", str(cm.exception))

    def test_cli_reports_a_broken_config_file_cleanly(self):
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "bad.json"
            bad.write_text("{ not json")
            r = subprocess.run([sys.executable, "-m", "gisstack", "generate", "-c", str(bad), "-o", str(Path(td) / "o")],
                               capture_output=True, text=True, cwd=Path(__file__).resolve().parents[1])
            self.assertNotEqual(r.returncode, 0)
            self.assertNotIn("Traceback", r.stderr)


class ShellQuotingTests(unittest.TestCase):
    """The quoting filters must round-trip arbitrary text through real bash without expansion."""
    SAMPLES = ["plain", "a b", "$(echo PWNED)", "`echo PWNED`", 'q"uote', "back\\slash", "a}b", "${HOME}", "x'y", "$$", "!history", "multi\nline", "*?[x]", ";|&<>"]

    def test_double_quote_and_default_word_filters_roundtrip(self):
        for text in self.SAMPLES:
            for tmpl in ('printf %s "{}"', 'X=; printf %s "${{X:-{}}}"'):
                filt = generate._shdw if "X:-" in tmpl else generate._shdq
                r = subprocess.run(["bash", "-c", tmpl.format(filt(text))], capture_output=True, text=True)
                self.assertEqual(r.stdout, text, (tmpl, text))

    def test_single_quote_filter_roundtrips(self):
        env = generate._env() if hasattr(generate, "_env") else None
        shq = env.filters["shq"]
        for text in self.SAMPLES:
            r = subprocess.run(["bash", "-c", f"printf %s {shq(text)}"], capture_output=True, text=True)
            self.assertEqual(r.stdout, text)


class InjectionFuzzTests(unittest.TestCase):
    """Phase 1a / S-01 regression: hostile text in ANY string field is either rejected or completely inert in every
    generated file.  'Inert' is checked by actually sourcing stack.env and running apply.sh / up.sh with stub tools."""
    PAYLOADS = ["$(touch {m})", "`touch {m}`", "x\"; touch {m}; \"", "x'; touch {m}; '", "a\ntouch {m}",
                "0.0.0.0/0 trust\n\theader_up X-Evil 1", "../../etc/passwd | tee {m}", "${{IFS}}{m}"]

    @staticmethod
    def string_paths(node, prefix=""):
        out = []
        for k, v in node.items():
            path = f"{prefix}{k}"
            if isinstance(v, dict):
                out += InjectionFuzzTests.string_paths(v, path + ".")
            elif (isinstance(v, str) or (isinstance(v, list) and v and all(isinstance(x, str) for x in v))
                  or (isinstance(v, (int, float)) and not isinstance(v, bool))):
                out.append(path)
        return out

    def test_every_field_with_every_payload(self):
        base = copy.deepcopy(config.default_config())
        base["targets"] = ["native", "compose", "k8s"]
        base["components"].update({"qgis_server": True, "geoserver": True, "mapproxy": True, "caddy": True, "webmap": True})
        paths = [p for p in self.string_paths(base) if not p.startswith("ssh.")]
        self.assertGreater(len(paths), 40)
        accepted = rejected = 0
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            bindir = td / "bin"
            bindir.mkdir()
            for tool in ("docker", "kubectl"):
                (bindir / tool).write_text("#!/bin/sh\nexit 0\n")
                (bindir / tool).chmod(0o755)
            env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")
            n = 0
            for path in paths:
                for payload in self.PAYLOADS:
                    n += 1
                    marker = td / f"pwn{n}"
                    cfg = copy.deepcopy(base)
                    cur = cfg
                    keys = path.split(".")
                    for k in keys[:-1]:
                        cur = cur[k]
                    orig = cur[keys[-1]]
                    text = payload.format(m=marker)
                    cur[keys[-1]] = [text] if isinstance(orig, list) else text
                    try:
                        resolved = config.resolve(cfg)
                    except config.ConfigError:
                        rejected += 1
                        continue
                    accepted += 1
                    out = generate.generate_bundle(resolved, td / f"b{n}")
                    for yml in list(out.rglob("*.yaml")) + list(out.rglob("*.yml")):
                        list(yaml.safe_load_all(yml.read_text(encoding="utf-8")))
                    subprocess.run(["bash", "-c", ". ./stack.env"], cwd=out / "native", env=env, capture_output=True)
                    subprocess.run(["bash", "apply.sh", "status"], cwd=out / "k8s", env=env, capture_output=True)
                    subprocess.run(["bash", "up.sh", "ps"], cwd=out / "compose", env=env, capture_output=True)
                    self.assertFalse(marker.exists(), f"command execution via {path} = {text!r}")
                    caddy = (out / "native/config/Caddyfile").read_text()
                    self.assertNotIn("header_up X-Evil", caddy, f"Caddyfile injection via {path}")
                    self.assertNotIn("0.0.0.0/0 trust", (out / "native/stack.env").read_text().split("PG_ALLOWED_CIDR")[1].split("\n")[0]
                                     if "PG_ALLOWED_CIDR" in (out / "native/stack.env").read_text() else "")
                    shutil.rmtree(out, ignore_errors=True)
        self.assertGreater(rejected, 200)       # most hostile values never get past validation
        self.assertGreater(accepted, 20)        # free-text fields are accepted, and were proven inert above


class SecureFileTests(unittest.TestCase):
    """Phase 1a / S-05: secrets are never written with loose permissions, not even briefly."""

    def test_write_private_has_the_final_mode_from_the_first_byte(self):
        old = os.umask(0o000)                      # the worst case: a umask that would make everything world-writable
        try:
            with tempfile.TemporaryDirectory() as td:
                f = Path(td) / "secret.json"
                fsutil.write_private(f, "one")
                fsutil.write_private(f, "two")     # atomic replace keeps the mode
                self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)
                self.assertEqual(f.read_text(), "two")
                self.assertEqual([x.name for x in Path(td).iterdir()], ["secret.json"])   # no temp leftovers
        finally:
            os.umask(old)

    def test_private_dir_creates_and_tightens(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "a" / "b"
            fsutil.private_dir(d)
            self.assertEqual(stat.S_IMODE(d.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(d.parent.stat().st_mode), 0o700)      # intermediate directories too
            loose = Path(td) / "loose"
            loose.mkdir(mode=0o755)
            os.chmod(loose, 0o755)
            fsutil.private_dir(loose)
            self.assertEqual(stat.S_IMODE(loose.stat().st_mode), 0o700)

    def test_home_dir_token_and_settings_are_private(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ["GISSTACK_HOME"] = str(Path(td) / "home")
            self.addCleanup(os.environ.pop, "GISSTACK_HOME", None)
            home = deploy.home_dir()
            self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o700)
            settings.save(lang="en")
            self.assertEqual(stat.S_IMODE((home / "settings.json").stat().st_mode), 0o600)
            try:
                from gisstack.webgui.app import load_or_create_token
            except ImportError:
                return
            load_or_create_token()
            self.assertEqual(stat.S_IMODE((home / "web.token").stat().st_mode), 0o600)
            config.resolve({"project": "perm"}, persist_secrets=True)
            self.assertEqual(stat.S_IMODE((home / "secrets" / "perm.json").stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((home / "secrets").stat().st_mode), 0o700)

    def test_bundle_secret_files_are_created_0600_never_wider(self):
        created = []
        real_open = os.open

        def spy(path, flags, mode=0o777, *a, **kw):
            created.append((str(path), mode))
            return real_open(path, flags, mode, *a, **kw)
        old = os.umask(0o000)
        os.open = spy
        try:
            with tempfile.TemporaryDirectory() as td:
                cfg = config.resolve({"targets": ["native", "compose", "k8s"]})
                out = generate.generate_bundle(cfg, Path(td) / "b")
                self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o700)
                for rel in ("native/stack.env", "compose/.env", "k8s/secret.yaml", "stack.json"):
                    self.assertEqual(stat.S_IMODE((out / rel).stat().st_mode), 0o600, rel)
                    modes = [m for p, m in created if p.endswith(rel)]
                    self.assertEqual(modes, [0o600], f"{rel} must be created 0600 (no write-then-chmod window)")
                self.assertEqual(stat.S_IMODE((out / "native/install.sh").stat().st_mode), 0o755)
                self.assertEqual(stat.S_IMODE((out / "native/config/Caddyfile").stat().st_mode), 0o644)
        finally:
            os.open = real_open
            os.umask(old)

    def test_remote_upload_uses_a_private_umask(self):
        src = (Path(__file__).resolve().parents[1] / "gisstack/deploy.py").read_text()
        self.assertIn("umask 077 && mkdir -p", src)


class BackupSafetyTests(unittest.TestCase):
    """Phase 1a / S-02: backups fail closed, are verified, never overwrite, and gate every destructive step."""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.td, ignore_errors=True))
        self.bin = self.td / "bin"
        self.bin.mkdir()
        self.stub("pg_dump", 'for a in "$@"; do last="$a"; done\n[ "$last" = "${GS_FAIL_DB:-none}" ] && exit 1\necho "DUMP of $last"')
        self.stub("pg_dumpall", 'echo "-- globals"')
        self.stub("pg_restore", 'if [ -n "${GS_FAIL_RESTORE:-}" ]; then exit 1; fi\ncat >/dev/null')
        self.rep = {"components": {"postgis": {"databases": ["legacy", "gis"]}}, "family": "deb", "pkgs_all": []}
        self.root = self.td / "backups"

    def stub(self, name, body):
        f = self.bin / name
        f.write_text("#!/bin/bash\n" + body + "\n")
        f.chmod(0o755)

    def env(self, **extra):
        return dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", GS_BACKUP_ROOT=str(self.root), GS_PGRUN="", **extra)

    def run_backup(self, ts="20260101-000000-abcd", comps=("postgis",), with_data=True, **extra):
        script = existing.backup_script(self.rep, list(comps), ts, with_data)
        return subprocess.run(["bash", "-c", script], env=self.env(**extra), capture_output=True, text=True)

    def test_success_writes_checksums_and_a_private_directory(self):
        r = self.run_backup()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("BACKUP_VERIFIED=", r.stdout)
        d = self.root / "20260101-000000-abcd"
        self.assertEqual(stat.S_IMODE(d.stat().st_mode), 0o700)
        sums = (d / "SHA256SUMS").read_text()
        for name in ("pg-db-legacy.dump", "pg-db-gis.dump", "pg-globals.sql", "manifest.txt"):
            self.assertIn(name, sums)
        self.assertEqual(subprocess.run(["sha256sum", "-c", "--quiet", "SHA256SUMS"], cwd=d).returncode, 0)

    def test_a_failed_dump_fails_closed(self):
        r = self.run_backup(GS_FAIL_DB="gis")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("BACKUP_FAILED", r.stdout)
        self.assertNotIn("BACKUP_VERIFIED", r.stdout)
        self.assertNotIn("BACKUP_DIR=", r.stdout)
        self.assertTrue((self.root / "20260101-000000-abcd.FAILED").is_dir())      # kept for inspection, never reused

    def test_an_unrestorable_dump_fails_closed(self):
        r = self.run_backup(GS_FAIL_RESTORE="1")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("BACKUP_VERIFIED", r.stdout)
        self.assertIn("not restorable", r.stdout)

    def test_a_failing_tar_fails_closed(self):
        f = self.td / "etc" / "caddy" / "Caddyfile"
        f.parent.mkdir(parents=True)
        f.write_text("x")
        old = existing.BACKUP_PATHS["caddy"]
        existing.BACKUP_PATHS["caddy"] = [str(self.td / "etc" / "caddy")]
        self.addCleanup(existing.BACKUP_PATHS.__setitem__, "caddy", old)
        self.stub("tar", "exit 2")
        r = self.run_backup(comps=("caddy",))
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("BACKUP_VERIFIED", r.stdout)
        self.assertIn("tar caddy", r.stdout)

    def test_an_existing_backup_directory_is_never_reused(self):
        self.assertEqual(self.run_backup().returncode, 0)
        marker = self.root / "20260101-000000-abcd" / "manifest.txt"
        before = marker.read_text()
        r = self.run_backup()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("BACKUP_EXISTS", r.stdout)
        self.assertEqual(marker.read_text(), before)

    def test_backup_ids_do_not_collide(self):
        self.assertEqual(len({existing.new_backup_id() for _ in range(200)}), 200)

    def make_archive(self, tamper=None, drop_sums=False):
        shutil.rmtree(self.td / "pack", ignore_errors=True)
        d = self.td / "pack" / "20260101-000000-abcd"
        d.mkdir(parents=True)
        (d / "a.txt").write_text("alpha")
        (d / "b.txt").write_text("beta")
        if not drop_sums:
            subprocess.run("sha256sum a.txt b.txt > SHA256SUMS", shell=True, cwd=d, check=True)
        if tamper:
            (d / tamper).write_text("tampered")
        out = self.td / "x.tar.gz"
        with tarfile.open(out, "w:gz") as tf:
            tf.add(d, arcname="20260101-000000-abcd")
        return out

    def test_downloaded_archives_are_verified(self):
        self.assertEqual(existing.verify_backup_archive(self.make_archive()), 2)
        for kwargs in ({"tamper": "b.txt"}, {"drop_sums": True}):
            with self.assertRaises(deploy.DeployError):
                existing.verify_backup_archive(self.make_archive(**kwargs))
        broken = self.td / "broken.tar.gz"
        broken.write_bytes(b"not a gzip")
        with self.assertRaises(deploy.DeployError):
            existing.verify_backup_archive(broken)

    def test_local_copy_never_overwrites_an_existing_backup(self):
        os.environ["GISSTACK_HOME"] = str(self.td / "home")
        self.addCleanup(os.environ.pop, "GISSTACK_HOME", None)
        dest = self.td / "dl" / "h"
        dest.mkdir(parents=True)
        (dest / "20260101-000000-abcd.tar.gz").write_text("precious")
        with self.assertRaises(deploy.DeployError):
            existing.fetch_backup("local", None, "20260101-000000-abcd", self.td / "dl", "h")
        self.assertEqual((dest / "20260101-000000-abcd.tar.gz").read_text(), "precious")

    # --- orchestration: nothing destructive may run unless a verified backup exists
    def orchestrate(self, **env_extra):
        rep = discover.analyse(discover.parse(SCAN_OUT), config.resolve({"components": {"geoserver": True, "qgis_server": True, "mapproxy": True, "caddy": True, "webmap": True}}, generate_secrets=False))
        rep["conflicts"] = discover.conflicts(rep, config.resolve({}, generate_secrets=False))
        ran = []
        env = self.env(**env_extra)
        real = (discover.scan, transport.run, existing.fetch_backup, deploy.deploy)

        def fake_run(mode, ssh, script, **kw):
            if "BACKUP_ROOT" in script:
                r = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
                return r.stdout
            ran.append(script[:60])
            return ""

        def fake_fetch(mode, ssh, ts, dest, label):
            out = self.td / f"{ts}.tar.gz"
            with tarfile.open(out, "w:gz") as tf:
                tf.add(self.root / ts, arcname=ts)
            return out
        discover.scan = lambda *a, **k: rep
        transport.run = fake_run
        existing.fetch_backup = fake_fetch
        deploy.deploy = lambda *a, **k: iter(["DEPLOYED"])
        try:
            plan = {"postgis": {"action": "reuse", "purge": False}, "tomcat": {"action": "remove", "purge": False},
                    "nginx": {"action": "disable", "purge": False}}
            lines = []
            err = None
            try:
                for line in existing.run(config.resolve({}), "native", "local", None, plan, lambda c: self.td, backup_dir=str(self.td)):
                    lines.append(line)
            except deploy.DeployError as e:
                err = e
            return lines, ran, err
        finally:
            discover.scan, transport.run, existing.fetch_backup, deploy.deploy = real

    def test_no_removal_after_a_failed_backup(self):
        lines, ran, err = self.orchestrate(GS_FAIL_DB="gis")
        self.assertIsNotNone(err)
        self.assertEqual(ran, [], "no remove/disable/import script may run when the backup failed")
        self.assertNotIn("DEPLOYED", lines)

    def test_removal_and_install_follow_a_verified_backup(self):
        lines, ran, err = self.orchestrate()
        self.assertIsNone(err, err)
        self.assertTrue(any("SHA-256 ✔" in l for l in lines))
        self.assertIn("DEPLOYED", lines)
        self.assertGreaterEqual(len(ran), 3)         # disable nginx, remove postgis, remove tomcat, import

    def test_purge_needs_explicit_confirmation(self):
        rep_calls = []
        real = discover.scan
        discover.scan = lambda *a, **k: rep_calls.append(1)
        try:
            with self.assertRaises(existing.PlanError):
                list(existing.run(config.resolve({}), "native", "local", None, {"tomcat": {"action": "remove", "purge": True}},
                                  lambda c: self.td))
            self.assertEqual(rep_calls, [])           # refused before even scanning the server
        finally:
            discover.scan = real

    def test_web_api_refuses_purge_without_confirmation(self):
        try:
            from gisstack.webgui.app import create_app
        except ImportError:
            self.skipTest("flask not installed")
        os.environ["GISSTACK_HOME"] = str(self.td / "h2")
        self.addCleanup(os.environ.pop, "GISSTACK_HOME", None)
        c = create_app("tok").test_client()
        c.get("/?token=tok")
        h = {"X-GisStack": "1"}
        body = {"mode": "local", "config": {"deploy_target": "native"}, "plan": {"tomcat": {"action": "remove", "purge": True}}}
        real = deploy.preflight
        deploy.preflight = lambda *a, **k: []
        self.addCleanup(setattr, deploy, "preflight", real)
        r = c.post("/api/existing/execute", json=body, headers=h)
        self.assertEqual(r.status_code, 400)
        self.assertIn("confirm_data_loss", r.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
