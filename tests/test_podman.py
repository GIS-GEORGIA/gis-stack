"""Podman as the Compose runtime: qualified image names and status/control/log scripts that work with `podman-compose`.

Both regressions were found by running a real deployment on Podman 5.8.9 (see docs/IMPLEMENTATION-STATUS.md): (1) Podman refuses short image
names (`postgis/postgis`) without a TTY when several registries are configured; (2) `podman compose ps -a --format json` does not exist with the
podman-compose provider, so every status read said "container not created".  Unit tested on a recorded real `podman ps --format json`."""
import json
import re
import subprocess
import unittest
from pathlib import Path

import yaml

from gisstack import config, generate
from gisstack.providers import compose

FIX = Path(__file__).parent / "fixtures" / "podman"
ALL = {"postgis": True, "geoserver": True, "tomcat": True, "qgis_server": True, "mapproxy": True, "caddy": True, "webmap": True}


def cfg_for(runtime):
    return config.resolve({"project": "demo", "runtime": runtime, "targets": ["compose"], "deploy_target": "compose", "components": ALL}, persist_secrets=False, generate_secrets=False)


def bundle(runtime):
    import tempfile
    d = Path(tempfile.mkdtemp())
    generate.generate_bundle(cfg_for(runtime), d)
    return d / "compose"


def qualified(ref):
    first = ref.split("/", 1)[0]
    return "/" in ref and ("." in first or ":" in first or first == "localhost")


class ImageNameTests(unittest.TestCase):
    def test_every_pulled_image_is_registry_qualified_for_podman(self):
        d = bundle("podman")
        compose_yml = yaml.safe_load((d / "docker-compose.yml").read_text())
        pulled = []
        for name, svc in compose_yml["services"].items():
            if "build" in svc:
                continue                                                # built locally, tagged <project>-<service>
            pulled.append((name, svc["image"]))
        self.assertGreaterEqual(len(pulled), 4)
        for name, ref in pulled:
            self.assertTrue(qualified(ref), f"{name}: {ref} is a short name")
        for df in d.rglob("Dockerfile"):
            text = df.read_text()
            for line in re.findall(r"^(?:FROM|ARG PG_IMAGE=)\s*(\S+)", text, re.M):
                if line.startswith("$"):
                    continue
                self.assertTrue(qualified(line), f"{df.parent.name}/Dockerfile: {line}")
            if "PG_IMAGE=" in text:
                self.assertIn("PG_IMAGE=docker.io/postgis/postgis", text)
        for name, svc in compose_yml["services"].items():
            if isinstance(svc.get("build"), dict):
                for v in svc["build"].get("args", {}).values():
                    if "/" in str(v):
                        self.assertTrue(qualified(str(v)), f"{name}: build arg {v}")

    def test_podman_services_are_not_put_into_one_pod(self):
        """Regression (reproduced on Ubuntu 24.04 + Podman 4.9.3 + podman-compose 1.6.0, rootless): the default pod cannot be created ("Unit
        user-libpod_pod_<id>.slice was already loaded"), which made the rootless CI job fail before any test ran."""
        compose_yml = yaml.safe_load((bundle("podman") / "docker-compose.yml").read_text())
        self.assertEqual(compose_yml["x-podman"], {"in_pod": False})
        self.assertNotIn("x-podman", yaml.safe_load((bundle("docker") / "docker-compose.yml").read_text()))

    def test_docker_bundles_are_unchanged(self):
        d = bundle("docker")
        text = (d / "docker-compose.yml").read_text() + "".join(p.read_text() for p in d.rglob("Dockerfile"))
        self.assertNotIn("docker.io/", text)
        self.assertRegex(text, r"postgis/postgis:\d+-")                                      # still the short name Docker has always had


class PodmanStatusTests(unittest.TestCase):
    def out(self, status=None, state=None):
        items = json.loads((FIX / "ps.json").read_text())
        if status is not None:
            items[0]["Status"] = status
        if state is not None:
            items[0]["State"] = state
        return json.dumps(items) + "\n@@inspect\n" + (FIX / "inspect.txt").read_text()

    def row(self, **kw):
        cfg = cfg_for("podman")
        return compose.rows(cfg, ["postgis"], compose.parse(self.out(**kw)), [])[0]

    def test_script_asks_the_engine_not_the_compose_provider(self):
        s = compose.status_script(cfg_for("podman"), [])
        self.assertIn("podman ps -a --filter label=com.docker.compose.project=demo --format json", s)
        self.assertNotIn("compose -p demo ps", s)
        self.assertEqual(subprocess.run(["bash", "-n"], input=s, capture_output=True, text=True).returncode, 0)
        self.assertIn("compose -p demo ps -a --format json", compose.status_script(cfg_for("docker"), []))        # Docker is unchanged

    def test_recorded_real_podman_output_is_parsed(self):
        p = compose.parse(self.out(status="Up 2 minutes (healthy)"))
        self.assertEqual([i["Service"] for i in p["items"]], ["postgis"])
        self.assertTrue(p["ips"].get("postgis"), p["ips"])                                   # the container's IP comes from `podman inspect`
        r = self.row(status="Up 2 minutes (healthy)")
        self.assertEqual((r["state"], r["ok"]), ("running", True))
        self.assertTrue(any("127.0.0.1" in x and "5432" in x for x in r["listen"]), r["listen"])

    def test_podman_health_wording(self):
        self.assertEqual((self.row(status="Up 3 seconds (starting)")["state"], self.row(status="Up 3 seconds (starting)")["ok"]), ("starting", False))
        self.assertEqual((self.row(status="Up 2 minutes (unhealthy)")["state"], self.row(status="Up 2 minutes (unhealthy)")["ok"]), ("failed", False))
        self.assertEqual(self.row(status="Exited (1) 5 seconds ago", state="exited")["state"], "stopped")
        self.assertEqual(self.row(status="Created", state="created")["state"], "stopped")

    def test_no_containers_is_missing_not_an_error(self):
        r = compose.rows(cfg_for("podman"), ["postgis"], compose.parse("[]\n@@inspect\n"), [])[0]
        self.assertEqual(r["state"], "missing")


class PodmanScriptTests(unittest.TestCase):
    def valid_bash(self, s):
        self.assertEqual(subprocess.run(["bash", "-n"], input=s, capture_output=True, text=True).returncode, 0, s)

    def test_enable_disable_use_the_engine_filter(self):
        s = compose.control_script(cfg_for("podman"), "enable", ["postgis", "geoserver"])
        self.valid_bash(s)
        self.assertIn("podman ps -a -q --filter label=com.docker.compose.project=demo --filter \"label=com.docker.compose.service=$s\"", s)
        self.assertNotIn("ps -a -q \"$s\"", s)
        self.assertIn("compose -p demo ps -a -q", compose.control_script(cfg_for("docker"), "enable", ["postgis"]))

    def test_docker_start_stop_restart_keep_using_compose_and_podman_uses_the_engine(self):
        for action in ("start", "stop", "restart"):
            self.assertIn(f"docker compose -p demo {action} postgis", compose.control_script(cfg_for("docker"), action, ["postgis"]))
            s = compose.control_script(cfg_for("podman"), action, ["postgis"])
            self.valid_bash(s)
            self.assertNotIn("compose", s.replace("com.docker.compose", ""))                 # no compose provider involved (podman-compose needs a compose file in the cwd)
            self.assertIn(f"podman {action} $ids", s)

    def test_podman_control_script_runs_against_the_engine_and_reports_missing_containers(self):
        import os
        import shutil
        import stat
        import subprocess
        import tempfile
        from pathlib import Path
        td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, td, True)
        (td / "podman").write_text('#!/usr/bin/env bash\necho "podman $*" >> "$FAKE_LOG"\n'
                                   'if [ "$1" = ps ]; then case "$*" in *service=postgis*) [ -z "$FAKE_NONE" ] && echo abc123; esac; fi\nexit 0\n')
        (td / "podman").chmod(0o755 | stat.S_IEXEC)
        env = {**os.environ, "PATH": f"{td}:{os.environ['PATH']}", "FAKE_LOG": str(td / "log")}
        for action in ("start", "stop", "restart"):
            r = subprocess.run(["bash", "-c", compose.control_script(cfg_for("podman"), action, ["postgis"])], capture_output=True, text=True, env=env, cwd="/", timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(f"podman {action} abc123", (td / "log").read_text())
        r = subprocess.run(["bash", "-c", compose.control_script(cfg_for("podman"), "start", ["postgis"])], capture_output=True, text=True, env={**env, "FAKE_NONE": "1"}, cwd="/", timeout=30)
        self.assertEqual(r.returncode, 1)
        self.assertIn("container not found", r.stderr)
        r = subprocess.run(["bash", "-c", compose.control_script(cfg_for("podman"), "stop", ["postgis"])], capture_output=True, text=True, env={**env, "FAKE_NONE": "1"}, cwd="/", timeout=30)
        self.assertEqual(r.returncode, 0)                                                       # stopping what is not there is not an error

    def test_logs_use_the_engine_and_no_unsupported_flag(self):
        s = compose.logs_script(cfg_for("podman"), "postgis", 50, True)
        self.valid_bash(s)
        self.assertIn("podman logs --tail 50 -f", s)
        self.assertNotIn("--no-color", s)
        self.assertIn("--no-color", compose.logs_script(cfg_for("docker"), "postgis", 50, False))


if __name__ == "__main__":
    unittest.main()
