"""Custom HTTP_PORT / HTTPS_PORT (audit 2026-11, G-2): real Caddy 2 started from the generated compose Caddyfile with the environment the generated
compose file passes.  The http->https redirect must name the https port (it used to send the client to the standard port).
Opt-in: GISSTACK_TEST_INTEGRATION=1 (Docker)."""
from __future__ import annotations

import http.client
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from gisstack import config, generate

ENABLED = os.environ.get("GISSTACK_TEST_INTEGRATION") == "1" and shutil.which("docker") is not None


def free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(ENABLED, "set GISSTACK_TEST_INTEGRATION=1 (needs Docker)")
class RedirectNamesTheCustomPort(unittest.TestCase):
    def run_caddy(self, http_port, https_port):
        cfg = config.resolve({**config.default_config(), "domain": "gis.test", "tls": "internal"})
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        generate.generate_bundle(cfg, tmp / "b")
        name = "gs-ports-" + uuid.uuid4().hex[:8]
        self.addCleanup(subprocess.run, ["docker", "rm", "-f", name], capture_output=True)
        r = subprocess.run(["docker", "run", "-d", "--name", name, "-e", f"HTTPS_PORT={https_port}",
                            "-p", f"127.0.0.1:{http_port}:80", "-p", f"127.0.0.1:{https_port}:443",
                            "-v", f"{tmp / 'b' / 'compose' / 'caddy' / 'Caddyfile'}:/etc/caddy/Caddyfile:ro", "caddy:2"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        for _ in range(40):
            try:
                c = http.client.HTTPConnection("127.0.0.1", http_port, timeout=3)
                c.request("GET", "/", headers={"Host": "gis.test"})
                return c.getresponse()
            except OSError:
                time.sleep(0.5)
        self.fail(subprocess.run(["docker", "logs", name], capture_output=True, text=True).stderr[-800:])

    def test_custom_ports(self):
        hp, sp = free_port(), free_port()
        resp = self.run_caddy(hp, sp)
        self.assertIn(resp.status, (301, 308))
        self.assertEqual(resp.getheader("Location"), f"https://gis.test:{sp}/")

    def test_path_and_query_are_kept(self):
        hp, sp = free_port(), free_port()
        self.run_caddy(hp, sp)
        c = http.client.HTTPConnection("127.0.0.1", hp, timeout=5)
        c.request("GET", "/geoserver/web/?a=1", headers={"Host": "gis.test"})
        self.assertEqual(c.getresponse().getheader("Location"), f"https://gis.test:{sp}/geoserver/web/?a=1")


if __name__ == "__main__":
    unittest.main()
