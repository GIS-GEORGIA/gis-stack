"""The Podman compose-provider guard (gisstack/podman_guard.py): runs the real generated bash with a fake `podman`.

Found on GitHub: `podman compose` resolved to the Docker Compose plugin, which needs the Podman API socket a rootless session does not run, and the
deployment died with "Cannot connect to the Docker daemon".  The guard names the provider, refuses exactly that combination with the ways out, and
never switches provider.  The real-Podman counterpart is tests/integration/test_podman_provider.py.  Unit tested."""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from gisstack import config, generate
from gisstack.providers import compose

FAKE = r'''#!/usr/bin/env bash
# fake podman: `compose version` reports a provider on stderr (as podman does); `info` answers the socket question
case "$1" in
  compose) [ "$2" = version ] || { [ -n "$FAKE_MARKER" ] && touch "$FAKE_MARKER"; exit 0; }
           [ -n "$FAKE_PROVIDER" ] && echo ">>>> Executing external compose provider \"$FAKE_PROVIDER\". Please refer to the documentation for details. <<<<" >&2
           echo "provider version 1.0"; exit 0 ;;
  info) echo "${FAKE_SOCKET:-/nonexistent/podman.sock}"; exit 0 ;;
esac
exit 0
'''


def cfg_for(runtime="podman"):
    return config.resolve({"project": "demo", "runtime": runtime, "targets": ["compose"], "deploy_target": "compose",
                           "components": {"postgis": True, "geoserver": False, "tomcat": False, "qgis_server": False, "mapproxy": False, "caddy": False, "webmap": False}},
                          persist_secrets=False, generate_secrets=False)


@unittest.skipUnless(shutil.which("bash"), "bash needed")
class GuardTests(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.td, True)
        (self.td / "bin").mkdir()
        p = self.td / "bin" / "podman"
        p.write_text(FAKE)
        p.chmod(p.stat().st_mode | stat.S_IEXEC)

    def guard(self, provider="", socket=None):
        from gisstack import podman_guard
        env = {**os.environ, "PATH": f"{self.td / 'bin'}:{os.environ['PATH']}", "FAKE_PROVIDER": provider}
        if socket is not None:
            env["FAKE_SOCKET"] = socket
        r = subprocess.run(["bash", "-c", podman_guard.GUARD + "\ngs_podman_provider_guard; echo \"rc=$?\""], capture_output=True, text=True, env=env, timeout=30)
        return r.stdout.strip(), r.stderr

    def test_docker_compose_provider_without_a_socket_is_refused_with_the_ways_out(self):
        out, err = self.guard("/usr/libexec/docker/cli-plugins/docker-compose")
        self.assertEqual(out, "rc=3")
        self.assertIn("compose provider: /usr/libexec/docker/cli-plugins/docker-compose", err)
        self.assertIn("Docker Compose provider", err)
        self.assertIn("systemctl --user enable --now podman.socket", err)
        self.assertIn("PODMAN_COMPOSE_PROVIDER=podman-compose", err)
        self.assertIn("does not switch provider by itself", err)

    def test_docker_compose_provider_with_a_running_socket_is_allowed(self):
        import socket as sk
        path = str(self.td / "podman.sock")
        srv = sk.socket(sk.AF_UNIX)
        self.addCleanup(srv.close)
        srv.bind(path)
        out, err = self.guard("/usr/local/bin/docker-compose", socket="unix://" + path)
        self.assertEqual(out, "rc=0")
        self.assertIn("compose provider: /usr/local/bin/docker-compose", err)                      # still named, so the log shows what was used

    def test_a_socket_path_that_is_not_a_socket_file_is_refused(self):
        # real Podman reports RemoteSocket.Exists=true for a socket that is not running (seen on Podman 5.8 / Fedora 44): only the file counts
        (self.td / "podman.sock").write_text("")
        out, _ = self.guard("/usr/libexec/docker/cli-plugins/docker-compose", socket=str(self.td / "podman.sock"))
        self.assertEqual(out, "rc=3")

    def test_podman_compose_provider_is_allowed_and_a_silent_provider_is_not_judged(self):
        self.assertEqual(self.guard("podman-compose")[0], "rc=0")
        self.assertEqual(self.guard("")[0], "rc=0")                                               # this Podman does not report a provider: nothing to judge

    def test_generated_up_sh_and_control_script_contain_the_guard_and_it_runs_before_compose(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        generate.generate_bundle(cfg_for(), d)
        up = (d / "compose" / "up.sh").read_text()
        self.assertIn("gs_podman_provider_guard || exit $?", up)
        self.assertLess(up.index("gs_podman_provider_guard || exit $?"), up.index('$COMPOSE --env-file .env up -d'))
        self.assertEqual(subprocess.run(["bash", "-n", str(d / "compose" / "up.sh")], capture_output=True).returncode, 0)
        ctl = compose.control_script(cfg_for(), "restart", ["postgis"])
        self.assertNotIn("gs_podman_provider_guard", ctl)                                       # start/stop/restart use the engine: no compose provider to check
        self.assertEqual(subprocess.run(["bash", "-n"], input=ctl, capture_output=True, text=True).returncode, 0)
        self.assertNotIn("gs_podman_provider_guard", compose.control_script(cfg_for("docker"), "restart", ["postgis"]))       # Docker is untouched

    def test_up_sh_stops_with_exit_3_before_starting_anything(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        generate.generate_bundle(cfg_for(), d)
        marker = self.td / "compose-was-run"
        env = {**os.environ, "PATH": f"{self.td / 'bin'}:{os.environ['PATH']}", "RUNTIME": "podman", "FAKE_MARKER": str(marker),
               "FAKE_PROVIDER": "/usr/libexec/docker/cli-plugins/docker-compose"}
        r = subprocess.run(["bash", "up.sh", "up"], cwd=d / "compose", capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("Nothing was started", r.stderr)
        self.assertFalse(marker.exists(), "`podman compose up` must not run when the provider cannot work")
        import socket as sk
        srv = sk.socket(sk.AF_UNIX)
        self.addCleanup(srv.close)
        srv.bind(str(self.td / "p.sock"))
        env["FAKE_SOCKET"] = str(self.td / "p.sock")                                                # with a socket the same script proceeds to compose
        r = subprocess.run(["bash", "up.sh", "up"], cwd=d / "compose", capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(marker.exists())


if __name__ == "__main__":
    unittest.main()
