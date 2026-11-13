"""S-7 with a REAL reverse proxy: `gisstack web` (waitress) behind Caddy (TLS, host network so the app sees 127.0.0.1 as the proxy).  Opt-in:
GISSTACK_TEST_INTEGRATION=1 + docker.  Asserted: through the proxy and with GISSTACK_TRUSTED_PROXIES the session cookie is Secure; without it (the proxy is
not trusted) it is not; a forged X-Forwarded-Proto sent straight to the app is ignored; the cookie is always HttpOnly + SameSite=Strict."""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from . import images

ROOT = Path(__file__).resolve().parents[2]
ENABLED = os.environ.get("GISSTACK_TEST_INTEGRATION") == "1" and shutil.which("docker") is not None and shutil.which("curl") is not None


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(ENABLED, "GISSTACK_TEST_INTEGRATION=1, docker and curl needed")
class WebBehindProxy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        images.require("docker", "caddy:2")
        cls.td = Path(tempfile.mkdtemp())
        cls.name = "gsproxy" + uuid.uuid4().hex[:6]
        cls.procs = []

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["docker", "rm", "-f", cls.name], capture_output=True)
        for p in cls.procs:
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
            if p.stdout:
                p.stdout.close()
        shutil.rmtree(cls.td, ignore_errors=True)

    def start_app(self, trusted):
        port = free_port()
        home = self.td / f"home{port}"
        home.mkdir()
        env = {**os.environ, "GISSTACK_HOME": str(home), "GISSTACK_PRINT_TOKEN": "1", "PYTHONPATH": str(ROOT)}
        env.pop("GISSTACK_TRUSTED_PROXIES", None)
        if trusted:
            env["GISSTACK_TRUSTED_PROXIES"] = trusted
        p = subprocess.Popen([sys.executable, "-m", "gisstack", "web", "--port", str(port), "--allow-host", "localhost"], cwd=ROOT, env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.procs.append(p)
        token = ""
        for _ in range(100):
            m = re.search(r"token=(\S+)", p.stdout.readline())
            if m:
                token = m.group(1)
                break
        self.assertTrue(token, "gisstack web did not print its URL")
        for _ in range(50):
            if subprocess.run(["curl", "-s", "-o", "/dev/null", f"http://127.0.0.1:{port}/healthz"], capture_output=True).returncode == 0:
                break
            time.sleep(0.2)
        return port, token

    def start_proxy(self, app_port):
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)
        pport = free_port()
        cf = self.td / "Caddyfile"
        cf.write_text(f"{{\n  admin off\n  auto_https disable_redirects\n}}\nhttps://localhost:{pport} {{\n  tls internal\n  reverse_proxy 127.0.0.1:{app_port}\n}}\n")
        r = subprocess.run(["docker", "run", "-d", "--name", self.name, "--network", "host", "-v", f"{cf}:/etc/caddy/Caddyfile:ro", "caddy:2"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        for _ in range(40):
            if subprocess.run(["curl", "-sk", "-o", "/dev/null", "-m", "3", f"https://localhost:{pport}/healthz"], capture_output=True).returncode == 0:
                return pport
            time.sleep(0.5)
        self.fail("the proxy did not come up: " + subprocess.run(["docker", "logs", "--tail", "20", self.name], capture_output=True, text=True).stderr)

    def login_cookie(self, url, token, *extra):
        r = subprocess.run(["curl", "-sk", "-m", "10", "-D", "-", "-o", "/dev/null", *extra, f"{url}/?token={token}"], capture_output=True, text=True)
        m = re.search(r"(?im)^set-cookie:\s*(gs_session=[^\r\n]*)", r.stdout)
        self.assertTrue(m, r.stdout)
        return m.group(1)

    def test_secure_only_behind_a_trusted_proxy(self):
        port, token = self.start_app(trusted="127.0.0.1")
        pport = self.start_proxy(port)
        via = self.login_cookie(f"https://localhost:{pport}", token)
        for flag in ("Secure", "HttpOnly", "SameSite=Strict"):
            self.assertIn(flag, via)
        direct = self.login_cookie(f"http://127.0.0.1:{port}", token)                          # the SSH-tunnel style access: plain http on loopback
        self.assertNotIn("Secure", direct)
        self.assertIn("HttpOnly", direct)
        forged = self.login_cookie(f"http://127.0.0.1:{port}", token, "-H", "X-Forwarded-Proto: https")
        self.assertTrue("Secure" in forged)                                                    # 127.0.0.1 IS the trusted proxy address here: a local process is the proxy
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)

    def test_an_untrusted_proxy_cannot_make_the_cookie_secure(self):
        port, token = self.start_app(trusted="")
        pport = self.start_proxy(port)
        via = self.login_cookie(f"https://localhost:{pport}", token)
        self.assertNotIn("Secure", via)                                                        # the proxy is not trusted: its header is ignored
        self.assertIn("HttpOnly", via)
        self.assertNotIn("Secure", self.login_cookie(f"http://127.0.0.1:{port}", token, "-H", "X-Forwarded-Proto: https"))
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)


if __name__ == "__main__":
    unittest.main()
