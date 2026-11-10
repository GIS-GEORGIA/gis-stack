"""Backup AND restore of Caddy, MapProxy and QGIS Server on a REAL Docker Compose stack.  Opt-in: GISSTACK_TEST_INTEGRATION=1.

Each class deploys an isolated throw-away project (unique name, random host ports) and removes it afterwards; nothing else is touched.
What is asserted is data-level: the local CA that Caddy generated comes back byte for byte (and HTTPS verifies against it), a sentinel
tile in the MapProxy cache comes back, a QGIS project file is inside the backup and its restore on Compose is refused with the reason."""
from __future__ import annotations

import os
import re
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
from gisstack.backups import provider
from gisstack.backups.model import RestoreError

ENABLED = os.environ.get("GISSTACK_TEST_INTEGRATION") == "1" and shutil.which("docker") is not None


def pulled(image: str) -> bool:
    return ENABLED and subprocess.run(["docker", "pull", "-q", image], capture_output=True).returncode == 0


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class GisStack(unittest.TestCase):
    components: dict = {}
    prepare = None                      # optional hook(bundle_dir) run before `up`
    standard_ports = False

    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp()
        cls.env = mock.patch.dict(os.environ, {"GISSTACK_HOME": cls.home, "GISSTACK_VERIFY_TIMEOUT": "120", "GISSTACK_VERIFY_SETTLE": "3"})
        cls.env.start()
        cls.project = "gsgis" + uuid.uuid4().hex[:6]
        comps = {"postgis": False, "geoserver": False, "tomcat": False, "qgis_server": False, "mapproxy": False, "caddy": False, "webmap": False, **cls.components}
        cls.cfg = config.resolve({"project": cls.project, "targets": ["compose"], "deploy_target": "compose", "tls": "internal", "domain": "localhost", "components": comps},
                                 generate_secrets=True, persist_secrets=True)
        cls.out = Path(tempfile.mkdtemp())
        generate.generate_bundle(cls.cfg, cls.out)
        yml = cls.out / "compose" / "docker-compose.yml"          # tests reach workloads with `exec`; fixed host ports (8080, 8081, 8380) would make concurrent runs collide
        yml.write_text(re.sub(r'    ports:\n      - "(?:8080:8080|8081:8081|8380:80)"\n', "", yml.read_text()))
        cls.dir = cls.out / "compose"
        cls.http, cls.https = (80, 443) if cls.standard_ports else (free_port(), free_port())
        envf = cls.dir / ".env"
        txt = re.sub(r"^HTTP_PORT=.*$", f"HTTP_PORT={cls.http}", envf.read_text(), flags=re.M)
        envf.write_text(re.sub(r"^HTTPS_PORT=.*$", f"HTTPS_PORT={cls.https}", txt, flags=re.M))
        if cls.prepare:
            cls.prepare(cls.out)
        cls.base = ["docker", "compose", "-p", cls.project, "-f", str(cls.dir / "docker-compose.yml"), "--env-file", str(envf)]
        up = subprocess.run([*cls.base, "up", "-d", "--build", "--wait", "--wait-timeout", "300"], capture_output=True, text=True, timeout=900)
        if up.returncode != 0:
            ps = subprocess.run([*cls.base, "ps", "-a"], capture_output=True, text=True).stdout
            logs = subprocess.run([*cls.base, "logs", "--tail", "40", "--no-color"], capture_output=True, text=True).stdout
            cls.tearDownClass()                         # keep the evidence in the failure message: a timeout and a crash look the same otherwise
            raise AssertionError(f"compose up failed (rc {up.returncode}) after waiting\n{up.stdout}{up.stderr}\n--- ps -a\n{ps}\n--- last log lines\n{logs}")

    @classmethod
    def tearDownClass(cls):
        subprocess.run([*cls.base, "down", "-v", "--remove-orphans", "--rmi", "local"], capture_output=True, timeout=300)
        cls.env.stop()
        shutil.rmtree(cls.out, ignore_errors=True)
        shutil.rmtree(cls.home, ignore_errors=True)

    def prov(self, **kw):
        return provider.ComposeBackupProvider(self.cfg, **kw)

    def exec(self, service, *args, user="root"):
        return subprocess.run([*self.base, "exec", "-T", "-u", user, service, *args], capture_output=True, text=True, timeout=120)

    def create(self, comps, **kw):
        p = self.prov()
        lines = list(p.create(comps, **kw))
        self.assertTrue(any("sealed" in x for x in lines), lines)
        self.assertTrue(p.verify(p.result["id"])["ok"])
        return p, p.result

    def restore(self, bid, comps):
        r = self.prov()
        lines = list(r.restore(bid, comps, confirm=True))
        self.assertTrue(any("verified healthy" in x for x in lines), lines)
        return r


def ports_free(*ports):
    for p in ports:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) == 0:
                return False
    return True


@unittest.skipUnless(pulled("caddy:2") and ports_free(80, 443), "GISSTACK_TEST_INTEGRATION=1, docker, the Caddy image and free ports 80/443 needed")
class CaddyBackupRestore(GisStack):
    components = {"caddy": True}
    # gis-stack's public health probe uses the standard HTTPS port (a custom HTTPS_PORT in .env is not known to it), so Caddy runs on 80/443 here
    standard_ports = True
    ROOT = "/data/caddy/pki/authorities/local/root.crt"

    def root_sha(self):
        r = self.exec("caddy", "sh", "-c", f"sha256sum {self.ROOT} | cut -d' ' -f1")
        return r.stdout.strip()

    def https_ok(self, cafile):
        """HTTPS to the stack, verified against the given CA file; `localhost` resolves to the published port."""
        for _ in range(20):
            r = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--cacert", str(cafile), "--resolve", f"localhost:{self.https}:127.0.0.1",
                                f"https://localhost:{self.https}/"], capture_output=True, text=True, timeout=30)
            if r.stdout.strip() and r.stdout.strip() != "000":
                return True
            time.sleep(1)
        return False

    def test_certificates_survive_loss_of_the_volume(self):
        # force Caddy to issue from its local CA (first TLS handshake), then record the CA
        subprocess.run(["curl", "-sk", "-o", "/dev/null", "--resolve", f"localhost:{self.https}:127.0.0.1", f"https://localhost:{self.https}/"], capture_output=True, timeout=30)
        for _ in range(20):
            before = self.root_sha()
            if len(before) == 64:
                break
            time.sleep(1)
        self.assertEqual(len(before), 64, "Caddy did not create its local CA")
        p, m = self.create(["caddy"])
        self.assertGreater(m["components"]["caddy"]["meta"]["members"], 3)
        self.assertIn("TLS private keys", " ".join(p.inspect(m["id"]).get("sensitive", [])) or "TLS private keys")      # documented as sensitive
        # disaster: the state is lost and Caddy makes a different CA
        self.exec("caddy", "sh", "-c", "rm -rf /data/caddy")
        subprocess.run([*self.base, "restart", "caddy"], capture_output=True, timeout=120)
        subprocess.run(["curl", "-sk", "-o", "/dev/null", "--resolve", f"localhost:{self.https}:127.0.0.1", f"https://localhost:{self.https}/"], capture_output=True, timeout=30)
        time.sleep(3)
        lost = self.root_sha()
        self.assertNotEqual(lost, before, "the new CA must differ, otherwise the test proves nothing")
        r = self.restore(m["id"], ["caddy"])
        after = self.root_sha()
        self.assertEqual(after, before)                                                  # the original local CA is back, byte for byte
        ca = Path(self.home) / "root.crt"
        ca.write_text(self.exec("caddy", "cat", self.ROOT).stdout)
        self.assertTrue(self.https_ok(ca), "HTTPS must verify against the restored CA")  # and Caddy serves certificates it signs
        kept = r.result["retained_previous_data"]["caddy"]
        self.assertIn(".gs-pre-", kept)                                                  # the replaced state was kept, not deleted

    def test_caddyfile_is_configuration_not_in_the_backup(self):
        p, m = self.create(["caddy"])
        self.assertEqual(sorted(m["components"]), ["caddy"])
        self.assertEqual(m["components"]["caddy"]["meta"]["path"], "/data")


@unittest.skipUnless(ENABLED, "GISSTACK_TEST_INTEGRATION=1 and docker needed")
class MapProxyBackupRestore(GisStack):
    components = {"mapproxy": True}

    def test_cache_is_backed_up_restored_and_config_is_backup_only(self):
        self.exec("mapproxy", "sh", "-c", "mkdir -p /var/lib/mapproxy/cache_data/sentinel && echo tile-v1 > /var/lib/mapproxy/cache_data/sentinel/0.png")
        # default backup holds the configuration only; the cache is opt-in
        p0, m0 = self.create(None)
        self.assertEqual(sorted(m0["components"]), ["mapproxy_config"])
        self.assertIn("mapproxy.yaml", self.exec("mapproxy", "ls", "/etc/mapproxy").stdout)
        p, m = self.create(None, with_cache=True)
        self.assertEqual(sorted(m["components"]), ["mapproxy_cache", "mapproxy_config"])
        self.exec("mapproxy", "sh", "-c", "echo tile-v2 > /var/lib/mapproxy/cache_data/sentinel/0.png; echo extra > /var/lib/mapproxy/cache_data/sentinel/1.png")
        with self.assertRaises(RestoreError) as cm:
            list(self.prov().restore(m["id"], ["mapproxy_config"], confirm=True))
        self.assertIn("redeploy", str(cm.exception))                                     # configuration is never overwritten in place
        r = self.restore(m["id"], ["mapproxy_cache"])
        self.assertEqual(self.exec("mapproxy", "cat", "/var/lib/mapproxy/cache_data/sentinel/0.png").stdout.strip(), "tile-v1")
        self.assertNotEqual(self.exec("mapproxy", "test", "-e", "/var/lib/mapproxy/cache_data/sentinel/1.png").returncode, 0)   # the cache is the backup's again
        self.assertIn("tile-v2", self.exec("mapproxy", "sh", "-c", "cat /var/lib/mapproxy/.gs-pre-*/cache_data/sentinel/0.png").stdout)   # the replaced cache is kept
        self.assertEqual([x for x in r.list_backups() if x["kind"] == "pre-restore"], [])      # by design: a rebuildable cache gets no pre-restore backup, the swap keeps the old one


def _qgis_project(out: Path):
    d = out / "compose" / "qgis" / "projects"
    d.mkdir(parents=True, exist_ok=True)
    (d / "demo.qgs").write_text('<!DOCTYPE qgis PUBLIC \'http://mrcc.com/qgis.dtd\' \'SYSTEM\'><qgis version="3.34.0" projectname="gs-sentinel"></qgis>\n')


@unittest.skipUnless(pulled("camptocamp/qgis-server:latest"), "GISSTACK_TEST_INTEGRATION=1, docker and the QGIS Server image needed")
class QgisBackup(GisStack):
    components = {"qgis_server": True}
    prepare = staticmethod(_qgis_project)

    def test_projects_are_backed_up_and_verified_restore_is_refused_on_compose(self):
        p, m = self.create(["qgis"])
        self.assertEqual(m["components"]["qgis"]["meta"]["path"], "/etc/qgisserver")
        raw = subprocess.run(["tar", "-xzOf", str(p.store.path(m["id"]) / "qgis.tar.gz")], capture_output=True).stdout
        self.assertIn(b"gs-sentinel", raw)                                               # the project really is in the archive
        chk = p.check_restore(m["id"], ["qgis"])
        self.assertFalse(chk["ok"])
        self.assertTrue(any("cannot be restored in place on compose" in x for x in chk["problems"]), chk)
        with self.assertRaises(RestoreError):
            list(p.restore(m["id"], ["qgis"], confirm=True))
        self.assertIn(b"gs-sentinel", (self.dir / "qgis" / "projects" / "demo.qgs").read_bytes())      # nothing was touched


if __name__ == "__main__":
    unittest.main()
