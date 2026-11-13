"""S-3: the public health probe of Compose stacks uses the host port Caddy REALLY publishes (read from the engine), not a hard-coded 80/443 - so a custom
HTTP_PORT / HTTPS_PORT in `.env` neither makes a healthy stack look unhealthy nor makes a restore undo itself.  Real engines: tests/integration/
test_stack_backup_gis.py (Docker Compose, random host ports) and test_podman_reliability.py (Podman)."""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gisstack import config
from gisstack.providers import common, compose


def cfg_for(runtime="docker", tls="internal"):
    return config.resolve({"project": "prb", "runtime": runtime, "targets": ["compose"], "deploy_target": "compose", "tls": tls, "domain": "gis.example.org",
                           "components": {"caddy": True, "mapproxy": True, "postgis": False}}, generate_secrets=False, persist_secrets=False)


def script_for(**kw):
    cfg = cfg_for(**kw)
    return compose.status_script(cfg, common.checks(cfg, "compose", ["caddy", "mapproxy"]))


class ProbeScriptTests(unittest.TestCase):
    def test_the_probe_url_and_resolve_use_the_discovered_port_not_443(self):
        s = script_for()
        self.assertIn('--resolve "gis.example.org:${GS_PUBLIC_PORT}:127.0.0.1" "https://gis.example.org:${GS_PUBLIC_PORT}/"', s)
        self.assertNotIn(":443:127.0.0.1", s)
        self.assertIn("docker port", s)
        self.assertIn("443/tcp", s)

    def test_without_tls_it_asks_for_port_80(self):
        s = script_for(tls="none")
        self.assertIn("80/tcp", s)
        self.assertIn("GS_PUBLIC_PORT=80", s)
        self.assertIn('"http://gis.example.org:${GS_PUBLIC_PORT}/"', s)

    def test_podman_asks_the_podman_engine(self):
        s = script_for(runtime="podman")
        self.assertIn("podman port", s)
        self.assertIn("podman ps -q --filter label=com.docker.compose.project=prb", s)

    def test_native_and_kubernetes_probes_are_unchanged(self):
        cfg = config.resolve({"project": "prb", "targets": ["native"], "deploy_target": "native", "tls": "internal", "domain": "gis.example.org",
                              "components": {"caddy": True, "mapproxy": True, "postgis": False}}, generate_secrets=False, persist_secrets=False)
        for c in common.checks(cfg, "native", ["caddy"]):
            self.assertNotIn("@PORT@", c.get("url", "") + c.get("resolve", ""))
        self.assertIn("gis.example.org:443:127.0.0.1", " ".join(c.get("resolve", "") for c in common.checks(cfg, "native", ["caddy"])))

    def run_prelude(self, port_output, runtime="docker"):
        """Execute the prelude against a fake engine that answers `ps -q` with an id and `port` with `port_output`."""
        td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, td, True)
        eng = td / runtime
        eng.write_text('#!/bin/sh\ncase "$1" in\n  ps) echo abc123 ;;\n  port) printf "%s" "$FAKE_PORT" ;;\nesac\n')
        eng.chmod(0o755 | stat.S_IEXEC)
        script = common.public_port_prelude(runtime, "--filter label=com.docker.compose.project=prb", True) + 'echo "PORT=$GS_PUBLIC_PORT"\n'
        with mock.patch.dict(os.environ, {"PATH": f"{td}:{os.environ['PATH']}", "FAKE_PORT": port_output}):
            return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30).stdout.strip()

    def test_the_prelude_reads_ipv4_ipv6_and_falls_back_safely(self):
        self.assertEqual(self.run_prelude("0.0.0.0:18443\n"), "PORT=18443")
        self.assertEqual(self.run_prelude("0.0.0.0:18443\n[::]:18443\n"), "PORT=18443")
        self.assertEqual(self.run_prelude("[::]:9443\n"), "PORT=9443")
        self.assertEqual(self.run_prelude(""), "PORT=443")                                    # engine says nothing: the standard port
        self.assertEqual(self.run_prelude("garbage: not a port"), "PORT=443")


if __name__ == "__main__":
    unittest.main()
