"""S-4 on a REAL Podman engine (rootful or rootless): the hardened stack (capabilities dropped, no privilege gain, read-only root file system) starts, passes
gis-stack's own health verification and serves through Caddy.  PostGIS, MapProxy and Caddy are the services whose root file system is read-only
(hardening.READ_ONLY_TMPFS); for each the engine's own record AND the kernel's view are checked: `ReadonlyRootfs`, `CapDrop`, a refused write to `/`, no
"Read-only file system" message in the container log, CapEff / NoNewPrivs of PID 1 (read with `podman exec ... /proc/self/status`: on rootless Podman
`/proc/1/status` does not show the container process).  GeoServer, QGIS Server and Tomcat are left out (heavy images, writable root).
tests/integration/test_hardening.py is the Docker Compose twin.  Opt-in: GISSTACK_TEST_PODMAN=1 (see docs/TESTING.md)."""
from __future__ import annotations

import re
import subprocess
import time
import unittest

from gisstack import hardening

from .test_stack_backup_podman import PodmanStackBase, free_port, pulled

PG_IMAGE = "docker.io/postgis/postgis:17-3.5"
CADDY_IMAGE = "docker.io/library/caddy:2"
SERVICES = ("postgis", "mapproxy", "caddy")
CAP_NAMES = {0: "CHOWN", 1: "DAC_OVERRIDE", 3: "FOWNER", 6: "SETGID", 7: "SETUID", 10: "NET_BIND_SERVICE", 21: "SYS_ADMIN"}


def sh(*args, timeout=120):
    return subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)


@unittest.skipUnless(pulled(PG_IMAGE) and pulled(CADDY_IMAGE), "GISSTACK_TEST_PODMAN=1, podman with a compose provider and the PostGIS and Caddy images needed")
class HardenedStackOnPodman(PodmanStackBase):
    components = {"postgis": True, "mapproxy": True, "caddy": True}
    https = 0

    @classmethod
    def prepare_bundle(cls):
        cls.https = free_port()
        envf = cls.out / "compose" / ".env"
        txt = re.sub(r"^HTTP_PORT=.*$", f"HTTP_PORT={free_port()}", envf.read_text(), flags=re.M)
        envf.write_text(re.sub(r"^HTTPS_PORT=.*$", f"HTTPS_PORT={cls.https}", txt, flags=re.M))      # rootless Podman cannot bind 80 / 443

    def test_the_hardened_stack_is_read_only_where_configured_and_the_kernel_agrees(self):
        self.assertEqual(sorted(hardening.READ_ONLY_TMPFS), sorted(SERVICES), "this test must cover every service with a read-only root")
        for service in SERVICES:
            cid = self.ctr(service)
            host = sh("podman", "inspect", "--format", "{{.HostConfig.ReadonlyRootfs}}|{{json .HostConfig.CapDrop}}|{{json .HostConfig.SecurityOpt}}", cid).stdout.strip()
            ro, drop, secopt = host.split("|", 2)
            self.assertEqual(ro, "true", f"{service}: ReadonlyRootfs")
            # Docker records `["ALL"]`; Podman expands it to the explicit list of capabilities it drops FROM ITS DEFAULT SET and leaves out the ones added back
            # (measured: postgis -> ["CAP_FSETID","CAP_KILL","CAP_NET_BIND_SERVICE","CAP_SETFCAP","CAP_SETPCAP","CAP_SYS_CHROOT"]).  So the engine's record must say ALL
            # or must not list a capability the service is meant to keep; what the process really holds is checked below from the kernel.
            if "ALL" not in drop:
                self.assertTrue(drop.startswith("[") and "CAP_" in drop, f"{service}: no capability is recorded as dropped: {drop}")
                for cap in hardening.CAPS[service]:
                    self.assertNotIn(f"CAP_{cap}", drop, f"{service}: {cap} is both kept and dropped")
            if service not in hardening.NO_PRIVILEGE_LOCK:
                self.assertIn("no-new-privileges", secopt, service)
            w = sh("podman", "exec", cid, "sh", "-c", "touch /etc/gisstack-write-test")
            self.assertNotEqual(w.returncode, 0, f"{service}: wrote to the root file system")
            logs = sh("podman", "logs", cid)
            self.assertNotIn("Read-only file system", logs.stdout + logs.stderr, f"{service}: something tried to write to the read-only root")
            status = sh("podman", "exec", cid, "sh", "-c", "grep -E '^(CapEff|NoNewPrivs):' /proc/self/status").stdout
            st = dict(line.split(":\t") for line in status.strip().splitlines())
            mask = int(st["CapEff"], 16)
            got = {name for bit, name in CAP_NAMES.items() if mask & (1 << bit)}
            allowed = set(hardening.CAPS[service])
            self.assertTrue(got <= allowed, f"{service} runs with {got}, allowed {allowed}")
            self.assertEqual(mask & (1 << 21), 0, f"{service} has CAP_SYS_ADMIN")
            if service not in hardening.NO_PRIVILEGE_LOCK:
                self.assertEqual(st.get("NoNewPrivs"), "1", f"{service}: the kernel flag")

    def test_mapproxy_serves_through_caddy_on_a_read_only_root(self):
        code = ""
        for _ in range(40):
            code = sh("curl", "-sk", "-m", "8", "-o", "/dev/null", "-w", "%{http_code}", "--resolve", f"localhost:{self.https}:127.0.0.1",
                      f"https://localhost:{self.https}/mapproxy/demo/").stdout.strip()
            if code == "200":
                break
            time.sleep(2)
        self.assertEqual(code, "200")
        cache = sh("podman", "exec", self.ctr("mapproxy"), "sh", "-c", "touch /var/lib/mapproxy/cache_data/gisstack-write-test && echo writable")
        self.assertIn("writable", cache.stdout + cache.stderr, "the cache volume must stay writable")


if __name__ == "__main__":
    unittest.main()
