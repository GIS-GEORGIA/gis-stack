"""Phase 1b / S-03: secrets must never appear in the argument list of any process started by install.sh.

The helper functions are extracted from the rendered script and run in real bash; GeoServer's REST call is
replayed against a local HTTP server with the real curl, so the escaping is checked end to end."""
import base64
import json
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from gisstack import catalog, config, generate, pins

NASTY = ["plain", 'q"uote', "back\\slash", "$(touch X)", "`id`", "a'b", "sp ace", "%s %d", "ünï-კოდი", "a:b:c", "{}\"}", "x" * 200]


def _render(everything: bool = False) -> str:
    cfg = config.resolve({"targets": ["native"], "components": {"postgis": True, "tomcat": True, "geoserver": True, "mapproxy": everything,
                                                                  "caddy": everything, "qgis_server": False}})
    with tempfile.TemporaryDirectory() as td:
        return (generate.generate_bundle(cfg, Path(td) / "b") / "native/install.sh").read_text()


def _helpers(script: str) -> str:
    m = re.search(r"^hexof\(\).*?^gs_newpass_body\(\).*?$", script, re.S | re.M)
    assert m, "helper functions missing from install.sh"
    return m.group(0)


class RenderedScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = _render()

    def test_no_secret_variable_on_a_command_line(self):
        for bad in ('-v dbpass=', '-u "admin:$', "-u 'admin:", '-d "{\\"newPassword', 'PGPASSWORD='):
            self.assertNotIn(bad, self.script, bad)

    def test_every_variable_holding_a_password_is_only_used_inside_helpers_or_files(self):
        for line in self.script.splitlines():
            code = line.split("#", 1)[0]
            if re.search(r"\$\{?(DB_PASSWORD|GS_ADMIN_PASSWORD)\b", code):
                self.assertRegex(code, r"hexof|gs_newpass_body|gs_curl|printf|echo|credentials|password=\$DB_PASSWORD|admin / \$GS_ADMIN_PASSWORD", line)


@unittest.skipUnless(shutil.which("bash") and shutil.which("curl") and shutil.which("od"), "bash/curl/od needed")
class HelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helpers = _helpers(_render())

    def test_hexof_roundtrips_any_text(self):
        for pw in NASTY:
            r = subprocess.run(["bash", "-c", self.helpers + '\nhexof "$1"', "bash", pw], capture_output=True, text=True)
            self.assertEqual(bytes.fromhex(r.stdout).decode("utf-8"), pw)

    def test_gs_curl_sends_credentials_and_body_intact_without_putting_them_in_argv(self):
        seen = {}

        class H(BaseHTTPRequestHandler):
            def do_PUT(self):
                seen["auth"] = self.headers.get("Authorization")
                seen["body"] = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
                seen["ctype"] = self.headers.get("Content-Type")
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            for old, new in [("geoserver", pw) for pw in NASTY] + [(pw, "n3w") for pw in NASTY[:6]]:
                seen.clear()
                url = f"http://127.0.0.1:{srv.server_port}/rest/security/self/password"
                r = subprocess.run(["bash", "-c", self.helpers + '\ngs_curl admin "$1" "$(gs_newpass_body "$2")" "$3"', "bash", old, new, url],
                                   capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, (old, new, r.stderr))
                self.assertEqual(base64.b64decode(seen["auth"].split()[1]).decode(), f"admin:{old}")
                self.assertEqual(json.loads(seen["body"]), {"newPassword": new})
                self.assertEqual(seen["ctype"], "application/json")
        finally:
            srv.shutdown()
            srv.server_close()

    def test_secret_not_visible_in_process_arguments_while_curl_runs(self):
        """A stub 'curl' records its own argv; the password may only be on stdin."""
        with tempfile.TemporaryDirectory() as td:
            stub = Path(td) / "curl"
            stub.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$STUB_ARGS"\ncat > "$STUB_STDIN"\n')
            stub.chmod(0o755)
            env = {"PATH": f"{td}:/usr/bin:/bin", "STUB_ARGS": f"{td}/args", "STUB_STDIN": f"{td}/stdin"}
            secret = "S3cr3t-Pa55"
            r = subprocess.run(["bash", "-c", self.helpers + f'\ngs_curl admin {secret} "$(gs_newpass_body {secret}2)" http://x/y'],
                               capture_output=True, text=True, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn(secret, Path(td, "args").read_text())
            self.assertIn(secret, Path(td, "stdin").read_text())


class PinnedDownloadTests(unittest.TestCase):
    """Phase 1b / S-04: everything install.sh downloads and runs as root is checked against a pinned SHA-256."""

    def test_every_geoserver_channel_has_pins_for_war_and_all_extensions(self):
        for ch in catalog.GEOSERVER_CHANNELS.values():
            v = ch["version"]
            self.assertIn(f"geoserver-{v}-war.zip", pins.PINS)
            for ext in catalog.GEOSERVER_EXTENSIONS:
                self.assertIn(f"geoserver-{v}-{ext}-plugin.zip", pins.PINS, ext)

    def test_caddy_pinned_for_both_architectures_and_all_pins_look_like_sha256(self):
        for arch in ("amd64", "arm64"):
            self.assertIn(f"caddy_{pins.CADDY_VERSION}_linux_{arch}.tar.gz", pins.PINS)
        for name, h in pins.PINS.items():
            self.assertRegex(h, r"^[0-9a-f]{64}$", name)

    def test_bundle_ships_pins_and_hashed_requirements(self):
        cfg = config.resolve({"targets": ["native"]})
        with tempfile.TemporaryDirectory() as td:
            out = generate.generate_bundle(cfg, Path(td) / "b")
            self.assertEqual((out / "native/config/pins.sha256").read_text(), pins.pins_file())
            reqs = [(out / "native/config" / n).read_text() for n in ("mapproxy-requirements.txt", "mapproxy-requirements-py310.txt")]
        for req in reqs:
            blocks = re.split(r"\n(?=[A-Za-z0-9_.-]+==)", req)[1:]
            self.assertGreater(len(blocks), 10)
            for b in blocks:
                self.assertRegex(b, r"--hash=sha256:[0-9a-f]{64}", b.splitlines()[0])
            self.assertIn("mapproxy==", req)
        self.assertRegex(reqs[0], r"(?m)^pillow==13\.")           # the Python 3.11+ list carries the patched Pillow

    def test_installer_has_no_unverified_download_left(self):
        script = _render(everything=True)
        self.assertNotIn("caddyserver.com/api/download", script)
        self.assertNotRegex(script, r"pip install[^\n]*\b(mapproxy|gunicorn)\b(?![^\n]*-r)")
        self.assertIn("--require-hashes", script)
        self.assertEqual(len(re.findall(r"^\s*verify_pin ", script, re.M)), 3)    # geoserver war, geoserver extension, caddy

    def _verify(self, pin_lines: str, name: str, content: bytes, extra_env=None):
        script = _render()
        m = re.search(r"^verify_pin\(\).*?^\}", script, re.S | re.M)
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "config").mkdir()
            (Path(td) / "config/pins.sha256").write_text(pin_lines)
            f = Path(td) / "blob"
            f.write_bytes(content)
            code = f'HERE="{td}"\ndie() {{ echo "DIE: $*"; exit 1; }}\nwarn() {{ echo "WARN: $*"; }}\n{m.group(0)}\nverify_pin "{f}" "{name}"'
            return subprocess.run(["bash", "-c", code], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", **(extra_env or {})})

    def test_verify_pin_accepts_match_rejects_mismatch_and_unknown(self):
        import hashlib
        good = hashlib.sha256(b"payload").hexdigest()
        self.assertEqual(self._verify(f"{good}  a.zip\n", "a.zip", b"payload").returncode, 0)
        bad = self._verify(f"{good}  a.zip\n", "a.zip", b"tampered")
        self.assertEqual(bad.returncode, 1)
        self.assertIn("mismatch", bad.stdout)
        unknown = self._verify(f"{good}  a.zip\n", "other.zip", b"payload")
        self.assertEqual(unknown.returncode, 1)
        self.assertIn("no pinned SHA-256", unknown.stdout)
        override = self._verify(f"{good}  a.zip\n", "other.zip", b"payload", {"GS_ALLOW_UNVERIFIED": "1"})
        self.assertEqual(override.returncode, 0)
        self.assertIn("UNVERIFIED", override.stdout)


if __name__ == "__main__":
    unittest.main()
