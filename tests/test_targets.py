"""targets.Place: how the managers' scripts reach a program in a Compose container or a Kubernetes pod (no Docker/cluster needed: docker and kubectl are fakes)."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gisstack import config, targets, transport
from gisstack.core.errors import DeployError
from gisstack.geoserver import gateway as gsg
from gisstack.mapproxy.server import PlaceGateway
from gisstack.postgis.server import ScriptGateway

FAKE_DOCKER = """#!/bin/bash
echo "$(basename "$0") $*" >> "$GS_LOG"
if [ "$1" = ps ]; then [ -n "$GS_HAVE" ] && echo "abc123def456"; exit 0; fi
if [ "$1" = exec ]; then cat > /dev/null; echo "EXEC-OK"; exit 0; fi
exit 0
"""
FAKE_KUBECTL = """#!/bin/bash
echo "kubectl $*" >> "$GS_LOG"
case " $* " in *" get "*) [ -n "$GS_HAVE" ] && { echo "$4"; exit 0; } || exit 1;; esac
if [[ " $* " == *" exec "* ]]; then cat > /dev/null; echo "EXEC-OK"; exit 0; fi
exit 0
"""


def cfg_for(target: str, **kw):
    return config.resolve({"project": "demo", "targets": [target], "deploy_target": target, **kw})


class Base(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.td, True)
        (self.td / "bin").mkdir()
        for n, body in (("docker", FAKE_DOCKER), ("kubectl", FAKE_KUBECTL), ("podman", FAKE_DOCKER)):
            (self.td / "bin" / n).write_text(body)
            (self.td / "bin" / n).chmod(0o755)
        self.log = self.td / "log"
        p = mock.patch.dict(os.environ, {"PATH": f"{self.td / 'bin'}:{os.environ['PATH']}", "GS_LOG": str(self.log), "GS_HAVE": "1"})
        p.start()
        self.addCleanup(p.stop)

    def run_script(self, script, stdin=""):
        return subprocess.run(["bash", "-c", script], input=stdin, capture_output=True, text=True, timeout=30)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []


class PlaceTests(Base):
    def test_native_has_no_prelude_and_containers_do(self):
        self.assertEqual(targets.place_of(cfg_for("native"), "postgis").prelude(), "")
        for t in ("compose", "k8s"):
            self.assertIn("gs_in()", targets.place_of(cfg_for(t), "postgis").prelude())

    def test_unknown_component_or_target_is_an_error(self):
        with self.assertRaises(DeployError):
            targets.place_of(cfg_for("compose"), "caddy")
        with self.assertRaises(DeployError):
            targets.place_of({"deploy_target": "mainframe", "project": "x"}, "postgis")

    def test_compose_finds_the_container_by_label_and_execs_with_user_and_env(self):
        pl = targets.place_of(cfg_for("compose"), "postgis")
        r = self.run_script(pl.prelude() + 'gs_in nobody psql -c "select 1"')
        self.assertEqual(r.stdout.strip(), "EXEC-OK", r.stderr)
        ps, ex = self.calls()
        self.assertIn("label=com.docker.compose.project=demo", ps)
        self.assertIn("label=com.docker.compose.service=postgis", ps)
        self.assertTrue(ex.startswith("docker exec -i -u nobody -e PGPASSWORD -e PGCONNECT_TIMEOUT -e PGOPTIONS abc123def456 psql"), ex)
        self.log.unlink()
        self.run_script(pl.prelude() + 'gs_in "" psql')
        self.assertNotIn("-u", self.calls()[1])                          # empty user: the image's own user

    def test_podman_runtime_is_used_when_configured(self):
        pl = targets.place_of(cfg_for("compose", runtime="podman"), "postgis")
        self.run_script(pl.prelude() + "gs_in '' true")
        self.assertTrue(all(c.startswith("podman ") for c in self.calls()), self.calls())

    def test_missing_container_fails_clearly_and_before_running_anything(self):
        pl = targets.place_of(cfg_for("compose"), "geoserver")
        with mock.patch.dict(os.environ, {"GS_HAVE": ""}):
            r = self.run_script(pl.prelude() + "echo SHOULD-NOT-RUN")
        self.assertEqual(r.returncode, 98)
        self.assertIn("geoserver: container is not running", r.stderr)
        self.assertNotIn("SHOULD-NOT-RUN", r.stdout)

    def test_kubernetes_uses_namespace_and_workload_and_ignores_user(self):
        pl = targets.place_of(cfg_for("k8s", k8s={"namespace": "gis-prod"}), "mapproxy")
        r = self.run_script(pl.prelude() + "gs_in nobody true")
        self.assertEqual(r.stdout.strip(), "EXEC-OK", r.stderr)
        self.assertIn("kubectl -n gis-prod get deployment/mapproxy -o name", self.calls()[0])
        self.assertIn("kubectl -n gis-prod exec -i deployment/mapproxy -- true", self.calls()[1])
        self.assertNotIn("nobody", self.calls()[1])
        with mock.patch.dict(os.environ, {"GS_HAVE": ""}):
            r = self.run_script(pl.prelude() + "echo SHOULD-NOT-RUN")
        self.assertEqual(r.returncode, 98)
        self.assertIn("not found in namespace gis-prod", r.stderr)

    def test_every_prelude_is_valid_bash(self):
        for t in ("compose", "k8s"):
            for comp in targets.WORKLOADS:
                r = subprocess.run(["bash", "-n"], input=targets.place_of(cfg_for(t), comp).prelude(), capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, (t, comp, r.stderr))

    def test_values_are_shell_quoted(self):
        pl = targets.Place("compose", "postgis", project="a b; touch /tmp/pwned", runtime="docker")
        self.run_script(pl.prelude() + "gs_in '' true")
        self.assertFalse(Path("/tmp/pwned").exists())
        self.assertIn("label=com.docker.compose.project=a b; touch /tmp/pwned", self.calls()[0])      # one argument, not shell syntax


class GatewayScriptTests(Base):
    def capture(self, fn):
        seen = []

        def fake(mode, ssh, script, **kw):
            seen.append((script, kw))
            return "RC 0\nSTATUS 200\nBODY e30=\n"
        with mock.patch.object(transport, "run", fake):
            fn()
        return seen

    def test_geoserver_script_runs_inside_the_container_and_keeps_the_password_on_stdin(self):
        cfg = cfg_for("compose", geoserver={"admin_password": "Sup3r-Secret-Pw"})
        gw = gsg.for_config(cfg, "local", None)
        self.assertEqual(gw.port, 8080)
        (script, kw), = self.capture(lambda: gw.request("GET", "/rest/about/version.json"))
        self.assertIn("gs_in '' bash -c", script)
        self.assertIn("127.0.0.1:8080/geoserver", kw["input_text"])
        self.assertNotIn("Sup3r-Secret-Pw", script)                         # never in the command line
        self.assertIn("Sup3r-Secret-Pw", kw["input_text"])
        self.assertFalse(kw.get("root"), "containers are reached with the operator's own rights")

    def test_native_geoserver_is_unchanged(self):
        cfg = cfg_for("native", geoserver={"admin_password": "Sup3r-Secret-Pw"})
        gw = gsg.for_config(cfg, "local", None)
        (script, _), = self.capture(lambda: gw.request("GET", "/rest/about/version.json"))
        self.assertNotIn("gs_in", script)

    def test_postgis_uses_gs_in_with_the_stacks_superuser_and_never_sudo(self):
        gw = ScriptGateway(cfg_for("compose"), "local", None)
        seen = []
        with mock.patch.object(transport, "run", lambda mode, ssh, script, **kw: seen.append((script, kw)) or "@@x 0\n1\n"):
            gw.sections("gis", {"x": "select 1"})
        script, kw = seen[0]
        self.assertIn(f"PGRUN=\"gs_in postgres env PGUSER={gw.owner}\"", script)
        self.assertFalse(kw["root"])
        self.assertEqual(gw.port, 5432)
        native = ScriptGateway(cfg_for("native"), "local", None)
        self.assertEqual(native.pgrun, "${GS_PGRUN-runuser -u postgres --}")

    def test_postgis_on_k8s_passes_client_settings_through_env_not_the_environment(self):
        gw = ScriptGateway(cfg_for("k8s"), "local", None)
        seen = []
        with mock.patch.object(transport, "run", lambda mode, ssh, script, **kw: seen.append(script) or "@@rc 0\n@@err\n@@out\nx\n"):
            gw.run("select 1", role="rw", db="gis", write=True)
        self.assertIn("gs_in nobody env PGCONNECT_TIMEOUT=5 'PGOPTIONS=-c statement_timeout=60000' psql", seen[0])

    def test_mapproxy_kubernetes_refuses_edits_but_validation_runs_in_the_pod(self):
        cfg = cfg_for("k8s")
        gw = PlaceGateway(targets.place_of(cfg, "mapproxy"), "local", None)
        outs = ['RESULT {"ok": true, "errors": [], "warnings": []}']
        with mock.patch.object(transport, "run", lambda mode, ssh, script, **kw: outs[0]):
            self.assertTrue(gw.apply("a: 1", None, dry_run=True).ok)
            with self.assertRaises(DeployError) as cm:
                gw.apply("a: 1", None, dry_run=False)
        self.assertIn("ConfigMap", str(cm.exception))
        self.assertEqual(gw.backups(), [])
        with self.assertRaises(DeployError):
            gw.read_backup("20261108-120000-abcd")


if __name__ == "__main__":
    unittest.main()
