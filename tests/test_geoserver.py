"""Phase 4: GeoServer manager - fake gateway, the real curl gateway against a local HTTP server, API routes, and (opt-in) a real GeoServer container."""
import base64
import http.server
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from gisstack import transport
from gisstack.core.errors import DeployError
from gisstack.geoserver import gateway as gw_mod, manager as gsm
from gisstack.geoserver.gateway import CurlGateway, Gateway, Response

HAVE_FLASK = __import__("importlib").util.find_spec("flask") is not None
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAfbLI3wAAAABJRU5ErkJggg==")


def js(obj, status=200):
    return Response(status, {"content-type": "application/json"}, json.dumps(obj).encode())


class Fake(Gateway):
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def request(self, method, path, *, query=None, body=None, content_type="", accept="", timeout=30):
        self.calls.append((method, path, query, body))
        r = self.routes.get((method, path))
        if r is None:
            return Response(404, {}, b"no such thing")
        return r(query, body) if callable(r) else r


WS = js({"workspaces": {"workspace": [{"name": "topp"}, {"name": "empty"}]}})
FULL = {
    ("GET", "/rest/about/version.json"): js({"about": {"resource": [{"@name": "GeoServer", "Version": "2.28.6"}, {"@name": "GeoTools", "Version": "34"}]}}),
    ("GET", "/rest/workspaces.json"): WS,
    ("GET", "/rest/workspaces/topp/datastores.json"): js({"dataStores": {"dataStore": [{"name": "pg"}]}}),
    ("GET", "/rest/workspaces/empty/datastores.json"): js({"dataStores": ""}),
    ("GET", "/rest/layers.json"): js({"layers": {"layer": {"name": "topp:roads"}}}),         # a single item is an object, not a list
    ("GET", "/rest/layergroups.json"): js({"layerGroups": ""}),
    ("GET", "/rest/styles.json"): js({"styles": {"style": [{"name": "line"}, {"name": "polygon"}]}}),
}
WMS = b"""<?xml version="1.0"?><WMS_Capabilities version="1.3.0" xmlns="http://www.opengis.net/wms"><Capability><Request><GetMap><Format>image/png</Format><Format>image/jpeg</Format></GetMap></Request>
<Layer><Title>root</Title><Layer><Name>topp:roads</Name><Title>Roads</Title></Layer><Layer><Name>topp:rivers</Name></Layer></Layer></Capability></WMS_Capabilities>"""


class InventoryTests(unittest.TestCase):
    def test_inventory_handles_geoservers_json_quirks(self):
        inv = gsm.inventory(Fake(FULL))
        self.assertEqual(inv["errors"], {})
        self.assertEqual(inv["version"], {"GeoServer": "2.28.6", "GeoTools": "34"})
        self.assertEqual([w["name"] for w in inv["workspaces"]], ["topp", "empty"])
        self.assertEqual(inv["datastores"], [{"name": "pg", "workspace": "topp"}])
        self.assertEqual(inv["layers"], [{"name": "topp:roads", "workspace": "topp", "title": "roads"}])
        self.assertEqual(inv["layergroups"], [])
        self.assertEqual([s["name"] for s in inv["styles"]], ["line", "polygon"])

    def test_one_failing_listing_does_not_hide_the_rest(self):
        routes = dict(FULL)
        routes[("GET", "/rest/styles.json")] = Response(500, {}, b"boom")
        inv = gsm.inventory(Fake(routes))
        self.assertIn("styles", inv["errors"])
        self.assertEqual(len(inv["layers"]), 1)

    def test_wrong_credentials_stop_early_with_a_clear_message(self):
        g = Fake({("GET", "/rest/about/version.json"): Response(401, {}, b"")})
        inv = gsm.inventory(g)
        self.assertIn("not allowed", inv["errors"]["version"])
        self.assertLessEqual(len(g.calls), 2)                                  # did not hammer GeoServer with the same bad credentials

    def test_error_mapping(self):
        for status, code in ((401, "forbidden"), (403, "forbidden"), (404, "not_found"), (409, "conflict"), (400, "bad_request"), (500, "upstream"), (503, "upstream")):
            with self.assertRaises(gsm.GeoServerError) as cm:
                gsm.workspaces(Fake({("GET", "/rest/workspaces.json"): Response(status, {}, b"<html>x</html>")}))
            self.assertEqual((cm.exception.code, cm.exception.status), (code, status))

    def test_non_json_answer_is_an_upstream_error(self):
        with self.assertRaises(gsm.GeoServerError) as cm:
            gsm.workspaces(Fake({("GET", "/rest/workspaces.json"): Response(200, {}, b"<html>login</html>")}))
        self.assertEqual(cm.exception.code, "upstream")

    def test_names_are_validated_before_anything_is_sent(self):
        g = Fake(FULL)
        for bad in ("../x", "a/b", "a b", "", "x" * 80, "a;b", "a?b", "%2e%2e", "a\nb"):
            with self.assertRaises(gsm.GeoServerError, msg=bad):
                gsm.datastores(g, bad)
            with self.assertRaises(gsm.GeoServerError, msg=bad):
                gsm.layer_detail(g, bad)
        for bad in ("a:b:c", ":x", "x:", "ws:../y"):
            with self.assertRaises(gsm.GeoServerError, msg=bad):
                gsm.layer_detail(g, bad)
        self.assertEqual(g.calls, [])

    def test_layer_detail(self):
        g = Fake({("GET", "/rest/layers/topp:roads.json"): js({"layer": {"name": "roads", "type": "VECTOR", "enabled": True, "defaultStyle": {"name": "line"},
                                                                         "resource": {"name": "topp:roads", "@class": "featureType"}}})})
        d = gsm.layer_detail(g, "topp:roads")
        self.assertEqual((d["type"], d["default_style"], d["resource_type"]), ("VECTOR", "line", "featureType"))


class CapabilitiesTests(unittest.TestCase):
    def test_wms(self):
        c = gsm.parse_capabilities(WMS, "WMS")
        self.assertEqual([x["name"] for x in c["layers"]], ["topp:roads", "topp:rivers"])
        self.assertEqual(c["layers"][0]["title"], "Roads")
        self.assertEqual((c["version"], c["formats"]), ("1.3.0", ["image/jpeg", "image/png"]))

    def test_wfs_and_wmts(self):
        wfs = b'<wfs:WFS_Capabilities xmlns:wfs="http://www.opengis.net/wfs/2.0" version="2.0.0"><FeatureTypeList><FeatureType><Name>topp:roads</Name><Title>R</Title></FeatureType></FeatureTypeList></wfs:WFS_Capabilities>'
        self.assertEqual(gsm.parse_capabilities(wfs, "WFS")["layers"][0]["name"], "topp:roads")
        wmts = b'<Capabilities xmlns="http://www.opengis.net/wmts/1.0" xmlns:ows="http://www.opengis.net/ows/1.1" version="1.0.0"><Contents><Layer><ows:Title>T</ows:Title><ows:Identifier>topp:roads</ows:Identifier></Layer></Contents></Capabilities>'
        self.assertEqual(gsm.parse_capabilities(wmts, "WMTS")["layers"], [{"name": "topp:roads", "title": "T"}])

    def test_hostile_xml_is_refused(self):
        bomb = b'<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]><WMS_Capabilities>&b;</WMS_Capabilities>'
        xxe = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><WMS_Capabilities>&e;</WMS_Capabilities>'
        for doc in (bomb, xxe, b"<WMS_Capabilities><!ENTITY x 'y'></WMS_Capabilities>"):
            with self.assertRaises(gsm.GeoServerError):
                gsm.parse_capabilities(doc, "WMS")
        with self.assertRaises(gsm.GeoServerError):
            gsm.parse_capabilities(b"not xml", "WMS")

    def test_service_exception_is_reported(self):
        with self.assertRaises(gsm.GeoServerError) as cm:
            gsm.parse_capabilities(b"<ServiceExceptionReport><ServiceException>layer gone</ServiceException></ServiceExceptionReport>", "WMS")
        self.assertIn("layer gone", str(cm.exception))

    def test_fetch_uses_the_right_endpoint(self):
        g = Fake({("GET", "/ows"): Response(200, {}, WMS), ("GET", "/gwc/service/wmts"): Response(500, {}, b"")})
        self.assertEqual(len(gsm.capabilities(g, "WMS")["layers"]), 2)
        self.assertEqual(g.calls[0][2]["service"], "WMS")
        with self.assertRaises(gsm.GeoServerError):
            gsm.capabilities(g, "WMTS")
        with self.assertRaises(gsm.GeoServerError):
            gsm.capabilities(g, "CSW")


class PreviewTests(unittest.TestCase):
    def png(self, q, b):
        return Response(200, {"content-type": "image/png"}, PNG)

    def test_good_request_is_proxied(self):
        g = Fake({("GET", "/ows"): self.png})
        r = gsm.preview(g, "topp:roads", "-180,-90,180,90", 256, 128)
        self.assertEqual((r.content_type, r.body[:4]), ("image/png", b"\x89PNG"))
        q = g.calls[0][2]
        self.assertEqual((q["request"], q["layers"], q["width"], q["height"], q["srs"], q["format"]), ("GetMap", "topp:roads", "256", "128", "EPSG:4326", "image/png"))

    def test_bad_parameters_never_reach_geoserver(self):
        g = Fake({("GET", "/ows"): self.png})
        good = ("topp:roads", "0,0,1,1", 100, 100)
        cases = [(("a/../b", "0,0,1,1", 100, 100), {}), (("x&request=GetFeature", "0,0,1,1", 100, 100), {}), (good, {"fmt": "image/svg+xml"}), (good, {"fmt": "text/html"}),
                 ((good[0], good[1], 10, 100), {}), ((good[0], good[1], 100, 5000), {}), ((good[0], "0,0,1", 100, 100), {}), ((good[0], "1,1,0,0", 100, 100), {}),
                 ((good[0], "a,b,c,d", 100, 100), {}), (good, {"srs": "EPSG:4326&x=1"}), (good, {"srs": "CRS:84"}), (good, {"style": "a&b"})]
        for args, kw in cases:
            with self.assertRaises(gsm.GeoServerError, msg=(args, kw)):
                gsm.preview(g, *args, **kw)
        self.assertEqual(g.calls, [])

    def test_an_error_document_is_not_passed_off_as_an_image(self):
        xml = Response(200, {"content-type": "application/vnd.ogc.se_xml"}, b"<ServiceExceptionReport><ServiceException>no layer</ServiceException></ServiceExceptionReport>")
        with self.assertRaises(gsm.GeoServerError) as cm:
            gsm.preview(Fake({("GET", "/ows"): xml}), "x", "0,0,1,1", 64, 64)
        self.assertIn("no layer", str(cm.exception))
        html = Response(200, {"content-type": "text/html"}, b"<script>alert(1)</script>")
        with self.assertRaises(gsm.GeoServerError) as cm:
            gsm.preview(Fake({("GET", "/ows"): html}), "x", "0,0,1,1", 64, 64)
        self.assertNotIn("script", str(cm.exception))


class HealthAndChangeTests(unittest.TestCase):
    def test_health_each_check_stands_alone(self):
        routes = dict(FULL)
        routes[("GET", "/ows")] = Response(200, {}, WMS)
        h = gsm.health(Fake(routes))
        self.assertTrue(h["ok"])
        self.assertEqual((h["version"]["GeoServer"], h["workspaces"]), ("2.28.6", 2))
        bad = gsm.health(Fake({("GET", "/rest/about/version.json"): Response(401, {}, b""), ("GET", "/rest/workspaces.json"): Response(401, {}, b"")}))
        self.assertFalse(bad["ok"])
        self.assertEqual([c["ok"] for c in bad["checks"]], [False, False, False])

    def test_validate_findings(self):
        issues = gsm.validate(Fake(FULL))
        self.assertIn(("info", "workspace empty"), [(i["level"], i["what"]) for i in issues])
        routes = dict(FULL)
        routes[("GET", "/rest/layers.json")] = js({"layers": ""})
        issues = gsm.validate(Fake(routes))
        self.assertIn(("warn", "workspace topp"), [(i["level"], i["what"]) for i in issues])
        self.assertIn("nothing is published", " ".join(i["message"] for i in issues))

    def test_create_and_delete_workspace(self):
        g = Fake({("POST", "/rest/workspaces"): Response(201, {}, b""), ("DELETE", "/rest/workspaces/topp"): Response(200, {}, b"")})
        self.assertEqual(gsm.create_workspace(g, "new_ws"), {"name": "new_ws"})
        self.assertEqual(json.loads(g.calls[0][3]), {"workspace": {"name": "new_ws"}})
        gsm.delete_workspace(g, "topp", recurse=True)
        self.assertEqual(g.calls[1][2], {"recurse": "true"})
        with self.assertRaises(gsm.GeoServerError) as cm:
            gsm.create_workspace(Fake({("POST", "/rest/workspaces"): Response(409, {}, b"Workspace 'x' already exists")}), "x")
        self.assertEqual(cm.exception.code, "conflict")
        with self.assertRaises(gsm.GeoServerError):
            gsm.delete_workspace(g, "../etc")
        with self.assertRaises(gsm.GeoServerError) as cm:                       # GeoServer refuses non-empty workspaces without recurse
            gsm.delete_workspace(Fake({("DELETE", "/rest/workspaces/topp"): Response(403, {}, b"not empty")}), "topp")
        self.assertEqual(cm.exception.code, "forbidden")


class GatewayScriptTests(unittest.TestCase):
    def test_secret_goes_to_stdin_not_into_the_command(self):
        seen = {}

        def fake_run(mode, ssh, script, *, root=False, timeout=40, input_text=None):
            seen.update(script=script, stdin=input_text, mode=mode)
            return "RC 0\nSTATUS 200\nHDR Content-Type: application/json\nERR \nBODY " + base64.b64encode(b"{}").decode() + "\n"
        g = CurlGateway("ssh", {"host": "h", "user": "u"}, 8080, "admin", 'p"w\\d\n!')
        with mock.patch.object(transport, "run", fake_run):
            r = g.request("POST", "/rest/workspaces", body=b'{"a":"@/etc/passwd"}', content_type="application/json")
        self.assertEqual((r.status, r.content_type, r.body), (200, "application/json", b"{}"))
        self.assertNotIn("p\"w", seen["script"])
        self.assertNotIn("admin", seen["script"])
        self.assertIn('user = "admin:p\\"w\\\\d\\n!"', seen["stdin"])
        self.assertIn("data-raw = ", seen["stdin"])                                # never data-binary: '@file' would read a local file
        self.assertIn("url = \"http://127.0.0.1:8080/geoserver/rest/workspaces\"", seen["stdin"])

    def test_bad_paths_rejected(self):
        g = CurlGateway("local", None, 8080, "a", "b")
        for p in ("rest", "/rest/../x", "/rest?x=1", "/a#b", "/a\\b"):
            with self.assertRaises(DeployError, msg=p):
                g.request("GET", p)

    def test_curl_failures_are_readable_and_never_echo_the_password(self):
        for rc, text in ((7, "connection refused"), (28, "timed out"), (63, "too large")):
            with self.assertRaises(DeployError) as cm:
                gw_mod.parse_output(f"RC {rc}\nSTATUS 000\nERR curl: (7) Failed to connect with secretpw\nBODY \n", "secretpw")
            self.assertIn(text, str(cm.exception))
            self.assertNotIn("secretpw", str(cm.exception))

    def test_config_requirements(self):
        base = {"deploy_target": "native", "project": "p", "tomcat": {"port": 8081}, "k8s": {"namespace": "ns"}, "geoserver": {"admin_user": "admin", "admin_password": "x"}}
        self.assertEqual(gw_mod.for_config(base, "local", None).port, 8081)
        for target in ("compose", "k8s"):                       # every target is supported; inside a container GeoServer always listens on 8080
            gw = gw_mod.for_config({**base, "deploy_target": target}, "local", None)
            self.assertEqual((gw.port, gw.place.kind), (8080, target))
        with self.assertRaises(DeployError):
            gw_mod.for_config({**base, "deploy_target": "mainframe"}, "local", None)
        with self.assertRaises(DeployError):
            gw_mod.for_config({**base, "geoserver": {"admin_user": "admin", "admin_password": ""}}, "local", None)


class _Handler(http.server.BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def _answer(self):
        auth = self.headers.get("Authorization", "")
        _Handler.seen.append((self.command, self.path, auth, self.rfile.read(int(self.headers.get("Content-Length") or 0)), self.headers.get("X-GisStack")))
        if auth != "Basic " + base64.b64encode(b"admin:s3cr3t").decode():
            self.send_response(401)
            self.end_headers()
            return
        if self.path.startswith("/geoserver/ows"):
            body, ct = PNG, "image/png"
        else:
            body, ct = json.dumps({"about": {"resource": [{"@name": "GeoServer", "Version": "9.9"}]}}).encode(), "application/json"
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    do_GET = do_POST = do_DELETE = _answer


@unittest.skipUnless(shutil.which("curl") and shutil.which("base64"), "curl needed")
class RealCurlGatewayTests(unittest.TestCase):
    """The actual shell script, executed locally against a loopback HTTP server that checks the Basic credentials."""
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.port = cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        _Handler.seen.clear()

    def test_round_trip_with_credentials_and_binary_body(self):
        g = CurlGateway("local", None, self.port, "admin", "s3cr3t")
        self.assertEqual(gsm.version(g), {"GeoServer": "9.9"})
        r = gsm.preview(g, "topp:roads", "0,0,1,1", 64, 64)
        self.assertEqual(r.body, PNG)                                              # binary survives base64 transport
        self.assertTrue(all(c[2].startswith("Basic ") for c in _Handler.seen))
        self.assertEqual(_Handler.seen[0][4], "1")

    def test_wrong_password_is_a_forbidden_error(self):
        with self.assertRaises(gsm.GeoServerError) as cm:
            gsm.version(CurlGateway("local", None, self.port, "admin", "nope"))
        self.assertEqual(cm.exception.code, "forbidden")

    def test_post_body_with_special_characters_arrives_intact(self):
        g = CurlGateway("local", None, self.port, "admin", "s3cr3t")
        payload = json.dumps({"workspace": {"name": "x"}, "note": 'quote " back\\ nl\n tab\t @file ქართული'}).encode()
        g.request("POST", "/rest/workspaces", body=payload, content_type="application/json")
        self.assertEqual(_Handler.seen[-1][3], payload)

    def test_connection_refused(self):
        s = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        port = s.server_address[1]
        s.server_close()
        with self.assertRaises(DeployError) as cm:
            CurlGateway("local", None, port, "a", "b").request("GET", "/rest/x")
        self.assertIn("refused", str(cm.exception))


@unittest.skipUnless(HAVE_FLASK, "flask needed")
class ApiTests(unittest.TestCase):
    NATIVE = {"deploy_target": "native", "targets": ["native"], "project": "gs", "geoserver": {"admin_password": "Adm1n_pw_x"}}

    def setUp(self):
        self.td = tempfile.mkdtemp()
        p = mock.patch.dict(os.environ, {"GISSTACK_HOME": self.td, "GISSTACK_FEATURES": "geoserver_manager"})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.td, True)
        from gisstack.api import auth
        from gisstack.webgui.app import create_app
        self.c = create_app("tok").test_client()
        self.tok = {s: auth.TokenStore().create(s, s)[0] for s in ("read", "operate", "admin")}
        self.fake = Fake({**FULL, ("GET", "/ows"): lambda q, b: Response(200, {"content-type": "image/png"}, PNG) if q.get("request") == "GetMap" else Response(200, {}, WMS),
                          ("POST", "/rest/workspaces"): Response(201, {}, b""), ("DELETE", "/rest/workspaces/topp"): Response(200, {}, b""),
                          ("POST", "/rest/reload"): Response(200, {}, b"")})
        p2 = mock.patch("gisstack.api.routes_geoserver.for_config", lambda cfg, mode, ssh: self.fake)
        p2.start()
        self.addCleanup(p2.stop)

    def post(self, path, body=None, token="tok"):
        r = self.c.post("/api/v1/geoserver" + path, json={"mode": "local", "config": self.NATIVE, **(body or {})}, headers={"Authorization": f"Bearer {token}"})
        self.addCleanup(r.close)
        return r

    def test_read_endpoints_and_response_contracts(self):
        from gisstack.api import schema
        from gisstack.api.registry import registry
        for path, body in (("/inventory", {}), ("/health", {}), ("/validate", {}), ("/capabilities", {"service": "WMS"}), ("/layers/detail", {"name": "topp:roads"})):
            self.fake.routes[("GET", "/rest/layers/topp:roads.json")] = js({"layer": {"name": "roads", "type": "VECTOR"}})
            r = self.post(path, body, self.tok["read"])
            self.assertEqual(r.status_code, 200, (path, r.get_json()))
            route = registry.find("POST", "/geoserver" + path)
            self.assertEqual(schema.validate(r.get_json()["data"], route.response, registry.components), [], path)

    def test_preview_is_an_image(self):
        r = self.post("/preview", {"layers": "topp:roads", "bbox": "0,0,1,1", "width": 64, "height": 64}, self.tok["read"])
        self.assertEqual((r.status_code, r.headers["Content-Type"], r.data[:4], r.headers["Cache-Control"]), (200, "image/png", b"\x89PNG", "no-store"))
        self.assertEqual(self.post("/preview", {"layers": "a/../b", "bbox": "0,0,1,1", "width": 64, "height": 64}).status_code, 400)

    def test_scopes(self):
        self.assertEqual(self.post("/workspaces", {"name": "x"}, self.tok["operate"]).status_code, 403)
        self.assertEqual(self.post("/workspaces", {"name": "x"}, self.tok["admin"]).status_code, 200)
        self.assertEqual(self.post("/reload", None, self.tok["read"]).status_code, 403)
        self.assertEqual(self.post("/reload", None, self.tok["operate"]).status_code, 200)

    def test_recursive_delete_needs_confirmation(self):
        r = self.post("/workspaces/delete", {"name": "topp", "recurse": True}, self.tok["admin"])
        self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (400, "plan"))
        self.assertNotIn(("DELETE", "/rest/workspaces/topp", {"recurse": "true"}, None), self.fake.calls)
        r = self.post("/workspaces/delete", {"name": "topp", "recurse": True, "confirm": True}, self.tok["admin"])
        self.assertEqual(r.get_json()["data"], {"deleted": "topp"})

    def test_geoserver_errors_become_clean_api_errors(self):
        self.fake.routes[("GET", "/rest/workspaces.json")] = Response(401, {}, b"")
        self.fake.routes[("GET", "/rest/about/version.json")] = Response(401, {}, b"")
        r = self.post("/capabilities", {"service": "WMS"})
        self.assertEqual(r.status_code, 200)
        self.fake.routes[("GET", "/ows")] = Response(403, {}, b"")
        r = self.post("/capabilities", {"service": "WMS"})
        self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (403, "forbidden"))
        self.fake.routes[("POST", "/rest/workspaces")] = Response(409, {}, b"exists")
        r = self.post("/workspaces", {"name": "x"})
        self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (409, "conflict"))

    def test_password_never_in_any_answer(self):
        for path, body in (("/inventory", {}), ("/health", {}), ("/validate", {})):
            self.assertNotIn("Adm1n_pw_x", self.post(path, body).get_data(as_text=True))

    def test_feature_flag(self):
        with mock.patch.dict(os.environ, {"GISSTACK_FEATURES": ""}):
            self.assertEqual(self.post("/inventory").get_json()["error"]["code"], "feature_disabled")

    def test_a_missing_admin_password_is_a_clear_error_on_every_target(self):
        with mock.patch("gisstack.api.routes_geoserver.for_config", gw_mod.for_config):
            for target in ("native", "compose", "k8s"):
                r = self.c.post("/api/v1/geoserver/health", json={"mode": "local", "config": {"deploy_target": target, "targets": [target], "geoserver": {"admin_password": ""}}},
                                headers={"Authorization": "Bearer tok"})
                self.addCleanup(r.close)
                self.assertEqual(r.status_code, 400, target)
                self.assertIn("admin_password", r.get_json()["error"]["message"])


# --------------------------------------------------------------------------- real GeoServer (opt-in)
IMAGE = "docker.osgeo.org/geoserver:2.28.6"


def _gs_available():
    if not shutil.which("docker"):
        return False
    if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode == 0:
        return True
    return os.environ.get("GISSTACK_TEST_DOCKER") == "1" and subprocess.run(["docker", "pull", "-q", IMAGE], capture_output=True).returncode == 0


class DirectGateway(Gateway):
    """Same interface, plain urllib to a mapped port (the curl path is covered above)."""
    def __init__(self, port, user, pw):
        self.base, self.auth = f"http://127.0.0.1:{port}/geoserver", "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()

    def request(self, method, path, *, query=None, body=None, content_type="", accept="", timeout=30):
        import urllib.parse
        url = self.base + path + ("?" + urllib.parse.urlencode(query) if query else "")
        req = urllib.request.Request(url, data=body, method=method, headers={"Authorization": self.auth, **({"Content-Type": content_type} if content_type else {}), **({"Accept": accept} if accept else {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:      # noqa: S310
                return Response(r.status, {k.lower(): v for k, v in r.headers.items()}, r.read())
        except urllib.error.HTTPError as e:
            return Response(e.code, {k.lower(): v for k, v in e.headers.items()}, e.read())


@unittest.skipUnless(_gs_available(), f"Docker and {IMAGE} needed (GISSTACK_TEST_DOCKER=1 pulls it)")
class RealGeoServerTests(unittest.TestCase):
    PW = "Test_pw_12345"

    @classmethod
    def setUpClass(cls):
        cls.name = f"gs-test-{os.getpid()}"
        subprocess.run(["docker", "run", "-d", "--rm", "--name", cls.name, "-p", "127.0.0.1::8080", "-e", f"GEOSERVER_ADMIN_PASSWORD={cls.PW}", IMAGE], check=True, capture_output=True)
        out = subprocess.run(["docker", "port", cls.name, "8080/tcp"], capture_output=True, text=True, check=True).stdout
        cls.port = int(out.strip().splitlines()[0].rsplit(":", 1)[1])
        cls.gw = DirectGateway(cls.port, "admin", cls.PW)
        deadline = time.time() + 240
        while time.time() < deadline:
            try:
                if cls.gw.request("GET", "/rest/about/version.json", accept="application/json").status == 200:
                    return
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(3)
        logs = subprocess.run(["docker", "logs", "--tail", "30", cls.name], capture_output=True, text=True).stdout
        subprocess.run(["docker", "rm", "-f", cls.name], capture_output=True)
        raise RuntimeError("GeoServer did not start:\n" + logs)

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["docker", "rm", "-f", cls.name], capture_output=True)

    def test_version_inventory_health_and_capabilities(self):
        self.assertTrue(gsm.version(self.gw)["GeoServer"].startswith("2.28"))
        inv = gsm.inventory(self.gw)
        self.assertEqual(inv["errors"], {})
        self.assertTrue(inv["workspaces"])
        h = gsm.health(self.gw)
        self.assertTrue(h["ok"], h)
        for svc in ("WMS", "WFS", "WMTS"):
            self.assertEqual(gsm.capabilities(self.gw, svc)["service"], svc)

    def test_wrong_credentials_and_missing_things(self):
        with self.assertRaises(gsm.GeoServerError) as cm:
            gsm.workspaces(DirectGateway(self.port, "admin", "wrong"))
        self.assertEqual(cm.exception.code, "forbidden")
        with self.assertRaises(gsm.GeoServerError) as cm:
            gsm.layer_detail(self.gw, "nope:nothing")
        self.assertEqual(cm.exception.code, "not_found")

    def test_workspace_lifecycle_and_conflicts(self):
        gsm.create_workspace(self.gw, "gs_manager_test")
        self.assertIn("gs_manager_test", [w["name"] for w in gsm.workspaces(self.gw)])
        with self.assertRaises(gsm.GeoServerError) as cm:
            gsm.create_workspace(self.gw, "gs_manager_test")
        self.assertIn(cm.exception.code, ("conflict", "bad_request"))
        gsm.delete_workspace(self.gw, "gs_manager_test")
        self.assertNotIn("gs_manager_test", [w["name"] for w in gsm.workspaces(self.gw)])
        gsm.reload_catalog(self.gw)

    def test_preview_of_a_sample_layer_or_clean_error(self):
        layers = gsm.layers(self.gw)
        if not layers:
            with self.assertRaises(gsm.GeoServerError):
                gsm.preview(self.gw, "nope:nothing", "-180,-90,180,90", 64, 64)
            return
        r = gsm.preview(self.gw, layers[0]["name"], "-180,-90,180,90", 128, 64)
        self.assertEqual(r.content_type, "image/png")


if __name__ == "__main__":
    unittest.main()
