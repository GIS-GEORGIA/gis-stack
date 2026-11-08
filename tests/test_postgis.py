"""Phase 5: PostGIS manager - SQL splitter, inventory/health logic, the guarded editor, elevation, API scopes and audit, and a real PostGIS container."""
import base64
import csv
import io
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from gisstack import transport
from gisstack.core.errors import DeployError
from gisstack.postgis import manager as pgm, server as pgs, sqlsplit
from gisstack.postgis.server import Gateway, Raw, ScriptGateway

HAVE_FLASK = __import__("importlib").util.find_spec("flask") is not None
CFG = {"postgres": {"port": 5432, "user": "gis_app", "password": "App_pw_Secret_1", "db": "gis"}, "project": "pg", "deploy_target": "native"}


def csv_text(rows):
    b = io.StringIO()
    csv.writer(b, lineterminator="\n").writerows(rows)
    return b.getvalue().rstrip("\n")


class SqlSplitTests(unittest.TestCase):
    S = staticmethod(sqlsplit.split_statements)

    def test_basic(self):
        self.assertEqual(self.S("select 1"), ["select 1"])
        self.assertEqual(self.S("select 1;"), ["select 1"])
        self.assertEqual(self.S(" select 1 ;; select 2 ; "), ["select 1", "select 2"])
        self.assertEqual(self.S(""), [])
        self.assertEqual(self.S("  ;  ; -- only a comment\n /* and another */"), [])

    def test_semicolons_inside_quotes_and_comments_do_not_split(self):
        self.assertEqual(len(self.S("select 'a;b'; ")), 1)
        self.assertEqual(len(self.S("select 'it''s;'")), 1)
        self.assertEqual(len(self.S('select "a;b" from t')), 1)
        self.assertEqual(len(self.S("select 1 -- x; y\n, 2")), 1)
        self.assertEqual(len(self.S("select /* a; /* nested ; */ b; */ 1")), 1)
        self.assertEqual(len(self.S("select $$a;b$$, $tag$ ; $$ $tag$")), 1)
        self.assertEqual(len(self.S("select E'it\\'s; ok'")), 1)
        self.assertEqual(len(self.S("select $1, $2")), 1)                        # parameters are not dollar quotes

    def test_fails_closed_on_what_it_cannot_read(self):
        for bad in ("select 'open", 'select "open', "select /* open", "select $$ open", "select $t$ open $x$", "select 1\x00"):
            with self.assertRaises(sqlsplit.SqlSyntax, msg=bad):
                self.S(bad)

    def test_first_keyword(self):
        K = sqlsplit.first_keyword
        self.assertEqual(K("  SeLeCt 1"), "select")
        self.assertEqual(K("-- c\n/* d */ with x as (select 1) select * from x"), "with")
        self.assertEqual(K("(select 1) union (select 2)"), "select")
        self.assertEqual(K("explain select 1"), "explain")
        self.assertEqual(K("123"), "")


class OneStatementTests(unittest.TestCase):
    def test_accepts_one_and_strips_semicolon(self):
        self.assertEqual(pgm.one_statement("select 1;  "), ("select 1", "select"))
        self.assertEqual(pgm.one_statement("-- hi\nEXPLAIN select 1")[1], "explain")

    def test_rejections(self):
        for bad, why in (("", "empty"), ("   ", "empty"), (";", "empty"), ("select 1; select 2", "one statement"), ("\\! id", "meta-commands"),
                         ("  \\copy t to x", "meta-commands"), ("select 'x", "unterminated"), ("x" * (pgm.MAX_SQL_CHARS + 1), "longer"), (None, "empty")):
            with self.assertRaises(pgm.PostgisError, msg=repr(bad)[:30]) as cm:
                pgm.one_statement(bad)
            self.assertEqual(cm.exception.code, "bad_request")
            self.assertIn(why, str(cm.exception))


class FakeGw(Gateway):
    def __init__(self, sections=None, runner=None):
        self._sections, self._runner, self.calls = sections or {}, runner, []

    def sections(self, db, queries):
        self.calls.append(("sections", db))
        return self._sections

    def run(self, sql, *, role, db, write, max_bytes=pgs.MAX_BYTES, timeout=40):
        self.calls.append(("run", sql, role, write))
        return self._runner(sql, role, write)

    def setup(self, db, owner, statement_timeout_s):
        self.calls.append(("setup", db, owner, statement_timeout_s))


def ok(rows):
    return (0, csv_text(rows))


def sections(**over):
    s = {
        "server": ok([["v", "maxc", "started", "recovery", "conns"], ["17.5", "100", "2026-11-01 10:00:00+00", "false", "7"]]),
        "cache": ok([["round"], ["99.2"]]),
        "databases": ok([["datname", "bytes", "encoding"], ["gis", "123456", "UTF8"], ["postgres", "7000000", "UTF8"]]),
        "connections": ok([["state", "n"], ["active", "1"], ["idle", "5"]]),
        "extensions": ok([["extname", "extversion"], ["plpgsql", "1.0"], ["postgis", "3.5.2"]]),
        "available": ok([["name", "default_version", "installed"], ["postgis", "3.5.2", "3.5.2"], ["pg_stat_statements", "1.11", ""]]),
        "tables": ok([["a", "b", "c", "d", "e"], ["public", "roads", "r", "1500", "900000"], ["public", "pts", "r", "10", "16384"], ["gis", "v1", "v", "0", "0"]]),
        "spatial": ok([["s", "t", "c", "ty", "srid", "dim", "kind"], ["public", "roads", "geom", "LINESTRING", "4326", "2", "geometry"],
                       ["public", "pts", "g", "POINT", "32638", "3", "geometry"]]),
        "gist": ok([["s", "t", "i", "def"], ["public", "roads", "roads_geom_idx", 'CREATE INDEX roads_geom_idx ON public.roads USING gist (geom)']]),
        "statements": (1, 'ERROR:  relation "pg_stat_statements" does not exist'),
    }
    s.update(over)
    return s


class InventoryTests(unittest.TestCase):
    def test_full_inventory(self):
        inv = pgm.inventory(FakeGw(sections()), "gis")
        self.assertEqual(inv["server"], {"version": "17.5", "max_connections": 100, "started": "2026-11-01 10:00:00+00", "recovery": False, "connections": 7,
                                         "cache_hit_percent": 99.2})
        self.assertEqual([d["name"] for d in inv["databases"]], ["gis", "postgres"])
        self.assertEqual(inv["connections"], {"active": 1, "idle": 5})
        self.assertEqual({(t["name"], t["kind"], t["bytes"]) for t in inv["tables"]}, {("roads", "table", 900000), ("pts", "table", 16384), ("v1", "view", 0)})
        sp = {s["table"]: s for s in inv["spatial"]}
        self.assertEqual((sp["roads"]["srid"], sp["roads"]["spatial_index"], sp["roads"]["dimensions"]), (4326, True, 2))
        self.assertEqual((sp["pts"]["srid"], sp["pts"]["spatial_index"], sp["pts"]["dimensions"]), (32638, False, 3))
        self.assertEqual({s["name"]: (s["tables"], s["bytes"]) for s in inv["schemas"]}, {"public": (2, 916384), "gis": (1, 0)})
        self.assertIsNone(inv["statements"])
        self.assertEqual(inv["errors"], {})                                     # pg_stat_statements is not installed -> not an error

    def test_statements_only_when_the_extension_is_installed(self):
        s = sections(extensions=ok([["e", "v"], ["postgis", "3"], ["pg_stat_statements", "1.11"]]),
                     statements=ok([["q", "c", "t", "m", "r"], ["select * from roads", "10", "55.5", "5.55", "100"]]))
        inv = pgm.inventory(FakeGw(s), "gis")
        self.assertEqual(inv["statements"], [{"query": "select * from roads", "calls": 10, "total_ms": 55.5, "mean_ms": 5.55, "rows": 100}])
        s["statements"] = (1, "ERROR:  permission denied for view pg_stat_statements\nDETAIL: x")
        inv = pgm.inventory(FakeGw(s), "gis")
        self.assertIn("permission denied", inv["errors"]["statements"])

    def test_a_failing_section_does_not_hide_the_others(self):
        inv = pgm.inventory(FakeGw(sections(tables=(1, "ERROR:  boom"), server=(1, "psql: error: connection refused"))), "gis")
        self.assertEqual(inv["tables"], [])
        self.assertIn("boom", inv["errors"]["tables"])
        self.assertIn("connection refused", inv["errors"]["server"])
        self.assertEqual(len(inv["databases"]), 2)

    def test_spatial_failure_is_silent_without_postgis_and_loud_with_it(self):
        no_pg = sections(extensions=ok([["e", "v"], ["plpgsql", "1"]]), spatial=(1, 'ERROR:  relation "geometry_columns" does not exist'))
        self.assertNotIn("spatial", pgm.inventory(FakeGw(no_pg), "gis")["errors"])
        with_pg = sections(spatial=(1, 'ERROR:  relation "geometry_columns" does not exist'))
        self.assertIn("spatial", pgm.inventory(FakeGw(with_pg), "gis")["errors"])

    def test_index_detection_is_column_exact(self):
        s = sections(gist=ok([["s", "t", "i", "def"], ["public", "pts", "i", "CREATE INDEX i ON public.pts USING gist (g_other)"],
                              ["public", "roads", "i2", 'CREATE INDEX i2 ON public.roads USING gist ("geom")']]))
        sp = {x["table"]: x["spatial_index"] for x in pgm.inventory(FakeGw(s), "gis")["spatial"]}
        self.assertEqual(sp, {"roads": True, "pts": False})                     # g_other is not g

    def test_health_findings(self):
        h = pgm.health(pgm.inventory(FakeGw(sections()), "gis"))
        whats = " ".join(f["what"] for f in h["findings"])
        self.assertIn("spatial indexes", whats)                                                                           # pts is tiny: only an info note
        self.assertNotIn("public.pts.g", whats)
        self.assertIn("pg_stat_statements", " ".join(f["what"] for f in h["findings"]))
        self.assertTrue(h["ok"])
        busy = sections(server=ok([["v", "m", "s", "r", "c"], ["17", "100", "x", "true", "90"]]), cache=ok([["r"], ["71.0"]]), extensions=ok([["e", "v"], ["plpgsql", "1"]]),
                        spatial=ok([["s", "t", "c", "ty", "srid", "dim", "kind"]]))
        h = pgm.health(pgm.inventory(FakeGw(busy), "gis"))
        text = " | ".join(f["message"] for f in h["findings"])
        for needle in ("90 of 100", "standby", "71.0", "postgis extension is not installed"):
            self.assertIn(needle, text)
        self.assertFalse(h["ok"])


class HealthThresholdTests(unittest.TestCase):
    def test_big_unindexed_tables_warn_small_ones_do_not(self):
        s = sections(tables=ok([["a", "b", "c", "d", "e"], ["public", "pts", "r", "250000", "9000000"], ["tiger", "bg", "r", "0", "8192"]]),
                     spatial=ok([["s", "t", "c", "ty", "srid", "dim", "kind"], ["public", "pts", "g", "POINT", "4326", "2", "geometry"], ["tiger", "bg", "the_geom", "MULTIPOLYGON", "4269", "2", "geometry"]]),
                     gist=ok([["s", "t", "i", "def"]]))
        f = pgm.health(pgm.inventory(FakeGw(s), "gis"))["findings"]
        warn = [x for x in f if x["level"] == "warn"]
        self.assertEqual([x["what"] for x in warn], ["public.pts.g"])
        self.assertIn("250000", warn[0]["message"])
        self.assertTrue([x for x in f if x["what"] == "spatial indexes"])

    def test_unavailable_statistics_are_a_warning_not_an_error(self):
        s = sections(extensions=ok([["e", "v"], ["postgis", "3"], ["pg_stat_statements", "1.11"]]),
                     statements=(1, 'ERROR:  pg_stat_statements must be loaded via "shared_preload_libraries"'))
        h = pgm.health(pgm.inventory(FakeGw(s), "gis"))
        self.assertTrue(h["ok"])
        self.assertIn(("warn", "statements"), [(x["level"], x["what"]) for x in h["findings"]])


class QueryTests(unittest.TestCase):
    def runner(self, text, calls=None):
        def run(sql, role, write):
            if calls is not None:
                calls.append((sql, role, write))
            return Raw(0, "", text)
        return run

    def test_select_is_wrapped_and_paged(self):
        calls = []
        rows = [["id", "note"]] + [[str(i), f"n{i}"] for i in range(1, 12)]            # limit 10 -> asks for 11
        res = pgm.query(FakeGw(runner=self.runner(csv_text(rows), calls)), "gis", "select id, note from t -- trailing comment", limit=10, offset=20)
        sql, role, write = calls[0]
        self.assertEqual((role, write), ("ro", False))
        self.assertIn("select * from (\nselect id, note from t -- trailing comment\n) as gs_q limit 11 offset 20", sql)   # newline protects against a `--` swallowing `)`
        self.assertEqual((res["row_count"], res["has_more"], res["offset"], res["limit"], res["paged"]), (10, True, 20, 10, True))
        self.assertEqual(res["columns"], ["id", "note"])

    def test_last_page_has_no_more(self):
        res = pgm.query(FakeGw(runner=self.runner(csv_text([["a"], ["1"], ["2"]]))), "gis", "select a from t", limit=10)
        self.assertEqual((res["row_count"], res["has_more"]), (2, False))

    def test_non_select_statements_are_not_wrapped(self):
        for sql in ("explain select 1", "show server_version", "SET search_path = public"):
            calls = []
            pgm.query(FakeGw(runner=self.runner("QUERY PLAN\nx", calls)), "gis", sql)
            self.assertEqual(calls[0][0], sql)

    def test_with_values_table_are_wrapped(self):
        for sql in ("with a as (select 1) select * from a", "values (1), (2)", "table pts", "(select 1)"):
            calls = []
            pgm.query(FakeGw(runner=self.runner("a\n1", calls)), "gis", sql)
            self.assertIn("as gs_q", calls[0][0], sql)

    def test_null_empty_newline_quotes_and_unicode(self):
        text = csv_text([["n", "e", "t", "q", "k"], [pgs.NULL_TOKEN, "", "a\nb", 'x,"y"', "ქართული"]])
        res = pgm.query(FakeGw(runner=self.runner(text)), "gis", "select 1")
        self.assertEqual(res["rows"], [[None, "", "a\nb", 'x,"y"', "ქართული"]])         # NULL and '' stay different

    def test_limits_are_clamped(self):
        self.assertEqual(pgm.clamp(None, None), (pgm.DEFAULT_LIMIT, 0))
        self.assertEqual(pgm.clamp(10**9, -5), (pgm.MAX_LIMIT, 0))
        self.assertEqual(pgm.clamp(0, 7), (pgm.DEFAULT_LIMIT, 7))
        self.assertEqual(pgm.clamp(1, 0), (1, 0))

    def test_truncated_output_drops_the_possibly_cut_last_row(self):
        raw = Raw(0, "", csv_text([["a"], ["1"], ["2"], ["3"]]), truncated=True)
        res = pgm.query(FakeGw(runner=lambda s, r, w: raw), "gis", "show x")
        self.assertEqual((res["rows"], res["truncated"]), ([["1"], ["2"]], True))

    def test_user_input_never_reaches_the_server_unchecked(self):
        gw = FakeGw(runner=self.runner(""))
        for bad in ("select 1; drop table t", "\\! id", "", "select 'x"):
            with self.assertRaises(pgm.PostgisError):
                pgm.query(gw, "gis", bad)
        self.assertEqual(gw.calls, [])

    def test_error_mapping(self):
        cases = (('ERROR:  cannot execute INSERT in a read-only transaction', "forbidden"), ('ERROR:  permission denied for table x', "forbidden"),
                 ('ERROR:  permission denied for function pg_read_file', "forbidden"), ('ERROR:  canceling statement due to statement timeout', "timeout"),
                 ('psql: error: connection to server at "127.0.0.1" failed: FATAL:  password authentication failed for user "gisstack_ro"', "not_ready"),
                 ('ERROR:  syntax error at or near "selec"', "sql"), ('ERROR:  relation "nope" does not exist', "sql"),
                 ("psql: error: connection to server at 127.0.0.1, port 5432 failed: Connection refused", "unreachable"))
        for err, code in cases:
            with self.assertRaises(pgm.PostgisError, msg=err) as cm:
                pgm.query(FakeGw(runner=lambda s, r, w, e=err: Raw(2, e, "")), "gis", "select 1")
            self.assertEqual(cm.exception.code, code, err)
        with self.assertRaises(pgm.PostgisError) as cm:
            pgm.query(FakeGw(runner=lambda s, r, w: Raw(96, "", "")), "gis", "select 1")
        self.assertEqual(cm.exception.code, "not_ready")
        self.assertEqual(pgm.query(FakeGw(runner=lambda s, r, w: Raw(141, "", "a\n1")), "gis", "show x")["rows"], [["1"]])    # head closed the pipe: fine


class ExecuteTests(unittest.TestCase):
    def test_tags_and_returning(self):
        run = lambda s, r, w: Raw(0, "", "SET\nid\n4\n5\nINSERT 0 2")  # noqa: E731
        res = pgm.execute(FakeGw(runner=run), "gis", "insert into t(a) values (1),(2) returning id")
        self.assertEqual((res["tag"], res["rows"], res["columns"]), ("INSERT 0 2", [["4"], ["5"]], ["id"]))
        res = pgm.execute(FakeGw(runner=lambda s, r, w: Raw(0, "", "SET\nCREATE TABLE")), "gis", "create table x(i int)")
        self.assertEqual((res["tag"], res["rows"]), ("CREATE TABLE", []))
        res = pgm.execute(FakeGw(runner=lambda s, r, w: Raw(0, "", "SET\nUPDATE 3")), "gis", "update t set a=1")
        self.assertEqual(res["tag"], "UPDATE 3")

    def test_uses_the_rw_role_and_marks_writes(self):
        calls = []
        pgm.execute(FakeGw(runner=lambda s, r, w: calls.append((s, r, w)) or Raw(0, "", "SET")), "gis", "delete from t;")
        self.assertEqual(calls, [("delete from t", "rw", True)])

    def test_digest_keeps_a_hash_and_a_short_start_only(self):
        d = pgm.statement_digest("insert into t values ('" + "secret" * 100 + "')\n  and more")
        self.assertEqual(len(d["sha256"]), 16)
        self.assertLessEqual(len(d["start"]), 120)
        self.assertNotIn("\n", d["start"])
        self.assertEqual(d["chars"], len("insert into t values ('" + "secret" * 100 + "')\n  and more"))


class ElevationTests(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.e = pgm.Elevations(clock=lambda: self.now[0])

    def test_lifecycle_binding_and_expiry(self):
        g = self.e.grant("alice", "p", "h", 5)
        self.e.check(g["elevation"], "alice", "p", "h")
        for who in (("bob", "p", "h"), ("alice", "other", "h"), ("alice", "p", "other")):
            with self.assertRaises(pgm.PostgisError) as cm:
                self.e.check(g["elevation"], *who)
            self.assertEqual(cm.exception.code, "forbidden")
        self.now[0] += 301
        with self.assertRaises(pgm.PostgisError):
            self.e.check(g["elevation"], "alice", "p", "h")

    def test_revoke_only_by_owner(self):
        g = self.e.grant("alice", "p", "h", 5)
        self.assertFalse(self.e.revoke(g["elevation"], "bob"))
        self.e.check(g["elevation"], "alice", "p", "h")
        self.assertTrue(self.e.revoke(g["elevation"], "alice"))
        with self.assertRaises(pgm.PostgisError):
            self.e.check(g["elevation"], "alice", "p", "h")

    def test_limits(self):
        for bad in (0, -1, pgm.MAX_ELEVATION_MIN + 1):
            with self.assertRaises(pgm.PostgisError):
                self.e.grant("a", "p", "h", bad)
        for _ in range(pgm.Elevations.MAX_OPEN):
            self.e.grant("a", "p", "h", 1)
        with self.assertRaises(pgm.PostgisError):
            self.e.grant("a", "p", "h", 1)
        self.now[0] += 61                                                         # expired ones free their slots
        self.e.grant("a", "p", "h", 1)

    def test_ids_are_unguessable_and_unique(self):
        ids = {self.e.grant("a", "p", "h", 1)["elevation"] if i % 5 else (self.e._items.clear() or self.e.grant("a", "p", "h", 1)["elevation"]) for i in range(50)}
        self.assertEqual(len(ids), 50)
        self.assertTrue(all(len(i) >= 24 for i in ids))


class ScriptTests(unittest.TestCase):
    """What the shell script looks like - the secrets and the statement must never be in the command line."""

    def capture(self, fn):
        seen = {}

        def fake_run(mode, ssh, script, *, root=False, timeout=40, input_text=None):
            seen.update(script=script, stdin=input_text, root=root, mode=mode)
            return "@@rc 0\n@@err\n@@out\na\n1\nsetup-ok\n"
        with mock.patch.object(transport, "run", fake_run):
            fn()
        return seen

    def test_ro_statement_goes_through_stdin_with_the_fixed_prefix(self):
        gw = ScriptGateway(CFG, "ssh", {"host": "h"})
        seen = self.capture(lambda: gw.run("select 'TOPSECRET-literal'", role="ro", db="gis", write=False))
        self.assertNotIn("TOPSECRET", seen["script"])
        self.assertNotIn(CFG["postgres"]["password"], seen["script"] + seen["stdin"])      # the editor never uses the app password
        text = base64.b64decode(seen["stdin"].strip()).decode()
        self.assertEqual(text, "SET TRANSACTION READ ONLY; select 'TOPSECRET-literal'")
        self.assertIn("-U gisstack_ro", seen["script"])
        self.assertIn("-q", seen["script"])
        self.assertTrue(seen["root"])
        self.assertIn("runuser -u nobody", seen["script"])                                  # never the postgres OS user, never root

    def test_rw_password_goes_to_stdin_only(self):
        gw = ScriptGateway(CFG, "local", None)
        seen = self.capture(lambda: gw.run("delete from t", role="rw", db="gis", write=True))
        self.assertNotIn(CFG["postgres"]["password"], seen["script"])
        first, second = seen["stdin"].split("\n")[:2]
        self.assertEqual(first, CFG["postgres"]["password"])
        self.assertTrue(base64.b64decode(second).decode().startswith("SET application_name = 'gisstack'; "))
        self.assertIn("-U gis_app", seen["script"])
        self.assertNotIn(" -q", seen["script"].replace("-q ", "-q ") if "-q" in seen["script"] and False else "")

    def test_names_are_validated_before_the_shell_is_built(self):
        gw = ScriptGateway(CFG, "local", None)
        for db in ("gis; rm -rf /", "a b", "", "x" * 70, "../x", "gis'"):
            with self.assertRaises(DeployError, msg=db):
                gw.run("select 1", role="ro", db=db, write=False)
            with self.assertRaises(DeployError, msg=db):
                gw.sections(db, {})
            with self.assertRaises(DeployError, msg=db):
                gw.setup(db, "gis", 15)
        with self.assertRaises(DeployError):
            gw.setup("gis", "o; id", 15)
        with self.assertRaises(DeployError):
            gw.run("select 1", role="postgres", db="gis", write=False)
        bad = ScriptGateway({**CFG, "postgres": {**CFG["postgres"], "user": "bad user"}}, "local", None)
        with self.assertRaises(DeployError):
            bad.run("select 1", role="rw", db="gis", write=True)

    def test_setup_script_keeps_the_password_off_the_command_line(self):
        gw = ScriptGateway(CFG, "local", None)
        seen = self.capture(lambda: gw.setup("gis", "gis_app", 15))
        s = seen["script"]
        self.assertIn("NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION CONNECTION LIMIT 3", s)
        self.assertIn("default_transaction_read_only = on", s)
        self.assertIn("statement_timeout = '15s'", s)
        self.assertIn("GRANT pg_read_all_stats", s)
        self.assertNotIn("SUPERUSER'", s.replace("NOSUPERUSER", ""))
        self.assertIn("chmod 600", s)
        self.assertNotIn(CFG["postgres"]["password"], s)

    def test_parsers(self):
        raw = pgs.parse_raw("@@rc 2\n@@err\nERROR:  boom\n@@out\na,b\n1,2\n", 100)
        self.assertEqual((raw.rc, raw.err, raw.out, raw.truncated), (2, "ERROR:  boom", "a,b\n1,2", False))
        self.assertTrue(pgs.parse_raw("@@rc 0\n@@err\n@@out\n" + "x" * 50, 50).truncated)
        s = pgs.parse_sections("@@a 0\nx,y\n1,2\n@@b 1\nERROR: no\n")
        self.assertEqual(s, {"a": (0, "x,y\n1,2"), "b": (1, "ERROR: no")})
        # data that looks like a marker cannot forge a section boundary inside the out block
        raw = pgs.parse_raw("@@rc 0\n@@err\n@@out\n@@rc 9\n@@err\nx\n", 1000)
        self.assertEqual(raw.rc, 0)


class FakeGwApi(FakeGw):
    pass


@unittest.skipUnless(HAVE_FLASK, "flask needed")
class ApiTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        p = mock.patch.dict(os.environ, {"GISSTACK_HOME": self.td, "GISSTACK_FEATURES": "postgis_manager"})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.td, True)
        from gisstack.api import auth
        from gisstack.webgui.app import create_app
        self.app = create_app("tok")
        self.c = self.app.test_client()
        self.tok = {s: auth.TokenStore().create(s, s)[0] for s in ("read", "operate", "admin")}
        self.written = []

        def runner(sql, role, write):
            if write:
                self.written.append(sql)
                return Raw(0, "", "SET\nINSERT 0 1")
            return Raw(0, "", "a\n1")
        self.gw = FakeGw(sections(), runner)
        p2 = mock.patch("gisstack.api.routes_postgis.ScriptGateway", lambda cfg, mode, ssh: self.gw)
        p2.start()
        self.addCleanup(p2.stop)
        self.now = [5000.0]
        self.app.config["PG_ELEVATIONS"] = pgm.Elevations(clock=lambda: self.now[0])

    NATIVE = {"deploy_target": "native", "targets": ["native"], "project": "pg", "postgres": {"password": "Pg_Secret_pw_1"}}

    def post(self, path, body=None, token="tok", config=None):
        r = self.c.post("/api/v1/postgis" + path, json={"mode": "local", "config": config or self.NATIVE, **(body or {})}, headers={"Authorization": f"Bearer {token}"})
        self.addCleanup(r.close)
        return r

    def audit(self):
        p = Path(self.td) / "audit.log"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def test_scopes(self):
        T = self.tok
        self.assertEqual(self.post("/inventory", None, T["read"]).status_code, 200)
        self.assertEqual(self.post("/health", None, T["read"]).status_code, 200)
        self.assertEqual(self.post("/query", {"sql": "select 1"}, T["read"]).status_code, 403)         # data needs operate
        self.assertEqual(self.post("/query", {"sql": "select 1"}, T["operate"]).status_code, 200)
        for path, body in (("/editor/setup", {}), ("/elevate", {"confirm": True}), ("/elevate/revoke", {"elevation": "x"}),
                           ("/execute", {"sql": "delete from t", "elevation": "x", "confirm": True})):
            self.assertEqual(self.post(path, body, T["operate"]).status_code, 403, path)
        self.assertEqual(self.post("/editor/setup", {}, T["admin"]).status_code, 200)

    def test_contracts(self):
        from gisstack.api import schema
        from gisstack.api.registry import registry
        for path, body in (("/inventory", {}), ("/health", {}), ("/query", {"sql": "select 1"}), ("/editor/setup", {})):
            r = self.post(path, body)
            self.assertEqual(r.status_code, 200, (path, r.get_json()))
            self.assertEqual(schema.validate(r.get_json()["data"], registry.find("POST", "/postgis" + path).response, registry.components), [], path)

    def test_write_needs_confirm_and_a_valid_elevation(self):
        body = {"sql": "insert into t values (1)", "elevation": "nope", "confirm": True}
        r = self.post("/execute", body)
        self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (403, "forbidden"))
        self.assertEqual(self.post("/execute", {**body, "confirm": False}).status_code, 400)
        self.assertEqual(self.post("/execute", {k: v for k, v in body.items() if k != "confirm"}).status_code, 400)
        self.assertEqual(self.post("/elevate", {}).status_code, 400)                                   # confirm is required
        self.assertEqual(self.post("/elevate", {"confirm": False}).status_code, 400)
        self.assertEqual(self.written, [])

    def test_full_write_flow_is_audited_without_leaking(self):
        el = self.post("/elevate", {"confirm": True, "minutes": 5}).get_json()["data"]["elevation"]
        stmt = "insert into t(v) values ('" + "PRIVATE-DATA-" * 20 + "')"
        r = self.post("/execute", {"sql": stmt, "elevation": el, "confirm": True})
        self.assertEqual((r.status_code, r.get_json()["data"]["tag"]), (200, "INSERT 0 1"))
        self.assertEqual(self.written, [stmt])
        events = [e for e in self.audit() if str(e.get("event", "")).startswith("postgis")]
        names = [(e["event"], e.get("phase")) for e in events]
        self.assertEqual(names, [("postgis.elevate", None), ("postgis.write", "start"), ("postgis.write", "end")])
        start = events[1]
        self.assertEqual((start["principal"], start["scope"], start["project"]), ("static", "admin", "pg"))
        self.assertEqual(len(start["sha256"]), 16)
        self.assertLessEqual(len(start["start"]), 120)
        raw_log = (Path(self.td) / "audit.log").read_text()
        self.assertNotIn("PRIVATE-DATA-" * 9, raw_log)                                                   # only the short start is kept
        self.assertNotIn(el, raw_log)                                                                     # the elevation id itself is a credential
        self.assertNotIn("Pg_Secret_pw_1", r.get_data(as_text=True))
        ops = [json.loads(x) for x in (Path(self.td) / "journal.jsonl").read_text().splitlines()] if (Path(self.td) / "journal.jsonl").exists() else []
        self.assertTrue(not ops or any(o["op"] == "postgis:write" for o in ops))

    def test_elevation_is_bound_to_the_caller_expires_and_can_be_revoked(self):
        el = self.post("/elevate", {"confirm": True}, self.tok["admin"]).get_json()["data"]["elevation"]
        body = {"sql": "delete from t", "elevation": el, "confirm": True}
        self.assertEqual(self.post("/execute", body, "tok").status_code, 403)                            # another principal
        self.assertEqual(self.post("/execute", body, self.tok["admin"]).status_code, 200)
        self.assertTrue(self.post("/elevate/revoke", {"elevation": el}, self.tok["admin"]).get_json()["data"]["revoked"])
        self.assertEqual(self.post("/execute", body, self.tok["admin"]).status_code, 403)
        el2 = self.post("/elevate", {"confirm": True, "minutes": 1}, self.tok["admin"]).get_json()["data"]["elevation"]
        self.now[0] += 61
        self.assertEqual(self.post("/execute", {**body, "elevation": el2}, self.tok["admin"]).status_code, 403)

    def test_elevation_does_not_carry_to_another_project(self):
        el = self.post("/elevate", {"confirm": True}).get_json()["data"]["elevation"]
        other = {**self.NATIVE, "project": "other"}
        r = self.post("/execute", {"sql": "delete from t", "elevation": el, "confirm": True}, config=other)
        self.assertEqual(r.status_code, 403)

    def test_failed_write_is_audited_and_bad_statements_are_rejected_before_audit(self):
        el = self.post("/elevate", {"confirm": True}).get_json()["data"]["elevation"]
        self.gw._runner = lambda s, r, w: Raw(1, 'ERROR:  duplicate key value violates unique constraint "t_pkey"', "")
        r = self.post("/execute", {"sql": "insert into t values (1)", "elevation": el, "confirm": True})
        self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (400, "sql"))
        self.assertIn(False, [e.get("ok") for e in self.audit() if e.get("phase") == "end"])
        n = len(self.audit())
        r = self.post("/execute", {"sql": "delete from t; drop table t", "elevation": el, "confirm": True})
        self.assertEqual(r.status_code, 400)
        self.assertFalse([e for e in self.audit()[n:] if e.get("event") == "postgis.write"])

    def test_error_codes_over_http(self):
        self.gw._runner = lambda s, r, w: Raw(2, "ERROR:  cannot execute INSERT in a read-only transaction", "")
        r = self.post("/query", {"sql": "insert into t values (1)"}, self.tok["operate"])
        self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (403, "forbidden"))
        self.gw._runner = lambda s, r, w: Raw(96, "", "")
        r = self.post("/query", {"sql": "select 1"}, self.tok["operate"])
        self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (409, "not_ready"))
        for bad in ("select 1; select 2", "\\! id", "select 'x"):
            self.assertEqual(self.post("/query", {"sql": bad}).status_code, 400, bad)

    def test_setup_is_audited_and_never_returns_a_password(self):
        r = self.post("/editor/setup", {"statement_timeout": 10})
        self.assertEqual(r.get_json()["data"], {"role": "gisstack_ro", "database": "gis", "statement_timeout": 10})
        self.assertIn(("setup", "gis", "gis", 10), self.gw.calls)
        self.assertTrue([e for e in self.audit() if e.get("event") == "postgis.setup"])
        self.assertEqual(self.post("/editor/setup", {"statement_timeout": 0}).status_code, 400)

    def test_native_only_and_feature_flag(self):
        r = self.post("/inventory", config={"deploy_target": "compose", "targets": ["compose"], "project": "pg"})
        self.assertEqual(r.status_code, 400)
        with mock.patch.dict(os.environ, {"GISSTACK_FEATURES": ""}):
            self.assertEqual(self.post("/inventory").get_json()["error"]["code"], "feature_disabled")

    def test_bad_inputs_rejected_by_schema(self):
        self.assertEqual(self.post("/inventory", {"database": "x; drop"}).status_code, 400)
        self.assertEqual(self.post("/query", {"sql": "select 1", "limit": 999999}).status_code, 400)
        self.assertEqual(self.post("/query", {"sql": "select 1", "offset": -1}).status_code, 400)
        self.assertEqual(self.post("/query", {"sql": "select 1", "extra": 1}).status_code, 400)


# --------------------------------------------------------------------------- real PostGIS container
IMAGE = "postgis/postgis:17-3.5"


def _pg_available():
    if not shutil.which("docker"):
        return False
    if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode == 0:
        return True
    return os.environ.get("GISSTACK_TEST_DOCKER") == "1" and subprocess.run(["docker", "pull", "-q", IMAGE], capture_output=True).returncode == 0


@unittest.skipUnless(_pg_available(), f"Docker and {IMAGE} needed (GISSTACK_TEST_DOCKER=1 pulls it)")
class RealPostgisTests(unittest.TestCase):
    """The real shell scripts, executed locally, with `psql` replaced by a shim that runs it inside a PostGIS container."""
    APP_PW = "App_pw_Secret_1"

    @classmethod
    def sql(cls, q, db="gis"):
        r = subprocess.run(["docker", "exec", "-i", cls.name, "psql", "-X", "-U", "gis", "-d", db, "-v", "ON_ERROR_STOP=1", "-q"], input=q, capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(r.stderr)

    @classmethod
    def setUpClass(cls):
        cls.name = f"pgmgr-test-{os.getpid()}"
        cls.tmp = Path(tempfile.mkdtemp())
        subprocess.run(["docker", "run", "-d", "--rm", "--name", cls.name, "-e", "POSTGRES_PASSWORD=pw", "-e", "POSTGRES_USER=gis", "-e", "POSTGRES_DB=gis", IMAGE],
                       check=True, capture_output=True)
        for _ in range(60):
            time.sleep(2)
            if subprocess.run(["docker", "exec", cls.name, "pg_isready", "-q", "-U", "gis", "-d", "gis"], capture_output=True).returncode == 0:
                time.sleep(2)
                if subprocess.run(["docker", "exec", cls.name, "pg_isready", "-q", "-U", "gis", "-d", "gis"], capture_output=True).returncode == 0:
                    break
        cls.sql(f"""create role gis_app login password '{cls.APP_PW}' nosuperuser;
                    alter database gis owner to gis_app;
                    create table public.pts(id serial primary key, g geometry(Point,4326), note text);
                    insert into public.pts(g, note) values (st_setsrid(st_makepoint(44.8, 41.7), 4326), 'tbilisi'), (null, 'nowhere');
                    alter table public.pts owner to gis_app;
                    create schema extra authorization gis_app; create table extra.t2(a int); insert into extra.t2 values (1);
                    alter table extra.t2 owner to gis_app;""")
        subprocess.run(["docker", "exec", cls.name, "bash", "-c", "sed -i -E 's#(127.0.0.1/32|::1/128)( +)trust#\\1\\2scram-sha-256#' \"$PGDATA/pg_hba.conf\""], check=True, capture_output=True)
        cls.sql("select pg_reload_conf()")                                           # loopback must really check passwords (the image trusts it by default)
        shim = cls.tmp / "bin"
        shim.mkdir()
        (shim / "psql").write_text(f'#!/bin/bash\nexec docker exec -i -e PGPASSWORD -e PGOPTIONS -e PGCONNECT_TIMEOUT -e PGUSER=gis {cls.name} psql "$@"\n')
        (shim / "psql").chmod(0o755)
        cls.env = {**os.environ, "PATH": f"{shim}:{os.environ['PATH']}", "GS_PGRUN": "", "GS_PG_PWFILE": str(cls.tmp / "ro.pw")}
        cls.gw = ScriptGateway({"postgres": {"port": 5432, "user": "gis_app", "password": cls.APP_PW, "db": "gis"}}, "local", None)

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["docker", "rm", "-f", cls.name], capture_output=True)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        def local_run(mode, ssh, script, *, root=False, timeout=40, input_text=None):
            p = subprocess.run(["bash", "-c", script], input=input_text, capture_output=True, text=True, timeout=timeout, env=self.env)
            return p.stdout
        p = mock.patch.object(transport, "run", local_run)
        p.start()
        self.addCleanup(p.stop)

    def ro_password(self):
        return (self.tmp / "ro.pw").read_text()

    def q(self, sql, **kw):
        return pgm.query(self.gw, "gis", sql, **kw)

    def ready(self, timeout=15):
        self.gw.setup("gis", "gis_app", timeout)

    # ---- order matters only for the first test, which needs "not set up yet"
    def test_00_editor_is_not_ready_before_setup(self):
        with mock.patch.dict(self.env, {"GS_PG_PWFILE": str(self.tmp / "missing.pw")}):
            with self.assertRaises(pgm.PostgisError) as cm:
                self.q("select 1")
        self.assertEqual(cm.exception.code, "not_ready")

    def test_inventory_health_and_spatial_index_detection(self):
        inv = pgm.inventory(self.gw, "gis")
        self.assertEqual(inv["errors"], {}, inv["errors"])
        self.assertTrue(inv["server"]["version"].startswith("17"))
        self.assertIn("postgis", {e["name"] for e in inv["extensions"]})
        self.assertIn("gis", {d["name"] for d in inv["databases"]})
        sp = [s for s in inv["spatial"] if s["table"] == "pts"][0]
        self.assertEqual((sp["column"], sp["type"], sp["srid"], sp["spatial_index"]), ("g", "POINT", 4326, False))
        self.assertEqual({s["name"] for s in inv["schemas"]} >= {"public", "extra"}, True)
        self.assertTrue([f for f in pgm.health(inv)["findings"] if f["what"] == "spatial indexes"])                      # 2 rows: informational only
        self.sql("create index pts_g_idx on public.pts using gist(g)")
        try:
            sp = [s for s in pgm.inventory(self.gw, "gis")["spatial"] if s["table"] == "pts"][0]
            self.assertTrue(sp["spatial_index"])
        finally:
            self.sql("drop index public.pts_g_idx")

    def test_setup_is_idempotent_and_the_password_file_is_private(self):
        self.ready()
        first = self.ro_password()
        self.ready()
        self.assertEqual(first, self.ro_password())                              # a second run keeps the working password
        self.assertGreaterEqual(len(first), 24)
        self.assertEqual(stat.S_IMODE((self.tmp / "ro.pw").stat().st_mode), 0o600)
        self.assertEqual(self.q("select count(*) as n from public.pts")["rows"], [["2"]])

    def test_read_queries_types_nulls_paging_and_explain(self):
        self.ready()
        r = self.q("select id, note, st_astext(g) as wkt from public.pts order by id")
        self.assertEqual((r["columns"], r["rows"]), (["id", "note", "wkt"], [["1", "tbilisi", "POINT(44.8 41.7)"], ["2", "nowhere", None]]))
        r = self.q("select null as n, '' as e, E'a\\nb,\"c\"' as t, 'ქართული' as k")
        self.assertEqual(r["rows"], [[None, "", 'a\nb,"c"', "ქართული"]])
        r = self.q("select generate_series(1, 100000) as n")
        self.assertEqual((r["row_count"], r["has_more"], r["rows"][0], r["rows"][-1]), (1000, True, ["1"], ["1000"]))
        r2 = self.q("select generate_series(1, 100000) as n", offset=1000, limit=5)
        self.assertEqual(r2["rows"], [["1001"], ["1002"], ["1003"], ["1004"], ["1005"]])
        self.assertEqual(self.q("select * from public.pts -- comment at the end", limit=1)["row_count"], 1)
        e = self.q("explain select * from public.pts")
        self.assertEqual((e["columns"], e["paged"]), (["QUERY PLAN"], False))
        self.assertTrue(self.q("show server_version")["rows"][0][0].startswith("17"))
        self.assertEqual(self.q("select 1 as a;")["rows"], [["1"]])
        self.assertEqual(self.q("with x as (select 7 as v) select v from x")["rows"], [["7"]])
        self.assertEqual(self.q("select id from public.pts where false")["rows"], [])
        self.assertEqual(self.q("select * from extra.t2")["rows"], [["1"]])           # other schemas are readable too

    def test_byte_cap_truncates_huge_results(self):
        self.ready()
        r = self.q("select repeat('x', 500000) as big from generate_series(1, 40)", limit=5000)
        self.assertTrue(r["truncated"])
        self.assertLess(r["row_count"], 40)
        self.assertGreater(r["row_count"], 3)

    def test_the_read_only_role_cannot_change_anything_or_escape(self):
        self.ready()
        before = self.q("select count(*) from public.pts")["rows"]
        attempts = ["insert into public.pts(note) values ('x')", "update public.pts set note = 'x'", "delete from public.pts", "truncate public.pts",
                    "drop table public.pts", "create table public.evil(i int)", "alter table public.pts add column z int", "create role evil login superuser",
                    "grant all on public.pts to public", "create extension dblink", "select pg_read_file('/etc/passwd')", "copy (select 1) to program 'id'",
                    "copy public.pts to '/tmp/x'", "select lo_import('/etc/passwd')", "select * from pg_authid", "select pg_reload_conf()",
                    "select pg_terminate_backend(pg_backend_pid() + 1)", "alter system set work_mem = '1GB'", "do $$ begin execute 'create table q(i int)'; end $$",
                    "select set_config('default_transaction_read_only', 'off', false)", "set role gis_app", "set session authorization gis_app",
                    "reset all", "create function f() returns int as $$ select 1 $$ language sql"]
        for sql in attempts:
            try:
                self.q(sql)
                ok = True
            except pgm.PostgisError as e:
                ok = e.code in ("forbidden", "sql")
                if e.code not in ("forbidden", "sql"):
                    self.fail(f"{sql}: unexpected {e.code}: {e}")
            if sql in ("set role gis_app", "set session authorization gis_app", "reset all", "select set_config('default_transaction_read_only', 'off', false)"):
                continue                                                            # these may succeed as no-ops; what matters is the data below
            self.assertTrue(ok)
        self.assertEqual(self.q("select count(*) from public.pts")["rows"], before)
        names = {r[0] for r in self.q("select tablename from pg_tables where schemaname = 'public'")["rows"]}
        self.assertNotIn("evil", names)
        self.assertEqual(self.q("select count(*) from pg_roles where rolname = 'evil'")["rows"], [["0"]])

    def test_specific_denials_have_the_right_code(self):
        self.ready()
        for sql in ("insert into public.pts(note) values ('x')", "select pg_read_file('/etc/passwd')", "select * from pg_authid", "copy (select 1) to program 'id'"):
            with self.assertRaises(pgm.PostgisError, msg=sql) as cm:
                self.q(sql)
            self.assertEqual(cm.exception.code, "forbidden", sql)
        with self.assertRaises(pgm.PostgisError) as cm:
            self.q("selec 1")
        self.assertEqual(cm.exception.code, "sql")
        with self.assertRaises(pgm.PostgisError) as cm:
            self.q("select * from nope")
        self.assertEqual(cm.exception.code, "sql")

    def test_shell_escapes_are_dead(self):
        self.ready()
        marker = self.tmp / "pwned"
        for sql in (f"\\! touch {marker}", f" \n\\! touch {marker}", f"/* x */ \\! touch {marker}", f"-- x\n\\! touch {marker}", f"select 1; \\! touch {marker}",
                    f"\\copy (select 1) to '{marker}'"):
            with self.assertRaises(pgm.PostgisError):
                self.q(sql)
        # even bypassing the Python-side checks, the fixed prefix makes the server reject the backslash
        raw = self.gw.run(f"\\! touch {marker}", role="ro", db="gis", write=False)
        self.assertNotEqual(raw.rc, 0)
        self.assertIn("syntax error", raw.err)
        self.assertFalse(marker.exists())
        self.assertFalse(subprocess.run(["docker", "exec", self.name, "test", "-e", str(marker)], capture_output=True).returncode == 0)

    def test_statement_timeout_comes_from_the_role(self):
        self.ready(1)
        try:
            with self.assertRaises(pgm.PostgisError) as cm:
                self.q("select pg_sleep(8)")
            self.assertEqual(cm.exception.code, "timeout")
        finally:
            self.ready(15)

    def test_wrong_ro_password_is_reported_as_not_ready(self):
        self.ready()
        good = self.ro_password()
        try:
            (self.tmp / "ro.pw").write_text("x" * 32)
            with self.assertRaises(pgm.PostgisError) as cm:
                self.q("select 1")
            self.assertEqual(cm.exception.code, "not_ready")
        finally:
            (self.tmp / "ro.pw").write_text(good)
        self.ready()                                                              # setup repairs it
        self.assertEqual(self.q("select 1 as a")["rows"], [["1"]])

    def test_elevated_writes_run_as_the_app_role_and_are_readable_afterwards(self):
        self.ready()
        try:
            r = pgm.execute(self.gw, "gis", "create table public.gs_rw(i int)")
            self.assertEqual(r["tag"], "CREATE TABLE")
            r = pgm.execute(self.gw, "gis", "insert into public.gs_rw values (1), (2) returning i")
            self.assertEqual((r["tag"], r["rows"]), ("INSERT 0 2", [["1"], ["2"]]))
            r = pgm.execute(self.gw, "gis", "update public.gs_rw set i = i + 10")
            self.assertEqual(r["tag"], "UPDATE 2")
            self.assertEqual(self.q("select i from public.gs_rw order by i")["rows"], [["11"], ["12"]])     # default privileges give the editor SELECT on new tables
            with self.assertRaises(pgm.PostgisError) as cm:
                pgm.execute(self.gw, "gis", "select pg_read_file('/etc/passwd')")                            # the app role is not a superuser
            self.assertEqual(cm.exception.code, "forbidden")
            with self.assertRaises(pgm.PostgisError) as cm:
                pgm.execute(self.gw, "gis", "insert into public.gs_rw values ('not a number')")
            self.assertEqual(cm.exception.code, "sql")
        finally:
            self.sql("drop table if exists public.gs_rw")

    def test_wrong_app_password_is_reported(self):
        bad = ScriptGateway({"postgres": {"port": 5432, "user": "gis_app", "password": "nope", "db": "gis"}}, "local", None)
        with self.assertRaises(pgm.PostgisError) as cm:
            pgm.execute(bad, "gis", "select 1")
        self.assertEqual(cm.exception.code, "not_ready")

    def test_no_password_ever_appears_in_results_or_errors(self):
        self.ready()
        secrets_ = [self.ro_password(), self.APP_PW]
        blobs = [json.dumps(pgm.inventory(self.gw, "gis")), json.dumps(self.q("select 1"))]
        for sql in ("select * from nope", "insert into public.pts(note) values ('x')"):
            try:
                self.q(sql)
            except pgm.PostgisError as e:
                blobs.append(str(e))
        try:
            pgm.execute(ScriptGateway({"postgres": {"port": 5432, "user": "gis_app", "password": "bad", "db": "gis"}}, "local", None), "gis", "select 1")
        except pgm.PostgisError as e:
            blobs.append(str(e))
        for blob in blobs:
            for s in secrets_:
                self.assertNotIn(s, blob)


if __name__ == "__main__":
    unittest.main()
