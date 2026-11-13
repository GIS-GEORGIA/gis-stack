"""Podman production reliability against a REAL Podman engine.  Opt-in: GISSTACK_TEST_PODMAN=1 (see test_stack_backup_podman.py for the requirements).

* provider diagnostics: when `podman compose` resolves to the Docker Compose plugin and the Podman API socket is not running, `up.sh` stops with a clear
  message BEFORE starting anything (it never switches provider by itself);
* rollback: deploy PostgreSQL 17 -> a failed upgrade (16 on 17's data) is detected -> rollback restores 17 with the data and the password intact;
* monitoring: the collection script gives real host metrics, service state and availability for a Podman stack (per-service CPU/memory only when
  `podman stats` returns rows - it returns none inside some nested/containerised engines)."""
from __future__ import annotations

import os
import re
import shutil
import socket
import stat
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from gisstack import config, deploy, hardening, deployments, generate, services
from gisstack.backups import provider
from gisstack.backups import provider as bprov

ENABLED = os.environ.get("GISSTACK_TEST_PODMAN") == "1" and shutil.which("podman") is not None
IMAGES = ("docker.io/postgis/postgis:17-3.5", "docker.io/postgis/postgis:16-3.5")


def pulled() -> bool:
    """Every image is present locally or can be pulled now (a registry that is unreachable must not silently skip a test whose images are cached)."""
    return ENABLED and all(subprocess.run(["podman", "image", "exists", i], capture_output=True).returncode == 0
                           or subprocess.run(["podman", "pull", "-q", i], capture_output=True).returncode == 0 for i in IMAGES)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def cfg_for(project, pg_version="17", port=None):
    return config.resolve({"project": project, "runtime": "podman", "postgres": {"port": port or free_port(), "version": pg_version}, "targets": ["compose"],
                           "deploy_target": "compose", "components": {"postgis": True, "geoserver": False, "tomcat": False, "qgis_server": False, "mapproxy": False,
                                                                       "caddy": False, "webmap": False}}, generate_secrets=True, persist_secrets=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.project = "gspr" + uuid.uuid4().hex[:6]
        env = mock.patch.dict(os.environ, {"GISSTACK_HOME": self.home, "GISSTACK_VERIFY_TIMEOUT": "60", "GISSTACK_VERIFY_SETTLE": "3"})
        env.start()
        self.addCleanup(env.stop)
        self.dirs = []
        self.addCleanup(self.destroy)

    def destroy(self):
        subprocess.run(["bash", "-c", f"ids=$(podman ps -aq --filter label=com.docker.compose.project={self.project}); [ -z \"$ids\" ] || podman rm -f $ids >/dev/null 2>&1; "
                        f"vols=$(podman volume ls -q | grep '^{self.project}_'); [ -z \"$vols\" ] || podman volume rm -f $vols >/dev/null 2>&1; "
                        f"podman network ls -q --filter name={self.project} | xargs -r podman network rm >/dev/null 2>&1"], capture_output=True, timeout=300)
        for d in self.dirs:
            shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(self.home, ignore_errors=True)

    def bundle(self, cfg):
        d = Path(tempfile.mkdtemp())
        self.dirs.append(d)
        generate.generate_bundle(cfg, d)
        return d

    def deploy(self, cfg):
        lines = []
        try:
            for line in deploy.deploy(cfg, self.bundle(cfg), "compose", "local"):
                lines.append(line)
        except Exception as e:
            raise AssertionError(f"deployment failed: {e}\n" + "\n".join(lines[-30:])) from e
        return lines


def provider_state():
    """(provider path or '', socket exists) as `podman compose` / `podman info` report them."""
    out = subprocess.run(["podman", "compose", "version"], capture_output=True, text=True, env={k: v for k, v in os.environ.items() if k != "PODMAN_COMPOSE_PROVIDER"})
    m = re.search(r'Executing external compose provider "([^"]+)"', out.stdout + out.stderr)
    path = subprocess.run(["podman", "info", "--format", "{{.Host.RemoteSocket.Path}}"], capture_output=True, text=True).stdout.strip().replace("unix://", "")
    return (m.group(1) if m else ""), bool(path) and os.path.exists(path) and stat.S_ISSOCK(os.stat(path).st_mode)


@unittest.skipUnless(ENABLED, "GISSTACK_TEST_PODMAN=1 and podman needed")
class ProviderDiagnostic(Base):
    def test_docker_compose_provider_without_a_socket_gets_a_clear_error_and_nothing_is_started(self):
        prov, socket_up = provider_state()
        if os.path.basename(prov) != "docker-compose" or socket_up:
            self.skipTest(f"needs `podman compose` to resolve to docker-compose with the Podman socket NOT running (provider={prov!r}, socket={socket_up})")
        cfg = cfg_for(self.project)
        d = self.bundle(cfg)
        env = {k: v for k, v in os.environ.items() if k != "PODMAN_COMPOSE_PROVIDER"}
        r = subprocess.run(["bash", "up.sh", "up"], cwd=d / "compose", env={**env, "RUNTIME": "podman"}, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("Docker Compose provider", r.stderr)
        self.assertIn("podman.socket", r.stderr)
        self.assertIn("PODMAN_COMPOSE_PROVIDER=podman-compose", r.stderr)
        self.assertNotIn("Cannot connect to the Docker daemon", r.stderr)                       # the provider's own misleading message is never reached
        left = subprocess.run(["podman", "ps", "-aq", "--filter", f"label=com.docker.compose.project={self.project}"], capture_output=True, text=True).stdout.strip()
        self.assertEqual(left, "")                                                              # nothing was started
        # the same through gis-stack's own deployment path: a DeployError whose output carries the diagnostic
        with mock.patch.dict(os.environ, env, clear=True), self.assertRaises(Exception) as cm:
            list(deploy.deploy(cfg, d, "compose", "local"))
        self.assertIn("exit 3", str(cm.exception))


@unittest.skipUnless(pulled(), "GISSTACK_TEST_PODMAN=1, podman with a compose provider and the PostGIS 16/17 images needed")
class PodmanRollback(Base):
    def psql(self, cfg, sql):
        return provider.PodmanBackupProvider(cfg).psql(sql, db=cfg["postgres"]["db"])

    def test_failed_upgrade_is_detected_and_rolled_back_without_losing_data_or_secrets(self):
        port = free_port()
        cfg1 = cfg_for(self.project, "17", port)
        lines = self.deploy(cfg1)
        self.assertTrue(any("ყველა პროგრამა მუშაობს" in x for x in lines), "\n".join(lines[-12:]))
        rec1 = deployments.records(self.project)[0]
        self.assertEqual((rec1["outcome"], rec1["target"]), ("ok", "compose"))
        self.psql(cfg1, "create table keepme(v text); insert into keepme values ('before-upgrade')")

        cfg2 = cfg_for(self.project, "16", port)
        self.assertEqual(cfg2["postgres"]["password"], cfg1["postgres"]["password"])               # existing secrets are reused
        self.deploy(cfg2)                                                                           # PostgreSQL 16 refuses 17's data: crash loop
        rec2 = deployments.records(self.project)[0]
        self.assertEqual(rec2["outcome"], "unhealthy", rec2)
        self.assertNotEqual(rec2["id"], rec1["id"])

        plan = services.rollback_plan(cfg2, "local", None, "compose")
        self.assertTrue(plan["eligible"], plan)
        snap, _ = services.rollback_config(cfg2, "local", None, "compose", rec1["id"])
        self.assertEqual(snap["postgres"]["version"], "17")
        self.assertEqual(snap["postgres"]["password"], cfg1["postgres"]["password"])
        list(services.rollback_steps(snap, self.bundle(snap), "compose", "local", None, rec1["id"]))
        rec3 = deployments.records(self.project)[0]
        self.assertEqual((rec3["kind"], rec3["outcome"], rec3["rollback_of"]), ("rollback", "ok", rec1["id"]))
        self.assertEqual(self.psql(cfg1, "select v from keepme"), "before-upgrade")                  # data intact, original password still works
        self.assertIn("17", self.psql(cfg1, "show server_version"))
        again = services.rollback_plan(cfg2, "local", None, "compose", rec1["id"])
        self.assertFalse(again["eligible"])
        self.assertIn("nothing to do", again["reason"])


def host_address():
    """A non-loopback IPv4 address of this machine (the one a UDP socket would use to reach the outside; nothing is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.connect(("10.255.255.255", 1))
            a = sk.getsockname()[0]
    except OSError:
        return ""
    return "" if a.startswith("127.") else a


@unittest.skipUnless(ENABLED and host_address() and shutil.which("curl"), "GISSTACK_TEST_PODMAN=1, podman, curl and a non-loopback host address needed")
class PodmanExposure(Base):
    """S-2 on a real Podman engine (rootful and rootless): MapProxy stands for the directly published services (no Caddy)."""

    def mp_cfg(self, **extra):
        return config.resolve({"project": self.project, "runtime": "podman", "targets": ["compose"], "deploy_target": "compose", **extra,
                               "components": {"postgis": False, "geoserver": False, "tomcat": False, "qgis_server": False, "mapproxy": True, "caddy": False, "webmap": False}},
                              generate_secrets=True, persist_secrets=True)

    def reach(self, host, secs=40):
        for _ in range(secs):
            r = subprocess.run(["curl", "-s", "-o", "/dev/null", "-m", "4", "-w", "%{http_code}", f"http://{host}:8081/demo/"], capture_output=True, text=True)
            if r.stdout.strip() in ("200", "301", "302"):
                return True
            time.sleep(1)
        return False

    def test_default_is_loopback_only_and_remote_access_is_explicit(self):
        host = host_address()
        self.deploy(self.mp_cfg())
        ports = subprocess.run(["podman", "ps", "--filter", f"label=com.docker.compose.project={self.project}", "--format", "{{.Ports}}"], capture_output=True, text=True).stdout
        self.assertIn("127.0.0.1:8081", ports)
        self.assertNotIn("0.0.0.0:8081", ports)
        self.assertTrue(self.reach("127.0.0.1"), "the port must work on loopback")
        self.assertFalse(self.reach(host, 3), f"{host}:8081 must NOT answer: the port is bound to loopback only")
        self.destroy()
        self.deploy(self.mp_cfg(network={"bind": "0.0.0.0"}))
        self.assertIn("0.0.0.0:8081", subprocess.run(["podman", "ps", "--filter", f"label=com.docker.compose.project={self.project}", "--format", "{{.Ports}}"], capture_output=True, text=True).stdout)
        self.assertTrue(self.reach(host), f"with network.bind = 0.0.0.0 {host}:8081 must answer")


@unittest.skipUnless(ENABLED, "GISSTACK_TEST_PODMAN=1 and podman needed")
class PodmanHardening(Base):
    """S-4 on a real Podman engine (rootful and rootless): dropped capabilities + no-new-privileges still give a healthy PostGIS / GeoServer / MapProxy / Caddy
    stack, the kernel agrees, and PostgreSQL data survives switching hardening off and on."""

    def cfg(self, harden, **extra):
        from gisstack import catalog  # noqa: F401
        return config.resolve({"project": self.project, "runtime": "podman", "postgres": {"port": free_port()}, "targets": ["compose"], "deploy_target": "compose", "tls": "internal",
                               "domain": "localhost", "security": {"harden_containers": harden}, **extra,
                               "components": {"postgis": True, "geoserver": True, "tomcat": False, "qgis_server": False, "mapproxy": True, "caddy": True, "webmap": False}},
                              generate_secrets=True, persist_secrets=True)

    def up(self, harden, pg_port):
        import re as _re
        cfg = self.cfg(harden)
        cfg["postgres"]["port"] = pg_port
        d = self.bundle(cfg)
        envf = d / "compose" / ".env"
        txt = _re.sub(r"^HTTP_PORT=.*$", f"HTTP_PORT={free_port()}", envf.read_text(), flags=_re.M)
        envf.write_text(_re.sub(r"^HTTPS_PORT=.*$", f"HTTPS_PORT={free_port()}", txt, flags=_re.M))
        lines = list(deploy.deploy(cfg, d, "compose", "local"))
        self.assertTrue(any("ყველა პროგრამა მუშაობს" in x for x in lines), "\n".join(lines[-12:]))
        return cfg

    def status1(self, service):
        cid = subprocess.run(["podman", "ps", "-q", "--filter", f"label=com.docker.compose.project={self.project}", "--filter", f"label=com.docker.compose.service={service}"],
                             capture_output=True, text=True).stdout.split()[0]
        out = subprocess.run(["podman", "exec", cid, "sh", "-c", "grep -E '^(CapEff|NoNewPrivs):' /proc/self/status"], capture_output=True, text=True).stdout
        return cid, dict(line.split(":\t") for line in out.strip().splitlines())

    def test_hardened_stack_is_healthy_the_kernel_agrees_and_data_survives_the_toggle(self):
        pg_port = free_port()
        cfg = self.up(True, pg_port)
        for service in ("postgis", "geoserver", "mapproxy", "caddy"):
            cid, st = self.status1(service)
            self.assertEqual(st.get("NoNewPrivs"), "1", service)
            bits = {"CHOWN": 0, "DAC_OVERRIDE": 1, "FOWNER": 3, "SETGID": 6, "SETUID": 7, "NET_BIND_SERVICE": 10}      # Linux capability numbers
            allowed = sum(1 << bits[c] for c in hardening.CAPS[service])
            eff = int(st["CapEff"], 16)
            self.assertEqual(eff & ~allowed, 0, f"{service}: CapEff {st['CapEff']} has capabilities beyond {hardening.CAPS[service]}")   # the kernel's view (podman inspect expands CapDrop)
            self.assertEqual(eff & (1 << 21), 0, f"{service} has CAP_SYS_ADMIN")
        prov = bprov.PodmanBackupProvider(cfg)
        prov.psql("drop table if exists hardened_t; create table hardened_t(v text); insert into hardened_t values ('kept')", db=cfg["postgres"]["db"])
        cfg = self.up(False, pg_port)                                   # unhardened containers on the same volumes
        self.assertEqual(self.status1("postgis")[1].get("NoNewPrivs"), "0")
        prov = bprov.PodmanBackupProvider(cfg)
        self.assertEqual(prov.psql("select v from hardened_t", db=cfg["postgres"]["db"]), "kept")
        prov.psql("insert into hardened_t values ('unhardened')", db=cfg["postgres"]["db"])
        cfg = self.up(True, pg_port)                                    # and hardened again
        prov = bprov.PodmanBackupProvider(cfg)
        self.assertEqual(self.status1("postgis")[1].get("NoNewPrivs"), "1")
        self.assertEqual(prov.psql("select string_agg(v, ',' order by v) from hardened_t", db=cfg["postgres"]["db"]), "kept,unhardened")


@unittest.skipUnless(ENABLED, "GISSTACK_TEST_PODMAN=1 and podman needed")
class PodmanCaddyCustomPorts(Base):
    """S-3 on Podman: Caddy published on custom HTTP/HTTPS host ports (`.env`); gis-stack's health verification must read them from the engine."""

    def test_deploy_and_backup_restore_with_non_default_ports(self):
        import re as _re
        from gisstack import ops
        cfg = config.resolve({"project": self.project, "runtime": "podman", "targets": ["compose"], "deploy_target": "compose", "tls": "internal", "domain": "localhost",
                              "components": {"postgis": False, "geoserver": False, "tomcat": False, "qgis_server": False, "mapproxy": False, "caddy": True, "webmap": False}},
                             generate_secrets=True, persist_secrets=True)
        d = self.bundle(cfg)
        envf = d / "compose" / ".env"
        http, https = free_port(), free_port()
        txt = _re.sub(r"^HTTP_PORT=.*$", f"HTTP_PORT={http}", envf.read_text(), flags=_re.M)
        envf.write_text(_re.sub(r"^HTTPS_PORT=.*$", f"HTTPS_PORT={https}", txt, flags=_re.M))
        lines = list(deploy.deploy(cfg, d, "compose", "local"))
        self.assertTrue(any("ყველა პროგრამა მუშაობს" in x for x in lines), "\n".join(lines[-12:]))        # gis-stack's own verification found Caddy healthy
        row = [r for r in ops.status(cfg, "local", None, "compose")["rows"] if r["id"] == "caddy"][0]
        self.assertTrue(row["ok"], row)
        self.assertIn(f":{https}", subprocess.run(["podman", "ps", "--filter", f"label=com.docker.compose.project={self.project}", "--format", "{{.Ports}}"], capture_output=True, text=True).stdout)
        p = bprov.PodmanBackupProvider(cfg)
        list(p.create(["caddy"]))
        r = bprov.PodmanBackupProvider(cfg)
        out = list(r.restore(p.result["id"], ["caddy"], confirm=True))
        self.assertTrue(any("verified healthy" in x for x in out), out)                                  # the restore is not undone by a hard-coded port


@unittest.skipUnless(pulled(), "GISSTACK_TEST_PODMAN=1, podman with a compose provider and the PostGIS images needed")
class PodmanLifecycle(Base):
    def states(self, cfg):
        from gisstack import ops
        return {r["id"]: r["state"] for r in ops.status(cfg, "local", None, "compose")["rows"]}

    def wait_state(self, cfg, want, seconds=90):
        end = time.time() + seconds
        while time.time() < end:
            if self.states(cfg).get("postgis") == want:
                return True
            time.sleep(2)
        return False

    def test_stop_start_restart_through_gis_stack_from_any_directory(self):
        """`podman-compose -p NAME stop` fails with "no compose.yaml found" outside the bundle directory; the control scripts use the engine instead."""
        from gisstack import ops
        cfg = cfg_for(self.project)
        self.deploy(cfg)
        self.assertTrue(self.wait_state(cfg, "running"), self.states(cfg))
        cwd = os.getcwd()
        os.chdir("/")                                                                              # not the bundle directory
        self.addCleanup(os.chdir, cwd)
        for action, want in (("stop", "stopped"), ("start", "running"), ("restart", "running")):
            lines = list(ops.control(cfg, "local", None, action, None, "compose"))
            self.assertIn("postgis: " + action + " done", "\n".join(lines))
            self.assertTrue(self.wait_state(cfg, want), (action, self.states(cfg)))
        self.assertEqual(provider.PodmanBackupProvider(cfg).psql("select 1", db=cfg["postgres"]["db"]), "1")     # and the database answers after the cycle


@unittest.skipUnless(pulled(), "GISSTACK_TEST_PODMAN=1, podman with a compose provider and the PostGIS images needed")
class PodmanMonitoring(Base):
    def test_collection_on_a_real_podman_stack(self):
        from gisstack.monitoring import service
        cfg = cfg_for(self.project)
        self.deploy(cfg)
        mon = service.Monitor(cfg, "local", None, "compose")
        try:
            res = mon.tick()
            self.assertEqual(res["errors"], [])
            snap = service.snapshot(mon.store)
        finally:
            mon.store.close()
        m = snap["metrics"]
        for name in ("host_cpu_percent", "host_mem_used_percent", "disk_used_percent", "host_load1", "monitor_reachable", "collect_errors"):
            self.assertIn(name, m, f"real metric {name} missing")
        self.assertEqual(m["monitor_reachable"][0]["value"], 1.0)
        self.assertEqual(m["collect_errors"][0]["value"], 0.0)
        up = {s["labels"]["service"]: s["value"] for s in m["service_up"]}
        self.assertEqual(up, {"postgis": 1.0})                                                      # read from the Podman engine, not invented
        self.assertEqual(snap["availability"], {"postgis": 100.0})
        self.assertEqual(snap["alerts"], [])
        self.assertEqual(m["pg_up"][0]["value"], 1.0)                                               # asked of the real database through the Podman engine
        self.assertGreaterEqual(m["pg_connections_total"][0]["value"], 1.0)
        self.assertEqual(m["backup_count"][0]["value"], 0.0)
        # a real backup is counted and aged; a real FAILED backup (killed while verifying, leaving a .FAILED entry) fires the alert; a later good one resolves it
        prov = provider.PodmanBackupProvider(cfg)
        list(prov.create(["postgres"]))
        with mock.patch.object(provider.BackupProvider, "_verify_dump", side_effect=RuntimeError("connection lost")), self.assertRaises(RuntimeError):
            list(prov.create(["postgres"]))
        mon2 = service.Monitor(cfg, "local", None, providers=[])
        try:
            res = mon2.tick()
            names = {n["rule"]: n["state"] for n in res["notifications"]}
            self.assertEqual(names.get("backup_failed"), "firing", res["notifications"])
            m2 = service.snapshot(mon2.store)["metrics"]
            self.assertEqual((m2["backup_count"][0]["value"], m2["backup_failed_count"][0]["value"]), (1.0, 1.0))
            self.assertLess(m2["backup_last_age_seconds"][0]["value"], 600)
            time.sleep(1.1)
            list(prov.create(["postgres"]))
            res = mon2.tick()
            self.assertEqual({n["rule"]: n["state"] for n in res["notifications"]}.get("backup_failed"), "resolved", res["notifications"])
        finally:
            mon2.store.close()
        stats = subprocess.run(["podman", "stats", "--no-stream", "--format", "{{.Name}}"], capture_output=True, text=True).stdout.split()
        print(f"\n[podman stats] rows for this project: {sum(self.project in x for x in stats)} (per-service CPU/memory asserted only when > 0)")
        if any(self.project in x for x in stats):                                                   # only where the engine reports container usage at all
            self.assertTrue([s for s in m.get("service_cpu_percent", []) if s["labels"]["service"] == "postgis"])
            self.assertGreater([s for s in m["service_rss_bytes"] if s["labels"]["service"] == "postgis"][0]["value"], 0)


if __name__ == "__main__":
    unittest.main()
