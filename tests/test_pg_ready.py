"""Native PostgreSQL readiness probe (backlog item from the 2026-11 audit): `pg_isready` when it exists, the port check only as a fallback.
Pure bash with a stub `pg_isready` first on PATH; the real-PostgreSQL check is tests/integration/test_pg_ready.py."""
import os
import shlex
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from gisstack import config
from gisstack.monitoring import collect
from gisstack.providers import common


TOOLS = ("ls", "tail", "awk", "date", "bash")


def sandbox():
    """A PATH that holds ONLY the few tools the probe needs - never the machine's own pg_isready (GitHub's runners ship PostgreSQL, so /usr/bin/pg_isready exists there)."""
    d = tempfile.mkdtemp()
    for t in TOOLS:
        os.symlink(shutil.which(t), os.path.join(d, t))
    return d


def run(script, path_dir=None):
    box = sandbox()
    try:
        env = {"PATH": f"{path_dir}:{box}" if path_dir else box, "HOME": "/tmp"}
        return subprocess.run([shutil.which("bash"), "-c", script], capture_output=True, text=True, env=env, timeout=60).stdout
    finally:
        shutil.rmtree(box, True)


def stub(directory, code, name="pg_isready"):
    p = Path(directory) / name
    p.write_text(f"#!/bin/sh\necho \"$@\" > {shlex.quote(str(Path(directory) / 'args'))}\nexit {code}\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)


class Checks(unittest.TestCase):
    def test_native_postgis_is_a_readiness_check_not_a_port_check(self):
        cfg = config.resolve(config.default_config())
        pg = [c for c in common.checks(cfg, "native", ["postgis"]) if c["comp"] == "postgis"]
        self.assertEqual([c["kind"] for c in pg], ["pgready"])
        self.assertTrue(common.check_ok("pgready", "open"))
        self.assertFalse(common.check_ok("pgready", "closed"))

    def test_qgis_fastcgi_keeps_the_port_check(self):
        cfg = config.resolve(config.default_config())
        self.assertIn("tcp", [c["kind"] for c in common.checks(cfg, "native", ["qgis_server"])])


class Shell(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)

    def deploy_probe(self, port="5432"):
        cfg = config.resolve(config.default_config())
        cfg["postgres"]["port"] = int(port)
        return common.probe_script([c for c in common.checks(cfg, "native", ["postgis"]) if c["comp"] == "postgis"])

    def test_server_answering_is_open(self):
        stub(self.tmp, 0)
        self.assertEqual(run(self.deploy_probe(), self.tmp).strip(), "@@tcp 0 open")
        self.assertIn("-h 127.0.0.1 -p 5432", (Path(self.tmp) / "args").read_text())

    def test_rejecting_or_silent_server_is_closed_even_if_the_port_is_open(self):
        for rc in (1, 2, 3):                                      # 1 = starting up / recovery, 2 = no response, 3 = no attempt
            stub(self.tmp, rc)
            self.assertEqual(run(self.deploy_probe(), self.tmp).strip(), "@@tcp 0 closed", rc)

    def test_without_the_binary_it_degrades_to_the_port_check_on_a_closed_port(self):
        self.assertEqual(run(self.deploy_probe("1"), None).strip(), "@@tcp 0 closed")

    def test_monitor_probe_line_has_the_shape_the_parser_reads(self):
        stub(self.tmp, 0)
        cfg = config.resolve(config.default_config())
        out = run(collect._probe_lines(cfg, "native", ["postgis"]), self.tmp)
        line = [x for x in out.splitlines() if x.startswith("probe ")][0].split()
        self.assertEqual((line[0], line[1], line[2], line[4]), ("probe", "postgis:ready_5432", "open", "tcp"))
        float(line[3])

    def test_binary_found_below_the_debian_and_rpm_directories(self):
        d = Path(self.tmp) / "usr" / "pgsql-16" / "bin"
        d.mkdir(parents=True)
        stub(d, 0)
        script = common.PG_READY_FN + "\necho \"@@tcp 0 $(pg_ready 5432)\"\n"
        script = script.replace("/usr/pgsql-*/bin/pg_isready", f"{self.tmp}/usr/pgsql-*/bin/pg_isready").replace("/usr/lib/postgresql/*/bin/pg_isready", f"{self.tmp}/none/*/bin/pg_isready")     # GitHub's runners have the real Debian path
        self.assertEqual(run(script).strip(), "@@tcp 0 open")


if __name__ == "__main__":
    unittest.main()
