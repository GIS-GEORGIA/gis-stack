"""Phase 2: every provider honours the same interface; ops/deploy are thin fronts over it."""
import unittest

from gisstack import config, deploy, ops, providers
from gisstack.core.errors import DeployError
from gisstack.providers import common

TARGETS = ("native", "compose", "k8s")


def cfg(target, **kw):
    return config.resolve({"targets": [target], "deploy_target": target, "components": {"qgis_server": True, "mapproxy": True, "caddy": True, "webmap": True}, **kw})


class InterfaceTests(unittest.TestCase):
    def test_registry_has_all_targets_and_rejects_unknown_ones(self):
        self.assertEqual(set(common.TARGETS), set(TARGETS))
        for t in TARGETS:
            self.assertEqual(providers.get(t).name, t)
        with self.assertRaises(DeployError):
            providers.get("mainframe")

    def test_entry_points_and_privilege_model(self):
        self.assertEqual([providers.get(t).entry for t in TARGETS], ["install.sh", "up.sh", "apply.sh"])
        self.assertEqual([providers.get(t).clean_entry for t in TARGETS], ["uninstall.sh", "up.sh", "apply.sh"])
        self.assertEqual([providers.get(t).needs_root for t in TARGETS], [True, False, False])

    def test_scripts_are_produced_for_every_action_and_component(self):
        for t in TARGETS:
            c, prov = cfg(t), providers.get(t)
            ids = common.components(c, t, None)
            for action in common.ACTIONS:
                self.assertTrue(prov.control_script(c, action, ids).startswith("set -u"), (t, action))
            for comp in common.ORDER:
                for follow in (False, True):
                    self.assertTrue(prov.logs_script(c, comp, 10, follow).strip(), (t, comp))

    def test_argument_builders_per_target(self):
        n, co, k = (providers.get(t) for t in TARGETS)
        self.assertEqual(n.clean_args(["caddy"], True, False, False), ["--only", "caddy", "--purge"])
        self.assertEqual(n.clean_args([], False, True, True), ["--remove-postgres", "--force-postgres"])
        self.assertEqual(co.clean_args(["caddy"], True, False, False), ["clean", "--purge", "caddy"])
        self.assertEqual(n.apply_args("qgis_server"), ["--only", "qgis"])
        self.assertEqual(co.apply_args("webmap"), ["only", "caddy"])
        for p in (co, k):
            with self.assertRaises(DeployError):                     # PostgreSQL package removal is native-only
                p.clean_args([], False, True, False)

    def test_status_rows_for_every_component_even_when_nothing_is_installed(self):
        answers = {"native": "", "compose": "[]\n@@inspect\n", "k8s": '{"items": []}'}
        for t in TARGETS:
            c = cfg(t)
            ids = common.components(c, t, None)
            rows = providers.get(t).status_rows(c, ids, lambda script, t=t: answers[t])
            self.assertEqual([r["id"] for r in rows], ids)
            self.assertTrue(all(r["state"] in ("missing", "stopped", "unknown") and not r["ok"] for r in rows), t)

    def test_status_rows_see_the_scripts_each_provider_builds(self):
        seen = {}
        for t in TARGETS:
            c = cfg(t)
            providers.get(t).status_rows(c, ["caddy"], lambda script, t=t: (seen.setdefault(t, script), '{"items": []}' if t == "k8s" else "")[1])
        self.assertIn("systemctl show", seen["native"])
        self.assertIn("compose", seen["compose"])
        self.assertIn("kubectl", seen["k8s"])

    def test_ops_and_deploy_delegate_to_the_provider(self):
        c = cfg("k8s")
        ids = common.components(c, "k8s", ["geoserver"])
        self.assertEqual(ops.control_script(c, "k8s", "stop", ids), providers.get("k8s").control_script(c, "stop", ids))
        self.assertEqual(deploy.apply_args("compose", "qgis_server"), providers.get("compose").apply_args("qgis_server"))
        with self.assertRaises(DeployError):
            ops.control_script(c, "k8s", "explode", ids)
        with self.assertRaises(DeployError):
            ops.control_script(c, "k8s", "start", [])
        with self.assertRaises(DeployError):
            ops.logs_script(c, "k8s", "nope")
        with self.assertRaises(DeployError):
            deploy.apply_args("native", "nope")

    def test_a_new_target_needs_only_a_module_and_a_registration(self):
        """Extensibility: a fake provider plugs into the same front (ops.control_script) without touching ops or deploy."""
        import types
        m = types.SimpleNamespace(ENTRY="go.sh", CLEAN_ENTRY="go.sh", ROOT=False, control_script=lambda c, a, o: f"set -u\necho {a} {o}\n",
                                  logs_script=lambda c, comp, n, f: "echo log\n", clean_args=lambda *a: [], apply_args=lambda c: [c],
                                  fetch_status=lambda c, ids, run: [])
        providers._REGISTRY["fake"] = providers.Provider("fake", m)          # type: ignore[arg-type]
        self.addCleanup(providers._REGISTRY.pop, "fake")
        self.assertEqual(ops.control_script({}, "fake", "start", ["caddy"]), "set -u\necho start ['caddy']\n")
        self.assertEqual(providers.get("fake").entry, "go.sh")


if __name__ == "__main__":
    unittest.main()


class ComposeHealthTextTests(unittest.TestCase):
    """Regression: right after `docker compose up` the Health field is empty while Status already says "(health: starting)"; the row was
    reported running/ok and a crash-looping failed upgrade looked healthy (found on real Docker Compose)."""

    def row(self, status, health="", state="running"):
        from gisstack import config
        from gisstack.providers import compose
        cfg = config.resolve({"project": "demo", "targets": ["compose"], "deploy_target": "compose"}, persist_secrets=False, generate_secrets=False)
        parsed = {"items": [{"Service": "postgis", "State": state, "Health": health, "Status": status}], "ips": {}, "http": {}}
        return compose.rows(cfg, ["postgis"], parsed, [])[0]

    def test_empty_health_with_starting_status_is_starting_not_ok(self):
        r = self.row("Up Less than a second (health: starting)")
        self.assertEqual((r["state"], r["ok"]), ("starting", False))

    def test_unhealthy_in_status_text(self):
        r = self.row("Up 2 minutes (unhealthy)")
        self.assertEqual((r["state"], r["ok"]), ("failed", False))

    def test_healthy_and_plain_running_are_still_ok(self):
        self.assertTrue(self.row("Up 2 minutes (healthy)")["ok"])
        self.assertTrue(self.row("Up 2 minutes", health="healthy")["ok"])
        self.assertEqual(self.row("Restarting (1) 3 seconds ago", state="restarting")["state"], "starting")
