"""S-2 on a REAL Docker Compose stack: what is actually published, and who can reach it.  Opt-in: GISSTACK_TEST_INTEGRATION=1.

MapProxy stands for the directly published services.  Reachability from "another machine" is measured from another container on the default bridge
network going to the host's bridge address (a non-loopback address of this host): a port bound to 127.0.0.1 refuses it, a port bound to 0.0.0.0 answers.
Cases: default (loopback), explicit network.bind 0.0.0.0, and Caddy as the only public entry point (needs free host ports 80/443)."""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from gisstack import config, generate

from . import images

ENABLED = os.environ.get("GISSTACK_TEST_INTEGRATION") == "1" and shutil.which("docker") is not None
PORT = 8081


def sh(*args, timeout=300):
    return subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)


def ports_free(*ports):
    for p in ports:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) == 0:
                return False
    return True


def bridge_gateway():
    r = sh("docker", "network", "inspect", "bridge", "--format", "{{(index .IPAM.Config 0).Gateway}}")
    return r.stdout.strip()


@unittest.skipUnless(ENABLED and ports_free(PORT), "GISSTACK_TEST_INTEGRATION=1, docker and a free port 8081 needed")
class ComposeExposure(unittest.TestCase):
    ALPINE = "alpine:3"

    @classmethod
    def setUpClass(cls):
        images.require("docker", cls.ALPINE)
        cls.gw = bridge_gateway()
        if not cls.gw:
            raise unittest.SkipTest("no docker bridge gateway address")
        cls.home = tempfile.mkdtemp()
        cls.env = mock.patch.dict(os.environ, {"GISSTACK_HOME": cls.home})
        cls.env.start()
        cls.project = "gsnet" + uuid.uuid4().hex[:6]
        cls.dirs = []

    @classmethod
    def tearDownClass(cls):
        for d in cls.dirs:
            sh("docker", "compose", "-p", cls.project, "-f", str(d / "compose" / "docker-compose.yml"), "--env-file", str(d / "compose" / ".env"), "down", "-v", "--rmi", "local", "--remove-orphans")
            shutil.rmtree(d, ignore_errors=True)
        cls.env.stop()
        shutil.rmtree(cls.home, ignore_errors=True)

    def up(self, raw, components):
        comps = {"postgis": False, "geoserver": False, "tomcat": False, "qgis_server": False, "mapproxy": True, "caddy": False, "webmap": False, **components}
        cfg = config.resolve({"project": self.project, "targets": ["compose"], "deploy_target": "compose", "tls": "internal", "domain": "localhost", "components": comps, **raw},
                             generate_secrets=True, persist_secrets=True)
        out = Path(tempfile.mkdtemp())
        self.dirs.append(out)
        generate.generate_bundle(cfg, out)
        d = out / "compose"
        base = ["docker", "compose", "-p", self.project, "-f", str(d / "docker-compose.yml"), "--env-file", str(d / ".env")]
        r = sh(*base, "up", "-d", "--build", "--wait", "--wait-timeout", "240", timeout=900)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr + sh(*base, "logs", "--tail", "30", "--no-color").stdout)
        self.addCleanup(sh, *base, "down", "-v", "--remove-orphans")
        return base

    def bindings(self, base):
        """What the engine publishes, as `address:hostport` words (parsed from the JSON of `compose ps`: the human format differs between Compose versions)."""
        import json
        words = []
        for line in sh(*base, "ps", "--format", "json").stdout.splitlines():
            line = line.strip()
            if not line.startswith(("{", "[")):
                continue
            data = json.loads(line)
            for row in data if isinstance(data, list) else [data]:
                for pub in row.get("Publishers") or []:
                    if pub.get("PublishedPort"):
                        words.append(f"{pub.get('URL', '')}:{pub['PublishedPort']}")
        return " ".join(sorted(set(words)))

    def from_another_container(self, port, path="/demo/"):
        r = sh("docker", "run", "--rm", self.ALPINE, "wget", "-q", "-T", "6", "-O", "/dev/null", f"http://{self.gw}:{port}{path}")
        return r.returncode == 0

    def from_host_loopback(self, port, path="/demo/"):
        for _ in range(20):
            r = sh("curl", "-s", "-o", "/dev/null", "-m", "5", "-w", "%{http_code}", f"http://127.0.0.1:{port}{path}")
            if r.stdout.strip() in ("200", "301", "302"):
                return True
            time.sleep(1)
        return False

    def test_1_default_publishes_on_loopback_only(self):
        base = self.up({}, {})
        pub = self.bindings(base)
        self.assertIn(f"127.0.0.1:{PORT}", pub)
        self.assertNotIn(f"0.0.0.0:{PORT}", pub)
        self.assertTrue(self.from_host_loopback(PORT), "the published port must work from this machine")
        self.assertFalse(self.from_another_container(PORT), "another machine / container must NOT reach the port")
        sh(*base, "down", "-v", "--remove-orphans")

    def test_2_remote_access_is_an_explicit_choice(self):
        base = self.up({"network": {"bind": "0.0.0.0"}}, {})
        self.assertIn(f"0.0.0.0:{PORT}", self.bindings(base))
        self.assertTrue(self.from_another_container(PORT), "with network.bind = 0.0.0.0 the port is reachable from another network position")
        sh(*base, "down", "-v", "--remove-orphans")

    def test_3_with_caddy_only_caddy_is_published_and_it_proxies(self):
        if not ports_free(80, 443):
            self.skipTest("host ports 80/443 are busy")
        base = self.up({}, {"caddy": True})
        pub = self.bindings(base)
        self.assertNotIn(f"127.0.0.1:{PORT}", pub)
        self.assertNotIn(f"0.0.0.0:{PORT}", pub, "MapProxy must not be published when Caddy is the entry point")
        self.assertIn("0.0.0.0:443", pub)                                                       # Caddy: the intended public entry point
        self.assertFalse(self.from_another_container(PORT))
        code = ""
        for _ in range(30):
            code = sh("curl", "-sk", "-m", "8", "-o", "/dev/null", "-w", "%{http_code}", "--resolve", "localhost:443:127.0.0.1", "https://localhost/mapproxy/demo/").stdout.strip()
            if code == "200":
                break
            time.sleep(2)
        self.assertEqual(code, "200", "MapProxy must be reachable through Caddy")
        sh(*base, "down", "-v", "--remove-orphans")


if __name__ == "__main__":
    unittest.main()
