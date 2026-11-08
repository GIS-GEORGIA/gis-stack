"""Phase 6: MapProxy manager - config view/edits/backups, cache statistics, seeding guards, cleanup, preview, health; API scopes and audit;
and the real shell scripts against a real MapProxy (real WSGI app, real seeding, real cache files)."""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import yaml

from gisstack import transport
from gisstack.core.errors import DeployError
from gisstack.mapproxy import manager as mpm, server as mps
from gisstack.mapproxy.server import Check, Gateway, ScriptGateway, Tile

HAVE_FLASK = __import__("importlib").util.find_spec("flask") is not None
HAVE_MAPPROXY = __import__("importlib").util.find_spec("mapproxy") is not None and shutil.which("bash") and shutil.which("curl") and shutil.which("base64")

BASE = """services:
  demo:
  tms: {use_grid_names: true, origin: nw}
  wmts:
  wms: {srs: ['EPSG:3857'], image_formats: ['image/png']}
layers:
  - {name: roads, title: Roads, sources: [roads_cache]}
  - {name: basemap, title: OSM, sources: [basemap_cache]}
caches:
  roads_cache:
    grids: [GLOBAL_WEBMERCATOR, UTM38N]
    sources: [roads_src]
    format: image/png
    cache: {type: file, directory_layout: tms}
  basemap_cache:
    grids: [GLOBAL_WEBMERCATOR]
    sources: [osm_tiles]
    format: image/png
    cache: {type: file}
sources:
  roads_src:
    type: wms
    req: {url: 'http://admin:s3cret@127.0.0.1:8080/geoserver/wms?token=ABC123&map=x', layers: 'ws:roads', transparent: true}
    http: {headers: {Authorization: Bearer SECRETVALUE}}
  osm_tiles:
    type: tile
    grid: GLOBAL_WEBMERCATOR
    url: https://tile.openstreetmap.org/%(z)s/%(x)s/%(y)s.png
grids:
  UTM38N: {srs: 'EPSG:32638', bbox: [166000, 0, 834000, 9330000], bbox_srs: 'EPSG:32638', origin: nw}
globals:
  cache: {base_dir: /var/lib/gisstack/mapproxy/cache_data, lock_dir: /var/lib/gisstack/mapproxy/cache_data/locks}
"""
SEED = """seeds:
  nightly: {caches: [roads_cache], coverages: [area], levels: {from: 0, to: 8}}
coverages:
  area: {bbox: [39.9, 41.0, 46.8, 43.7], srs: 'EPSG:4326'}
"""


class FakeGw(Gateway):
    def __init__(self, config=BASE, seed=SEED, ok=True):
        self.files = {"mapproxy.yaml": config, "seed.yaml": seed}
        self.ok, self.applied, self.errors = ok, [], ["boom"]
        self._backups = {}
        self.stats_raw = {"available": True, "disk_total_kb": 1000, "disk_free_kb": 800, "entries": {}}
        self.seed_calls, self.seed_total, self.seed_running = [], 12, 0
        self.cleanups, self.tile_answer = [], Tile(200, "image/png", b"\x89PNG")
        self.probe_data = {"unit": "active", "http": 200, "caps": b"", "seeding": 0, "writable": "yes", "disk_total_kb": 1000, "disk_free_kb": 800, "installed": True,
                           "seeding_known": True}

    def read(self):
        return dict(self.files)

    def apply(self, config, seed, dry_run):
        self.applied.append((config, seed, dry_run))
        if not self.ok:
            return Check(False, list(self.errors))
        if dry_run:
            return Check(True)
        bid = f"20261108-1200{len(self._backups):02d}-abcd"
        self._backups[bid] = dict(self.files)
        self.files["mapproxy.yaml"] = config
        if seed is not None:
            self.files["seed.yaml"] = seed
        return Check(True, backup=bid)

    def backups(self):
        return [{"id": k, "created": 1, "bytes": 10} for k in sorted(self._backups, reverse=True)]

    def read_backup(self, backup_id):
        if backup_id not in self._backups:
            raise DeployError("mapproxy: no such backup")
        return dict(self._backups[backup_id])

    def stats(self):
        return self.stats_raw

    def seed(self, seed, concurrency, dry_run, stop):
        self.seed_calls.append((seed, concurrency, dry_run))
        if dry_run:
            yield "Seeding tasks"
            yield f"[10:00:00]  0 100.00% 1.0, 2.0, 3.0, 4.0 ({self.seed_total} tiles)" if self.seed_total is not None else "no estimate here"
        else:
            yield "[10:00:01]  0  50.00% bbox (3 tiles)"
            yield "[10:00:02]  0 100.00% bbox (6 tiles)"

    def cleanup(self, names, older_than_days, dry_run):
        self.cleanups.append((list(names), older_than_days, dry_run))
        return [{"entry": n, "files": 10, "bytes": 2048} for n in names]

    def tile(self, layer, grid, z, x, y, ext):
        self.tile_args = (layer, grid, z, x, y, ext)
        return self.tile_answer

    def probe(self):
        return {**self.probe_data, "seeding": self.seed_running}


# --------------------------------------------------------------------------- config view
class SummaryTests(unittest.TestCase):
    def test_summary_shapes_and_secret_hiding(self):
        s = mpm.summarize(mpm.load(BASE), mpm.load(SEED))
        self.assertEqual([x["name"] for x in s["layers"]], ["roads", "basemap"])
        self.assertEqual({c["name"]: (c["grids"], c["type"]) for c in s["caches"]}["roads_cache"], (["GLOBAL_WEBMERCATOR", "UTM38N"], "file"))
        src = {x["name"]: x for x in s["sources"]}
        self.assertEqual(src["roads_src"]["url"], "http://***@127.0.0.1:8080/geoserver/wms?token=***&map=x")
        self.assertEqual(src["roads_src"]["header_names"], ["Authorization"])                  # header NAMES only
        blob = json.dumps(s)
        for secret in ("s3cret", "ABC123", "SECRETVALUE", "admin:"):
            self.assertNotIn(secret, blob)
        self.assertEqual([g["name"] for g in s["grids"]], ["GLOBAL_MERCATOR", "GLOBAL_WEBMERCATOR", "GLOBAL_GEODETIC", "UTM38N"])
        self.assertTrue(s["demo"])
        self.assertEqual(s["seeds"], [{"name": "nightly", "caches": ["roads_cache"], "levels": {"from": 0, "to": 8}}])
        self.assertEqual(s["cache_dir"], "/var/lib/gisstack/mapproxy/cache_data")

    def test_redact_url_cases(self):
        R = mpm.redact_url
        self.assertEqual(R("https://u:p@h.example/x?apikey=1&a=2"), "https://***@h.example/x?apikey=***&a=2")
        self.assertEqual(R("http://[::1]:8080/a"), "http://[::1]:8080/a")
        self.assertEqual(R(None), "")
        self.assertEqual(R("http://h/%(z)s/%(x)s/%(y)s.png"), "http://h/%(z)s/%(x)s/%(y)s.png")

    def test_broken_documents(self):
        for bad, code in (("a: [unclosed", "config"), ("- a\n- b", "config")):
            with self.assertRaises(mpm.MapproxyError) as cm:
                mpm.load(bad)
            self.assertEqual(cm.exception.code, code)
        with self.assertRaises(mpm.MapproxyError) as cm:
            mpm.load(None)
        self.assertEqual(cm.exception.code, "not_found")
        self.assertEqual(mpm.load(""), {})


# --------------------------------------------------------------------------- edits
class EditTests(unittest.TestCase):
    def conf(self):
        return mpm.load(BASE)

    def test_add_wms_layer(self):
        c = self.conf()
        changes = mpm.add_layer(c, {"name": "parcels", "title": "Parcels", "source": "wms", "url": "http://127.0.0.1:8080/geoserver/wms", "layers": "ws:parcels",
                                    "grids": ["GLOBAL_WEBMERCATOR", "UTM38N"], "format": "png"})
        self.assertEqual(changes, ["layer parcels", "cache parcels_cache", "source parcels_src"])
        self.assertEqual(c["layers"][-1], {"name": "parcels", "title": "Parcels", "sources": ["parcels_cache"]})
        self.assertEqual(c["caches"]["parcels_cache"]["grids"], ["GLOBAL_WEBMERCATOR", "UTM38N"])
        self.assertEqual(c["sources"]["parcels_src"]["req"], {"url": "http://127.0.0.1:8080/geoserver/wms", "layers": "ws:parcels", "transparent": True})

    def test_add_tile_and_jpeg_and_mixed(self):
        c = self.conf()
        mpm.add_layer(c, {"name": "ortho", "source": "tile", "url": "https://t.example.org/%(z)s/%(x)s/%(y)s.jpg", "format": "jpeg"})
        self.assertEqual(c["caches"]["ortho_cache"]["format"], "image/jpeg")
        self.assertEqual(c["sources"]["ortho_src"]["grid"], "GLOBAL_WEBMERCATOR")
        mpm.add_layer(c, {"name": "mix", "source": "wms", "url": "http://h/wms", "layers": "a", "format": "mixed"})
        self.assertEqual((c["caches"]["mix_cache"]["format"], c["caches"]["mix_cache"]["request_format"]), ("mixed", "image/png"))
        self.assertNotIn("transparent", mpm.load(mpm.dump(c, "x"))["sources"]["ortho_src"])

    def test_add_layer_rejections(self):
        ok = {"name": "x1", "source": "wms", "url": "http://h/wms", "layers": "a"}
        bad = [({**ok, "name": "1bad"}, "bad_request"), ({**ok, "name": "has space"}, "bad_request"), ({**ok, "name": "roads"}, "conflict"),
               ({**ok, "grids": ["NOPE"]}, "bad_request"), ({**ok, "format": "gif"}, "bad_request"), ({**ok, "source": "ftp"}, "bad_request"),
               ({**ok, "url": "ftp://h/x"}, "bad_request"), ({**ok, "url": "file:///etc/passwd"}, "bad_request"), ({**ok, "url": "http://u:p@h/x"}, "bad_request"),
               ({**ok, "url": "http://169.254.169.254/latest"}, "bad_request"), ({**ok, "url": "http://metadata.google.internal/x"}, "bad_request"),
               ({**ok, "layers": "a;b"}, "bad_request"), ({**ok, "layers": ""}, "bad_request"),
               ({"name": "t1", "source": "tile", "url": "http://h/{z}/{x}/{y}.png"}, "bad_request")]
        for params, code in bad:
            c = self.conf()
            with self.assertRaises(mpm.MapproxyError, msg=str(params)) as cm:
                mpm.add_layer(c, params)
            self.assertEqual(cm.exception.code, code, params)
            self.assertEqual(c, self.conf(), "a rejected edit must not change the document")

    def test_remove_layer_removes_only_what_nothing_else_uses(self):
        c = self.conf()
        self.assertEqual(mpm.remove_layer(c, {"name": "roads"}), ["layer roads", "cache roads_cache", "source roads_src"])
        self.assertEqual([x["name"] for x in c["layers"]], ["basemap"])
        c = self.conf()
        c["layers"].append({"name": "roads2", "title": "again", "sources": ["roads_cache"]})            # a second layer on the same cache
        self.assertEqual(mpm.remove_layer(c, {"name": "roads"}), ["layer roads"])
        self.assertIn("roads_cache", c["caches"])
        with self.assertRaises(mpm.MapproxyError) as cm:
            mpm.remove_layer(c, {"name": "ghost"})
        self.assertEqual(cm.exception.code, "not_found")

    def test_grids(self):
        c = self.conf()
        self.assertEqual(mpm.add_grid(c, {"name": "UTM39N", "srs": "EPSG:32639", "bbox": [0, 0, 1000, 1000]}), ["grid UTM39N"])
        self.assertEqual(c["grids"]["UTM39N"], {"srs": "EPSG:32639", "bbox": [0.0, 0.0, 1000.0, 1000.0], "bbox_srs": "EPSG:32639", "origin": "nw"})
        for params, code in (({"name": "UTM39N", "srs": "EPSG:32639", "bbox": [0, 0, 1, 1]}, "conflict"), ({"name": "GLOBAL_WEBMERCATOR", "srs": "EPSG:3857", "bbox": [0, 0, 1, 1]}, "conflict"),
                             ({"name": "bad name", "srs": "EPSG:3857", "bbox": [0, 0, 1, 1]}, "bad_request"), ({"name": "g", "srs": "3857", "bbox": [0, 0, 1, 1]}, "bad_request"),
                             ({"name": "g", "srs": "EPSG:3857", "bbox": [1, 1, 0, 0]}, "bad_request"), ({"name": "g", "srs": "EPSG:3857", "bbox": [0, 0, 1]}, "bad_request"),
                             ({"name": "g", "srs": "EPSG:3857", "bbox": ["a", 0, 1, 1]}, "bad_request")):
            with self.assertRaises(mpm.MapproxyError, msg=str(params)) as cm:
                mpm.add_grid(c, params)
            self.assertEqual(cm.exception.code, code, params)
        with self.assertRaises(mpm.MapproxyError) as cm:
            mpm.remove_grid(c, {"name": "UTM38N"})                                  # roads_cache uses it
        self.assertEqual(cm.exception.code, "conflict")
        self.assertEqual(mpm.remove_grid(c, {"name": "UTM39N"}), ["grid UTM39N"])
        with self.assertRaises(mpm.MapproxyError):
            mpm.remove_grid(c, {"name": "GLOBAL_WEBMERCATOR"})

    def test_edit_dry_run_then_real_with_backup(self):
        gw = FakeGw()
        p = {"name": "extra", "source": "tile", "url": "http://t.example.org/%(z)s/%(x)s/%(y)s.png"}
        r = mpm.edit(gw, "add_layer", p, dry_run=True)
        self.assertEqual((r["dry_run"], r["backup"]), (True, ""))
        self.assertEqual(gw.files["mapproxy.yaml"], BASE)                           # nothing written
        r = mpm.edit(gw, "add_layer", p, dry_run=False)
        self.assertTrue(r["backup"])
        new = mpm.load(gw.files["mapproxy.yaml"])
        self.assertIn("extra_cache", new["caches"])
        self.assertIn("roads_cache", new["caches"])                                 # everything else survived the round trip
        self.assertTrue(gw.files["mapproxy.yaml"].startswith("# Edited by the gisstack MapProxy manager"))
        self.assertEqual(gw._backups[r["backup"]]["mapproxy.yaml"], BASE)           # the old file is what was backed up

    def test_a_config_mapproxy_rejects_is_never_installed(self):
        gw = FakeGw(ok=False)
        with self.assertRaises(mpm.MapproxyError) as cm:
            mpm.edit(gw, "add_layer", {"name": "x1", "source": "wms", "url": "http://h/wms", "layers": "a"}, dry_run=False)
        self.assertEqual(cm.exception.code, "config")
        self.assertIn("boom", str(cm.exception))
        self.assertEqual(gw.files["mapproxy.yaml"], BASE)
        with self.assertRaises(mpm.MapproxyError):
            mpm.edit(gw, "drop_everything", {})

    def test_restore_backs_up_the_current_file_first(self):
        gw = FakeGw()
        r = mpm.edit(gw, "add_layer", {"name": "extra", "source": "wms", "url": "http://h/wms", "layers": "a"}, dry_run=False)
        res = mpm.restore(gw, r["backup"])
        self.assertEqual(res["restored"], r["backup"])
        self.assertEqual(gw.files["mapproxy.yaml"], BASE)
        self.assertIn(res["backup_of_previous"], gw._backups)
        self.assertIn("extra_cache", mpm.load(gw._backups[res["backup_of_previous"]]["mapproxy.yaml"])["caches"])
        with self.assertRaises(DeployError):
            mpm.restore(gw, "20200101-000000-ffff")
        gw.ok = False
        with self.assertRaises(mpm.MapproxyError):
            mpm.restore(gw, r["backup"])


# --------------------------------------------------------------------------- statistics
class StatsTests(unittest.TestCase):
    def test_grouping_by_cache_longest_prefix_and_unmanaged(self):
        gw = FakeGw()
        gw.stats_raw["entries"] = {
            "roads_cache_EPSG3857": {"name": "roads_cache_EPSG3857", "kind": "dir", "bytes": 1000, "tiles": 10, "partial": False, "levels": {1: 4, 0: 6}},
            "roads_cache_UTM38N": {"name": "roads_cache_UTM38N", "kind": "dir", "bytes": 500, "tiles": 5, "partial": True, "levels": {}},
            "basemap_cache.gpkg": {"name": "basemap_cache.gpkg", "kind": "file", "bytes": 4096, "tiles": None, "partial": False, "levels": {}},
            "old_leftover": {"name": "old_leftover", "kind": "dir", "bytes": 77, "tiles": 1, "partial": False, "levels": {}}}
        st = mpm.cache_stats(gw)
        by = {c["name"]: c for c in st["caches"]}
        self.assertEqual((by["roads_cache"]["bytes"], by["roads_cache"]["tiles"], by["roads_cache"]["partial"]), (1500, 15, True))
        self.assertEqual([lv["level"] for lv in by["roads_cache"]["entries"][0]["levels"]], [0, 1])          # sorted
        self.assertEqual((by["basemap_cache"]["bytes"], by["basemap_cache"]["tiles"]), (4096, 0))
        self.assertEqual([u["entry"] for u in st["unmanaged"]], ["old_leftover"])
        self.assertEqual(st["total_bytes"], 1000 + 500 + 4096 + 77)

    def test_a_cache_whose_name_is_a_prefix_of_another(self):
        conf = BASE.replace("  basemap_cache:", "  roads_cache_old:\n    grids: [GLOBAL_WEBMERCATOR]\n    sources: [osm_tiles]\n  basemap_cache:")
        gw = FakeGw(config=conf)
        gw.stats_raw["entries"] = {"roads_cache_old_EPSG3857": {"name": "roads_cache_old_EPSG3857", "kind": "dir", "bytes": 9, "tiles": 1, "partial": False, "levels": {}}}
        by = {c["name"]: c for c in mpm.cache_stats(gw)["caches"]}
        self.assertEqual((by["roads_cache_old"]["bytes"], by["roads_cache"]["bytes"]), (9, 0))

    def test_parse_stats(self):
        out = "D 1000 400\nE a_EPSG3857 dir 12345 7 0\nL a_EPSG3857 0 1\nL a_EPSG3857 1 6\nE b.gpkg file 99 - 0\nE_PARTIAL a_EPSG3857\nL ghost 0 5\nnoise\n"
        r = mps.parse_stats(out)
        self.assertEqual((r["disk_total_kb"], r["disk_free_kb"]), (1000, 400))
        self.assertEqual(r["entries"]["a_EPSG3857"]["levels"], {0: 1, 1: 6})
        self.assertTrue(r["entries"]["a_EPSG3857"]["partial"])
        self.assertIsNone(r["entries"]["b.gpkg"]["tiles"])
        self.assertNotIn("ghost", r["entries"])
        self.assertFalse(mps.parse_stats("NOCACHE")["available"])


# --------------------------------------------------------------------------- seeding
class SeedBuildTests(unittest.TestCase):
    P = {"caches": ["roads_cache"], "level_from": 2, "level_to": 6, "bbox": [39.9, 41.0, 46.8, 43.7]}

    def build(self, **over):
        text, caches = mpm.build_seed(mpm.load(BASE), {**self.P, **over})
        return yaml.safe_load(text), caches

    def test_default_skips_existing_tiles(self):
        doc, caches = self.build()
        task = doc["seeds"]["gs_manual"]
        self.assertEqual(caches, ["roads_cache"])
        self.assertEqual((task["levels"], task["coverages"]), ({"from": 2, "to": 6}, ["area"]))
        self.assertEqual(str(task["refresh_before"]["time"]).startswith("2000-01-01"), True)      # MapProxy's default would re-fetch EVERY tile
        self.assertEqual(doc["coverages"]["area"], {"bbox": [39.9, 41.0, 46.8, 43.7], "srs": "EPSG:4326"})

    def test_refresh_rebuilds_everything(self):
        doc, _ = self.build(refresh=True)
        self.assertEqual(doc["seeds"]["gs_manual"]["refresh_before"], {"minutes": 0})

    def test_grids_are_checked_against_the_cache(self):
        self.assertEqual(self.build(grids=["UTM38N"])[0]["seeds"]["gs_manual"]["grids"], ["UTM38N"])
        with self.assertRaises(mpm.MapproxyError):
            self.build(grids=["GLOBAL_GEODETIC"])

    def test_rejections(self):
        for over in ({"caches": []}, {"caches": ["nope_cache"]}, {"level_from": 5, "level_to": 2}, {"level_to": 21}, {"level_from": -1}, {"bbox": [1, 2, 3]},
                     {"bbox": [5, 5, 1, 1]}, {"bbox": [-181, 0, 10, 10]}, {"bbox": [0, -91, 10, 10]}, {"bbox": ["a", 0, 1, 1]}):
            with self.assertRaises(mpm.MapproxyError, msg=str(over)):
                self.build(**over)

    def test_openstreetmap_is_not_seeded_deep(self):
        with self.assertRaises(mpm.MapproxyError) as cm:
            self.build(caches=["basemap_cache"], level_to=11)
        self.assertEqual(cm.exception.code, "conflict")
        self.assertIn("usage policy", str(cm.exception))
        self.build(caches=["basemap_cache"], level_to=10)

    def test_estimate_parsing(self):
        self.assertEqual(mpm.estimate_tiles(["x", "[22:58:16]  0 100.00% 1.0, 2.0, 3.0, 4.0 (11 tiles)"]), 11)
        self.assertEqual(mpm.estimate_tiles(["[1]  0   0.00% a (0 tiles)", "[2]  0 100.00% a (7 tiles)", ""]), 7)
        self.assertIsNone(mpm.estimate_tiles(["nothing", "useful"]))


class SeedRunTests(unittest.TestCase):
    P = {"caches": ["roads_cache"], "level_to": 6, "bbox": [39.9, 41.0, 46.8, 43.7]}

    def run_seed(self, gw, **over):
        return list(mpm.seed(gw, {**self.P, **over}))

    def test_default_is_only_an_estimate(self):
        gw = FakeGw()
        lines = self.run_seed(gw)
        self.assertEqual([c[2] for c in gw.seed_calls], [True])                    # dry run only
        self.assertIn("== estimated tiles to create: 12", lines)
        self.assertNotIn("== seeding", lines)

    def test_real_run_needs_confirm_and_estimates_first(self):
        gw = FakeGw()
        with self.assertRaises(mpm.MapproxyError) as cm:
            self.run_seed(gw, dry_run=False)
        self.assertEqual(cm.exception.code, "plan")
        self.assertEqual(gw.seed_calls, [])
        lines = self.run_seed(gw, dry_run=False, confirm=True, concurrency=3)
        self.assertEqual([(c[1], c[2]) for c in gw.seed_calls], [(1, True), (3, False)])
        self.assertEqual(lines[-1], "== seeding finished")

    def test_tile_limit_guard_stops_before_anything_is_created(self):
        gw = FakeGw()
        gw.seed_total = 5_000_000
        with self.assertRaises(mpm.MapproxyError) as cm:
            self.run_seed(gw, dry_run=False, confirm=True)
        self.assertEqual(cm.exception.code, "conflict")
        self.assertEqual([c[2] for c in gw.seed_calls], [True])
        gw = FakeGw()
        gw.seed_total = 5_000_000
        self.run_seed(gw, dry_run=False, confirm=True, max_tiles=6_000_000)          # raised on purpose
        self.assertEqual([c[2] for c in gw.seed_calls], [True, False])
        gw = FakeGw()
        gw.seed_total = mpm.MAX_TILES_HARD + 1
        with self.assertRaises(mpm.MapproxyError):
            self.run_seed(gw, dry_run=False, confirm=True, max_tiles=10 ** 12)          # the hard cap holds

    def test_no_estimate_means_no_seeding(self):
        gw = FakeGw()
        gw.seed_total = None
        with self.assertRaises(mpm.MapproxyError) as cm:
            self.run_seed(gw, dry_run=False, confirm=True)
        self.assertIn("blind", str(cm.exception))
        self.assertEqual([c[2] for c in gw.seed_calls], [True])

    def test_one_seeding_at_a_time(self):
        gw = FakeGw()
        gw.seed_running = 1
        with self.assertRaises(mpm.MapproxyError) as cm:
            self.run_seed(gw)
        self.assertEqual(cm.exception.code, "busy")


# --------------------------------------------------------------------------- cleanup, preview, health
class CleanupPreviewHealthTests(unittest.TestCase):
    def gw(self):
        g = FakeGw()
        g.stats_raw["entries"] = {"roads_cache_EPSG3857": {"name": "roads_cache_EPSG3857", "kind": "dir", "bytes": 1, "tiles": 1, "partial": False, "levels": {}},
                                  "basemap_cache_EPSG3857": {"name": "basemap_cache_EPSG3857", "kind": "dir", "bytes": 1, "tiles": 1, "partial": False, "levels": {}}}
        return g

    def test_cleanup_only_touches_the_named_caches_entries(self):
        g = self.gw()
        r = mpm.cleanup(g, "roads_cache", None, dry_run=True)
        self.assertEqual(g.cleanups, [(["roads_cache_EPSG3857"], None, True)])
        self.assertEqual((r["files"], r["bytes"], r["dry_run"]), (10, 2048, True))
        mpm.cleanup(g, "roads_cache", 30, dry_run=False)
        self.assertEqual(g.cleanups[-1], (["roads_cache_EPSG3857"], 30, False))

    def test_cleanup_rejects_unknown_caches_and_handles_empty(self):
        g = self.gw()
        with self.assertRaises(mpm.MapproxyError) as cm:
            mpm.cleanup(g, "nope_cache", None)
        self.assertEqual(cm.exception.code, "not_found")
        for evil in ("../etc", "roads_cache/..", "locks", ""):
            with self.assertRaises(mpm.MapproxyError):
                mpm.cleanup(g, evil, None)
        self.assertEqual(g.cleanups, [])
        g.stats_raw["entries"] = {}
        self.assertEqual(mpm.cleanup(g, "roads_cache", None)["note"], "nothing cached")

    def test_tile_preview_checks_layer_grid_and_range(self):
        g = FakeGw()
        t = mpm.tile_preview(g, "roads", "GLOBAL_WEBMERCATOR", 3, 4, 5)
        self.assertEqual((t.content_type, g.tile_args), ("image/png", ("roads", "GLOBAL_WEBMERCATOR", 3, 4, 5, "png")))
        for args in (("ghost", "GLOBAL_WEBMERCATOR", 0, 0, 0), ("roads", "UTM39N", 0, 0, 0), ("roads", "GLOBAL_WEBMERCATOR", 25, 0, 0), ("roads", "GLOBAL_WEBMERCATOR", 0, -1, 0),
                     ("roads", "GLOBAL_WEBMERCATOR", 0, 0, 2 ** 26)):
            with self.assertRaises(mpm.MapproxyError, msg=str(args)):
                mpm.tile_preview(g, *args)
        g.tile_answer = Tile(200, "text/html", b"<script>alert(1)</script>")
        with self.assertRaises(mpm.MapproxyError) as cm:
            mpm.tile_preview(g, "roads", "GLOBAL_WEBMERCATOR", 0, 0, 0)
        self.assertNotIn("script", str(cm.exception))
        g.tile_answer = Tile(500, "text/plain", b"x")
        with self.assertRaises(mpm.MapproxyError):
            mpm.tile_preview(g, "roads", "GLOBAL_WEBMERCATOR", 0, 0, 0)

    def test_health_findings(self):
        g = FakeGw()
        g.probe_data["caps"] = b'<Capabilities xmlns="http://www.opengis.net/wmts/1.0" xmlns:ows="http://www.opengis.net/ows/1.1"><Contents><Layer><ows:Identifier>roads</ows:Identifier></Layer></Contents></Capabilities>'
        h = mpm.health(g)
        self.assertTrue(h["ok"], h)
        self.assertEqual(h["layers_served"], ["roads"])
        whats = [(f["level"], f["what"]) for f in h["findings"]]
        self.assertIn(("warn", "wmts"), whats)                                      # basemap configured but not advertised
        self.assertIn(("info", "demo"), whats)
        g.probe_data.update(unit="failed", http=502, writable="no", disk_total_kb=1000, disk_free_kb=50, installed=False)
        h = mpm.health(g)
        self.assertFalse(h["ok"])
        text = " | ".join(f["message"] for f in h["findings"])
        for needle in ("not installed", "failed", "HTTP 502", "not writable", "free on the cache volume"):
            self.assertIn(needle, text)
        g.ok = False
        self.assertFalse(mpm.health(g)["config_valid"])

    def test_capabilities_parser_refuses_hostile_xml(self):
        for doc in (b'<!DOCTYPE x [<!ENTITY a "b">]><Capabilities/>', b"<!ENTITY x 'y'>", b"not xml", b""):
            self.assertEqual(mpm.parse_capabilities(doc), [])


# --------------------------------------------------------------------------- script construction
class ScriptTests(unittest.TestCase):
    def capture(self, fn):
        seen = {}

        def fake_run(mode, ssh, script, *, root=False, timeout=40, input_text=None):
            seen.update(script=script, stdin=input_text, root=root)
            return "RESULT " + json.dumps({"ok": True, "errors": [], "warnings": []}) + "\n"
        with mock.patch.object(transport, "run", fake_run):
            fn()
        return seen

    def test_config_content_travels_on_stdin_only(self):
        gw = ScriptGateway("ssh", {"host": "h"})
        seen = self.capture(lambda: gw.apply("secret_marker: TOPSECRET\n", "seeds: {}\n", True))
        self.assertNotIn("TOPSECRET", seen["script"])
        self.assertTrue(seen["root"])
        first, second, _ = seen["stdin"].split("\n")
        self.assertEqual(mps.base64.b64decode(first).decode(), "secret_marker: TOPSECRET\n")
        self.assertEqual(mps.base64.b64decode(second).decode(), "seeds: {}\n")
        self.assertIn("DRY=1", seen["script"])
        self.assertIn("runuser -u mapproxy", seen["script"])                         # validation never runs as root
        self.assertEqual(self.capture(lambda: gw.apply("a: 1\n", None, False))["stdin"].split("\n")[1], "-")

    def test_inputs_are_validated_before_a_shell_exists(self):
        gw = ScriptGateway("local", None)
        for bad in ("../x", "a b", "", "x;id", "a/b", "$(id)", "x" * 90):
            with self.assertRaises(DeployError, msg=bad):
                gw.cleanup([bad], None, True)
            with self.assertRaises(DeployError, msg=bad):
                gw.tile(bad, "GLOBAL_WEBMERCATOR", 0, 0, 0, "png")
            with self.assertRaises(DeployError, msg=bad):
                gw.tile("roads", bad, 0, 0, 0, "png")
        for n in ("locks", "tile_locks"):
            with self.assertRaises(DeployError):
                gw.cleanup([n], None, True)
        for days in (0, -1, 40000):
            with self.assertRaises(DeployError):
                gw.cleanup(["a_cache_EPSG3857"], days, True)
        with self.assertRaises(DeployError):
            gw.tile("roads", "G", 0, 0, 0, "svg")
        with self.assertRaises(DeployError):
            gw.read_backup("../../etc")
        with self.assertRaises(DeployError):
            list(gw.seed("x", 9, True, None))
        with self.assertRaises(DeployError):
            gw.apply("x" * (mps.MAX_FILE + 1), None, True)

    def test_without_pgrep_real_deletion_and_real_seeding_are_refused(self):
        def fake_run(mode, ssh, script, *, root=False, timeout=40, input_text=None):
            return "NOPGREP\n"
        gw = ScriptGateway("local", None)
        with mock.patch.object(transport, "run", fake_run), self.assertRaises(DeployError) as cm:
            gw.cleanup(["a_cache_EPSG3857"], None, False)
        self.assertIn("pgrep", str(cm.exception))
        g = FakeGw()
        g.probe_data["seeding_known"] = False
        with self.assertRaises(mpm.MapproxyError) as cm2:
            list(mpm.seed(g, {"caches": ["roads_cache"], "level_to": 3, "bbox": [39, 41, 46, 43], "dry_run": False, "confirm": True}))
        self.assertIn("pgrep", str(cm2.exception))
        self.assertEqual(g.seed_calls, [])
        list(mpm.seed(g, {"caches": ["roads_cache"], "level_to": 3, "bbox": [39, 41, 46, 43]}))             # an estimate is harmless
        self.assertEqual([c[2] for c in g.seed_calls], [True])

    def test_cleanup_script_guards(self):
        gw = ScriptGateway("local", None)
        seen = self.capture(lambda: gw.cleanup(["a_cache_EPSG3857"], 30, True))
        s = seen["script"]
        for needle in ("pgrep -f '[m]approxy-seed'", "command -v pgrep", "readlink -f", '[ -L "$e" ]', "--one-file-system", "DRY=1", "DAYS=30"):
            self.assertIn(needle, s)

    def test_parsers(self):
        self.assertEqual(mps._check('RESULT {"ok": false, "errors": ["e"], "warnings": ["w"]}\nBACKUP 20261108-120000-abcd\n'),
                         Check(False, ["e"], ["w"], "20261108-120000-abcd"))
        self.assertFalse(mps._check("garbage").ok)
        self.assertEqual(mps._files("@@mapproxy.yaml\n" + mps.b64("a: 1") + "\n@@seed.yaml\n-\n"), {"mapproxy.yaml": "a: 1", "seed.yaml": None})
        with self.assertRaises(DeployError):
            mps._files("@@mapproxy.yaml\n!!!notbase64!!!\n")


# --------------------------------------------------------------------------- API
@unittest.skipUnless(HAVE_FLASK, "flask needed")
class ApiTests(unittest.TestCase):
    NATIVE = {"deploy_target": "native", "targets": ["native"], "project": "mp"}

    def setUp(self):
        self.td = tempfile.mkdtemp()
        p = mock.patch.dict(os.environ, {"GISSTACK_HOME": self.td, "GISSTACK_FEATURES": "mapproxy_manager"})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.td, True)
        from gisstack.api import auth
        from gisstack.webgui.app import create_app
        self.c = create_app("tok").test_client()
        self.tok = {s: auth.TokenStore().create(s, s)[0] for s in ("read", "operate", "admin")}
        self.gw = FakeGw()
        self.gw.stats_raw["entries"] = {"roads_cache_EPSG3857": {"name": "roads_cache_EPSG3857", "kind": "dir", "bytes": 5, "tiles": 2, "partial": False, "levels": {0: 2}}}
        p2 = mock.patch("gisstack.api.routes_mapproxy.ScriptGateway", lambda mode, ssh: self.gw)
        p2.start()
        self.addCleanup(p2.stop)

    def post(self, path, body=None, token="tok", config=None):
        r = self.c.post("/api/v1/mapproxy" + path, json={"mode": "local", "config": config or self.NATIVE, **(body or {})}, headers={"Authorization": f"Bearer {token}"})
        self.addCleanup(r.close)
        return r

    def audit(self):
        p = Path(self.td) / "audit.log"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def test_scopes(self):
        T = self.tok
        for path, body in (("/config", {}), ("/validate", {}), ("/health", {}), ("/cache", {}), ("/config/backups", {})):
            self.assertEqual(self.post(path, body, T["read"]).status_code, 200, path)
        self.assertEqual(self.post("/tile", {"layer": "roads", "grid": "GLOBAL_WEBMERCATOR", "z": 0, "x": 0, "y": 0}, T["read"]).status_code, 200)
        add = {"op": "add_layer", "params": {"name": "x1", "source": "wms", "url": "http://h/wms", "layers": "a"}}
        for path, body in (("/config/edit", add), ("/config/restore", {"id": "20261108-120000-abcd", "confirm": True}),
                           ("/seed", {"caches": ["roads_cache"], "level_to": 3, "bbox": [39, 41, 46, 43]}), ("/cache/cleanup", {"cache": "roads_cache"})):
            for scope in ("read", "operate"):
                self.assertEqual(self.post(path, body, T[scope]).status_code, 403, (path, scope))

    def test_contracts(self):
        from gisstack.api import schema
        from gisstack.api.registry import registry
        for path in ("/config", "/validate", "/health", "/cache", "/config/backups"):
            r = self.post(path)
            self.assertEqual(r.status_code, 200, (path, r.get_json()))
            self.assertEqual(schema.validate(r.get_json()["data"], registry.find("POST", "/mapproxy" + path).response, registry.components), [], path)

    def test_config_never_returns_credentials(self):
        text = self.post("/config").get_data(as_text=True)
        for secret in ("s3cret", "ABC123", "SECRETVALUE"):
            self.assertNotIn(secret, text)

    def test_edit_defaults_to_a_dry_run_and_audits_real_changes(self):
        body = {"op": "add_layer", "params": {"name": "x1", "source": "wms", "url": "http://h/wms", "layers": "a"}}
        d = self.post("/config/edit", body).get_json()["data"]
        self.assertEqual((d["dry_run"], d["backup"]), (True, ""))
        self.assertEqual(self.gw.files["mapproxy.yaml"], BASE)
        self.assertFalse([e for e in self.audit() if e.get("event") == "mapproxy.edit"])
        d = self.post("/config/edit", {**body, "dry_run": False}).get_json()["data"]
        self.assertTrue(d["backup"])
        ev = [e for e in self.audit() if e.get("event") == "mapproxy.edit"][0]
        self.assertEqual((ev["op"], ev["principal"], ev["scope"], ev["backup"]), ("add_layer", "static", "admin", d["backup"]))
        self.assertNotIn("http://h/wms", json.dumps(ev))                                     # content is not copied into the audit log

    def test_edit_errors_become_clean_api_errors(self):
        r = self.post("/config/edit", {"op": "add_layer", "params": {"name": "roads", "source": "wms", "url": "http://h/wms", "layers": "a"}})
        self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (409, "conflict"))
        r = self.post("/config/edit", {"op": "add_layer", "params": {"name": "x1", "source": "wms", "url": "file:///etc/passwd", "layers": "a"}})
        self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (400, "bad_request"))
        self.gw.ok = False
        r = self.post("/config/edit", {"op": "add_layer", "params": {"name": "x2", "source": "wms", "url": "http://h/wms", "layers": "a"}, "dry_run": False})
        self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (400, "config"))
        self.assertEqual(self.post("/config/edit", {"op": "format_disk", "params": {}}).status_code, 400)

    def test_restore_needs_confirm(self):
        self.assertEqual(self.post("/config/restore", {"id": "20261108-120000-abcd"}).status_code, 400)
        self.assertEqual(self.post("/config/restore", {"id": "20261108-120000-abcd", "confirm": False}).status_code, 400)
        self.assertEqual(self.post("/config/restore", {"id": "../../etc", "confirm": True}).status_code, 400)
        self.post("/config/edit", {"op": "add_layer", "params": {"name": "x1", "source": "wms", "url": "http://h/wms", "layers": "a"}, "dry_run": False})
        bid = self.gw.backups()[0]["id"]
        r = self.post("/config/restore", {"id": bid, "confirm": True})
        self.assertEqual(r.get_json()["data"]["restored"], bid)
        self.assertTrue([e for e in self.audit() if e.get("event") == "mapproxy.restore"])

    def test_cleanup_is_a_dry_run_unless_confirmed(self):
        r = self.post("/cache/cleanup", {"cache": "roads_cache"})
        self.assertEqual((r.get_json()["data"]["dry_run"], self.gw.cleanups[-1][2]), (True, True))
        r = self.post("/cache/cleanup", {"cache": "roads_cache", "dry_run": False})
        self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (400, "plan"))
        self.assertEqual(len(self.gw.cleanups), 1)
        r = self.post("/cache/cleanup", {"cache": "roads_cache", "dry_run": False, "confirm": True, "older_than_days": 30})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.gw.cleanups[-1], (["roads_cache_EPSG3857"], 30, False))
        ev = [e for e in self.audit() if e.get("event") == "mapproxy.cleanup"]
        self.assertEqual((len(ev), ev[0]["cache"], ev[0]["older_than_days"]), (1, "roads_cache", 30))
        for bad in ({"cache": "../x"}, {"cache": "a b"}, {"cache": "roads_cache", "older_than_days": 0}):
            self.assertEqual(self.post("/cache/cleanup", bad).status_code, 400, bad)

    def wait(self, jid):
        for _ in range(100):
            d = self.c.get(f"/api/v1/jobs/{jid}", headers={"Authorization": "Bearer tok"}).get_json()["data"]
            if d["done"]:
                return d
            time.sleep(0.05)
        self.fail("job did not finish")

    def test_seed_job_estimate_and_guards(self):
        body = {"caches": ["roads_cache"], "level_to": 4, "bbox": [39.9, 41.0, 46.8, 43.7]}
        jid = self.post("/seed", body).get_json()["data"]["job"]
        d = self.wait(jid)
        self.assertTrue(d["ok"], d)
        self.assertIn("== estimated tiles to create: 12", d["lines"])
        self.assertEqual([c[2] for c in self.gw.seed_calls], [True])
        r = self.post("/seed", {**body, "dry_run": False})
        self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (400, "plan"))
        jid = self.post("/seed", {**body, "dry_run": False, "confirm": True}).get_json()["data"]["job"]
        d = self.wait(jid)
        self.assertTrue(d["ok"])
        self.assertEqual([c[2] for c in self.gw.seed_calls], [True, True, False])
        self.assertTrue([e for e in self.audit() if e.get("event") == "mapproxy.seed" and e["dry_run"] is False])
        for bad in ({"caches": ["nope_cache"]}, {"level_to": 30}, {"bbox": [1, 2, 3, 4], "level_to": 3, "caches": ["roads_cache"], "extra": 1}, {"bbox": [5, 5, 1, 1]}):
            r = self.post("/seed", {**body, **bad})
            self.assertEqual(r.status_code, 400, bad)

    def test_tile_is_an_image_and_errors_are_json(self):
        r = self.post("/tile", {"layer": "roads", "grid": "GLOBAL_WEBMERCATOR", "z": 2, "x": 1, "y": 1})
        self.assertEqual((r.status_code, r.headers["Content-Type"], r.headers["Cache-Control"]), (200, "image/png", "no-store"))
        r = self.post("/tile", {"layer": "ghost", "grid": "GLOBAL_WEBMERCATOR", "z": 2, "x": 1, "y": 1})
        self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (404, "not_found"))
        self.assertEqual(self.post("/tile", {"layer": "roads", "grid": "G", "z": 99, "x": 0, "y": 0}).status_code, 400)

    def test_native_only_and_feature_flag(self):
        r = self.post("/config", config={"deploy_target": "compose", "targets": ["compose"], "project": "mp"})
        self.assertEqual(r.status_code, 400)
        with mock.patch.dict(os.environ, {"GISSTACK_FEATURES": ""}):
            self.assertEqual(self.post("/config").get_json()["error"]["code"], "feature_disabled")


# --------------------------------------------------------------------------- real MapProxy
@unittest.skipUnless(HAVE_MAPPROXY and shutil.which("pgrep"), "mapproxy, bash, curl, base64 and pgrep needed")
class RealMapproxyTests(unittest.TestCase):
    """The real shell scripts, run locally, against a real MapProxy: its own validator, `mapproxy-seed`, a real WSGI app and real cache files."""

    @classmethod
    def setUpClass(cls):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from wsgiref.simple_server import WSGIRequestHandler, make_server

        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (256, 256), (200, 60, 60)).save(buf, "PNG")
        png = buf.getvalue()
        cls.delay = 0.0
        outer = cls

        class Tiles(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                time.sleep(outer.delay)
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(png)))
                self.end_headers()
                self.wfile.write(png)
        class Quiet404(ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                pass                                              # a cancelled seeder drops its connection: not an error here
        cls.up = Quiet404(("127.0.0.1", 0), Tiles)
        threading.Thread(target=cls.up.serve_forever, daemon=True).start()
        cls.tmp = Path(tempfile.mkdtemp())
        for d in ("cfg", "cache", "bk", "bin"):
            (cls.tmp / d).mkdir()
        cls.conf_text = f"""services:
  demo:
  tms: {{use_grid_names: true, origin: nw}}
  wmts:
  wms: {{srs: ['EPSG:3857'], image_formats: ['image/png']}}
layers:
  - {{name: roads, title: Roads, sources: [roads_cache]}}
caches:
  roads_cache:
    grids: [GLOBAL_WEBMERCATOR]
    sources: [roads_src]
    format: image/png
    cache: {{type: file, directory_layout: tms}}
sources:
  roads_src:
    type: tile
    grid: GLOBAL_WEBMERCATOR
    url: http://127.0.0.1:{cls.up.server_address[1]}/%(z)s/%(x)s/%(y)s.png
grids: {{}}
globals:
  cache: {{base_dir: {cls.tmp}/cache, lock_dir: {cls.tmp}/cache/locks, tile_lock_dir: {cls.tmp}/cache/tile_locks}}
"""
        (cls.tmp / "cfg/mapproxy.yaml").write_text(cls.conf_text)
        (cls.tmp / "cfg/seed.yaml").write_text("seeds: {}\n")
        from mapproxy.wsgiapp import make_wsgi_app
        app = make_wsgi_app(str(cls.tmp / "cfg/mapproxy.yaml"), reloader=False)

        class Quiet(WSGIRequestHandler):
            def log_message(self, *a):
                pass
        cls.wsgi = make_server("127.0.0.1", 0, app, handler_class=Quiet)
        threading.Thread(target=cls.wsgi.serve_forever, daemon=True).start()
        (cls.tmp / "bin/systemctl").write_text("#!/bin/sh\necho active\n")
        (cls.tmp / "bin/systemctl").chmod(0o755)
        cls.env = {**os.environ, "PATH": f"{cls.tmp / 'bin'}:{os.environ['PATH']}", "GS_MP_DIR": str(cls.tmp / "cfg"), "GS_MP_CACHE": str(cls.tmp / "cache"),
                   "GS_MP_BACKUPS": str(cls.tmp / "bk"), "GS_MP_VENV": sys.prefix, "GS_MP_RUNAS": "", "GS_MP_PORT": str(cls.wsgi.server_address[1])}
        if not (Path(sys.prefix) / "bin/mapproxy-seed").exists():
            raise unittest.SkipTest("mapproxy-seed is not in this environment's bin directory")
        cls.gw = ScriptGateway("local", None)

    @classmethod
    def tearDownClass(cls):
        for s in (cls.up, cls.wsgi):
            s.shutdown()
            s.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        outer = self

        def local_run(mode, ssh, script, *, root=False, timeout=40, input_text=None):
            return subprocess.run(["bash", "-c", script], input=input_text, capture_output=True, text=True, timeout=timeout, env=outer.env).stdout

        def local_stream(mode, ssh, script, *, root=False, stop=None, deadline=None):
            from gisstack import runner
            yield from runner.stream(["bash", "-c", script], env=outer.env, stop=stop, error=DeployError, echo=False, deadline=deadline, idle=None, grace=2)
        for name, fn in (("run", local_run), ("stream", local_stream)):
            p = mock.patch.object(transport, name, fn)
            p.start()
            self.addCleanup(p.stop)
        self.reset()

    def reset(self):
        (self.tmp / "cfg/mapproxy.yaml").write_text(self.conf_text)
        (self.tmp / "cfg/seed.yaml").write_text("seeds: {}\n")
        shutil.rmtree(self.tmp / "cache", ignore_errors=True)
        (self.tmp / "cache").mkdir()
        shutil.rmtree(self.tmp / "bk", ignore_errors=True)
        type(self).delay = 0.0

    BBOX = [39.9, 41.0, 46.8, 43.7]

    def seed_args(self, **over):
        return {"caches": ["roads_cache"], "level_from": 0, "level_to": 4, "bbox": self.BBOX, "dry_run": False, "confirm": True, **over}

    # ---- validation
    def test_mapproxy_itself_validates_strictly(self):
        f = self.gw.read()
        self.assertTrue(self.gw.apply(f["mapproxy.yaml"], f["seed.yaml"], True).ok)
        typo = self.gw.apply(f["mapproxy.yaml"].replace("caches:", "cachez:", 1), None, True)
        self.assertFalse(typo.ok)
        self.assertIn("cachez", " ".join(typo.errors))
        ref = self.gw.apply(f["mapproxy.yaml"].replace("sources: [roads_src]", "sources: [nosrc]"), None, True)
        self.assertFalse(ref.ok)
        self.assertIn("nosrc", " ".join(ref.errors))
        self.assertFalse(self.gw.apply("a: [unclosed", None, True).ok)
        self.assertFalse(self.gw.apply(f["mapproxy.yaml"], "seeds:\n  x:\n    caches: [nope]\n    coverages: [zz]\n", True).ok)
        self.assertEqual(f["mapproxy.yaml"], (self.tmp / "cfg/mapproxy.yaml").read_text())          # a dry run changes nothing

    def test_the_generated_gisstack_configuration_validates(self):
        from gisstack import config as gconfig, generate
        cfg = gconfig.resolve({"deploy_target": "native", "targets": ["native"], "project": "gen", "mapproxy": {
            "upstream_basemap": "osm", "grids": ["GLOBAL_WEBMERCATOR", "UTM38N"], "seed": {"enabled": True, "levels": [0, 6], "bbox": [39.9, 41.0, 46.8, 43.7]},
            "layers": [{"name": "roads", "title": "Roads", "source": "geoserver", "wms_layers": "ws:roads"}]}}, generate_secrets=True)
        out = generate.generate_bundle(cfg, self.tmp / "bundle")
        d = Path(out) / "native/config/mapproxy"
        res = self.gw.apply((d / "mapproxy.yaml").read_text(), (d / "seed.yaml").read_text(), True)
        self.assertTrue(res.ok, res)

    # ---- edits, backups, restore
    def test_edit_installs_atomically_and_keeps_a_restorable_backup(self):
        before = (self.tmp / "cfg/mapproxy.yaml").read_text()
        r = mpm.edit(self.gw, "add_layer", {"name": "more", "source": "tile", "url": f"http://127.0.0.1:{self.up.server_address[1]}/%(z)s/%(x)s/%(y)s.png"}, dry_run=False)
        now = (self.tmp / "cfg/mapproxy.yaml").read_text()
        self.assertIn("more_cache", now)
        self.assertEqual((self.tmp / "bk" / r["backup"] / "mapproxy.yaml").read_text(), before)
        self.assertFalse(list((self.tmp / "cfg").glob(".*.new")))                                  # no temp files left behind
        self.assertEqual(oct((self.tmp / "cfg/mapproxy.yaml").stat().st_mode & 0o777), "0o644")
        self.assertEqual([b["id"] for b in self.gw.backups()], [r["backup"]])
        res = mpm.restore(self.gw, r["backup"])
        self.assertEqual((self.tmp / "cfg/mapproxy.yaml").read_text(), before)
        self.assertEqual(len(self.gw.backups()), 2)
        self.assertTrue((self.tmp / "bk" / res["backup_of_previous"] / "mapproxy.yaml").read_text().count("more_cache"))

    def test_an_invalid_edit_leaves_files_and_backups_untouched(self):
        before = (self.tmp / "cfg/mapproxy.yaml").read_text()
        res = self.gw.apply(before.replace("sources: [roads_src]", "sources: [nosrc]"), None, False)
        self.assertFalse(res.ok)
        self.assertEqual((self.tmp / "cfg/mapproxy.yaml").read_text(), before)
        self.assertEqual(self.gw.backups(), [])

    def test_only_the_newest_30_backups_are_kept(self):
        for i in range(33):
            (self.tmp / "bk" / f"20200101-0000{i:02d}-aaaa").mkdir(parents=True)
        self.gw.apply(self.conf_text, None, False)
        self.assertEqual(len(self.gw.backups()), 30)
        self.assertTrue(any(b["id"].startswith("2026") or b["id"] > "20200101-000032-aaaa" for b in self.gw.backups()))

    # ---- seeding
    def test_seed_estimate_real_seed_skip_existing_refresh_and_stats(self):
        est = list(mpm.seed(self.gw, self.seed_args(dry_run=True)))
        total = int(next(x for x in est if x.startswith("== estimated")).rsplit(" ", 1)[1])
        self.assertGreater(total, 3)
        self.assertEqual(self.gw.stats()["entries"], {})                                            # an estimate creates nothing
        lines = list(mpm.seed(self.gw, self.seed_args()))
        self.assertEqual(lines[-1], "== seeding finished")
        st = mpm.cache_stats(self.gw)
        roads = [c for c in st["caches"] if c["name"] == "roads_cache"][0]
        self.assertEqual(roads["tiles"], total)
        self.assertGreater(roads["bytes"], 0)
        self.assertEqual(sum(lv["tiles"] for lv in roads["entries"][0]["levels"]), total)
        # the second run must NOT fetch what is already there (MapProxy's own default would re-fetch everything)
        again = list(mpm.seed(self.gw, self.seed_args(dry_run=True)))
        self.assertIn("== estimated tiles to create: 0", again)
        refresh = list(mpm.seed(self.gw, self.seed_args(dry_run=True, refresh=True)))
        self.assertIn(f"== estimated tiles to create: {total}", refresh)

    def test_seeding_can_be_cancelled_and_leaves_no_process_behind(self):
        type(self).delay = 0.4
        stop = threading.Event()
        gen = mpm.seed(self.gw, self.seed_args(level_to=7, concurrency=1), stop)
        got = []
        for line in gen:
            got.append(line)
            if line == "== seeding":
                break
        threading.Timer(1.5, stop.set).start()
        t0 = time.time()
        for line in gen:
            got.append(line)
        self.assertLess(time.time() - t0, 15)
        time.sleep(1.0)
        self.assertEqual(subprocess.run(["pgrep", "-f", "[m]approxy-seed"], capture_output=True).returncode, 1)
        self.assertEqual(got[-1], "== seeding cancelled (tiles created so far are kept)")

    def test_the_tile_limit_stops_a_big_seed_before_any_tile_exists(self):
        with self.assertRaises(mpm.MapproxyError) as cm:
            list(mpm.seed(self.gw, self.seed_args(level_to=12, bbox=[-10, -10, 10, 10], max_tiles=50)))
        self.assertEqual(cm.exception.code, "conflict")
        self.assertEqual(self.gw.stats()["entries"], {})

    # ---- preview and health
    def test_tile_preview_through_the_running_mapproxy(self):
        t = mpm.tile_preview(self.gw, "roads", "GLOBAL_WEBMERCATOR", 1, 0, 0)
        self.assertEqual((t.status, t.content_type, t.body[:4]), (200, "image/png", b"\x89PNG"))
        self.assertEqual(len(mpm.cache_stats(self.gw)["caches"][0]["entries"]), 1)                 # previewing filled the cache
        with self.assertRaises(mpm.MapproxyError):
            mpm.tile_preview(self.gw, "roads", "GLOBAL_WEBMERCATOR", 99, 0, 0)

    def test_health_against_the_real_service(self):
        h = mpm.health(self.gw)
        self.assertEqual((h["http"], h["service"], h["config_valid"]), (200, "active", True))
        self.assertIn("roads", h["layers_served"])
        self.assertTrue(h["ok"], h["findings"])
        self.assertFalse(h["seeding"])

    # ---- cleanup
    def fill(self):
        list(mpm.seed(self.gw, self.seed_args(level_to=3)))
        return self.tmp / "cache"

    def test_cleanup_dry_run_reports_exactly_what_would_go(self):
        cache = self.fill()
        du = int(subprocess.run(["du", "-sb", str(cache / "roads_cache_EPSG3857")], capture_output=True, text=True).stdout.split()[0])
        n = sum(1 for p in (cache / "roads_cache_EPSG3857").rglob("*") if p.is_file())
        r = mpm.cleanup(self.gw, "roads_cache", None, dry_run=True)
        self.assertEqual((r["files"], r["bytes"], r["dry_run"]), (n, du, True))
        self.assertEqual(sum(1 for p in (cache / "roads_cache_EPSG3857").rglob("*") if p.is_file()), n)      # nothing deleted

    def test_cleanup_by_age_removes_only_old_tiles(self):
        cache = self.fill()
        files = sorted(p for p in (cache / "roads_cache_EPSG3857").rglob("*") if p.is_file())
        old = files[: len(files) // 2]
        for p in old:
            os.utime(p, (time.time() - 90 * 86400,) * 2)
        r = mpm.cleanup(self.gw, "roads_cache", 30, dry_run=False)
        self.assertEqual(r["files"], len(old))
        left = [p for p in (cache / "roads_cache_EPSG3857").rglob("*") if p.is_file()]
        self.assertEqual(sorted(left), sorted(set(files) - set(old)))
        self.assertFalse([d for d in (cache / "roads_cache_EPSG3857").rglob("*") if d.is_dir() and not any(d.iterdir())])      # emptied directories are pruned

    def test_cleanup_everything_then_the_cache_refills_on_demand(self):
        cache = self.fill()
        r = mpm.cleanup(self.gw, "roads_cache", None, dry_run=False)
        self.assertGreater(r["files"], 0)
        self.assertFalse((cache / "roads_cache_EPSG3857").exists())
        self.assertTrue(cache.exists())
        t = mpm.tile_preview(self.gw, "roads", "GLOBAL_WEBMERCATOR", 0, 0, 0)
        self.assertEqual(t.status, 200)

    def test_cleanup_never_follows_symlinks_or_leaves_the_cache_dir(self):
        victim = self.tmp / "victim"
        victim.mkdir()
        (victim / "precious.txt").write_text("keep")
        (self.tmp / "cache" / "roads_cache_EVIL").symlink_to(victim)
        r = self.gw.cleanup(["roads_cache_EVIL"], None, False)
        self.assertEqual(r, [{"entry": "roads_cache_EVIL", "skipped": "missing"}])
        self.assertEqual((victim / "precious.txt").read_text(), "keep")
        for name in ("..", "../victim", "a/b", "locks"):
            with self.assertRaises(DeployError):
                self.gw.cleanup([name], None, False)

    def test_cleanup_refuses_while_seeding_runs(self):
        cache = self.fill()
        p = subprocess.Popen(["bash", "-c", "exec -a mapproxy-seed sleep 20"])
        try:
            time.sleep(0.3)
            with self.assertRaises(DeployError) as cm:
                self.gw.cleanup(["roads_cache_EPSG3857"], None, False)
            self.assertIn("seeding is running", str(cm.exception))
            self.assertTrue((cache / "roads_cache_EPSG3857").exists())
            with self.assertRaises(mpm.MapproxyError) as cm2:
                list(mpm.seed(self.gw, self.seed_args(dry_run=True)))
            self.assertEqual(cm2.exception.code, "busy")
        finally:
            p.kill()
            p.wait()


if __name__ == "__main__":
    unittest.main()
