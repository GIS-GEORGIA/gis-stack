"""The Tomcat manager must never be reachable through Caddy, also not through Tomcat's ";parameter" path normalisation.

Found in the 2026-11 audit with real Caddy 2 + Tomcat 11: `/apps/x/..;/manager/html` is not seen as `/apps/manager*` by Caddy, but Tomcat removes
the `;` part of every segment and serves the manager (HTTP 401 = the manager answered).  The generated Caddyfile now refuses those paths.  The test
renders the real Caddyfile, runs it with real Caddy in front of real Tomcat (manager deployed, its address restriction removed so that only Caddy
stands in the way) and also proves the test is sensitive: the same attack against a Caddyfile WITHOUT the rule reaches the manager.
Opt-in: GISSTACK_TEST_INTEGRATION=1 (Docker)."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from gisstack import config, generate

from . import images

ENABLED = os.environ.get("GISSTACK_TEST_INTEGRATION") == "1" and shutil.which("docker") is not None
TOMCAT, CADDY = "tomcat:11.0-jre17", "caddy:2"
TRICKS = ["/apps/x/..;/manager/html", "/apps/;/manager/html", "/apps/./;/manager/html", "/apps/x/.;/..;/manager/html", "/apps/%2e%2e%3b/manager/html"]
PLAIN = ["/apps/manager/html", "/apps/MANAGER/html", "/apps/x/../manager/html", "/apps//manager/html", "/apps/host-manager/html"]


def sh(*a, timeout=120):
    return subprocess.run(list(a), capture_output=True, text=True, timeout=timeout)


def status(port, path):
    """HTTP status without any client-side path clean-up (urllib sends the path as written)."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def apps_block(caddyfile: str, upstream: str) -> str:
    m = re.search(r"(\t(?:@tomcat_path_trick \{.*?\n\t\}\n\thandle @tomcat_path_trick \{.*?\n\t\}\n)?\thandle /apps/manager\*.*?handle_path /apps/\* \{.*?\n\t\}\n)", caddyfile, re.S)
    assert m, "no Tomcat block in the generated Caddyfile"
    return m.group(1).replace("127.0.0.1:8080", upstream)


class RenderedRule(unittest.TestCase):
    """Fast check without Docker: the generated native Caddyfile carries the rule (and still forbids the manager)."""

    def test_caddyfile_has_the_path_parameter_rule_before_the_proxy(self):
        out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, out, True)
        generate.generate_bundle(config.resolve(config.default_config()), out)
        text = (out / "native/config/Caddyfile").read_text(encoding="utf-8")
        rule, mgr, proxy = text.index("@tomcat_path_trick"), text.index("handle /apps/manager*"), text.index("handle_path /apps/*")
        self.assertLess(rule, proxy)
        self.assertLess(mgr, proxy)
        self.assertIn(r"path_regexp (^|/)\.{0,2};", text)
        self.assertIn("host-manager", text)


@unittest.skipUnless(ENABLED, "GISSTACK_TEST_INTEGRATION=1 and docker needed")
class RealCaddyInFrontOfRealTomcat(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        images.require("docker", TOMCAT, CADDY)
        cls.tag = "gstm" + uuid.uuid4().hex[:6]
        cls.tmp = Path(tempfile.mkdtemp())
        out = cls.tmp / "bundle"
        generate.generate_bundle(config.resolve(config.default_config()), out)
        text = (out / "native/config/Caddyfile").read_text(encoding="utf-8")
        cls.net = f"{cls.tag}-net"
        sh("docker", "network", "create", cls.net)
        sh("docker", "run", "-d", "--name", f"{cls.tag}-tc", "--network", cls.net, "--entrypoint", "bash", TOMCAT, "-c",
           'cp -r webapps.dist/manager webapps/manager && cp -r webapps.dist/ROOT webapps/ROOT && '
           'sed -i "s/<Valve.*RemoteCIDRValve.*//;s/<Valve.*RemoteAddrValve.*//" webapps/manager/META-INF/context.xml && catalina.sh run')
        cls.ports = {}
        for name, block in (("fixed", apps_block(text, f"{cls.tag}-tc:8080")),
                            ("old", re.sub(r"\t@tomcat_path_trick \{.*?\n\t\}\n\thandle @tomcat_path_trick \{.*?\n\t\}\n", "", apps_block(text, f"{cls.tag}-tc:8080"), flags=re.S))):
            cf = cls.tmp / f"Caddyfile.{name}"
            cf.write_text(":8088 {\n" + block + "}\n", encoding="utf-8")
            r = sh("docker", "run", "-d", "--name", f"{cls.tag}-{name}", "--network", cls.net, "-v", f"{cf}:/etc/caddy/Caddyfile:ro", "-p", "127.0.0.1::8088", CADDY)
            assert r.returncode == 0, r.stderr
            cls.ports[name] = int(sh("docker", "port", f"{cls.tag}-{name}", "8088/tcp").stdout.strip().rsplit(":", 1)[1])
        deadline = time.time() + 120
        while time.time() < deadline and status(cls.ports["fixed"], "/apps/") != 200:
            time.sleep(2)

    @classmethod
    def tearDownClass(cls):
        sh("docker", "rm", "-f", f"{cls.tag}-tc", f"{cls.tag}-fixed", f"{cls.tag}-old")
        sh("docker", "network", "rm", cls.net)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_a_normal_application_path_still_reaches_tomcat(self):
        self.assertEqual(status(self.ports["fixed"], "/apps/"), 200)
        self.assertEqual(status(self.ports["fixed"], "/apps/foo;jsessionid=abc"), 404)       # reached Tomcat (its 404), not refused by Caddy

    def test_the_manager_is_forbidden_in_every_spelling(self):
        for path in PLAIN + TRICKS:
            self.assertEqual(status(self.ports["fixed"], path), 403, path)

    def test_without_the_rule_the_attack_reaches_the_manager(self):
        """Sensitivity of this test: the pre-fix Caddyfile lets the `..;` spelling through to the manager (401 = the manager asks for a login)."""
        self.assertEqual(status(self.ports["old"], "/apps/manager/html"), 403)
        self.assertEqual(status(self.ports["old"], "/apps/x/..;/manager/html"), 401)


if __name__ == "__main__":
    unittest.main()
