"""`gisstack web` startup: the production server is the default when available, and the access token never goes to a log.  Unit tested."""
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

try:
    from gisstack.webgui import app as webapp
    HAVE_FLASK = True
except ImportError:
    HAVE_FLASK = False


class Out(io.StringIO):
    def __init__(self, tty):
        super().__init__()
        self.tty = tty

    def isatty(self):
        return self.tty


@unittest.skipUnless(HAVE_FLASK, "flask needed")
class StartupTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.td, True)
        p = mock.patch.dict(os.environ, {"GISSTACK_HOME": self.td, "GISSTACK_TOKEN": "", "GISSTACK_PRINT_TOKEN": ""})
        p.start()
        self.addCleanup(p.stop)
        self.flask_app = mock.MagicMock()
        self.flask_app.config = {"MAX_CONTENT_LENGTH": 1000}
        pc = mock.patch.object(webapp, "create_app", return_value=self.flask_app)
        pc.start()
        self.addCleanup(pc.stop)

    def run_main(self, server="auto", tty=False, waitress="present"):
        out = Out(tty)
        fake = mock.MagicMock()
        mods = {"waitress": fake} if waitress == "present" else {"waitress": None}
        with mock.patch.dict(sys.modules, mods), mock.patch.object(sys, "stdout", out):
            webapp.main("127.0.0.1", 8765, server, (), False)
        return out.getvalue(), fake

    def test_auto_uses_waitress_when_it_is_installed(self):
        out, fake = self.run_main("auto")
        fake.serve.assert_called_once()
        self.flask_app.run.assert_not_called()
        self.assertIn("waitress production server", out)
        self.assertNotIn("WARNING", out)

    def test_auto_falls_back_to_the_development_server_with_a_warning(self):
        out, _ = self.run_main("auto", waitress="missing")
        self.flask_app.run.assert_called_once()
        self.assertIn("WARNING: waitress is not installed", out)

    def test_explicit_choices_are_respected(self):
        _, fake = self.run_main("werkzeug")
        self.flask_app.run.assert_called_once()
        fake.serve.assert_not_called()
        with self.assertRaises(SystemExit) as cm:
            self.run_main("waitress", waitress="missing")
        self.assertIn("waitress is not installed", str(cm.exception))

    def test_the_access_token_is_printed_only_to_a_terminal(self):
        token = webapp.load_or_create_token()
        out, _ = self.run_main("werkzeug", tty=True)
        self.assertIn(f"?token={token}", out)
        out, _ = self.run_main("werkzeug", tty=False)                       # a journal / log file / pipe
        self.assertNotIn(token, out)
        self.assertIn("not printed", out)
        self.assertIn("web.token", out)
        with mock.patch.dict(os.environ, {"GISSTACK_PRINT_TOKEN": "1"}):
            out, _ = self.run_main("werkzeug", tty=False)
        self.assertIn(f"?token={token}", out)                               # explicit opt-in (used by the browser tests)


if __name__ == "__main__":
    unittest.main()
