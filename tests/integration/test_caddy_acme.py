"""Caddy ACME state backup/restore against a REAL ACME server (Pebble, Let's Encrypt's test CA) on real Docker Compose.  Opt-in: GISSTACK_TEST_INTEGRATION=1.

A throw-away Compose project runs gis-stack's Caddy next to Pebble; Caddy obtains a real certificate for gis.example.com over HTTP-01 (Docker DNS resolves the
name to Caddy).  Asserted: the certificate and ACME account are in the backup; no log line, manifest or console output contains private-key material;
backup files are owner-only; after the state is destroyed Caddy re-issues (control: Pebble's issuance count rises); after the restore the SAME certificate
is served again with identical owners/modes and Caddy does NOT ask Pebble for another one.  Nothing leaves the machine; no public CA is contacted."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from gisstack import config, generate
from gisstack.backups import provider

ENABLED = os.environ.get("GISSTACK_TEST_INTEGRATION") == "1" and shutil.which("docker") is not None
PEBBLE = "ghcr.io/letsencrypt/pebble:latest"
HOST = "gis.example.com"


def pulled(image: str) -> bool:
    """The image is available: already present locally, or pulled now (a registry that is unreachable must not silently skip a test whose image is cached)."""
    if not ENABLED:
        return False
    if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0:
        return True
    return subprocess.run(["docker", "pull", "-q", image], capture_output=True).returncode == 0


def sh(*args, timeout=120):
    return subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)


def ports_free(*ports):
    import socket
    for port in ports:
        with socket.socket() as sk:
            if sk.connect_ex(("127.0.0.1", port)) == 0:
                return False
    return True


@unittest.skipUnless(pulled("caddy:2") and pulled(PEBBLE) and ports_free(80, 443), "GISSTACK_TEST_INTEGRATION=1, docker, the Caddy and Pebble images and free ports 80/443 needed")
class CaddyAcmeBackupRestore(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp()
        cls.env = mock.patch.dict(os.environ, {"GISSTACK_HOME": cls.home, "GISSTACK_VERIFY_TIMEOUT": "120", "GISSTACK_VERIFY_SETTLE": "3"})
        cls.env.start()
        cls.project = "gsacme" + uuid.uuid4().hex[:6]
        comps = {"postgis": False, "geoserver": False, "tomcat": False, "qgis_server": False, "mapproxy": False, "caddy": True, "webmap": False}
        cls.cfg = config.resolve({"project": cls.project, "targets": ["compose"], "deploy_target": "compose", "tls": "auto", "domain": HOST, "email": "ops@example.com",
                                  "components": comps}, generate_secrets=True, persist_secrets=True)
        cls.out = Path(tempfile.mkdtemp())
        generate.generate_bundle(cls.cfg, cls.out)
        d = cls.dir = cls.out / "compose"
        # the product's Caddyfile, with only the ACME directory swapped for Pebble (global options); everything else is gis-stack's own
        (d / "caddy" / "Caddyfile").write_text("{\n  acme_ca https://pebble:14000/dir\n  acme_ca_root /etc/caddy/pebble.minica.pem\n  email ops@example.com\n}\n"
                                               f"{HOST} {{\n  respond \"ok\"\n}}\n")
        c = sh("docker", "create", PEBBLE).stdout.strip()
        sh("docker", "cp", f"{c}:/test/certs/pebble.minica.pem", str(d / "pebble.minica.pem"))
        sh("docker", "rm", c)
        (d / "pebble-config.json").write_text('{"pebble": {"listenAddress": "0.0.0.0:14000", "managementListenAddress": "0.0.0.0:15000", "certificate": "test/certs/localhost/cert.pem", '
                                              '"privateKey": "test/certs/localhost/key.pem", "httpPort": 80, "tlsPort": 443, "ocspResponderURL": "", "externalAccountBindingRequired": false, '
                                              '"domainBlocklist": [], "retryAfter": {"authz": 1, "order": 1}, "keyAlgorithm": "ecdsa", "profiles": {"default": {"description": "d", "validityPeriod": 7776000}}}}')
        (d / "override.yml").write_text(f"""services:
  pebble:
    image: {PEBBLE}
    command: ["-config", "/cfg/pebble-config.json"]
    environment: {{PEBBLE_VA_NOSLEEP: "1"}}
    volumes: ["./pebble-config.json:/cfg/pebble-config.json:ro"]
  caddy:
    depends_on: [pebble]
    networks: {{default: {{aliases: ["{HOST}"]}}}}
    volumes: ["./pebble.minica.pem:/etc/caddy/pebble.minica.pem:ro"]
""")
        envf = d / ".env"                                                                                      # host ports 80/443: gis-stack's health probe of Caddy uses 443
        yml = d / "docker-compose.yml"
        cls.base = ["docker", "compose", "-p", cls.project, "-f", str(yml), "-f", str(d / "override.yml"), "--env-file", str(envf)]
        up = sh(*cls.base, "up", "-d", "--wait", "--wait-timeout", "120", timeout=300)
        if up.returncode != 0:
            logs = sh(*cls.base, "logs", "--tail", "40", "--no-color").stdout
            cls.tearDownClass()
            raise AssertionError(up.stdout + up.stderr + "\n" + logs)

    @classmethod
    def tearDownClass(cls):
        sh(*cls.base, "down", "-v", "--remove-orphans", timeout=240)
        cls.env.stop()
        shutil.rmtree(cls.out, ignore_errors=True)
        shutil.rmtree(cls.home, ignore_errors=True)

    # ------------------------------------------------------------------ helpers
    def cx(self, *args):
        return sh(*self.base, "exec", "-T", "caddy", *args)

    def issued(self):
        return sh(*self.base, "logs", "--no-color", "pebble").stdout.count("Issued certificate")

    def cert_hash(self):
        r = self.cx("sh", "-c", f"cat /data/caddy/certificates/*/{HOST}/{HOST}.crt 2>/dev/null | sha256sum | cut -d' ' -f1")
        return r.stdout.strip()

    def wait_cert(self, secs=60):
        for _ in range(secs):
            h = self.cert_hash()
            if h and h != "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855":      # sha256 of the empty string: no file yet
                return h
            time.sleep(1)
        self.fail("Caddy did not obtain a certificate from Pebble:\n" + sh(*self.base, "logs", "--tail", "30", "--no-color", "caddy").stdout)

    def tree(self):
        out = self.cx("sh", "-c", "cd /data && find . -not -name '.gs-*' -not -path './.gs-*' -printf '%u:%g %m %y %P\\n' | sort").stdout
        return out.splitlines()

    def key_fragment(self):
        pem = self.cx("sh", "-c", f"cat /data/caddy/certificates/*/{HOST}/{HOST}.key").stdout
        body = "".join(l for l in pem.splitlines() if not l.startswith("-----"))
        self.assertGreater(len(body), 60)
        return body[20:60]

    def test_acme_state_survives_loss_without_reissuing_and_leaks_no_key_material(self):
        h1 = self.wait_cert()
        issued1 = self.issued()
        self.assertGreaterEqual(issued1, 1)
        self.assertEqual(self.cx("sh", "-c", "ls /data/caddy/acme/*/users/*/*.json | wc -l").stdout.strip() != "0", True, "ACME account state exists")
        tree_before = self.tree()
        frag = self.key_fragment()

        p = provider.ComposeBackupProvider(self.cfg)
        lines = list(p.create(["caddy"], note="acme"))
        self.assertTrue(any("sealed" in x for x in lines))
        m = p.result
        self.assertTrue(p.verify(m["id"])["ok"])
        d = p.store.path(m["id"])
        for f in d.iterdir():
            self.assertEqual(oct(f.stat().st_mode & 0o777), "0o600", f.name)                         # owner-only
        self.assertEqual(oct(d.stat().st_mode & 0o777), "0o700")
        # nothing printed or written as metadata contains key material
        texts = "\n".join(lines) + "".join(f.read_text(errors="ignore") for f in d.iterdir() if f.suffix in (".json", ".log") or f.name == "SHA256SUMS")
        self.assertNotIn("PRIVATE KEY", texts)
        self.assertNotIn(frag, texts)
        raw = subprocess.run(["tar", "-xzOf", str(d / "caddy.tar.gz")], capture_output=True).stdout
        self.assertIn(HOST.encode(), raw)                                                                  # the certificate is in the archive (so the archive is sensitive)
        self.assertIn("TLS private keys", provider.SENSITIVE["caddy"])

        # control: without the restore, a destroyed state makes Caddy ask Pebble again
        self.cx("sh", "-c", "rm -rf /data/caddy")
        sh(*self.base, "restart", "caddy", timeout=120)
        h2 = self.wait_cert(90)
        self.assertNotEqual(h2, h1)
        self.assertGreater(self.issued(), issued1, "control failed: expected a re-issuance after the state was destroyed")
        issued2 = self.issued()

        r = provider.ComposeBackupProvider(self.cfg)
        lines = list(r.restore(m["id"], ["caddy"], confirm=True))
        self.assertTrue(any("verified healthy" in x for x in lines), lines)
        self.assertNotIn("PRIVATE KEY", "\n".join(lines))
        self.assertNotIn(frag, "\n".join(lines))
        time.sleep(15)                                                                                    # Caddy had time to (wrongly) renew
        self.assertEqual(self.cert_hash(), h1, "the restored certificate is the one that was backed up")
        self.assertEqual(self.issued(), issued2, "no unnecessary re-issuance after the restore")
        self.assertEqual(self.cx("wget", "--no-check-certificate", "-qO-", f"https://{HOST}/").stdout.strip(), "ok")     # and it is served
        self.assertEqual(self.tree(), tree_before, "owners/groups/modes of the restored state differ from the original")
        self.assertIn(".gs-pre-", r.result["retained_previous_data"]["caddy"])


if __name__ == "__main__":
    unittest.main()
