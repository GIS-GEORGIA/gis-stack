"""Phase 1b / R-04: pre-flight checks are pure functions over collected facts, plus a thin runner."""
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from gisstack import cli, config, deploy, preflight, transport


GOOD = {"os_id": "ubuntu", "os_version": "24.04", "arch": "x86_64", "mem_mb": "8000", "cpus": "4", "disk_/": "50000", "disk_/var": "50000",
        "disk_/opt": "50000", "ntp": "yes", "reach_github.com": "ok", "reach_pypi.org": "ok"}


def cfg(**kw):
    return config.resolve({"targets": ["native"], **kw})


def by_id(checks):
    return {c["id"]: c for c in checks}


class EvaluateTests(unittest.TestCase):
    def test_healthy_machine_passes_everything(self):
        res = preflight.evaluate(cfg(), "native", GOOD)
        self.assertTrue(all(c["status"] == "ok" for c in res), res)
        self.assertEqual(preflight.blocking(res), [])

    def test_unsupported_os_arch_memory_and_disk_block(self):
        facts = {**GOOD, "os_id": "arch", "os_version": "", "arch": "riscv64", "mem_mb": "300", "disk_/var": "400"}
        res = by_id(preflight.evaluate(cfg(), "native", facts))
        for cid in ("os", "arch", "memory", "disk"):
            self.assertEqual(res[cid]["status"], "fail", cid)

    def test_unverified_versions_and_low_resources_only_warn(self):
        need = preflight.needs(cfg(), "native")
        facts = {**GOOD, "os_id": "fedora", "os_version": "41", "mem_mb": str(need["mem_mb"] - 100), "disk_/": str(need["disk_mb"] - 100), "ntp": "no"}
        res = by_id(preflight.evaluate(cfg(), "native", facts))
        for cid in ("os", "memory", "disk", "time"):
            self.assertEqual(res[cid]["status"], "warn", cid)
        self.assertEqual(preflight.blocking(list(res.values())), [])

    def test_ubuntu_22_and_rocky_9_x_are_verified_but_other_majors_warn(self):
        for osid, ver, status in (("ubuntu", "22.04", "ok"), ("rocky", "9.8", "ok"), ("debian", "12", "ok"), ("ubuntu", "20.04", "warn"), ("rocky", "8.9", "warn")):
            self.assertEqual(by_id(preflight.evaluate(cfg(), "native", {**GOOD, "os_id": osid, "os_version": ver}))["os"]["status"], status, (osid, ver))

    def test_unreachable_download_host_blocks_and_is_named(self):
        res = by_id(preflight.evaluate(cfg(), "native", {**GOOD, "reach_github.com": "fail"}))
        self.assertEqual(res["network"]["status"], "fail")
        self.assertIn("github.com", res["network"]["detail"])

    def test_dns_check_only_with_tls_and_a_real_name(self):
        c = cfg(domain="gis.example.ge", tls="internal")
        self.assertIn("getent hosts gis.example.ge", preflight.script(c, "native"))
        self.assertNotIn("getent", preflight.script(cfg(), "native"))
        self.assertNotIn("getent", preflight.script(cfg(domain="localhost", tls="internal"), "native"))
        bad = by_id(preflight.evaluate(c, "native", {**GOOD, "dns": "gis.example.ge="}))
        self.assertEqual(bad["dns"]["status"], "warn")
        good = by_id(preflight.evaluate(c, "native", {**GOOD, "dns": "gis.example.ge=203.0.113.9"}))
        self.assertEqual(good["dns"]["status"], "ok")

    def test_hosts_follow_the_selected_components_and_target(self):
        minimal = cfg(components={"postgis": True, "tomcat": False, "geoserver": False, "mapproxy": False, "caddy": False, "qgis_server": False})
        hosts = preflight.hosts_for(minimal, "native")
        self.assertIn("apt.postgresql.org", hosts)
        self.assertNotIn("github.com", hosts)
        self.assertEqual(preflight.hosts_for(minimal, "compose"), preflight.HOSTS_CONTAINERS)

    def test_requirements_grow_with_components(self):
        small = preflight.needs(cfg(components={"postgis": True, "geoserver": False, "tomcat": False, "mapproxy": False, "caddy": False, "qgis_server": False}), "native")
        big = preflight.needs(cfg(), "native")
        self.assertGreater(big["disk_mb"], small["disk_mb"])
        self.assertGreater(big["mem_mb"], small["mem_mb"])


class ScriptTests(unittest.TestCase):
    def test_script_is_read_only_and_collects_the_facts(self):
        s = preflight.script(cfg(), "native")
        for key in ("os_id=", "arch=", "mem_mb=", "disk_", "ntp=", "reach_github.com"):
            self.assertIn(key, s)
        for bad in ("rm ", "apt-get", "dnf ", "yum ", "systemctl", "> /", "sudo"):
            self.assertNotIn(bad, s)

    def test_script_really_runs_here_and_parses(self):
        out = transport.run("local", None, preflight.script(cfg(components={"postgis": False, "tomcat": False, "geoserver": False, "mapproxy": False,
                                                                       "caddy": False, "qgis_server": False}), "native").replace("curl", "true"), timeout=60)
        facts = preflight.parse(out)
        self.assertIn(facts["arch"], preflight.SUPPORTED_ARCH + ("armv7l", "i686", "ppc64le", "s390x", "riscv64"))
        self.assertGreater(int(facts["mem_mb"]), 100)

    def test_parse_ignores_noise(self):
        self.assertEqual(preflight.parse("hello\nos_id=ubuntu\n=x\nbad key=1\nmem_mb=12\n"), {"os_id": "ubuntu", "mem_mb": "12"})


class IntegrationTests(unittest.TestCase):
    def test_run_with_empty_answer_degrades_to_a_warning(self):
        with mock.patch.object(transport, "run", return_value=""):
            res = preflight.run(cfg(), "local", None, "native")
        self.assertTrue(res["ok"])
        self.assertEqual(res["checks"][0]["status"], "warn")

    def test_cli_preflight_exit_codes(self):
        for facts, code in ((GOOD, 0), ({**GOOD, "arch": "sparc"}, 4)):
            out = "\n".join(f"{k}={v}" for k, v in facts.items())
            with mock.patch.object(transport, "run", return_value=out), redirect_stdout(io.StringIO()) as so:
                self.assertEqual(cli.main(["preflight"]), code)
            self.assertIn("✔", so.getvalue())

    def test_cli_preflight_json(self):
        out = "\n".join(f"{k}={v}" for k, v in GOOD.items())
        with mock.patch.object(transport, "run", return_value=out), redirect_stdout(io.StringIO()) as so:
            cli.main(["preflight", "--json"])
        self.assertTrue(json.loads(so.getvalue())["ok"])

    def test_deploy_stops_on_a_failing_preflight_unless_skipped(self):
        bad = "\n".join(f"{k}={v}" for k, v in {**GOOD, "arch": "sparc"}.items())
        with mock.patch.object(transport, "run", return_value=bad), mock.patch.object(deploy, "deploy", return_value=iter(["ran"])) as dep, \
                redirect_stderr(io.StringIO()) as se, redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["deploy", "--target", "compose"]), 4)
            dep.assert_not_called()
            self.assertIn("skip-preflight", se.getvalue())
        with mock.patch.object(transport, "run", return_value=bad), mock.patch.object(deploy, "deploy", return_value=iter(["ran"])) as dep, \
                redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["deploy", "--target", "compose", "--skip-preflight"]), 0)
            dep.assert_called_once()


@unittest.skipUnless(__import__("importlib").util.find_spec("flask"), "flask needed")
class WebTests(unittest.TestCase):
    def setUp(self):
        from gisstack.webgui.app import create_app
        self.c = create_app("tok").test_client()
        self.h = {"Authorization": "Bearer tok", "X-GisStack": "1"}

    def test_endpoint_and_deploy_gate(self):
        bad = "\n".join(f"{k}={v}" for k, v in {**GOOD, "arch": "sparc"}.items())
        body = {"config": {"targets": ["compose"], "deploy_target": "compose"}, "mode": "local"}
        with mock.patch.object(transport, "run", return_value=bad), mock.patch.object(deploy, "preflight", return_value=[]):
            r = self.c.post("/api/preflight", json=body, headers=self.h)
            self.assertEqual(r.status_code, 200)
            self.assertFalse(r.get_json()["ok"])
            r = self.c.post("/api/deploy", json=body, headers=self.h)
            self.assertEqual(r.status_code, 409)
            self.assertIn("preflight", r.get_json())


if __name__ == "__main__":
    unittest.main()
