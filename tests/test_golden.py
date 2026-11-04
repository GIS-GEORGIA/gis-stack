"""Phase 2 safety net: the architecture refactor must not change anything a user can observe.

* every file of generated bundles (all targets, all components) is compared by SHA-256 with a recorded manifest;
* every script the operations layer builds (status / control / logs per target and component, clean / apply arguments)
  is compared the same way.

Intentional changes: run `UPDATE_GOLDEN=1 python -m unittest tests.test_golden` and review the diff of tests/golden/*.json."""
import hashlib
import itertools
import json
import os
import tempfile
import unittest
from pathlib import Path

from gisstack.providers import common, compose, kubernetes, native
from gisstack import __version__, config, deploy, generate, ops

GOLDEN = Path(__file__).parent / "golden"
UPDATE = os.environ.get("UPDATE_GOLDEN") == "1"
SKIP_SUFFIX = (".tgz", ".zip", ".png", ".jpg", ".ico", ".woff", ".woff2")      # archives/binaries may embed timestamps


def _cfg(**over):
    base = {"project": "golden", "domain": "gis.example.ge", "email": "ops@example.ge", "tls": "internal",
            "targets": ["native", "compose", "k8s"], "runtime": "docker",
            "postgres": {"password": "Golden-Pg_pass1"}, "geoserver": {"admin_password": "Golden-Gs_pass1", "extensions": ["vectortiles", "css"]},
            "tomcat": {"manager_password": "Golden-Tc_pass1"},
            "components": {"postgis": True, "tomcat": True, "geoserver": True, "qgis_server": True, "mapproxy": True, "caddy": True,
                           "webmap": True, "mobile_templates": True}}
    for k, v in over.items():
        base[k] = {**base[k], **v} if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return config.resolve(base)


def _bundle_manifest(cfg) -> dict:
    with tempfile.TemporaryDirectory() as td:
        out = generate.generate_bundle(cfg, Path(td) / "b")
        res = {}
        for p in sorted(out.rglob("*")):
            if not p.is_file() or p.suffix in SKIP_SUFFIX or "vendor" in p.parts:
                continue
            data = p.read_bytes().replace(__version__.encode(), b"@VERSION@")
            res[p.relative_to(out).as_posix()] = hashlib.sha256(data).hexdigest()
        return res


def _scripts_manifest() -> dict:
    res = {}
    for target in ("native", "compose", "k8s"):
        cfg = _cfg(deploy_target=target)
        ids = common.components(cfg, target, None)
        for action in ops.ACTIONS:
            res[f"control/{target}/{action}"] = hashlib.sha256(ops.control_script(cfg, target, action, ids).encode()).hexdigest()
            res[f"control/{target}/{action}/one"] = hashlib.sha256(ops.control_script(cfg, target, action, ids[:1]).encode()).hexdigest()
        for comp, follow in itertools.product(ops.ORDER, (False, True)):
            res[f"logs/{target}/{comp}/{follow}"] = hashlib.sha256(ops.logs_script(cfg, target, comp, 50, follow).encode()).hexdigest()
        checks = common.checks(cfg, target, ids)
        if target == "native":
            res["status/native"] = hashlib.sha256(native.status_script(cfg, checks).encode()).hexdigest()
        elif target == "compose":
            res["status/compose"] = hashlib.sha256(compose.status_script(cfg, checks).encode()).hexdigest()
        else:
            res["status/k8s"] = hashlib.sha256(kubernetes.status_script(cfg).encode()).hexdigest()
        res[f"clean_args/{target}/all"] = deploy.clean_args(target, None, purge=True) if target != "native" else deploy.clean_args(target, None, True, True, True)
        res[f"clean_args/{target}/some"] = deploy.clean_args(target, ["caddy", "qgis_server"], purge=False)
        for comp in deploy.CLEAN_COMPONENTS:
            res[f"apply_args/{target}/{comp}"] = deploy.apply_args(target, comp)
    return res


def _check(testcase, name, actual):
    path = GOLDEN / name
    if UPDATE or not path.exists():
        path.write_text(json.dumps(actual, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
        if not UPDATE:
            testcase.fail(f"{name} did not exist and was created: review and commit it")
        return
    expected = json.loads(path.read_text())
    changed = sorted(k for k in set(expected) | set(actual) if expected.get(k) != actual.get(k))
    testcase.assertEqual(changed, [], f"{name}: these entries differ from the golden record (intentional? UPDATE_GOLDEN=1): {changed[:25]}")


class GoldenTests(unittest.TestCase):
    def test_full_bundle_is_unchanged(self):
        _check(self, "bundle_full.json", _bundle_manifest(_cfg()))

    def test_minimal_native_and_compose_only_bundles_are_unchanged(self):
        _check(self, "bundle_native_min.json", _bundle_manifest(_cfg(targets=["native"], deploy_target="native",
               components={"geoserver": False, "tomcat": False, "qgis_server": False, "mapproxy": False, "webmap": False, "mobile_templates": False})))
        _check(self, "bundle_compose_podman.json", _bundle_manifest(_cfg(targets=["compose"], deploy_target="compose", runtime="podman", tls="none")))

    def test_bundle_generation_is_deterministic(self):
        self.assertEqual(_bundle_manifest(_cfg()), _bundle_manifest(_cfg()))

    def test_operation_scripts_and_argument_builders_are_unchanged(self):
        _check(self, "operations.json", _scripts_manifest())


if __name__ == "__main__":
    unittest.main()
