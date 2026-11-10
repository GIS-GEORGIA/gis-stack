"""Phase 1b / S-06, S-10: sessions, rate limit, Host allow-list, size limit, audit log, bounded jobs."""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gisstack.webgui import security

try:
    import flask  # noqa: F401
    HAVE_FLASK = True
except ImportError:
    HAVE_FLASK = False


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class SessionTests(unittest.TestCase):
    def test_idle_and_absolute_expiry_and_revoke(self):
        clock = FakeClock()
        s = security.Sessions(idle=100, absolute=1000, clock=clock)
        sid = s.create()
        self.assertTrue(s.valid(sid))
        clock.t += 90
        self.assertTrue(s.valid(sid))                  # activity extends the idle window
        clock.t += 90
        self.assertTrue(s.valid(sid))
        clock.t += 101
        self.assertFalse(s.valid(sid))                 # idle too long
        sid2 = s.create()
        for _ in range(12):
            clock.t += 90
            s.valid(sid2)
        self.assertFalse(s.valid(sid2))                # absolute lifetime reached although always active
        sid3 = s.create()
        s.revoke(sid3)
        self.assertFalse(s.valid(sid3))
        self.assertFalse(s.valid("") or s.valid("nope"))

    def test_ids_are_unguessable_and_unique(self):
        s = security.Sessions()
        ids = {s.create() for _ in range(50)}
        self.assertEqual(len(ids), 50)
        self.assertTrue(all(len(i) >= 40 for i in ids))


class RateLimiterTests(unittest.TestCase):
    def test_blocks_after_n_failures_then_recovers(self):
        clock = FakeClock()
        r = security.RateLimiter(max_failures=3, window=60, clock=clock)
        for _ in range(3):
            self.assertFalse(r.blocked("1.2.3.4"))
            r.fail("1.2.3.4")
        self.assertTrue(r.blocked("1.2.3.4"))
        self.assertFalse(r.blocked("5.6.7.8"))         # per address
        clock.t += 61
        self.assertFalse(r.blocked("1.2.3.4"))


class HostTests(unittest.TestCase):
    def test_allow_list(self):
        for h in ("localhost", "localhost:8765", "127.0.0.1:9", "[::1]:8765", "LOCALHOST"):
            self.assertTrue(security.host_allowed(h), h)
        for h in ("evil.example.com", "evil.example.com:8765", "127.0.0.1.evil.com", "", "localhost.evil.com"):
            self.assertFalse(security.host_allowed(h), h)
        self.assertTrue(security.host_allowed("gis.example.ge:8765", ("gis.example.ge",)))
        self.assertTrue(security.host_allowed("anything", ("*",)))

    def test_summary_keeps_only_safe_facts(self):
        body = {"config": {"project": "p", "domain": "d.ge", "postgres": {"password": "TOPSECRET99"}}, "mode": "ssh", "components": ["caddy"],
                "ssh": {"host": "h", "user": "u", "port": 22, "key": "/home/u/.ssh/id_rsa", "password": "TOPSECRET99"}, "plan": {"x": "y"}}
        out = security.summarize(body)
        self.assertNotIn("TOPSECRET99", json.dumps(out))
        self.assertNotIn("id_rsa", json.dumps(out))
        self.assertEqual(out["ssh.host"], "h")
        self.assertEqual(out["config.project"], "p")
        self.assertEqual(out["components"], ["caddy"])
        self.assertEqual(security.summarize("junk"), {})


@unittest.skipUnless(HAVE_FLASK, "flask needed")
class AppTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        p = mock.patch.dict(os.environ, {"GISSTACK_HOME": self.td.name})
        p.start()
        self.addCleanup(p.stop)
        from gisstack.webgui.app import create_app
        self.create = create_app
        self.app = create_app("tok")
        self.c = self.app.test_client()

    def audit(self):
        p = Path(self.td.name) / "audit.log"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def test_token_becomes_a_session_cookie_that_is_not_the_token(self):
        r = self.c.get("/?token=tok")
        self.assertEqual(r.status_code, 302)
        cookie = r.headers["Set-Cookie"]
        self.assertIn("gs_session=", cookie)
        self.assertNotIn("tok;", cookie.split("gs_session=")[1][:6] + ";")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Max-Age=", cookie)
        self.assertEqual(self.c.get("/api/settings").status_code, 200)

    def test_old_style_token_cookie_is_not_accepted_any_more(self):
        self.c.set_cookie("gs_token", "tok")
        self.assertEqual(self.c.get("/api/settings").status_code, 401)

    def test_bearer_still_works_and_wrong_token_is_rejected(self):
        self.assertEqual(self.c.get("/api/settings", headers={"Authorization": "Bearer tok"}).status_code, 200)
        self.assertEqual(self.c.get("/api/settings", headers={"Authorization": "Bearer nope"}).status_code, 401)
        self.assertEqual(self.c.get("/?token=wrong").status_code, 401)

    def test_logout_revokes_the_session(self):
        self.c.get("/?token=tok")
        self.assertEqual(self.c.get("/api/settings").status_code, 200)
        self.assertEqual(self.c.post("/api/logout", headers={"X-GisStack": "1"}).status_code, 200)
        self.assertEqual(self.c.get("/api/settings").status_code, 401)

    def test_v1_logout_revokes_the_session_removes_the_cookie_and_is_audited(self):
        self.c.get("/?token=tok")
        self.assertEqual(self.c.get("/api/settings").status_code, 200)
        self.assertEqual(self.c.post("/api/v1/session/logout").status_code, 403)               # a session needs the CSRF header like every state change
        self.assertEqual(self.c.get("/api/settings").status_code, 200)
        r = self.c.post("/api/v1/session/logout", headers={"X-GisStack": "1"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["data"], {"revoked": True})
        self.assertIn("gs_session=;", r.headers["Set-Cookie"])
        self.assertEqual(self.c.get("/api/settings").status_code, 401)                        # the server-side session is gone, not only the cookie
        self.assertTrue([e for e in self.audit() if e.get("path") == "/api/v1/session/logout" and e.get("status") == 200])

    def test_v1_logout_with_a_bearer_token_revokes_nothing(self):
        r = self.c.post("/api/v1/session/logout", headers={"Authorization": "Bearer tok"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["data"], {"revoked": False})
        self.assertEqual(self.c.get("/api/v1/dashboard/summary", headers={"Authorization": "Bearer tok"}).status_code in (200, 405), True)
        self.assertEqual(self.c.get("/api/settings", headers={"Authorization": "Bearer tok"}).status_code, 200)   # the token still works
        self.assertEqual(self.c.post("/api/v1/session/logout").status_code, 401)               # unauthenticated: refused like everything else

    def test_legacy_logout_still_works_and_now_names_its_successor(self):
        self.c.get("/?token=tok")
        r = self.c.post("/api/logout", headers={"X-GisStack": "1"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("Deprecation"), "true")
        self.assertIn("/api/v1/session/logout", r.headers.get("Link", ""))
        self.assertEqual(self.c.get("/api/settings").status_code, 401)

    def test_session_expires(self):
        clock = FakeClock()
        self.app.config["SESSIONS"].clock = clock
        self.app.config["SESSIONS"].idle = 50
        self.c.get("/?token=tok")
        self.assertEqual(self.c.get("/api/settings").status_code, 200)
        clock.t += 51
        self.assertEqual(self.c.get("/api/settings").status_code, 401)

    def test_brute_force_gets_429_even_for_the_right_token(self):
        for _ in range(security.MAX_FAILURES):
            self.assertEqual(self.c.get("/?token=bad").status_code, 401)
        r = self.c.get("/?token=tok")
        self.assertEqual(r.status_code, 429)
        self.assertIn("Retry-After", r.headers)
        self.assertEqual(self.c.get("/healthz").status_code, 200)         # health probe is exempt

    def test_foreign_host_header_is_refused_and_extra_hosts_can_be_allowed(self):
        self.assertEqual(self.c.get("/healthz", headers={"Host": "evil.example.com"}).status_code, 400)
        self.assertEqual(self.c.get("/?token=tok", headers={"Host": "127.0.0.1.evil.com"}).status_code, 400)
        ok = self.create("tok", ("gis.example.ge",)).test_client()
        self.assertEqual(ok.get("/healthz", headers={"Host": "gis.example.ge:8765"}).status_code, 200)

    def test_oversized_body_is_refused(self):
        self.c.get("/?token=tok")
        r = self.c.post("/api/preview", data=b"{" + b" " * (3 * 1024 * 1024) + b"}", headers={"X-GisStack": "1", "Content-Type": "application/json"})
        self.assertEqual(r.status_code, 413)

    def test_audit_log_records_actions_but_never_secrets(self):
        self.c.get("/?token=tok")
        H = {"X-GisStack": "1"}
        body = {"config": {"project": "auditme", "postgres": {"password": "TOPSECRET99"}, "geoserver": {"admin_password": "TOPSECRET98"}},
                "mode": "ssh", "ssh": {"host": "srv", "user": "u", "port": 22, "key": "/k/id_rsa"}, "component": "nope"}
        self.c.post("/api/apply", json=body, headers=H)
        self.c.get("/api/settings")
        self.c.get("/?token=wrong")
        raw = (Path(self.td.name) / "audit.log").read_text()
        for secret in ("TOPSECRET99", "TOPSECRET98", "id_rsa", "tok"):
            self.assertNotIn(f'"{secret}', raw) if secret == "tok" else self.assertNotIn(secret, raw)
        events = self.audit()
        self.assertIn("login", [e["event"] for e in events])
        req = [e for e in events if e["event"] == "request"][0]
        self.assertEqual((req["method"], req["path"]), ("POST", "/api/apply"))
        self.assertEqual(req["config.project"], "auditme")
        self.assertEqual(req["ssh.host"], "srv")
        self.assertTrue(any(e["event"] == "denied" and e["why"] == "bad-token" for e in events))
        self.assertEqual(stat.S_IMODE((Path(self.td.name) / "audit.log").stat().st_mode), 0o600)
        self.assertFalse(any(e["path"] == "/api/settings" for e in events if e["event"] == "request"))       # GETs are not state changes

    def test_missing_csrf_header_is_denied_and_audited(self):
        self.c.get("/?token=tok")
        self.assertEqual(self.c.post("/api/settings", json={"lang": "en"}).status_code, 403)
        self.assertTrue(any(e.get("why") == "csrf-header" for e in self.audit()))

    def test_job_registry_is_bounded(self):
        from gisstack.webgui import app as appmod
        self.c.get("/?token=tok")
        with mock.patch.object(appmod, "MAX_JOBS", 3), mock.patch.object(appmod.ops_mod, "logs", return_value=iter(["x"])):
            ids = []
            for _ in range(6):
                r = self.c.post("/api/ops/logs", json={"config": {"deploy_target": "compose"}, "mode": "local", "component": "caddy"},
                                headers={"X-GisStack": "1"})
                self.assertEqual(r.status_code, 200, r.get_data())
                ids.append(r.get_json()["job"])
                for _ in range(50):
                    if self.app.config["JOBS"][ids[-1]].done:
                        break
                    import time
                    time.sleep(0.02)
            self.assertLessEqual(len(self.app.config["JOBS"]), 3)

    def test_job_log_is_capped(self):
        from gisstack.api import jobs as jobsmod
        with mock.patch.object(jobsmod, "MAX_JOB_LINES", 5):
            j = jobsmod.Job()
            for i in range(20):
                j.add(str(i))
        self.assertEqual(len(j.lines), 6)
        self.assertIn("truncated", j.lines[-1])

    def test_token_rotation_replaces_the_stored_token(self):
        from gisstack.webgui.app import load_or_create_token, rotate_token
        first = load_or_create_token()
        new = rotate_token()
        self.assertNotEqual(first, new)
        self.assertEqual(load_or_create_token(), new)
        self.assertEqual(stat.S_IMODE((Path(self.td.name) / "web.token").stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
