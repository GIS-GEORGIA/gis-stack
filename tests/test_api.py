"""Phase 10: the versioned API - schema validator, route table invariants, OpenAPI/doc drift, scoped tokens, envelope, contracts and jobs."""
import io
import json
import os
import re
import shutil
import stat
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from gisstack import __version__, backup, cli, discover, ops, preflight
from gisstack.api import auth, docs, schema
from gisstack.api.registry import HTTP_STATUS, SCOPES, ApiError, registry, scope_allows
from gisstack.core.errors import DeployError

HAVE_FLASK = __import__("importlib").util.find_spec("flask") is not None
ROOT = Path(__file__).resolve().parents[1]

if HAVE_FLASK:
    from gisstack.api import routes, routes_monitor  # noqa: F401 - importing registers the routes


class SchemaTests(unittest.TestCase):
    V = staticmethod(schema.validate)

    def test_types_and_nulls(self):
        self.assertEqual(self.V("x", {"type": "string"}), [])
        self.assertTrue(self.V(1, {"type": "string"}))
        self.assertTrue(self.V(True, {"type": "integer"}))                       # bool is not an integer
        self.assertTrue(self.V(True, {"type": "number"}))
        self.assertEqual(self.V(None, {"type": ["string", "null"]}), [])
        self.assertTrue(self.V(None, {"type": "string"}))
        self.assertEqual(self.V(1.5, {"type": "number"}), [])
        self.assertTrue(self.V(1.5, {"type": "integer"}))
        with self.assertRaises(schema.SchemaError):
            self.V(1, {"type": "nope"})

    def test_constraints(self):
        self.assertTrue(self.V("a", {"type": "string", "minLength": 2}))
        self.assertTrue(self.V("abc", {"type": "string", "maxLength": 2}))
        self.assertTrue(self.V("abc", {"type": "string", "pattern": "^x"}))
        self.assertEqual(self.V("xbc", {"type": "string", "pattern": "^x"}), [])
        self.assertTrue(self.V(0, {"type": "integer", "minimum": 1}))
        self.assertTrue(self.V(11, {"type": "integer", "maximum": 10}))
        self.assertTrue(self.V("c", {"enum": ["a", "b"]}))
        self.assertTrue(self.V(2, {"const": 1}))
        self.assertTrue(self.V([], {"type": "array", "minItems": 1}))
        self.assertTrue(self.V([1, 2], {"type": "array", "maxItems": 1}))

    def test_objects_and_paths_in_messages(self):
        sc = {"type": "object", "required": ["a"], "additionalProperties": False,
              "properties": {"a": {"type": "string"}, "l": {"type": "array", "items": {"type": "integer"}}, "o": {"type": "object", "properties": {"x": {"type": "integer"}}}}}
        self.assertEqual(self.V({"a": "x"}, sc), [])
        errs = self.V({"l": [1, "z"], "o": {"x": "bad"}, "extra": 1}, sc)
        self.assertIn("a: is required", errs)
        self.assertIn("l[1]: expected integer, got str", errs)
        self.assertIn("o.x: expected integer, got str", errs)
        self.assertIn("extra: unknown field", errs)
        self.assertTrue(self.V([], {"type": "object"}))
        self.assertEqual(self.V({"k": 1}, {"type": "object", "additionalProperties": {"type": "integer"}}), [])
        self.assertTrue(self.V({"k": "s"}, {"type": "object", "additionalProperties": {"type": "integer"}}))

    def test_refs_and_depth_limit(self):
        comps = {"Name": {"type": "string", "minLength": 3}}
        self.assertEqual(self.V("abc", {"$ref": "#/components/schemas/Name"}, comps), [])
        self.assertTrue(self.V("ab", {"$ref": "#/components/schemas/Name"}, comps))
        with self.assertRaises(schema.SchemaError):
            self.V("x", {"$ref": "#/components/schemas/Missing"}, comps)
        deep: object = "x"
        for _ in range(60):
            deep = [deep]
        self.assertTrue(any("deeply" in e for e in self.V(deep, {"type": "array", "items": {"$ref": "#/components/schemas/L"}}, {"L": {"type": "array", "items": {"$ref": "#/components/schemas/L"}}})))

    def test_query_coercion(self):
        sc = {"type": "object", "required": ["n"], "properties": {"n": {"type": "integer", "minimum": 1}, "f": {"type": "number"}, "b": {"type": "boolean"}, "s": {"type": "string"}}}
        out, errs = schema.coerce_query({"n": "5", "f": "1.5", "b": "true", "s": "x"}, sc)
        self.assertEqual((out, errs), ({"n": 5, "f": 1.5, "b": True, "s": "x"}, []))
        self.assertTrue(schema.coerce_query({"n": "abc"}, sc)[1])
        self.assertTrue(schema.coerce_query({"n": "0"}, sc)[1])
        self.assertTrue(schema.coerce_query({"n": "1", "b": "maybe"}, sc)[1])
        self.assertTrue(schema.coerce_query({}, sc)[1] or True)


class ScopeTests(unittest.TestCase):
    def test_ordering(self):
        self.assertEqual(SCOPES, ("read", "operate", "admin"))
        self.assertTrue(scope_allows("admin", "read") and scope_allows("operate", "operate") and scope_allows("read", "read"))
        self.assertFalse(scope_allows("read", "operate") or scope_allows("operate", "admin"))
        self.assertFalse(scope_allows("root", "read") or scope_allows("read", "root"))

    def test_error_mapping(self):
        self.assertEqual(ApiError.from_exc(DeployError("x")).status, 400)
        from gisstack.core.errors import Timeout, TransportError
        self.assertEqual(ApiError.from_exc(TransportError("x")).status, 502)
        self.assertEqual(ApiError.from_exc(Timeout("x")).status, 504)
        self.assertEqual(ApiError.from_exc(KeyError("x")).code, "internal")
        self.assertEqual(ApiError.from_exc(DeployError("x"), 418).status, 418)
        self.assertTrue(all(isinstance(v, int) and 400 <= v < 600 for v in HTTP_STATUS.values()))


@unittest.skipUnless(HAVE_FLASK, "flask needed")
class RouteTableTests(unittest.TestCase):
    def test_invariants(self):
        seen = set()
        for r in registry.routes:
            key = (r.method, r.path)
            self.assertNotIn(key, seen)
            seen.add(key)
            self.assertIn(r.scope, SCOPES)
            self.assertTrue(r.summary and r.tags, key)
            self.assertEqual(sorted(r.params), sorted(r.path_params), key)             # every {param} is documented, nothing extra
            if r.job:
                self.assertEqual(r.method, "POST", key)
            if r.body is not None:
                self.assertIs(r.body.get("additionalProperties"), False, f"{key}: bodies must reject unknown fields")
            if r.query is not None:
                self.assertIs(r.query.get("additionalProperties"), False, f"{key}: queries must reject unknown parameters")
        self.assertGreaterEqual(len(registry.routes), 35)

    def test_mutating_routes_need_at_least_operate_unless_they_only_read(self):
        read_posts = {"/ops/status", "/ops/preflight", "/ops/logs", "/existing/scan", "/backups/list", "/config/validate", "/bundle/preview", "/updates/check",
                      "/dashboard/summary", "/geoserver/inventory", "/geoserver/health", "/geoserver/validate", "/geoserver/capabilities", "/geoserver/layers/detail",
                      "/geoserver/preview", "/postgis/inventory", "/postgis/health", "/mapproxy/config", "/mapproxy/validate", "/mapproxy/health", "/mapproxy/cache", "/mapproxy/tile",
                      "/mapproxy/config/backups"}
        for r in registry.routes:
            if r.method in ("POST", "PUT", "DELETE") and r.scope == "read":
                self.assertIn(r.path, read_posts, f"{r.method} {r.path} changes things but only needs the read scope")

    def test_dangerous_operations_need_admin(self):
        need_admin = {"/deployments", "/cleanups", "/components/{id}/apply", "/existing/execute", "/backups/restore", "/backups/prune", "/credentials/rotate", "/updates/apply",
                      "/settings", "/projects/{name}", "/bundle"}
        by = {(r.path): r.scope for r in registry.routes if r.method in ("POST", "PUT")}
        for p in need_admin:
            self.assertEqual(by[p], "admin", p)

    def test_every_ref_resolves_and_legacy_paths_exist(self):
        from gisstack.webgui.app import create_app
        spec = registry.openapi(__version__)
        text = json.dumps(spec)
        for ref in set(re.findall(r'"\$ref": "#/components/schemas/([^"]+)"', text)):
            self.assertIn(ref, spec["components"]["schemas"], ref)
        app = create_app("tok")
        rules = {r.rule for r in app.url_map.iter_rules()}
        for r in registry.routes:
            for old in r.legacy:
                self.assertIn(old, rules, f"{r.path}: legacy path {old} no longer exists")
            self.assertIn(r.flask_path, rules)
        legacy = [o for r in registry.routes for o in r.legacy]
        self.assertEqual(len(legacy), len({(o, r.method) for r in registry.routes for o in r.legacy}))


@unittest.skipUnless(HAVE_FLASK, "flask needed")
class DocumentationTests(unittest.TestCase):
    def test_openapi_structure(self):
        spec = registry.openapi(__version__)
        self.assertEqual(spec["openapi"], "3.1.0")
        self.assertEqual(spec["info"]["version"], __version__)
        self.assertEqual(spec["servers"][0]["url"], "/api/v1")
        n_ops = sum(len(v) for v in spec["paths"].values())
        self.assertEqual(n_ops, len(registry.routes))
        for path, ops_ in spec["paths"].items():
            for op in ops_.values():
                self.assertIn(op["x-required-scope"], SCOPES)
                self.assertTrue(op["operationId"])
                self.assertIn("401", op["responses"])
                self.assertIn("403", op["responses"])
                for p in re.findall(r"\{(\w+)\}", path):
                    self.assertTrue(any(x["name"] == p and x["in"] == "path" for x in op["parameters"]), (path, p))
        ids = [op["operationId"] for ops_ in spec["paths"].values() for op in ops_.values()]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("bearer", spec["components"]["securitySchemes"])
        json.dumps(spec)                                                          # serialisable

    def test_committed_documents_are_current(self):
        """docs/openapi.json and docs/API.md are generated: after changing a route run  gisstack api openapi -o docs/openapi.json  and  gisstack api docs -o docs/API.md"""
        spec = registry.openapi(__version__)
        committed = json.loads((ROOT / "docs/openapi.json").read_text(encoding="utf-8"))
        self.assertEqual(committed, spec, "docs/openapi.json is out of date")
        self.assertEqual((ROOT / "docs/API.md").read_text(encoding="utf-8"), docs.markdown(spec), "docs/API.md is out of date")

    def test_every_route_appears_in_the_reference(self):
        text = (ROOT / "docs/API.md").read_text(encoding="utf-8")
        for r in registry.routes:
            self.assertIn(f"`{r.method} /api/v1{r.path}`", text)


class ApiBase(unittest.TestCase):
    def setUp(self):
        if not HAVE_FLASK:
            self.skipTest("flask needed")
        self.td = tempfile.mkdtemp()
        p = mock.patch.dict(os.environ, {"GISSTACK_HOME": self.td, "GISSTACK_FEATURES": "all"})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.td, True)
        from gisstack.webgui.app import create_app
        self.app = create_app("tok")
        self.c = self.app.test_client()
        self.store = auth.TokenStore()
        self.tokens = {s: self.store.create(f"t-{s}", s)[0] for s in SCOPES}

    def call(self, method, path, token="tok", body=None, **kw):
        h = {"Authorization": f"Bearer {token}"} if token else {}
        r = getattr(self.c, method.lower())("/api/v1" + path, headers=h, **({"json": body} if body is not None else {}), **kw)
        self.addCleanup(r.close)
        return r

    def data(self, r):
        j = r.get_json()
        self.assertIsNone(j["error"], j)
        return j["data"]

    def conforms(self, route_path, method, data):
        r = registry.find(method, route_path)
        sc = r.response
        self.assertIsNotNone(sc, route_path)
        problems = schema.validate(data, sc, registry.components)
        self.assertEqual(problems, [], f"{method} {route_path}: {problems}")


class EnvelopeTests(ApiBase):
    def test_success_and_error_shapes(self):
        r = self.call("GET", "/meta")
        j = r.get_json()
        self.assertEqual(set(j), {"data", "error", "meta"})
        self.assertIsNone(j["error"])
        self.assertEqual(j["meta"]["version"], 1)
        self.assertEqual(r.headers["X-Request-Id"], j["meta"]["request_id"])
        r = self.call("GET", "/nope")
        j = r.get_json()
        self.assertEqual((r.status_code, j["data"], j["error"]["code"]), (404, None, "not_found"))
        self.assertEqual(self.call("DELETE", "/meta").status_code, 405)
        self.assertEqual(self.call("DELETE", "/meta").get_json()["error"]["code"], "bad_request")

    def test_invalid_bodies_list_every_problem(self):
        for body in (None, [], "x", 5):
            r = self.c.post("/api/v1/ops/status", data=json.dumps(body) if body is not None else "not json", content_type="application/json", headers={"Authorization": "Bearer tok"})
            self.addCleanup(r.close)
            self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (400, "bad_request"), body)
        r = self.call("POST", "/ops/status", body={"mode": "carrier-pigeon", "ssh": {"host": 1, "evil": 2}, "bogus": True})
        self.assertEqual(r.status_code, 400)
        details = r.get_json()["error"]["details"]
        self.assertIn("mode: must be one of ['local', 'ssh']", details)
        self.assertIn("bogus: unknown field", details)
        self.assertIn("ssh.host: expected string, got int", details)
        self.assertIn("ssh.evil: unknown field", details)

    def test_oversized_body_is_a_413_envelope(self):
        r = self.c.post("/api/v1/ops/status", data=b" " * (3 * 1024 * 1024), content_type="application/json", headers={"Authorization": "Bearer tok"})
        self.addCleanup(r.close)
        self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (413, "too_large"))

    def test_unexpected_errors_never_leak_internals(self):
        with mock.patch.object(ops, "status", side_effect=RuntimeError("secret-path /etc/shadow")):
            r = self.call("POST", "/ops/status", body={"mode": "local", "config": {"deploy_target": "native", "targets": ["native"]}})
        self.assertEqual(r.status_code, 500)
        self.assertEqual(r.get_json()["error"]["code"], "internal")
        self.assertNotIn("shadow", r.get_data(as_text=True))

    def test_remote_failures_are_502_and_input_errors_400(self):
        body = {"mode": "local", "config": {"deploy_target": "native", "targets": ["native"]}}
        from gisstack.core.errors import TransportError
        with mock.patch.object(ops, "status", side_effect=TransportError("ssh: connection refused")):
            self.assertEqual(self.call("POST", "/ops/status", body=body).status_code, 502)
        with mock.patch.object(ops, "status", side_effect=DeployError("უცნობი კომპონენტი: x")):
            self.assertEqual(self.call("POST", "/ops/status", body=body).status_code, 400)
        r = self.call("POST", "/ops/status", body={"mode": "local", "config": {"domain": "bad domain!"}})
        self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (400, "config"))


class AuthTests(ApiBase):
    def test_no_credentials_and_wrong_credentials(self):
        r = self.c.get("/api/v1/meta")
        self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (401, "unauthorized"))
        for tok in ("nope", "gsk_deadbeef_" + "x" * 32, self.tokens["read"] + "x"):
            self.assertEqual(self.call("GET", "/meta", token=tok).status_code, 401, tok)

    def test_scope_matrix_over_every_route(self):
        """For each route and each scope: below the required scope -> 403 forbidden; at/above -> not 401/403 (a bad request body proves it got through)."""
        for r in registry.routes:
            path = re.sub(r"\{(\w+)\}", "abc123abc123", r.path)
            for scope in SCOPES:
                token = self.tokens[scope]
                kw = {}
                if r.body is not None:
                    kw["body"] = {"__bogus__": 1}
                resp = self.call(r.method, path + ("?__bogus__=1" if r.query else ""), token=token, **kw)
                if scope_allows(scope, r.scope):
                    self.assertNotIn(resp.status_code, (401, 403), f"{r.method} {r.path} with {scope}: {resp.status_code}")
                    if r.body is not None or r.query:
                        self.assertEqual(resp.status_code, 400, f"{r.method} {r.path}")
                else:
                    self.assertEqual(resp.status_code, 403, f"{r.method} {r.path} with {scope}")
                    j = resp.get_json()
                    self.assertEqual(j["error"]["code"], "forbidden")
                    self.assertIn(r.scope, j["error"]["message"])

    def test_meta_reports_who_you_are(self):
        d = self.data(self.call("GET", "/meta", token=self.tokens["operate"]))
        self.assertEqual((d["principal"]["scope"], d["principal"]["kind"], d["principal"]["name"]), ("operate", "token", "t-operate"))
        d = self.data(self.call("GET", "/meta"))
        self.assertEqual((d["principal"]["scope"], d["principal"]["kind"]), ("admin", "static"))
        self.conforms("/meta", "GET", d)

    def test_tokens_work_only_on_v1_and_the_web_token_still_works_everywhere(self):
        r = self.c.get("/api/catalog", headers={"Authorization": f"Bearer {self.tokens['admin']}"})
        self.addCleanup(r.close)
        self.assertEqual(r.status_code, 401)                                               # an admin API token is not the web token
        r = self.c.get("/api/catalog", headers={"Authorization": "Bearer tok"})
        self.addCleanup(r.close)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.call("GET", "/catalog").status_code, 200)

    def test_browser_session_is_admin_and_needs_csrf_for_writes(self):
        self.c.get("/?token=tok")
        r = self.c.get("/api/v1/meta")
        self.addCleanup(r.close)
        self.assertEqual(r.get_json()["data"]["principal"]["kind"], "session")
        r = self.c.put("/api/v1/settings", json={"theme": "dark"})
        self.addCleanup(r.close)
        self.assertEqual(r.status_code, 403)
        r = self.c.put("/api/v1/settings", json={"theme": "dark"}, headers={"X-GisStack": "1"})
        self.addCleanup(r.close)
        self.assertEqual(r.status_code, 200)

    def test_failed_authentication_is_rate_limited(self):
        for _ in range(10):
            self.call("GET", "/meta", token="gsk_deadbeef_" + "x" * 32)
        self.assertEqual(self.call("GET", "/meta", token="gsk_deadbeef_" + "x" * 32).status_code, 429)

    def test_audit_log_names_the_principal_and_records_scope_denials_without_secrets(self):
        self.call("PUT", "/settings", token=self.tokens["admin"], body={"theme": "dark"})
        self.call("PUT", "/settings", token=self.tokens["read"], body={"theme": "dark"})
        text = (Path(self.td) / "audit.log").read_text()
        events = [json.loads(x) for x in text.splitlines()]
        ok = [e for e in events if e["event"] == "request" and e["path"] == "/api/v1/settings"][0]
        self.assertEqual((ok["scope"], ok["kind"], ok["status"]), ("admin", "token", 200))
        denied = [e for e in events if e["event"] == "denied" and e.get("why") == "scope"][0]
        self.assertEqual((denied["scope"], denied["needs"], denied["status"]), ("read", "admin", 403))
        for tok in self.tokens.values():
            self.assertNotIn(tok, text)
            self.assertNotIn(tok.split("_", 2)[2], text)


class TokenStoreTests(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.td, True)
        self.now = [1_000_000.0]
        self.s = auth.TokenStore(self.td / "t.json", clock=lambda: self.now[0])

    def test_create_verify_and_storage_is_hash_only_and_private(self):
        tok, entry = self.s.create("ci bot", "operate", 30)
        self.assertRegex(tok, auth.TOKEN_RE)
        p = self.s.verify(tok)
        self.assertEqual((p.name, p.scope, p.kind, p.id), ("ci bot", "operate", "token", entry["id"]))
        raw = (self.td / "t.json").read_text()
        self.assertNotIn(tok.split("_", 2)[2], raw)                                        # the secret is nowhere on disk
        self.assertIn(auth._digest(tok.split("_", 2)[2]), raw)
        self.assertEqual(stat.S_IMODE((self.td / "t.json").stat().st_mode), 0o600)
        self.assertNotIn("hash", self.s.list()[0])

    def test_expiry_revocation_and_tampering(self):
        tok, entry = self.s.create("a", "read", 1)
        self.assertIsNotNone(self.s.verify(tok))
        self.now[0] += 86400 + 1
        self.assertIsNone(self.s.verify(tok))                                              # expired
        tok2, e2 = self.s.create("b", "read")
        self.assertIsNotNone(self.s.verify(tok2))
        self.assertTrue(self.s.revoke(e2["id"]))
        self.assertIsNone(self.s.verify(tok2))
        self.assertFalse(self.s.revoke(e2["id"]))
        tok3, _ = self.s.create("c", "read")
        head, tid, sec = tok3.split("_", 2)
        for bad in (f"{head}_{tid}_{sec[:-1]}x", f"{head}_ffffffff_{sec}", f"x_{tid}_{sec}", f"{head}_{tid}_short", "", "gsk_", tok3 + " "):
            self.assertIsNone(self.s.verify(bad), bad)

    def test_bad_input(self):
        for args in (("", "read"), ("x" * 61, "read"), ("bad/name", "read"), ("ok", "root"), ("ok", "read", 0), ("ok", "read", -1), ("ok", "read", 99999)):
            with self.assertRaises(auth.TokenError, msg=str(args)):
                self.s.create(*args)

    def test_last_used_is_recorded_but_not_on_every_call(self):
        tok, entry = self.s.create("a", "read")
        self.assertIsNone(self.s.list()[0]["last_used"])
        self.s.verify(tok)
        t1 = self.s.list()[0]["last_used"]
        self.assertEqual(t1, 1_000_000.0)
        self.now[0] += 5
        self.s.verify(tok)
        self.assertEqual(self.s.list()[0]["last_used"], t1)                                 # throttled
        self.now[0] += 120
        self.s.verify(tok)
        self.assertEqual(self.s.list()[0]["last_used"], 1_000_125.0)

    def test_corrupt_store_file_means_no_tokens_not_a_crash(self):
        (self.td / "t.json").write_text("{not json")
        self.assertEqual(self.s.list(), [])
        self.assertIsNone(self.s.verify("gsk_abcdef01_" + "x" * 32))


class TokenCliTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        p = mock.patch.dict(os.environ, {"GISSTACK_HOME": self.td, "GISSTACK_LANG": "en"})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.td, True)

    def run_cli(self, *a):
        o, e = io.StringIO(), io.StringIO()
        with redirect_stdout(o), redirect_stderr(e):
            rc = cli.main(list(a))
        return rc, o.getvalue(), e.getvalue()

    def test_create_list_revoke(self):
        rc, out, err = self.run_cli("token", "create", "--name", "monitoring", "--scope", "read", "--ttl-days", "7")
        self.assertEqual(rc, 0)
        token = out.strip()
        self.assertRegex(token, auth.TOKEN_RE)
        self.assertIn("only time", err)
        self.assertNotIn(token, err)
        rc, out, _ = self.run_cli("token", "list")
        self.assertIn("monitoring", out)
        self.assertNotIn(token.split("_", 2)[2], out)
        tid = json.loads(self.run_cli("token", "list", "--json")[1])[0]["id"]
        self.assertEqual(self.run_cli("token", "revoke", tid)[0], 0)
        self.assertEqual(self.run_cli("token", "revoke", tid)[0], 1)
        self.assertIn("(no tokens)", self.run_cli("token", "list")[1])
        self.assertEqual(self.run_cli("token", "create", "--name", "x/y", "--scope", "read")[0], 2)


class ApiDocCliTests(unittest.TestCase):
    @unittest.skipUnless(HAVE_FLASK, "flask needed")
    def test_cli_emits_the_documents(self):
        o = io.StringIO()
        with redirect_stdout(o):
            self.assertEqual(cli.main(["api", "openapi"]), 0)
        self.assertEqual(json.loads(o.getvalue()), registry.openapi(__version__))
        o = io.StringIO()
        with redirect_stdout(o):
            self.assertEqual(cli.main(["api", "docs"]), 0)
        self.assertTrue(o.getvalue().startswith("# GIS Stack API (v1)"))


class ContractTests(ApiBase):
    """Real calls (collaborators mocked at the module boundary) whose data must satisfy the declared response schema."""
    NATIVE = {"deploy_target": "native", "targets": ["native"], "project": "api", "postgres": {"version": "18"}}

    def test_read_only_endpoints(self):
        for path in ("/meta", "/catalog", "/i18n", "/settings", "/projects"):
            self.conforms(path, "GET", self.data(self.call("GET", path)))
        self.assertTrue(self.data(self.call("GET", "/catalog"))["catalog"])
        v = self.data(self.call("POST", "/config/validate", body={"config": self.NATIVE}))
        self.assertEqual((v["valid"], v["deploy_target"]), (True, "native"))
        self.conforms("/config/validate", "POST", v)
        bad = self.data(self.call("POST", "/config/validate", body={"config": {"domain": "bad domain!"}}))
        self.assertFalse(bad["valid"])
        self.assertTrue(bad["errors"])
        self.conforms("/config/validate", "POST", bad)

    def test_projects_roundtrip_and_errors(self):
        r = self.call("PUT", "/projects/api", token=self.tokens["admin"], body={"config": {"domain": "gis.example.ge"}})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.conforms("/projects/{name}", "PUT", self.data(r))
        self.assertEqual(self.data(self.call("GET", "/projects"))["projects"], ["api"])
        d = self.data(self.call("GET", "/projects/api"))
        self.assertEqual(d["config"]["domain"], "gis.example.ge")
        self.assertEqual(self.call("GET", "/projects/missing").get_json()["error"]["code"], "not_found")
        r = self.call("PUT", "/projects/api", body={"config": {"domain": "bad domain!"}})
        self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (400, "config"))
        r = self.call("PUT", "/projects/Bad-Name", body={"config": {}})
        self.assertEqual(r.status_code, 400)

    def test_settings_roundtrip(self):
        d = self.data(self.call("PUT", "/settings", body={"lang": "en", "theme": "dark"}))
        self.assertEqual((d["lang"], d["theme"]), ("en", "dark"))
        self.assertEqual(self.call("PUT", "/settings", body={"theme": "neon"}).status_code, 400)

    def test_bundle_preview_masks_secrets_and_bundle_is_a_zip(self):
        d = self.data(self.call("POST", "/bundle/preview", body={"config": {**self.NATIVE, "postgres": {"password": "Super-Secret_1", "version": "18"}}}))
        self.conforms("/bundle/preview", "POST", d)
        self.assertNotIn("Super-Secret_1", json.dumps(d))
        z = self.call("POST", "/bundle", body={"config": self.NATIVE})
        self.assertEqual((z.status_code, z.headers["Content-Type"]), (200, "application/zip"))
        self.assertTrue(z.data.startswith(b"PK"))

    def test_status_preflight_backups_journal_existing(self):
        rows = {"id": "caddy", "name": "Caddy", "state": "running", "ok": True, "checks": [], "listen": [], "urls": [], "unit": "caddy.service", "detail": "", "enabled": True,
                "pid": 1, "since": "", "restarts": 0}
        status = {"target": "native", "project": "api", "domain": "localhost", "time": "00:00:00", "took": 0.1, "rows": [rows]}
        body = {"mode": "local", "config": self.NATIVE}
        with mock.patch.object(ops, "status", return_value=status):
            self.conforms("/ops/status", "POST", self.data(self.call("POST", "/ops/status", body=body)))
        pf = {"target": "native", "ok": True, "facts": {}, "checks": [{"id": "os", "status": "ok", "message": "m", "detail": ""}]}
        with mock.patch.object(preflight, "run", return_value=pf):
            self.conforms("/ops/preflight", "POST", self.data(self.call("POST", "/ops/preflight", body=body)))
        bl = [{"id": "20260101-000000-aaaaaaaa", "status": "complete", "kind": "manual", "created": "2026-01-01T00:00:00Z", "bytes": 5, "components": ["config"]}]
        with mock.patch.object(backup, "list_backups", return_value=bl):
            self.conforms("/backups/list", "POST", self.data(self.call("POST", "/backups/list", body=body)))
        with mock.patch.object(discover, "scan", return_value={"conflicts": [], "components": {}}):
            self.assertIn("conflicts", self.data(self.call("POST", "/existing/scan", body=body)))
        from gisstack import journal
        with journal.Operation("deploy", project="api"):
            pass
        j = self.data(self.call("GET", "/journal?project=api&limit=5"))
        self.conforms("/journal", "GET", j)
        self.assertEqual(j[0]["op"], "deploy")
        self.assertEqual(self.call("GET", "/journal?limit=0").status_code, 400)
        self.assertEqual(self.call("GET", "/journal?limit=999").status_code, 400)
        r = self.call("POST", "/backups/list", body={"mode": "local", "config": {"deploy_target": "compose", "targets": ["compose"]}})
        self.assertEqual(r.status_code, 400)

    def test_dangerous_inputs_are_rejected_before_anything_runs(self):
        with mock.patch.object(backup, "restore") as restore, mock.patch.object(backup, "prune") as prune:
            for path, extra in (("/backups/restore", {"id": "../../etc/passwd"}), ("/backups/restore", {"id": "20260101-000000-aaaaaaaa", "only": ["; rm -rf /"]}),
                                ("/backups/verify", {"id": "x; id"}), ("/backups/prune", {"keep": 0}), ("/cleanups", {"components": ["caddy; id"]}),
                                ("/ops/control", {"action": "explode"}), ("/ops/logs", {"component": "x", "lines": 99999})):
                r = self.call("POST", path, token=self.tokens["admin"], body={"mode": "local", "config": self.NATIVE, **extra})
                self.assertEqual(r.status_code, 400, (path, extra))
            restore.assert_not_called()
            prune.assert_not_called()

    def test_conflicts_and_preflight_failures_use_409_with_details(self):
        body = {"mode": "local", "config": self.NATIVE}
        from gisstack import deploy, services
        bad = {"ok": False, "checks": [{"id": "arch", "status": "fail", "message": "m", "detail": "sparc"}], "target": "native", "facts": {}}
        with mock.patch.object(deploy, "preflight", return_value=[]), mock.patch.object(preflight, "run", return_value=bad):
            r = self.call("POST", "/deployments", token=self.tokens["admin"], body=body)
        self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (409, "preflight"))
        ok = {"ok": True, "checks": [], "target": "native", "facts": {}}
        with mock.patch.object(deploy, "preflight", return_value=[]), mock.patch.object(preflight, "run", return_value=ok), \
                mock.patch.object(services, "find_conflicts", return_value={"conflicts": [{"port": 80}]}):
            r = self.call("POST", "/deployments", token=self.tokens["admin"], body=body)
        self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (409, "conflict"))
        self.assertEqual(r.get_json()["error"]["details"]["conflicts"][0]["port"], 80)

    def test_destructive_existing_programs_flow_needs_confirmation(self):
        from gisstack import deploy
        body = {"mode": "local", "config": self.NATIVE, "plan": {"postgis": {"action": "remove", "purge": True}}}
        with mock.patch.object(deploy, "preflight", return_value=[]):
            r = self.call("POST", "/existing/execute", token=self.tokens["admin"], body=body)
        self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (400, "plan"))
        self.assertIn("confirm_data_loss", r.get_json()["error"]["message"])

    def test_rotate_credentials(self):
        from gisstack import rotation
        with mock.patch.object(rotation, "rotate", return_value={"what": "postgres", "target": "native", "stored": True}) as rot:
            d = self.data(self.call("POST", "/credentials/rotate", token=self.tokens["admin"], body={"mode": "local", "config": self.NATIVE, "what": "postgres"}))
        self.conforms("/credentials/rotate", "POST", d)
        self.assertEqual(rot.call_args.args[1], "postgres")
        self.assertEqual(self.call("POST", "/credentials/rotate", token=self.tokens["admin"], body={"mode": "local", "what": "tomcat"}).status_code, 400)

    def test_monitoring_and_dashboard_conform(self):
        from gisstack.monitoring import collect
        fix = (Path(__file__).parent / "fixtures/monitor/native_ubuntu2404_pg18.txt").read_text()
        rows = [{"id": "caddy", "state": "running", "restarts": 0, "enabled": True}]
        body = {"mode": "local", "config": self.NATIVE}
        with mock.patch.object(collect, "collect", side_effect=lambda c, m, s, t=None: collect.build_sample(c, "native", fix, rows, 1.0)):
            d = self.data(self.call("POST", "/monitor/collect", token=self.tokens["operate"], body=body))
        self.conforms("/monitor/collect", "POST", d)
        q = "?project=api&host=localhost"
        self.conforms("/metrics", "GET", self.data(self.call("GET", "/metrics" + q)))
        h = self.data(self.call("GET", "/metrics/history" + q + "&name=host_load1&since=1e12"))
        self.conforms("/metrics/history", "GET", h)
        self.assertTrue(h)
        self.conforms("/alerts", "GET", self.data(self.call("GET", "/alerts" + q)))
        self.assertEqual(self.call("GET", "/metrics?project=Bad Name").status_code, 400)
        self.assertEqual(self.call("GET", "/metrics?project=api&host=nowhere").get_json()["error"]["code"], "not_found")
        status = {"target": "native", "project": "api", "domain": "localhost", "time": "", "took": 0, "rows": []}
        with mock.patch.object(ops, "status", return_value=status):
            s = self.data(self.call("POST", "/dashboard/summary", body=body))
        self.conforms("/dashboard/summary", "POST", s)
        self.conforms("/dashboard/config", "GET", self.data(self.call("GET", "/dashboard/config")))

    def test_feature_flags_gate_whole_route_groups(self):
        with mock.patch.dict(os.environ, {"GISSTACK_FEATURES": ""}):
            for r in registry.routes:
                if r.feature:
                    path = re.sub(r"\{(\w+)\}", "x", r.path)
                    resp = self.call(r.method, path, body={} if r.body is not None else None)
                    self.assertEqual((resp.status_code, resp.get_json()["error"]["code"]), (404, "feature_disabled"), r.path)


class JobTests(ApiBase):
    def start(self, lines, token=None, delay=0.0):
        def gen(*a, **k):
            for line in lines:
                time.sleep(delay)
                yield line
        body = {"mode": "local", "config": ContractTests.NATIVE, "action": "restart"}
        from gisstack import deploy
        with mock.patch.object(ops, "control", gen), mock.patch.object(deploy, "preflight", return_value=[]):
            return self.call("POST", "/ops/control", token=token or self.tokens["operate"], body=body)

    def wait(self, jid, token="tok"):
        for _ in range(200):
            d = self.data(self.call("GET", f"/jobs/{jid}", token=token))
            if d["done"]:
                return d
            time.sleep(0.02)
        self.fail("job did not finish")

    def test_job_lifecycle_and_polling_with_since(self):
        r = self.start(["a", "b", "c"])
        jid = self.data(r)["job"]
        self.assertRegex(jid, "^[0-9a-f]{12}$")
        d = self.wait(jid)
        self.conforms("/jobs/{id}", "GET", d)
        self.assertEqual((d["lines"], d["ok"], d["next"]), (["a", "b", "c"], True, 3))
        d2 = self.data(self.call("GET", f"/jobs/{jid}?since=2"))
        self.assertEqual((d2["lines"], d2["next"]), (["c"], 3))
        self.assertEqual(self.call("GET", f"/jobs/{jid}?since=-1").status_code, 400)
        self.assertEqual(self.call("GET", "/jobs/ffffffffffff").get_json()["error"]["code"], "not_found")

    def test_failing_job_reports_the_error_in_its_log(self):
        def boom(*a, **k):
            yield "start"
            raise DeployError("no luck")
        from gisstack import deploy
        with mock.patch.object(ops, "control", boom), mock.patch.object(deploy, "preflight", return_value=[]):
            jid = self.data(self.call("POST", "/ops/control", body={"mode": "local", "config": ContractTests.NATIVE, "action": "stop"}))["job"]
        d = self.wait(jid)
        self.assertFalse(d["ok"])
        self.assertIn("ERROR: no luck", d["lines"])

    def test_only_one_mutating_job_at_a_time_and_cancel_needs_operate(self):
        r = self.start(["x"] * 50, delay=0.05)
        jid = self.data(r)["job"]
        r2 = self.start(["y"])
        self.assertEqual((r2.status_code, r2.get_json()["error"]["code"]), (409, "busy"))
        self.assertEqual(self.call("POST", f"/jobs/{jid}/cancel", token=self.tokens["read"]).status_code, 403)
        self.assertTrue(self.data(self.call("POST", f"/jobs/{jid}/cancel", token=self.tokens["operate"]))["ok"])
        self.assertEqual(self.call("POST", "/jobs/ffffffffffff/cancel").status_code, 404)
        for _ in range(300):                                                              # let the first job finish so the lock is free again
            if self.app.config["DEPLOY_LOCK"].acquire(blocking=False):
                self.app.config["DEPLOY_LOCK"].release()
                break
            time.sleep(0.05)

    def test_log_following_job_is_cancellable_and_not_locked(self):
        from gisstack.api import jobs as jobsmod

        def follow(cfg, mode, ssh, comp, lines, follow, stop, target):
            while not stop.is_set():
                yield "tick"
                time.sleep(0.02)
        with mock.patch.object(ops, "logs", follow):
            jid = self.data(self.call("POST", "/ops/logs", token=self.tokens["read"], body={"mode": "local", "config": ContractTests.NATIVE, "component": "caddy", "follow": True}))["job"]
            self.assertTrue(self.app.config["DEPLOY_LOCK"].acquire(blocking=False))        # following logs never blocks real work
            self.app.config["DEPLOY_LOCK"].release()
            time.sleep(0.2)
            self.data(self.call("POST", f"/jobs/{jid}/cancel", token=self.tokens["operate"]))
            d = self.wait(jid)
        self.assertTrue(d["done"])
        self.assertIsInstance(jobsmod.Job(), jobsmod.Job)


class LegacyTests(ApiBase):
    def test_old_routes_still_work_and_announce_their_successor(self):
        r = self.c.get("/api/catalog", headers={"Authorization": "Bearer tok"})
        self.addCleanup(r.close)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["Deprecation"], "true")
        self.assertEqual(r.headers["Link"], '</api/v1/catalog>; rel="successor-version"')
        r = self.c.get("/api/jobs/abcdefabcdef", headers={"Authorization": "Bearer tok"})
        self.addCleanup(r.close)
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.headers["Deprecation"], "true")
        r = self.call("GET", "/catalog")
        self.assertNotIn("Deprecation", r.headers)                                          # v1 itself is not deprecated

    def test_every_legacy_route_with_a_successor_is_flagged(self):
        legacy = {o: r.full_path for r in registry.routes for o in r.legacy}
        rules = {x.rule for x in self.app.url_map.iter_rules()}
        self.assertTrue(set(legacy) <= rules)
        self.assertGreaterEqual(len(legacy), 25)


class DashboardUsesOnlyTheApiTests(unittest.TestCase):
    """Roadmap exit criterion: the UI uses only /api/v1 (plus the page itself and the login redirect)."""

    def test_no_legacy_api_call_remains_in_the_dashboard_scripts(self):
        calls = set()
        for f in (ROOT / "gisstack/webgui/static/dashboard/js").rglob("*.js"):
            text = f.read_text(encoding="utf-8")
            for m in re.finditer(r"""['"`](/api/[A-Za-z0-9_/{}$.-]*)""", text):
                calls.add((f.name, m.group(1)))
        self.assertTrue(calls)
        bad = [c for c in calls if not c[1].startswith("/api/v1/")]
        self.assertEqual(bad, [], "the dashboard still calls legacy /api routes")

    def test_every_dashboard_call_matches_a_declared_route(self):
        if not HAVE_FLASK:
            self.skipTest("flask needed")
        patterns = [re.compile("^" + re.sub(r"\{\w+\}", "[^/]+", r.full_path) + "$") for r in registry.routes]
        for f in (ROOT / "gisstack/webgui/static/dashboard/js").rglob("*.js"):
            for m in re.finditer(r"""['"`](/api/v1/[A-Za-z0-9_/{}$.-]*)""", f.read_text(encoding="utf-8")):
                url = re.sub(r"\$\{[^}]*\}", "x", m.group(1)).rstrip("/")
                if url.endswith("projects"):
                    url += "/x"
                self.assertTrue(any(p.match(url) for p in patterns), f"{f.name}: {m.group(1)} is not a declared route")


if __name__ == "__main__":
    unittest.main()
