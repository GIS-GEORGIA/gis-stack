"""Security page: permission posture and audit trail (pure functions) and the two admin-only API routes.  Unit tested only."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gisstack import security_report as sr
from tests.test_api import ApiBase


class PostureTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        p = mock.patch.dict(os.environ, {"GISSTACK_HOME": self.td})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.td, True)
        self.home = Path(self.td)

    def by_id(self, **env):
        return {c["id"]: c for c in sr.posture(env)}

    def test_owner_only_is_ok_and_loose_modes_fail(self):
        tok = self.home / "web.token"
        tok.write_text("secret-value")
        os.chmod(tok, 0o644)
        got = self.by_id()
        self.assertEqual(got["token"]["status"], sr.FAIL)
        self.assertIn("0644", got["token"]["detail"])
        os.chmod(tok, 0o600)
        self.assertEqual(self.by_id()["token"]["status"], sr.OK)

    def test_never_reports_secret_content(self):
        (self.home / "web.token").write_text("super-secret-token-value")
        os.chmod(self.home / "web.token", 0o600)
        self.assertNotIn("super-secret-token-value", json.dumps(sr.posture({})))

    def test_missing_files_are_not_invented(self):
        self.assertNotIn("servers", self.by_id())
        self.assertNotIn("secret_files", self.by_id())

    def test_loose_secret_files_are_named(self):
        d = self.home / "secrets"
        d.mkdir(mode=0o700)
        (d / "a.json").write_text("{}")
        os.chmod(d / "a.json", 0o640)
        (d / "b.json").write_text("{}")
        os.chmod(d / "b.json", 0o600)
        c = self.by_id()["secret_files"]
        self.assertEqual(c["status"], sr.FAIL)
        self.assertIn("a.json", c["detail"])
        self.assertNotIn("b.json", c["detail"])

    def test_symlink_is_not_followed(self):
        real = self.home / "real"
        real.write_text("x")
        os.chmod(real, 0o644)
        os.symlink(real, self.home / "servers.json")
        self.assertEqual(self.by_id()["servers"]["status"], sr.WARN)

    def test_policy_flags(self):
        self.assertEqual(self.by_id()["signature"]["status"], sr.INFO)
        self.assertEqual(self.by_id(GISSTACK_REQUIRE_SIGNATURE="1")["signature"]["status"], sr.OK)
        self.assertIn("GISSTACK_TOKEN", self.by_id(GISSTACK_TOKEN="x")["token_env"]["detail"])


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.td, True)
        self.log = Path(self.td) / "audit.log"

    def write(self, lines):
        self.log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_newest_first_filter_limit_and_damage(self):
        self.write([json.dumps({"ts": 1, "event": "servers.save"}), "{not json", json.dumps([1, 2]),
                    json.dumps({"ts": 2, "event": "postgis.write"}), json.dumps({"ts": 3, "event": "postgis.write"})])
        got = sr.audit_events(10, log=self.log)
        self.assertEqual([e["ts"] for e in got], [3, 2, 1])
        self.assertEqual([e["ts"] for e in sr.audit_events(10, "postgis.", log=self.log)], [3, 2])
        self.assertEqual(len(sr.audit_events(1, log=self.log)), 1)
        self.assertEqual(len(sr.audit_events(10**6, log=self.log)), 3)           # clamped, not an error

    def test_missing_log_is_empty(self):
        self.assertEqual(sr.audit_events(log=self.log), [])

    def test_big_log_reads_only_the_tail_without_a_cut_line(self):
        rows = [json.dumps({"ts": i, "event": "e", "pad": "x" * 200}) for i in range(5000)]
        self.write(rows)
        self.assertGreater(self.log.stat().st_size, sr.TAIL_BYTES)
        got = sr.audit_events(sr.MAX_EVENTS, log=self.log)
        self.assertEqual(got[0]["ts"], 4999)
        self.assertEqual(len(got), sr.MAX_EVENTS)


class SecurityApiTests(ApiBase):
    def test_admin_only(self):
        for path in ("/security/posture", "/security/audit"):
            self.assertEqual(self.call("GET", path, self.tokens["read"]).status_code, 403, path)
            self.assertEqual(self.call("GET", path, self.tokens["operate"]).status_code, 403, path)
            self.assertEqual(self.call("GET", path, self.tokens["admin"]).status_code, 200, path)

    def test_contract_and_real_audit_trail(self):
        posture = self.data(self.call("GET", "/security/posture"))
        self.conforms("/security/posture", "GET", posture)
        self.assertTrue(any(c["id"] == "home" and c["status"] == "ok" for c in posture))
        self.app.config["AUDIT"].write(event="servers.save", principal="p", scope="admin", host="h")
        rows = self.data(self.call("GET", "/security/audit?event=servers."))
        self.assertEqual(rows[0]["event"], "servers.save")
        self.assertEqual(self.call("GET", "/security/audit?limit=0").status_code, 400)
        self.assertEqual(self.call("GET", "/security/audit?bogus=1").status_code, 400)


if __name__ == "__main__":
    unittest.main()
