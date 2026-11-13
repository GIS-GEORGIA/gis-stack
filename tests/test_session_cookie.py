"""S-7: the session cookie is HttpOnly + SameSite=Strict always, and Secure exactly when the browser really talks HTTPS - directly, or through a reverse
proxy that is TRUSTED (GISSTACK_TRUSTED_PROXIES).  A forwarded header from anybody else is ignored.  The documented local workflow (plain http on loopback,
usually through an SSH tunnel) must keep working: there the cookie must NOT be Secure (a browser would refuse to keep it)."""
import os
import tempfile
import unittest
from unittest import mock

from gisstack.webgui import security

try:
    import flask  # noqa: F401
    HAVE_FLASK = True
except ImportError:
    HAVE_FLASK = False

TOKEN = "tok-" + "x" * 40


class HelperTests(unittest.TestCase):
    def test_scheme_and_trusted_forwarded_proto(self):
        f = security.request_is_https
        self.assertTrue(f("https", "203.0.113.9", "", ()))                                   # TLS terminated by the app's own server
        self.assertFalse(f("http", "127.0.0.1", "https", ()))                                # nobody is trusted by default
        self.assertTrue(f("http", "127.0.0.1", "https", ("127.0.0.1",)))
        self.assertTrue(f("http", "127.0.0.1", "HTTPS, http", ("127.0.0.1",)))               # the first (client-facing) hop decides
        self.assertFalse(f("http", "127.0.0.1", "http", ("127.0.0.1",)))
        self.assertFalse(f("http", "10.0.0.5", "https", ("127.0.0.1",)))                     # not the trusted proxy's address
        self.assertFalse(f("http", "", "https", ("",)))

    def test_trusted_proxies_come_from_the_environment_only(self):
        with mock.patch.dict(os.environ, {"GISSTACK_TRUSTED_PROXIES": " 127.0.0.1 , 10.1.1.1,, "}):
            self.assertEqual(security.trusted_proxies_from_env(), ("127.0.0.1", "10.1.1.1"))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GISSTACK_TRUSTED_PROXIES", None)
            self.assertEqual(security.trusted_proxies_from_env(), ())


@unittest.skipUnless(HAVE_FLASK, "flask needed")
class CookieTests(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        p = mock.patch.dict(os.environ, {"GISSTACK_HOME": td.name})
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("GISSTACK_TRUSTED_PROXIES", None)

    def client(self, trusted=None):
        if trusted is not None:
            os.environ["GISSTACK_TRUSTED_PROXIES"] = trusted
        from gisstack.webgui.app import create_app
        return create_app(TOKEN).test_client()

    def login(self, c, **kw):
        r = c.get(f"/?token={TOKEN}", **kw)
        self.assertEqual(r.status_code, 302)
        return r.headers["Set-Cookie"]

    def test_plain_http_on_loopback_the_documented_workflow_gets_httponly_samesite_and_no_secure(self):
        c = self.client()
        cookie = self.login(c)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertNotIn("Secure", cookie)
        self.assertEqual(c.get("/api/settings").status_code, 200)                           # the cookie is kept and used: the tunnel flow works

    def test_an_untrusted_forwarded_proto_cannot_switch_secure_on(self):
        c = self.client()
        cookie = self.login(c, headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.1"})
        self.assertNotIn("Secure", cookie)
        self.assertEqual(c.get("/api/settings").status_code, 200)                           # and the login still works over plain http

    def test_behind_a_trusted_proxy_the_cookie_is_secure(self):
        c = self.client(trusted="127.0.0.1")                                                # the werkzeug test client connects from 127.0.0.1
        cookie = self.login(c, headers={"X-Forwarded-Proto": "https"})
        for flag in ("Secure", "HttpOnly", "SameSite=Strict"):
            self.assertIn(flag, cookie)

    def test_a_trusted_proxy_that_says_http_gets_a_plain_cookie(self):
        c = self.client(trusted="127.0.0.1")
        self.assertNotIn("Secure", self.login(c, headers={"X-Forwarded-Proto": "http"}))
        self.assertNotIn("Secure", self.login(self.client(trusted="127.0.0.1")))             # no header at all

    def test_only_the_configured_address_is_trusted(self):
        c = self.client(trusted="10.9.9.9")
        self.assertNotIn("Secure", self.login(c, headers={"X-Forwarded-Proto": "https"}))

    def test_direct_https_is_secure(self):
        c = self.client()
        self.assertIn("Secure", self.login(c, base_url="https://localhost"))

    def test_logout_expires_the_cookie_with_matching_flags(self):
        c = self.client(trusted="127.0.0.1")
        self.login(c, headers={"X-Forwarded-Proto": "https"})
        r = c.post("/api/v1/session/logout", headers={"X-GisStack": "1", "X-Forwarded-Proto": "https"}, base_url="https://localhost")
        self.assertEqual(r.status_code, 200)
        gone = r.headers["Set-Cookie"]
        self.assertIn("gs_session=;", gone)
        self.assertIn("Secure", gone)
        self.assertIn("HttpOnly", gone)

    def test_forwarded_host_never_widens_the_host_allow_list(self):
        c = self.client(trusted="127.0.0.1")
        r = c.get("/healthz", headers={"Host": "evil.example", "X-Forwarded-Host": "localhost", "X-Forwarded-Proto": "https"})
        self.assertEqual(r.status_code, 400)                                                # the Host header alone decides (DNS-rebinding protection)


if __name__ == "__main__":
    unittest.main()
