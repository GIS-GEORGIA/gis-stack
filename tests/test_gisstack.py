import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gisstack import config, deploy, generate, ops, projects, updater  # noqa: E402


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
                stub.write_text('#!/bin/sh\necho "$(basename "$0") $*" >> "%s"\n'
                                'case " $* " in *" config "*) printf "postgis\\ngeoserver\\nmapproxy\\ncaddy\\n" ;; esac\nexit 0\n' % log)
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

            c = run("k8s/apply.sh", "clean")
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
        ids = ops._components(cfg, "native", None)
        checks = ops._checks(cfg, "native", ids)
        # answer the probes: everything fine except the public QGIS check
        http = {i: ("open" if c["kind"] == "tcp" else "200") for i, c in enumerate(checks)}
        out = NATIVE_OUT + "".join(f"@@{'tcp' if checks[i]['kind'] == 'tcp' else 'http'} {i} {v}\n" for i, v in http.items())
        rows = {r["id"]: r for r in ops._native_rows(cfg, ids, ops.parse_native(out), checks)}
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
        self.assertTrue(ops._check_ok("http", "302"))
        self.assertFalse(ops._check_ok("http", "500"))
        self.assertTrue(ops._check_ok("alive", "500"))        # QGIS answers 500 without a project
        self.assertFalse(ops._check_ok("alive", "502"))
        self.assertFalse(ops._check_ok("http", "000"))
        self.assertTrue(ops._check_ok("tcp", "open"))

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
        rows = {r["id"]: r for r in ops._compose_rows(cfg, ["postgis", "geoserver", "caddy", "mapproxy"], ops.parse_compose(out), [])}
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
        rows = {r["id"]: r for r in ops._k8s_rows(cfg, ["geoserver", "caddy", "postgis"], [pod, svc, gs, caddy])}
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
            checks = ops._checks(cfg, "native", ops._components(cfg, "native", None))
            return NATIVE_OUT + "".join(f"@@{'tcp' if c['kind'] == 'tcp' else 'http'} {i} {'open' if c['kind'] == 'tcp' else '200'}\n"
                                        for i, c in enumerate(checks))
        real = ops._run
        ops._run = fake
        try:
            res = ops.status(cfg, "local", None, "native")
        finally:
            ops._run = real
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
            ops._components(cfg, "native", ["nope; rm"])

    def test_local_transport_runs_and_cancels(self):
        import threading
        self.assertEqual(ops._run("local", None, "echo hello").strip(), "hello")
        stop = threading.Event()
        got = []
        for line in ops._stream("local", None, "while true; do echo tick; sleep 0.1; done", stop=stop):
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


if __name__ == "__main__":
    unittest.main()
