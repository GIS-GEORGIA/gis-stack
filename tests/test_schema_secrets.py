"""Phase 1b: config schema_version + migrations, and secrets supplied by environment variable / secret file."""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gisstack import config, secretsrc


class SchemaTests(unittest.TestCase):
    def test_old_files_without_a_version_still_load_and_get_the_current_one(self):
        cfg = config.resolve({"project": "legacy"})
        self.assertEqual(cfg["schema_version"], config.SCHEMA_VERSION)

    def test_newer_or_malformed_versions_are_refused_clearly(self):
        with self.assertRaises(config.ConfigError) as cm:
            config.resolve({"schema_version": config.SCHEMA_VERSION + 1})
        self.assertIn("schema_version", str(cm.exception))
        for bad in ("1", -1, 1.5, True, None, [1]):
            if bad is None:
                continue
            with self.assertRaises(config.ConfigError, msg=repr(bad)):
                config.resolve({"schema_version": bad})

    def test_migration_chain_runs_in_order_and_reports_what_it_did(self):
        steps = []
        migs = {0: lambda r: r, 1: lambda r: (steps.append(1), {**r, "project": r["project"].upper()})[1],
                2: lambda r: (steps.append(2), {k: v for k, v in r.items() if k != "old_key"})[1]}
        with mock.patch.object(config, "SCHEMA_VERSION", 3), mock.patch.object(config, "MIGRATIONS", migs):
            out, notes = config.migrate({"schema_version": 1, "project": "abc", "old_key": 1})
        self.assertEqual(steps, [1, 2])
        self.assertEqual((out["project"], out["schema_version"], "old_key" in out), ("ABC", 3, False))
        self.assertEqual(len(notes), 1)
        self.assertIn("1 -> 3", notes[0])

    def test_unversioned_file_is_migrated_silently(self):
        _, notes = config.migrate({"project": "x"})
        self.assertEqual(notes, [])

    def test_generated_bundle_config_carries_the_version(self):
        from gisstack import generate
        with tempfile.TemporaryDirectory() as td:
            out = generate.generate_bundle(config.resolve({}), Path(td) / "b")
            self.assertEqual(json.loads((out / "stack.json").read_text())["schema_version"], config.SCHEMA_VERSION)


class SecretSourceTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        for n in secretsrc.NAMES:
            self.addCleanup(os.environ.pop, secretsrc.env_name(n), None)
            self.addCleanup(os.environ.pop, secretsrc.env_name(n) + "_FILE", None)
        p = mock.patch.dict(os.environ, {"GISSTACK_HOME": self.td.name})
        p.start()
        self.addCleanup(p.stop)

    def test_environment_variable_supplies_the_password(self):
        os.environ["GISSTACK_POSTGRES_PASSWORD"] = "From-Env_123"
        self.assertEqual(config.resolve({})["postgres"]["password"], "From-Env_123")

    def test_secret_file_supplies_the_password_and_must_be_private(self):
        f = Path(self.td.name) / "pg.secret"
        f.write_text("From-File_456\n")
        os.chmod(f, 0o600)
        os.environ["GISSTACK_GEOSERVER_ADMIN_PASSWORD_FILE"] = str(f)
        self.assertEqual(config.resolve({})["geoserver"]["admin_password"], "From-File_456")
        os.chmod(f, 0o644)
        with self.assertRaises(config.ConfigError) as cm:
            config.resolve({})
        self.assertIn("readable by group/others", str(cm.exception))

    def test_missing_or_oversized_file_is_an_error_not_a_silent_fallback(self):
        os.environ["GISSTACK_POSTGRES_PASSWORD_FILE"] = str(Path(self.td.name) / "nope")
        with self.assertRaises(config.ConfigError):
            config.resolve({})
        big = Path(self.td.name) / "big"
        big.write_text("x" * 5000)
        os.chmod(big, 0o600)
        os.environ["GISSTACK_POSTGRES_PASSWORD_FILE"] = str(big)
        with self.assertRaises(config.ConfigError):
            config.resolve({})

    def test_config_value_beats_provider_and_provider_beats_generated(self):
        os.environ["GISSTACK_POSTGRES_PASSWORD"] = "From-Env_123"
        self.assertEqual(config.resolve({"postgres": {"password": "Explicit_789"}})["postgres"]["password"], "Explicit_789")
        generated = config.resolve({}, persist_secrets=True)["geoserver"]["admin_password"]
        self.assertTrue(generated)

    def test_provided_secrets_are_validated_like_any_other(self):
        os.environ["GISSTACK_POSTGRES_PASSWORD"] = "bad pass; rm -rf /"
        with self.assertRaises(config.ConfigError):
            config.resolve({})

    def test_provided_secrets_are_not_written_to_the_secret_store(self):
        os.environ["GISSTACK_POSTGRES_PASSWORD"] = "From-Env_123"
        config.resolve({"project": "pp"}, persist_secrets=True)
        store = Path(self.td.name) / "secrets/pp.json"
        self.assertNotIn("From-Env_123", store.read_text())
        self.assertEqual(stat.S_IMODE(store.stat().st_mode), 0o600)

    def test_custom_providers_can_be_registered(self):
        class Vaultish:
            def get(self, name):
                return "Vault-Secret_1" if name == "tomcat.manager_password" else None
        saved = list(secretsrc._providers)
        self.addCleanup(lambda: secretsrc._providers.__setitem__(slice(None), saved))
        secretsrc.register(Vaultish())
        self.assertEqual(config.resolve({})["tomcat"]["manager_password"], "Vault-Secret_1")


if __name__ == "__main__":
    unittest.main()
