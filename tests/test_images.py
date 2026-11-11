"""tests/integration/images.py: an unavailable registry is told apart from a failed pull and from a test failure, retries are bounded, and in CI
(GISSTACK_REQUIRE_IMAGES=1) a missing image fails instead of skipping.  Real HTTP server for the registry, a fake `docker` for the engine."""
import os
import shutil
import stat
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from tests.integration import images

FAKE = r'''#!/usr/bin/env bash
# fake engine: `image inspect` succeeds only if $FAKE_STATE/present exists; `pull` fails FAKE_FAIL_PULLS times, then succeeds and marks present
echo "$*" >> "$FAKE_STATE/calls"
case "$1 $2" in
  "image inspect") [ -f "$FAKE_STATE/present" ] && { echo "docker.osgeo.org/geoserver@sha256:abc123 "; exit 0; } || exit 1 ;;
esac
if [ "$1" = pull ]; then
  n=$(grep -c '^pull' "$FAKE_STATE/calls")
  if [ "$n" -le "${FAKE_FAIL_PULLS:-0}" ]; then echo "Error: pinging container registry: i/o timeout" >&2; exit 1; fi
  touch "$FAKE_STATE/present"; exit 0
fi
exit 0
'''


class Registry:
    def __init__(self, status):
        outer = self
        self.status = status

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(outer.status)
                self.end_headers()

            def log_message(self, *a):
                pass
        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_port
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.td, True)
        (self.td / "bin").mkdir()
        (self.td / "bin" / "docker").write_text(FAKE)
        (self.td / "bin" / "docker").chmod(0o755 | stat.S_IEXEC)
        (self.td / "state").mkdir()
        env = mock.patch.dict(os.environ, {"PATH": f"{self.td / 'bin'}:{os.environ['PATH']}", "FAKE_STATE": str(self.td / "state"), "FAKE_FAIL_PULLS": "0"})
        env.start()
        self.addCleanup(env.stop)
        self.sleeps = []
        self.sleep = self.sleeps.append

    def calls(self):
        f = self.td / "state" / "calls"
        return f.read_text().splitlines() if f.exists() else []

    def image(self, port):
        return f"127.0.0.1:{port}/geoserver:2.28.6"

    def test_registry_name_parsing(self):
        self.assertEqual(images.registry_of("docker.osgeo.org/geoserver:2.28.6"), "docker.osgeo.org")
        self.assertEqual(images.registry_of("localhost:5000/x:1"), "localhost:5000")
        self.assertEqual(images.registry_of("postgis/postgis:17-3.5"), "registry-1.docker.io")
        self.assertEqual(images.registry_of("caddy:2"), "registry-1.docker.io")

    def test_the_guarded_geoserver_image_is_the_one_a_default_stack_deploys(self):
        from gisstack import config
        cfg = config.resolve({"project": "img", "targets": ["compose"], "deploy_target": "compose", "components": {"geoserver": True}}, generate_secrets=False, persist_secrets=False)
        self.assertEqual(images.geoserver_image(), cfg["geoserver"]["image"])

    def test_a_cached_image_needs_no_network(self):
        (self.td / "state" / "present").touch()
        code, msg = images.ensure("docker", "docker.osgeo.org/geoserver:2.28.6", sleep=self.sleep)
        self.assertEqual(code, images.OK, msg)
        self.assertEqual([c for c in self.calls() if c.startswith("pull")], [])               # never pulled: a registry outage cannot hurt a cached image
        self.assertIn("sha256:abc123", msg)

    def test_a_transient_failure_is_retried_then_succeeds(self):
        os.environ["FAKE_FAIL_PULLS"] = "2"
        code, msg = images.ensure("docker", "docker.osgeo.org/geoserver:2.28.6", sleep=self.sleep)
        self.assertEqual(code, images.OK, msg)
        self.assertEqual(len([c for c in self.calls() if c.startswith("pull")]), 3)
        self.assertEqual(len(self.sleeps), 2)                                                 # back-off between attempts

    def test_a_persistent_failure_is_bounded_and_reports_the_last_error(self):
        os.environ["FAKE_FAIL_PULLS"] = "99"
        reg = Registry(502)
        self.addCleanup(reg.close)
        code, msg = images.ensure("docker", self.image(reg.port), scheme="http", sleep=self.sleep)
        self.assertEqual(code, images.REGISTRY_UNAVAILABLE, msg)                              # HTTP 502 from /v2/: the registry is down
        self.assertEqual(len([c for c in self.calls() if c.startswith("pull")]), images.ATTEMPTS)
        self.assertIn("HTTP 502", msg)
        self.assertIn("i/o timeout", msg)                                                     # the engine's own error is kept

    def test_a_registry_that_answers_but_refuses_the_pull_is_a_pull_failure_not_an_outage(self):
        os.environ["FAKE_FAIL_PULLS"] = "99"
        reg = Registry(401)                                                                   # 401 on /v2/ = alive, asks for a token
        self.addCleanup(reg.close)
        code, msg = images.ensure("docker", self.image(reg.port), scheme="http", sleep=self.sleep)
        self.assertEqual(code, images.PULL_FAILED, msg)

    def test_a_registry_that_does_not_listen_is_an_outage(self):
        os.environ["FAKE_FAIL_PULLS"] = "99"
        reg = Registry(200)
        port = reg.port
        reg.close()                                                                           # nothing listens any more
        code, msg = images.ensure("docker", self.image(port), scheme="http", sleep=self.sleep)
        self.assertEqual(code, images.REGISTRY_UNAVAILABLE, msg)

    def test_digest_mismatch_is_its_own_failure(self):
        (self.td / "state" / "present").touch()
        code, msg = images.ensure("docker", "docker.osgeo.org/geoserver:2.28.6", expect_digest="sha256:def456", sleep=self.sleep)
        self.assertEqual(code, images.DIGEST_MISMATCH, msg)
        self.assertEqual(images.ensure("docker", "docker.osgeo.org/geoserver:2.28.6", expect_digest="sha256:abc123", sleep=self.sleep)[0], images.OK)

    def test_require_skips_on_a_developer_machine_and_fails_in_ci(self):
        os.environ["FAKE_FAIL_PULLS"] = "99"
        with mock.patch.object(images, "ATTEMPTS", 1), mock.patch.object(images, "ensure", return_value=(images.REGISTRY_UNAVAILABLE, "docker.osgeo.org does not answer (HTTP 502)")):
            with mock.patch.dict(os.environ, {"GISSTACK_REQUIRE_IMAGES": ""}), self.assertRaises(unittest.SkipTest) as sk:
                images.require("docker", "docker.osgeo.org/geoserver:2.28.6")
            self.assertIn("REGISTRY UNAVAILABLE", str(sk.exception))
            with mock.patch.dict(os.environ, {"GISSTACK_REQUIRE_IMAGES": "1"}), self.assertRaises(AssertionError) as ae:
                images.require("docker", "docker.osgeo.org/geoserver:2.28.6")
            self.assertTrue(str(ae.exception).startswith("REGISTRY UNAVAILABLE"))
            self.assertIn("not a test assertion", str(ae.exception))

    def test_cli_exit_codes_distinguish_outage_from_pull_failure(self):
        os.environ["FAKE_FAIL_PULLS"] = "99"
        up, down = Registry(401), Registry(503)
        self.addCleanup(up.close)
        self.addCleanup(down.close)
        def status(registry, timeout=15.0, scheme="https"):
            alive = registry.endswith(str(up.port))
            return alive, "HTTP 401" if alive else "HTTP 503"
        with mock.patch.object(images, "BACKOFF", 0), mock.patch.object(images, "registry_status", side_effect=status), mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(images.main([self.image(up.port)]), images.PULL_FAILED)
            self.assertEqual(images.main([self.image(down.port)]), images.REGISTRY_UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
