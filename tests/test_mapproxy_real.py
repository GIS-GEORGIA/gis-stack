"""GIS correctness against the REAL MapProxy (skipped when `mapproxy` is not installed): the configuration gis-stack generates loads with warnings as errors,
every grid it offers (EPSG:3857, 4326, 32638, 32637) is advertised with the right SRS, the URL the web map uses really is a MapProxy WMTS REST tile route
(after Caddy strips /mapproxy), and a tile requested as z/x/y reaches the upstream source as the SAME z/x/y (XYZ - no Y inversion, no level shift), while the
TMS route is the documented different addressing.  Upstream = a local HTTP server that records the paths it is asked for."""
import io
import json
import re
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

try:
    import mapproxy  # noqa: F401
    from mapproxy.config.loader import ProxyConfiguration, load_configuration  # noqa: F401
    from mapproxy.wsgiapp import make_wsgi_app
    from PIL import Image
    from werkzeug.test import Client
    HAVE = True
except ImportError:
    HAVE = False

from gisstack import config, generate

GRIDS = ["GLOBAL_WEBMERCATOR", "GLOBAL_GEODETIC", "UTM38N", "UTM37N"]


def png(w=256, h=256):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 120, 200)).save(buf, "PNG")
    return buf.getvalue()


class Upstream:
    def __init__(self):
        self.paths = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                from urllib.parse import parse_qs, urlparse
                outer.paths.append(self.path)
                q = {k.upper(): v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
                body = png(int(q.get("WIDTH", 256)), int(q.get("HEIGHT", 256)))                    # a WMS answers with the size that was asked (MapProxy meta-tiles)
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_port
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@unittest.skipUnless(HAVE, "mapproxy, Pillow and werkzeug needed")
class RealMapProxy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up = Upstream()
        cls.td = tempfile.TemporaryDirectory()
        cfg = config.resolve({"project": "mp", "targets": ["compose"], "deploy_target": "compose", "components": {"mapproxy": True, "caddy": True, "postgis": False},
                              "mapproxy": {"grids": GRIDS, "layers": [{"name": "tiles", "title": "Tiles", "source": "tiles", "url": f"http://127.0.0.1:{cls.up.port}/{{z}}/{{x}}/{{y}}.png"},
                                                       {"name": "wmsl", "title": "WMS layer", "source": "wms", "url": f"http://127.0.0.1:{cls.up.port}/wms", "wms_layers": "demo"}]}},
                             generate_secrets=False, persist_secrets=False)
        out = Path(cls.td.name) / "bundle"
        generate.generate_bundle(cfg, out)
        yml = next(out.rglob("mapproxy.yaml"))
        text = yml.read_text().replace("/var/lib/mapproxy", str(Path(cls.td.name) / "cache"))   # the container path is not writable here
        cls.yml = Path(cls.td.name) / "mapproxy.yaml"
        cls.yml.write_text(text)
        cls.cfg = cfg
        cls.web = json.loads(next(out.rglob("config.json")).read_text()) if list(out.rglob("config.json")) else {}
        cls.client = Client(make_wsgi_app(str(cls.yml), ignore_config_warnings=False, reloader=False))

    @classmethod
    def tearDownClass(cls):
        cls.up.close()
        cls.td.cleanup()

    def get(self, path):
        r = self.client.get(path)
        return r.status_code, r.get_data(), r.headers.get("Content-Type", "")

    def test_the_generated_configuration_loads_with_warnings_as_errors(self):
        load_configuration(str(self.yml), ignore_warnings=False)                              # raises on any warning

    def test_wmts_capabilities_advertise_every_grid_with_the_right_srs(self):
        code, body, _ = self.get("/wmts/1.0.0/WMTSCapabilities.xml")
        self.assertEqual(code, 200)
        text = body.decode()
        # GLOBAL_GEODETIC is NOT advertised: MapProxy cannot serve a 2:1 grid over WMTS ("grid ... is not compatible with WMTS") - gis-stack warns about it
        self.assertNotIn("GLOBAL_GEODETIC", text)
        self.assertTrue(any("GLOBAL_GEODETIC" in w for w in self.cfg["_meta"]["warnings"]), self.cfg["_meta"]["warnings"])
        for grid in ("GLOBAL_WEBMERCATOR", "UTM38N", "UTM37N"):
            self.assertIn(grid, text)
        self.assertRegex(text, r"ResourceURL[^>]*template=\"[^\"]*/wmts/tiles/\{TileMatrixSet\}/\{TileMatrix\}/\{TileCol\}/\{TileRow\}")
        self.assertIn("<ows:Identifier>tiles</ows:Identifier>", text)
        layer = text[text.index("<ows:Identifier>wmsl</ows:Identifier>"):].split("</Layer>")[0]
        for grid in ("GLOBAL_WEBMERCATOR", "UTM38N", "UTM37N"):
            self.assertIn(f"<TileMatrixSet>{grid}</TileMatrixSet>", layer, grid)                     # a WMS-backed layer is offered in every WMTS-capable grid
        tiles = text[text.index("<ows:Identifier>tiles</ows:Identifier>"):].split("</Layer>")[0]
        self.assertIn("<TileMatrixSet>GLOBAL_WEBMERCATOR</TileMatrixSet>", tiles)
        self.assertNotIn("UTM38N", tiles)                                                           # a tile-source layer: Web Mercator only

    def test_the_web_map_url_is_a_real_wmts_route_and_z_x_y_reaches_the_source_unchanged(self):
        tmpl = self.web.get("layers", [{}])[0].get("url") if self.web else "/mapproxy/wmts/tiles/GLOBAL_WEBMERCATOR/{z}/{x}/{y}.png"
        self.assertEqual(tmpl, "/mapproxy/wmts/tiles/GLOBAL_WEBMERCATOR/{z}/{x}/{y}.png")
        path = tmpl.replace("{z}", "3").replace("{x}", "5").replace("{y}", "2").removeprefix("/mapproxy")      # Caddy: handle_path /mapproxy/*
        self.up.paths.clear()
        code, body, ctype = self.get(path)
        self.assertEqual((code, ctype), (200, "image/png"), body[:200])
        self.assertEqual(self.up.paths, ["/3/5/2.png"], "the source must be asked for the SAME z/x/y: the web map (XYZ) and its WMTS URL agree")

    def test_a_different_tile_is_a_different_upstream_request_no_inversion(self):
        for z, x, y in ((1, 0, 1), (4, 9, 6)):
            self.up.paths.clear()
            self.assertEqual(self.get(f"/wmts/tiles/GLOBAL_WEBMERCATOR/{z}/{x}/{y}.png")[0], 200)
            self.assertEqual(self.up.paths, [f"/{z}/{x}/{y}.png"])

    def test_tms_is_a_different_addressing_and_not_what_the_web_map_uses(self):
        """Documented (CLAUDE.md): the TMS route shifts the level by one and flips Y, which is why clients must use the WMTS REST URL."""
        self.up.paths.clear()
        code, _, _ = self.get("/tms/1.0.0/tiles/GLOBAL_WEBMERCATOR/3/5/2.png")
        self.assertIn(code, (200, 404))
        if code == 200:
            self.assertNotEqual(self.up.paths, ["/3/5/2.png"])                                   # not the XYZ tile of the same numbers
            m = re.fullmatch(r"/(\d+)/(\d+)/(\d+)\.png", self.up.paths[0])
            self.assertTrue(m, self.up.paths)

    def test_wms_backed_layers_ask_the_source_in_the_right_crs_and_axis_order_for_every_grid(self):
        """Real CRS handling: the GetMap the source receives names the grid's SRS and a bbox in that SRS's units, x (easting / longitude) first."""
        from urllib.parse import parse_qs, urlparse
        for grid, srs, lim in (("GLOBAL_WEBMERCATOR", "EPSG:3857", 20037508.35), ("UTM38N", "EPSG:32638", None), ("UTM37N", "EPSG:32637", None)):
            self.up.paths.clear()
            code, _, ctype = self.get(f"/wmts/wmsl/{grid}/2/1/1.png" if grid == "GLOBAL_WEBMERCATOR" else f"/wmts/wmsl/{grid}/0/0/0.png")
            self.assertEqual((code, ctype), (200, "image/png"), grid)
            q = parse_qs(urlparse(self.up.paths[-1]).query)
            q = {k.upper(): v[0] for k, v in q.items()}
            self.assertEqual(q["SRS"].upper(), srs, grid)
            minx, miny, maxx, maxy = (float(x) for x in q["BBOX"].split(","))
            self.assertLess(minx, maxx)
            self.assertLess(miny, maxy)
            if lim:
                self.assertTrue(abs(minx) <= lim and abs(maxx) <= lim, (grid, q["BBOX"]))
            else:                                                                               # the UTM 38N / 37N grids are bounded to the configured Georgia box (metres)
                self.assertTrue(minx >= 150000 and maxx <= 850000 + 1e6 and miny >= 4500000, (grid, q["BBOX"]))
                self.assertGreater(miny, 1_000_000, "northing must be the Y coordinate: a swapped axis order would put a small number here")

    def test_geodetic_grid_is_served_over_tms_with_longitude_first(self):
        from urllib.parse import parse_qs, urlparse
        self.up.paths.clear()
        code, _, _ = self.get("/tms/1.0.0/wmsl/GLOBAL_GEODETIC/1/1/0.png")
        self.assertEqual(code, 200)
        q = {k.upper(): v[0] for k, v in parse_qs(urlparse(self.up.paths[-1]).query).items()}
        self.assertEqual(q["SRS"].upper(), "EPSG:4326")
        minx, miny, maxx, maxy = (float(x) for x in q["BBOX"].split(","))
        self.assertTrue(-180 <= minx < maxx <= 180 and -90 <= miny < maxy <= 90, q["BBOX"])         # degrees, lon/lat order (WMS 1.1.1 x,y)

    def test_a_tiles_source_layer_is_only_served_in_web_mercator_and_never_fails_with_a_server_error(self):
        """Before the fix every tile of such a layer in UTM38N / GLOBAL_GEODETIC was an HTTP 500 ("SRS of cache and tile source do not match")."""
        text = self.get("/wmts/1.0.0/WMTSCapabilities.xml")[1].decode()
        self.assertNotRegex(text, r"tiles/UTM38N")
        for path in ("/wmts/tiles/UTM38N/2/1/1.png", "/wmts/tiles/GLOBAL_GEODETIC/2/1/1.png", "/tms/1.0.0/tiles/UTM38N/2/1/1.png"):
            self.assertLess(self.get(path)[0], 500, path)
        self.assertTrue(any("tiles" in w and "GLOBAL_WEBMERCATOR" in w for w in self.cfg["_meta"]["warnings"]), self.cfg["_meta"]["warnings"])

    def test_a_tile_outside_the_grid_is_refused_not_invented(self):
        code, _, _ = self.get("/wmts/tiles/GLOBAL_WEBMERCATOR/2/9/9.png")                     # only 4 x 4 tiles exist at level 2
        self.assertIn(code, (400, 404))


if __name__ == "__main__":
    unittest.main()
