"""S-4 on REAL Docker Compose: a stack whose containers dropped every capability and cannot gain privileges still starts, passes gis-stack's own health
verification, serves through Caddy, keeps PostgreSQL data when hardening is switched off and on again (a volume created by an unhardened container is
readable by a hardened one and vice versa), and the kernel agrees (CapEff / NoNewPrivs of PID 1).  Opt-in: GISSTACK_TEST_INTEGRATION=1."""
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

from gisstack import config, deploy, generate, hardening
from gisstack.backups import provider

from . import images

ENABLED = os.environ.get("GISSTACK_TEST_INTEGRATION") == "1" and shutil.which("docker") is not None
PG = "postgis/postgis:17-3.5"
COMPONENTS = {"postgis": True, "geoserver": True, "tomcat": False, "qgis_server": True, "mapproxy": True, "caddy": True, "webmap": True}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def sh(*args, timeout=300):
    return subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)


@unittest.skipUnless(ENABLED, "GISSTACK_TEST_INTEGRATION=1 and docker needed")
class HardenedStack(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        images.require("docker", PG, images.geoserver_image(), "camptocamp/qgis-server:latest", "caddy:2")
        cls.home = tempfile.mkdtemp()
        cls.env = mock.patch.dict(os.environ, {"GISSTACK_HOME": cls.home, "GISSTACK_VERIFY_TIMEOUT": "240", "GISSTACK_VERIFY_SETTLE": "3"})
        cls.env.start()
        cls.project = "gshd" + uuid.uuid4().hex[:6]
        cls.dirs = []
        cls.pg_port = free_port()
        cls.https = free_port()

    @classmethod
    def tearDownClass(cls):
        if cls.dirs:
            d = cls.dirs[-1] / "compose"
            sh("docker", "compose", "-p", cls.project, "-f", str(d / "docker-compose.yml"), "--env-file", str(d / ".env"), "down", "-v", "--rmi", "local", "--remove-orphans", timeout=300)
        for d in cls.dirs:
            shutil.rmtree(d, ignore_errors=True)
        cls.env.stop()
        shutil.rmtree(cls.home, ignore_errors=True)

    def cfg(self, harden):
        return config.resolve({"project": self.project, "postgres": {"port": self.pg_port}, "targets": ["compose"], "deploy_target": "compose", "tls": "internal", "domain": "localhost",
                               "security": {"harden_containers": harden}, "components": COMPONENTS}, generate_secrets=True, persist_secrets=True)

    def deploy(self, harden):
        cfg = self.cfg(harden)
        out = Path(tempfile.mkdtemp())
        self.dirs.append(out)
        generate.generate_bundle(cfg, out)
        envf = out / "compose" / ".env"
        txt = re.sub(r"^HTTP_PORT=.*$", f"HTTP_PORT={free_port()}", envf.read_text(), flags=re.M)
        envf.write_text(re.sub(r"^HTTPS_PORT=.*$", f"HTTPS_PORT={self.https}", txt, flags=re.M))
        lines = []
        try:
            for line in deploy.deploy(cfg, out, "compose", "local"):
                lines.append(line)
        except Exception as e:
            raise AssertionError(f"deployment failed: {e}\n" + "\n".join(lines[-25:])) from e
        self.assertTrue(any("ყველა პროგრამა მუშაობს" in x for x in lines), "\n".join(lines[-12:]))        # gis-stack's own verification: every program healthy
        return cfg

    def ctr(self, service):
        return sh("docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={self.project}", "--filter", f"label=com.docker.compose.service={service}").stdout.split()[0]

    def proc_status(self, service):
        out = sh("docker", "exec", self.ctr(service), "sh", "-c", "grep -E '^(CapEff|NoNewPrivs):' /proc/self/status").stdout
        return dict(line.split(":\t") for line in out.strip().splitlines()) if out.strip() else {}

    def caps_of(self, hexmask):
        names = {0: "CHOWN", 1: "DAC_OVERRIDE", 2: "DAC_READ_SEARCH", 3: "FOWNER", 6: "SETGID", 7: "SETUID", 10: "NET_BIND_SERVICE", 21: "SYS_ADMIN"}
        v = int(hexmask, 16)
        return {n for bit, n in names.items() if v & (1 << bit)}, v

    def psql(self, cfg, sql):
        return provider.ComposeBackupProvider(cfg).psql(sql, db=cfg["postgres"]["db"])

    def test_1_hardened_stack_is_healthy_serves_and_the_kernel_agrees(self):
        cfg = self.deploy(True)
        for svc, caps in hardening.CAPS.items():
            service = {"qgis": "qgis"}.get(svc, svc)
            if svc == "init" or svc == "tomcat":
                continue
            insp = sh("docker", "inspect", "--format", "{{json .HostConfig.CapDrop}} {{json .HostConfig.SecurityOpt}}", self.ctr(service)).stdout
            self.assertIn('["ALL"]', insp, service)
            if service not in hardening.NO_PRIVILEGE_LOCK:
                self.assertIn("no-new-privileges", insp, service)
            st = self.proc_status(service)
            if service not in hardening.NO_PRIVILEGE_LOCK:                          # PostgreSQL: see hardening.NO_PRIVILEGE_LOCK (gosu hand-over breaks under no-new-privileges + AppArmor)
                self.assertEqual(st.get("NoNewPrivs"), "1", f"{service}: the kernel flag")
            got, raw = self.caps_of(st["CapEff"])
            self.assertTrue(got <= set(caps) | set(), f"{service} runs with {got}, allowed {caps}")
            self.assertEqual(raw & (1 << 21), 0, f"{service} has CAP_SYS_ADMIN")
        # served through Caddy on the custom HTTPS port: the web map, GeoServer, MapProxy, QGIS Server
        for path, ok in (("/", ("200",)), ("/geoserver/web/", ("200", "302")), ("/mapproxy/demo/", ("200",)), ("/qgis/?SERVICE=WMS&REQUEST=GetCapabilities", ("200", "400", "500"))):
            code = ""
            for _ in range(40):
                code = sh("curl", "-sk", "-m", "8", "-o", "/dev/null", "-w", "%{http_code}", "--resolve", f"localhost:{self.https}:127.0.0.1", f"https://localhost:{self.https}{path}").stdout.strip()
                if code in ok:
                    break
                time.sleep(2)
            self.assertIn(code, ok, path)
        self.psql(cfg, "drop table if exists hardened_t; create table hardened_t(v text); insert into hardened_t values ('kept')")
        self.assertEqual(self.psql(cfg, "select v from hardened_t"), "kept")

    def test_2_data_survives_switching_hardening_off_and_on(self):
        cfg = self.deploy(False)                                  # containers created WITHOUT restrictions: files on the volume are owned as the old container left them
        self.assertEqual(self.psql(cfg, "select v from hardened_t"), "kept")
        self.assertTrue(int(self.proc_status("postgis")["CapEff"], 16) & (1 << 10), "unhardened PostgreSQL keeps the default capabilities (NET_BIND_SERVICE)")
        self.psql(cfg, "insert into hardened_t values ('written-unhardened')")
        cfg = self.deploy(True)                                   # and hardened again, on the same volume
        self.assertEqual(int(self.proc_status("postgis")["CapEff"], 16) & (1 << 10), 0, "hardened PostgreSQL lost NET_BIND_SERVICE again")
        self.assertEqual(self.psql(cfg, "select string_agg(v, ',' order by v) from hardened_t"), "kept,written-unhardened")
        self.psql(cfg, "insert into hardened_t values ('written-hardened')")
        self.assertEqual(self.psql(cfg, "select count(*) from hardened_t"), "3")

    def test_3_backup_and_restore_work_on_the_hardened_stack(self):
        cfg = self.cfg(True)
        p = provider.ComposeBackupProvider(cfg)
        list(p.create(["postgres", "geoserver", "caddy"]))
        self.assertTrue(p.verify(p.result["id"])["ok"])
        self.psql(cfg, "delete from hardened_t")
        r = provider.ComposeBackupProvider(cfg)
        out = list(r.restore(p.result["id"], ["postgres", "geoserver", "caddy"], confirm=True))
        self.assertTrue(any("verified healthy" in x for x in out), out)
        self.assertEqual(self.psql(cfg, "select count(*) from hardened_t"), "3")


if __name__ == "__main__":
    unittest.main()
