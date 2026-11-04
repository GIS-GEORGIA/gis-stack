"""Phase 10: plugin contract, loader isolation, conformance kit and the integration points."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from gisstack import backup, plugins, providers
from gisstack.core.errors import DeployError
from gisstack.monitoring import notify
from gisstack.plugins import contract, kit, loader

HAVE_FLASK = __import__("importlib").util.find_spec("flask") is not None


class Good(contract.BackupProvider):
    name, description = "vault", "keeps copies in a vault"

    def __init__(self):
        self.stored = []

    def store(self, archive, backup_id):
        self.stored.append((archive.name, backup_id))
        return f"vault://{backup_id}"


class Pager(contract.MonitoringProvider):
    name = "pager"

    def notifiers(self):
        return [SimpleNamespace(name="pager", send=lambda note: None)]


class Sso(contract.AuthProvider):
    name = "sso"

    def authenticate(self, token):
        return {"name": "alice", "scope": "operate"} if token == "sso-good-token" else None


class Layers(contract.DataSource):
    name = "city"

    def layers(self):
        return [{"id": "roads", "title": "Roads", "kind": "wfs"}]


class FakeProv(contract.ServiceProvider):
    name = "lxd"

    def provider(self):
        return SimpleNamespace(entry="i.sh", clean_entry="c.sh", needs_root=True, status_rows=None, control_script=None, logs_script=None, clean_args=None, apply_args=None)


def ep(name, obj, boom=None):
    def load():
        if boom:
            raise boom
        return obj
    return SimpleNamespace(name=name, value=f"pkg:{name}", load=load)


class Env(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(os.environ, {"GISSTACK_FEATURES": "plugins"})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(loader.set_source, None)

    def install(self, **groups):
        table = {contract.KINDS[k][0]: v for k, v in groups.items()}
        loader.set_source(lambda g: table.get(g, []))


class ContractTests(unittest.TestCase):
    def test_kinds_cover_the_five_extension_points(self):
        self.assertEqual(set(contract.KINDS), {"service_provider", "monitoring_provider", "backup_provider", "auth_provider", "data_source"})
        self.assertEqual(contract.API_VERSION, 1)
        for group, base in contract.KINDS.values():
            self.assertTrue(group.startswith("gisstack."))
            self.assertTrue(issubclass(base, contract.Plugin))

    def test_contract_module_imports_nothing_from_gisstack(self):
        import ast
        for node in ast.walk(ast.parse(Path(contract.__file__).read_text())):
            if isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                self.assertNotEqual((node.module or "").split(".")[0], "gisstack")
            if isinstance(node, ast.Import):
                self.assertFalse(any(a.name.startswith("gisstack") for a in node.names))


class LoaderTests(Env):
    def test_off_by_default(self):
        with mock.patch.dict(os.environ, {"GISSTACK_FEATURES": ""}):
            self.install(backup_provider=[ep("vault", Good)])
            self.assertEqual(plugins.get("backup_provider"), [])
            self.assertFalse(plugins.describe()["enabled"])

    def test_loads_classes_and_instances(self):
        self.install(backup_provider=[ep("vault", Good)], data_source=[ep("city", Layers())])
        self.assertEqual([p.name for p in plugins.get("backup_provider")], ["vault"])
        self.assertEqual([p.name for p in plugins.get("data_source")], ["city"])
        d = plugins.describe()
        self.assertEqual({(p["kind"], p["name"]) for p in d["plugins"]}, {("backup_provider", "vault"), ("data_source", "city")})
        self.assertEqual(d["failures"], [])

    def test_a_broken_plugin_is_reported_and_the_rest_still_load(self):
        class Wrong:                       # not derived from the contract
            name = "wrong"

        class Future(Good):
            name, api_version = "future", 2

        class BadName(Good):
            name = "Bad Name!"

        class Dup(Good):
            name = "vault"
        self.install(backup_provider=[ep("a", Good), ep("b", Wrong), ep("c", Future), ep("d", BadName), ep("e", Dup), ep("f", None, boom=ImportError("no module x")),
                                      ep("g", type("Crash", (Good,), {"__init__": lambda self: 1 / 0}))])
        self.assertEqual([p.name for p in plugins.get("backup_provider")], ["vault"])
        reasons = " | ".join(f.reason for f in plugins.failures())
        for needle in ("must derive from", "plugin API 2", "name must match", "duplicate", "no module x", "ZeroDivisionError"):
            self.assertIn(needle, reasons)
        self.assertEqual(len(plugins.failures()), 6)

    def test_a_broken_metadata_source_does_not_raise(self):
        def boom(group):
            raise OSError("metadata unreadable")
        loader.set_source(boom)
        self.assertEqual(plugins.get("auth_provider"), [])
        self.assertIn("metadata unreadable", plugins.failures()[0].reason)

    def test_safe_call_swallows_plugin_errors(self):
        class Bad(Sso):
            def authenticate(self, token):
                raise RuntimeError("secret detail")
        self.assertIsNone(plugins.safe_call(Bad(), "authenticate", "x"))
        self.assertEqual(plugins.safe_call(Bad(), "authenticate", "x", default=5), 5)

    def test_unknown_kind(self):
        with self.assertRaises(KeyError):
            plugins.get("nope")

    def test_cache_and_reload(self):
        self.install(data_source=[ep("city", Layers)])
        a = plugins.get("data_source")[0]
        self.assertIs(plugins.get("data_source")[0], a)
        plugins.reload()
        self.assertIsNot(plugins.get("data_source")[0], a)


class ConformanceKitTests(unittest.TestCase):
    def test_good_plugins_conform(self):
        for kind, obj in (("backup_provider", Good), ("monitoring_provider", Pager), ("auth_provider", Sso), ("data_source", Layers), ("service_provider", FakeProv)):
            self.assertEqual(kit.verify(kind, obj), [], kind)

    def test_each_kind_catches_its_typical_mistakes(self):
        class StoresNothing(Good):
            def store(self, archive, backup_id):
                return ""

        class Deletes(Good):
            def store(self, archive, backup_id):
                archive.unlink()
                return "gone"

        class Raises(Good):
            def store(self, archive, backup_id):
                raise OSError("disk full")

        class OpenDoor(Sso):
            def authenticate(self, token):
                return {"name": "x", "scope": "root"}

        class Crashy(Sso):
            def authenticate(self, token):
                raise ValueError(token)

        class NoName(Pager):
            def notifiers(self):
                return [SimpleNamespace(send=lambda n: None)]

        class BadLayers(Layers):
            def layers(self):
                return [{"id": "x"}]

        class NoProv(FakeProv):
            def provider(self):
                return SimpleNamespace(entry="x")
        for kind, cls, needle in (("backup_provider", StoresNothing, "non-empty"), ("backup_provider", Deletes, "must not alter"), ("backup_provider", Raises, "OSError"),
                                  ("auth_provider", OpenDoor, "read/operate/admin"), ("auth_provider", Crashy, "raised ValueError"),
                                  ("monitoring_provider", NoName, "needs a str `name`"), ("data_source", BadLayers, "needs non-empty"), ("service_provider", NoProv, "lacks")):
            problems = kit.verify(kind, cls)
            self.assertTrue(any(needle in x for x in problems), (kind, cls.__name__, problems))

    def test_contract_violations(self):
        self.assertTrue(kit.verify("nope", Good))
        self.assertTrue(kit.verify("auth_provider", Good))                     # wrong base class
        self.assertTrue(kit.verify("backup_provider", object()))

    def test_unittest_mixin_runs_for_plugin_authors(self):
        class Mine(kit.ConformanceTests):
            kind = "data_source"
            plugin = staticmethod(Layers)
        with open(os.devnull, "w") as quiet:
            r = unittest.TextTestRunner(stream=quiet, verbosity=0).run(unittest.defaultTestLoader.loadTestsFromTestCase(Mine))
        self.assertTrue(r.wasSuccessful())


class IntegrationTests(Env):
    def test_service_provider_target_never_shadows_builtin(self):
        self.install(service_provider=[ep("lxd", FakeProv), ep("native", type("Evil", (FakeProv,), {"name": "native"}))])
        self.assertEqual(providers.get("lxd").entry, "i.sh")
        self.assertEqual(providers.get("native").name, "native")
        with self.assertRaises(DeployError):
            providers.get("unknown")

    def test_monitoring_plugin_adds_notifiers_without_breaking_dispatch(self):
        seen = []

        class Loud(contract.MonitoringProvider):
            name = "loud"

            def notifiers(self):
                return [SimpleNamespace(name="ok", send=seen.append), SimpleNamespace(name="boom", send=lambda n: 1 / 0)]
        self.install(monitoring_provider=[ep("loud", Loud)])
        chans = notify.plugin_notifiers()
        self.assertEqual(len(chans), 2)
        note = {"rule": "r", "subject": "s", "level": "warn", "state": "firing", "message": "m"}
        res = notify.dispatch([note], chans)
        self.assertEqual([(r["provider"], r["ok"]) for r in res], [("ok", True), ("boom", False)])
        self.assertEqual(seen, [note])

    def test_a_crashing_notifier_factory_is_ignored(self):
        class Crash(contract.MonitoringProvider):
            name = "crash"

            def notifiers(self):
                raise RuntimeError("x")
        self.install(monitoring_provider=[ep("crash", Crash)])
        self.assertEqual(notify.plugin_notifiers(), [])

    def test_offsite_backup_downloads_verifies_stores_and_cleans_up(self):
        vault = Good()
        self.install(backup_provider=[ep("vault", vault)])
        made = []

        def fake_download(cfg, mode, ssh, bid, dest=None):
            f = Path(dest) / f"{bid}.tar.gz"
            f.write_bytes(b"data")
            made.append(Path(dest))
            return f
        bid = "20260101-000000-aaaaaaaa"
        with mock.patch.object(backup, "download", fake_download), mock.patch.object(backup, "_require_native"):
            lines = list(backup.offsite({}, "local", None, bid))
        self.assertIn(f"vault: vault://{bid}", lines)
        self.assertEqual(vault.stored, [(f"{bid}.tar.gz", bid)])
        self.assertFalse(made[0].exists())                                    # temporary copy removed

    def test_offsite_reports_failures_and_still_tries_the_others(self):
        class Down(Good):
            name = "down"

            def store(self, archive, backup_id):
                raise OSError("unreachable")
        ok = Good()
        self.install(backup_provider=[ep("down", Down), ep("vault", ok)])
        bid = "20260101-000000-aaaaaaaa"
        def fake_download(c, m, s, b, dest=None):
            f = Path(dest) / "x.tar.gz"
            f.write_bytes(b"1")
            return f
        with mock.patch.object(backup, "download", fake_download), mock.patch.object(backup, "_require_native"):
            lines, err = [], None
            try:
                for ln in backup.offsite({}, "local", None, bid):
                    lines.append(ln)
            except DeployError as e:
                err = e
        self.assertIsNotNone(err)
        self.assertEqual(len(ok.stored), 1)
        self.assertTrue(any("OSError" in x for x in lines))
        self.assertNotIn("unreachable", "\n".join(lines))                     # no foreign error text in logs

    def test_offsite_without_providers_is_an_error(self):
        self.install()
        with mock.patch.object(backup, "_require_native"), self.assertRaises(DeployError):
            list(backup.offsite({}, "local", None, "20260101-000000-aaaaaaaa"))


@unittest.skipUnless(HAVE_FLASK, "flask needed")
class ApiIntegrationTests(Env):
    def setUp(self):
        super().setUp()
        self.td = tempfile.mkdtemp()
        p = mock.patch.dict(os.environ, {"GISSTACK_HOME": self.td})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.td, True)
        from gisstack.webgui.app import create_app
        self.c = create_app("tok").test_client()

    def get(self, path, token="tok"):
        r = self.c.get("/api/v1" + path, headers={"Authorization": f"Bearer {token}"})
        self.addCleanup(r.close)
        return r

    def test_plugins_endpoint_and_datasources(self):
        self.install(data_source=[ep("city", Layers)], backup_provider=[ep("bad", None, boom=ImportError("x"))])
        d = self.get("/plugins").get_json()["data"]
        self.assertTrue(d["enabled"])
        self.assertEqual(d["plugins"][0]["name"], "city")
        self.assertEqual(d["failures"][0]["kind"], "backup_provider")
        self.assertEqual(self.get("/datasources").get_json()["data"], [{"source": "city", "id": "roads", "title": "Roads", "kind": "wfs"}])

    def test_datasources_hidden_and_plugins_reports_disabled_when_flag_off(self):
        with mock.patch.dict(os.environ, {"GISSTACK_FEATURES": ""}):
            self.assertEqual(self.get("/datasources").get_json()["error"]["code"], "feature_disabled")
            self.assertFalse(self.get("/plugins").get_json()["data"]["enabled"])

    def test_auth_provider_tokens_work_on_v1_only_with_their_scope(self):
        self.install(auth_provider=[ep("sso", Sso)])
        r = self.get("/meta", token="sso-good-token")
        p = r.get_json()["data"]["principal"]
        self.assertEqual((r.status_code, p["name"], p["scope"], p["kind"]), (200, "alice", "operate", "plugin"))
        self.assertEqual(self.get("/meta", token="sso-bad-token").status_code, 401)
        self.assertEqual(self.get("/plugins", token="sso-good-token").status_code, 200)
        r = self.c.get("/api/catalog", headers={"Authorization": "Bearer sso-good-token"})      # legacy routes never accept plugin tokens
        self.addCleanup(r.close)
        self.assertEqual(r.status_code, 401)
        r = self.c.put("/api/v1/settings", json={"theme": "dark"}, headers={"Authorization": "Bearer sso-good-token"})   # operate < admin
        self.addCleanup(r.close)
        self.assertEqual(r.status_code, 403)

    def test_a_crashing_auth_plugin_cannot_lock_out_the_real_token(self):
        class Crash(Sso):
            def authenticate(self, token):
                raise RuntimeError("down")
        self.install(auth_provider=[ep("sso", Crash)])
        self.assertEqual(self.get("/meta").status_code, 200)
        self.assertEqual(self.get("/meta", token="whatever").status_code, 401)

    def test_offsite_route_needs_admin_and_the_flag(self):
        from gisstack.api import auth
        op = auth.TokenStore().create("o", "operate")[0]
        body = {"mode": "local", "id": "20260101-000000-aaaaaaaa"}
        r = self.c.post("/api/v1/backups/offsite", json=body, headers={"Authorization": f"Bearer {op}"})
        self.addCleanup(r.close)
        self.assertEqual(r.status_code, 403)
        with mock.patch.dict(os.environ, {"GISSTACK_FEATURES": ""}):
            r = self.c.post("/api/v1/backups/offsite", json=body, headers={"Authorization": "Bearer tok"})
            self.addCleanup(r.close)
            self.assertEqual(r.get_json()["error"]["code"], "feature_disabled")


if __name__ == "__main__":
    unittest.main()
