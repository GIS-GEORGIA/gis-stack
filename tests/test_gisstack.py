import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gisstack import config, deploy, generate, projects  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
