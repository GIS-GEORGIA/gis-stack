"""Native PostgreSQL readiness probe against a REAL PostgreSQL (audit 2026-11 backlog): the generated `pg_ready` shell function runs inside a real
postgis container (which has bash and pg_isready).  It must say "open" for the running server, "closed" for a closed port, and - the point of the
change - "closed" for a listener that accepts the TCP connection but never speaks the PostgreSQL protocol (what a bare port check calls healthy).
Opt-in: GISSTACK_TEST_INTEGRATION=1 (Docker)."""
from __future__ import annotations

import os
import shutil
import subprocess
import time
import unittest
import uuid

from gisstack.providers import common

ENABLED = os.environ.get("GISSTACK_TEST_INTEGRATION") == "1" and shutil.which("docker") is not None
IMAGE = "postgis/postgis:17-3.5"


def dx(name, script, timeout=120):
    return subprocess.run(["docker", "exec", name, "bash", "-c", script], capture_output=True, text=True, timeout=timeout)


@unittest.skipUnless(ENABLED, "set GISSTACK_TEST_INTEGRATION=1 (needs Docker)")
class PgReadyAgainstRealPostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = "gs-pgready-" + uuid.uuid4().hex[:8]
        r = subprocess.run(["docker", "run", "-d", "--name", cls.name, "-e", "POSTGRES_PASSWORD=x", IMAGE], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        for _ in range(60):
            if dx(cls.name, "pg_isready -q -h 127.0.0.1").returncode == 0:
                break
            time.sleep(2)
        else:
            raise AssertionError("postgres did not start")

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["docker", "rm", "-f", cls.name], capture_output=True)

    def ready(self, port):
        r = dx(self.name, common.PG_READY_FN + f"\npg_ready {port}\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_running_server_is_open(self):
        self.assertEqual(self.ready(5432), "open")

    def test_closed_port_is_closed(self):
        self.assertEqual(self.ready(5999), "closed")

    def test_listener_that_accepts_but_never_answers_is_closed(self):
        dx(self.name, "nohup perl -MIO::Socket::INET -e '$s=IO::Socket::INET->new(Listen=>5,LocalAddr=>\"127.0.0.1\",LocalPort=>5998,ReuseAddr=>1) or die; "
                      "while($c=$s->accept){sleep 60}' >/dev/null 2>&1 &")
        time.sleep(1)
        self.assertEqual(dx(self.name, "(exec 3<>/dev/tcp/127.0.0.1/5998) 2>/dev/null && echo tcp-open || echo tcp-closed").stdout.strip(), "tcp-open",
                         "the stand-in must be a listener that a port check accepts")
        self.assertEqual(self.ready(5998), "closed")


if __name__ == "__main__":
    unittest.main()
