"""The public QGIS Server probe must tell a working QGIS from a dead FastCGI behind Apache (S-14), which a status code alone cannot: a healthy QGIS Server
without a project answers HTTP 500 *with an OGC exception document*, a broken one answers HTTP 500 with Apache's own page.  Both probe shells (deployment
verification and monitoring) run here against a real HTTP server, with real bash and real curl."""
from __future__ import annotations

import shutil
import subprocess
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from gisstack import config
from gisstack.monitoring import collect
from gisstack.providers import common

BODIES = {
    "/healthy": (500, b'<?xml version="1.0"?>\n<ServerException>Project file error. For OWS services: please provide a SERVICE and a MAP parameter</ServerException>\n'),
    "/dead": (500, b"<html><h1>Internal Server Error</h1><p>The server encountered an internal error</p></html>"),
    "/ok-html": (200, b"<html>some other application answering on that port</html>"),
    "/empty": (500, b""),
}


class H(BaseHTTPRequestHandler):
    def do_GET(self):                                                   # noqa: N802
        code, body = BODIES.get(self.path.split("?")[0], (404, b"no"))
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@unittest.skipUnless(shutil.which("bash") and shutil.which("curl"), "bash and curl needed")
class BodyProbe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), H)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}?SERVICE=WMS&REQUEST=GetCapabilities"

    def verify_code(self, path):
        chk = {"comp": "qgis_server", "label": "public", "kind": "alive", "url": self.url(path), "match": "Exception"}
        out = subprocess.run(["bash", "-c", common.probe_script([chk])], capture_output=True, text=True, timeout=30).stdout
        code = next(line.split()[2] for line in out.splitlines() if line.startswith("@@http 0 "))
        return code, common.check_ok("alive", code)

    def test_the_default_checks_ask_for_the_marker_only_where_it_applies(self):
        cfg = config.resolve({**config.default_config(), "components": {**config.default_config()["components"], "qgis_server": True}})
        by = {c["comp"]: c for c in common.checks(cfg, "compose", ["geoserver", "mapproxy", "qgis_server"])}
        self.assertEqual(by["qgis_server"].get("match"), "Exception")
        self.assertNotIn("match", by["geoserver"])
        self.assertNotIn("match", by["mapproxy"])

    def test_verification_probe(self):
        self.assertEqual(self.verify_code("/healthy"), ("500", True))          # a real QGIS answer
        self.assertEqual(self.verify_code("/dead"), ("000", False))             # Apache's 500: no QGIS behind it
        self.assertEqual(self.verify_code("/ok-html"), ("000", False))          # another application on that port
        self.assertEqual(self.verify_code("/empty"), ("000", False))

    def test_monitoring_probe(self):
        cfg = config.resolve({**config.default_config(), "components": {**config.default_config()["components"], "qgis_server": True}})
        got = {}
        for path in BODIES:
            script = collect._probe_lines(cfg, "native", ["qgis_server"])
            # point the generated public probe at the test server, keep everything else of the generated script
            head = "\n".join(line for line in script.splitlines() if line.startswith(("probe_http()", "probe_httpm()", "probe_tcp()")))
            chk = f"probe_httpm qgis_server:public alive Exception '{self.url(path)}'"
            out = subprocess.run(["bash", "-c", head + "\n" + chk], capture_output=True, text=True, timeout=30).stdout
            got[path] = collect.probe_metrics([line.split() for line in out.splitlines()])[0]
        self.assertTrue(got["/healthy"]["up"])
        self.assertEqual(got["/healthy"]["code"], "500")
        for bad in ("/dead", "/ok-html", "/empty"):
            self.assertFalse(got[bad]["up"], bad)
            self.assertEqual(got[bad]["code"], "000", bad)

    def test_the_generated_monitoring_script_uses_the_marker_function(self):
        cfg = config.resolve({**config.default_config(), "components": {**config.default_config()["components"], "qgis_server": True}})
        text = collect._probe_lines(cfg, "compose", ["qgis_server"])
        self.assertIn("probe_httpm qgis_server:public alive Exception", text)


if __name__ == "__main__":
    unittest.main()
