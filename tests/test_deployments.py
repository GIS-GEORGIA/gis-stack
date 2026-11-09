"""Deployment records, health verification, rollback planning/execution and the rollback API.

Unit tested with a fake step runner and a fake status; the real install scripts, containers and clusters are NOT exercised here
(the opt-in suites in tests/integration cover deployment itself).  Not verified: a rollback on a real server."""
import json
import os
import shutil
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from gisstack import config, deploy, deployments, ops, services
from gisstack.core.errors import DeployError
from tests.test_api import ApiBase, ContractTests


def cfg_for(target="native", **over):
    c = config.resolve({"project": "demo", "deploy_target": target, "domain": "gis.example.org", **over}, persist_secrets=False, generate_secrets=False)
    c["postgres"]["password"] = c["postgres"].get("password") or "pg-secret-value-1"
    c["geoserver"]["admin_password"] = c["geoserver"].get("admin_password") or "gs-secret-value-1"
    return c


def rows(ok=True, state=None):
    return [{"id": "postgis", "state": state or ("running" if ok else "failed"), "ok": ok, "checks": [{"label": "tcp 5432", "ok": ok}]}]


class Env(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        p = mock.patch.dict(os.environ, {"GISSTACK_HOME": self.td, "GISSTACK_VERIFY_TIMEOUT": "0"})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.td, True)
        self.status_rows = rows(True)
        self.ran = []

        def entry(cfg, bundle, target, mode, ssh, entry, args, op="run"):
            self.ran.append((entry, list(args), op))
            if getattr(self, "boom", None):
                raise self.boom
            yield "installing"
        for patcher in (mock.patch.object(deploy, "_run_entry", entry), mock.patch.object(deploy, "_check_bundle"),
                        mock.patch.object(ops, "status", lambda *a, **k: {"rows": self.status_rows})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_deploy(self, cfg, target="native", **kw):
        return list(deploy.deploy(cfg, "/nonexistent", target, "local", None, **kw))


class VerifyTests(unittest.TestCase):
    def test_healthy_unhealthy_unknown_and_warming_up(self):
        cfg = cfg_for()
        self.assertEqual(deployments.verify(cfg, lambda: rows(True), timeout=0)["state"], "healthy")
        self.assertEqual(deployments.verify(cfg, lambda: rows(False), timeout=0)["state"], "unhealthy")
        calls = []

        def warming():
            calls.append(1)
            return rows(len(calls) >= 3)
        slept = []
        self.assertEqual(deployments.verify(cfg, warming, timeout=30, interval=5, sleep=slept.append)["state"], "healthy")
        self.assertEqual(slept, [5, 5])
        self.assertEqual(deployments.verify(cfg, lambda: rows(False), timeout=10, interval=5, sleep=lambda s: None)["state"], "unhealthy")

        def down():
            raise DeployError("ssh: connection refused for pg-secret-value-1")
        r = deployments.verify(cfg, down, timeout=0)
        self.assertEqual(r["state"], "unknown")                                       # an unreachable machine is never a pass
        self.assertNotIn("pg-secret-value-1", r["error"])
        self.assertEqual(deployments.verify(cfg, lambda: [], timeout=0)["state"], "unknown")


class RecordTests(Env):
    def test_ok_deploy_is_recorded_without_secrets(self):
        cfg = cfg_for()
        lines = self.run_deploy(cfg)
        self.assertTrue(any("ყველა პროგრამა" in x for x in lines))
        (rec,) = deployments.records("demo")
        self.assertEqual((rec["outcome"], rec["kind"], rec["target"], rec["mode"], rec["host"]), ("ok", "deploy", "native", "local", "localhost"))
        self.assertEqual(rec["before"][0]["id"], "postgis")
        raw = (Path(self.td) / "deployments" / f"{rec['id']}.json").read_text()
        for secret in (cfg["postgres"]["password"], cfg["geoserver"]["admin_password"]):
            self.assertNotIn(secret, raw)
        snap = Path(self.td) / "deployments" / f"{rec['id']}.config"
        self.assertEqual(stat.S_IMODE(snap.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((Path(self.td) / "deployments").stat().st_mode), 0o700)
        self.assertTrue(rec["config_saved"])

    def test_digest_ignores_secret_values_but_not_settings(self):
        a, b = cfg_for(), cfg_for()
        b["postgres"]["password"] = "rotated-password-2"
        self.assertEqual(deployments.digest(a), deployments.digest(b))
        b["domain"] = "other.example.org"
        self.assertNotEqual(deployments.digest(a), deployments.digest(b))

    def test_unhealthy_failed_aborted_unverified(self):
        cfg = cfg_for()
        self.status_rows = rows(False)
        lines = self.run_deploy(cfg)
        self.assertTrue(any(x.startswith("!! postgis") for x in lines))                # reported, not raised
        self.assertEqual(deployments.records("demo")[0]["outcome"], "unhealthy")
        self.boom = DeployError("install.sh exit 1 with pg-secret-value-1")
        with self.assertRaises(DeployError):
            self.run_deploy(cfg)
        rec = deployments.records("demo")[0]
        self.assertEqual(rec["outcome"], "failed")
        self.assertNotIn("pg-secret-value-1", rec["error"])
        self.boom = None
        gen = deploy.deploy(cfg, "/x", "native", "local", None)
        next(gen)
        gen.close()                                                                    # the consumer stopped reading
        self.assertEqual(deployments.records("demo")[0]["outcome"], "aborted")
        with mock.patch.dict(os.environ, {"GISSTACK_VERIFY": "0"}):
            self.run_deploy(cfg)
        self.assertEqual(deployments.records("demo")[0]["outcome"], "unverified")

    def test_unreachable_status_gives_unverified_not_ok(self):
        def down(*a, **k):
            raise DeployError("no route")
        with mock.patch.object(ops, "status", down):
            self.run_deploy(cfg_for())
        rec = deployments.records("demo")[0]
        self.assertEqual(rec["outcome"], "unverified")
        self.assertIn("unavailable", rec["before"])

    def test_stale_running_record_is_incomplete(self):
        rec = deployments.begin(cfg_for(), "native", "local", "localhost")
        self.assertEqual(deployments.records("demo")[0]["outcome"], "running")
        p = Path(self.td) / "deployments" / f"{rec['id']}.json"
        d = json.loads(p.read_text())
        d["ts"] -= deployments.STALE_AFTER + 10
        p.write_text(json.dumps(d))
        self.assertEqual(deployments.records("demo")[0]["outcome"], "incomplete")

    def test_every_target_is_tracked(self):
        for target in ("native", "compose", "k8s"):
            self.run_deploy(cfg_for(target), target)
        self.assertEqual(sorted(r["target"] for r in deployments.records("demo")), ["compose", "k8s", "native"])
        self.assertEqual(self.ran[0][0], "install.sh")

    def test_history_is_bounded_and_only_records_are_pruned(self):
        data = Path(self.td) / "keep.txt"
        data.write_text("user data")
        for _ in range(deployments.KEEP_SNAPSHOTS + 3):
            self.run_deploy(cfg_for())
        d = Path(self.td) / "deployments"
        self.assertLessEqual(len(list(d.glob("*.config"))), deployments.KEEP_SNAPSHOTS)
        self.assertEqual(data.read_text(), "user data")
        self.assertEqual(len(deployments.records("demo", 500)), deployments.KEEP_SNAPSHOTS + 3)       # records outlive their snapshots


class RollbackTests(Env):
    def history(self, *specs):
        """specs: (outcome, domain) oldest first; returns the record ids"""
        ids = []
        for outcome, domain in specs:
            c = cfg_for(domain=domain)
            rec = deployments.begin(c, "native", "local", "localhost")
            rec["ts"] = int(time.time()) - 1000 + len(ids)
            deployments.finish(rec, outcome)
            p = Path(self.td) / "deployments" / f"{rec['id']}.json"
            d = json.loads(p.read_text())
            d["ts"], d["n"] = rec["ts"], rec["ts"] * 10 ** 9 + len(ids)
            p.write_text(json.dumps(d))
            ids.append(rec["id"])
        return ids

    def plan(self, rid=""):
        return services.rollback_plan(cfg_for(), "local", None, "native", rid)

    def test_no_history_and_single_deploy_are_not_rollbackable(self):
        self.assertFalse(self.plan()["eligible"])
        self.history(("ok", "a.example.org"))
        p = self.plan()
        self.assertFalse(p["eligible"])
        self.assertIn("no earlier", p["reason"])

    def test_failed_deploy_after_healthy_one_is_eligible(self):
        good, bad = self.history(("ok", "a.example.org"), ("unhealthy", "b.example.org"))
        p = self.plan()
        self.assertTrue(p["eligible"], p)
        self.assertEqual((p["target"]["id"], p["current"]["id"]), (good, bad))
        self.assertIn("domain", p["changes"])
        self.assertTrue(any("No data" in x for x in p["limitations"]))
        self.assertNotIn("password", json.dumps(p).lower().replace("passwords", ""))

    def test_unverified_recovery_points_are_refused(self):
        good, mid, bad = self.history(("ok", "a.example.org"), ("unhealthy", "b.example.org"), ("failed", "c.example.org"))
        p = self.plan(mid)
        self.assertFalse(p["eligible"])
        self.assertIn("not verified healthy", p["reason"])
        self.assertFalse(self.plan("deadbeefdead")["eligible"])
        self.assertTrue(self.plan(good)["eligible"])

    def test_running_deployment_blocks_rollback(self):
        self.history(("ok", "a.example.org"))
        deployments.begin(cfg_for(domain="z.example.org"), "native", "local", "localhost")
        self.assertIn("still running", self.plan()["reason"])

    def test_other_machines_history_is_not_used(self):
        self.history(("ok", "a.example.org"), ("failed", "b.example.org"))
        p = services.rollback_plan(cfg_for(), "local", None, "compose")
        self.assertFalse(p["eligible"])

    def test_rollback_config_keeps_current_secrets(self):
        good, _bad = self.history(("ok", "a.example.org"), ("failed", "b.example.org"))
        current = cfg_for()
        current["postgres"]["password"] = "rotated-password-2"
        snap, _plan = services.rollback_config(current, "local", None, "native", good)
        self.assertEqual(snap["domain"], "a.example.org")                              # earlier settings...
        self.assertEqual(snap["postgres"]["password"], "rotated-password-2")           # ...current secrets
        with self.assertRaises(services.RollbackRefused) as cm:
            services.rollback_config(current, "local", None, "native", "deadbeefdead")
        self.assertFalse(cm.exception.plan["eligible"])

    def test_successful_rollback_is_recorded_idempotent_and_never_purges(self):
        good, bad = self.history(("ok", "a.example.org"), ("failed", "b.example.org"))
        snap, _ = services.rollback_config(cfg_for(), "local", None, "native", good)
        lines = list(services.rollback_steps(snap, Path("/x"), "native", "local", None, good))
        self.assertTrue(any("rollback" in x for x in lines))
        self.assertEqual(self.ran, [("install.sh", [], "rollback")])                  # the plain installer: no clean entry, no purge flags
        newest = deployments.records("demo")[0]
        self.assertEqual((newest["kind"], newest["rollback_of"], newest["outcome"]), ("rollback", good, "ok"))
        # again: the machine already runs that configuration and is healthy -> nothing to do (and the API refuses to repeat it)
        p = self.plan(good)
        self.assertFalse(p["eligible"])
        self.assertIn("nothing to do", p["reason"])

    def test_rollback_that_ends_unhealthy_is_a_failure_and_stops(self):
        good, _bad = self.history(("ok", "a.example.org"), ("failed", "b.example.org"))
        snap, _ = services.rollback_config(cfg_for(), "local", None, "native", good)
        self.status_rows = rows(False)
        with self.assertRaises(services.RollbackFailed):
            list(services.rollback_steps(snap, Path("/x"), "native", "local", None, good))
        self.assertEqual(len(self.ran), 1)                                            # nothing further was attempted
        self.assertEqual(deployments.records("demo")[0]["outcome"], "unhealthy")

    def test_rollback_whose_script_fails_is_recorded_as_failed(self):
        good, _bad = self.history(("ok", "a.example.org"), ("failed", "b.example.org"))
        snap, _ = services.rollback_config(cfg_for(), "local", None, "native", good)
        self.boom = DeployError("exit 3")
        with self.assertRaises(DeployError):
            list(services.rollback_steps(snap, Path("/x"), "native", "local", None, good))
        self.assertEqual(deployments.records("demo")[0]["outcome"], "failed")
        self.assertEqual(deployments.records("demo")[0]["kind"], "rollback")

    def test_every_target_has_a_limitation_text(self):
        for target in ("native", "compose", "k8s"):
            self.assertTrue(deployments.LIMITS[target])


class RollbackApiTests(ApiBase):
    BODY = {"mode": "local", "config": ContractTests.NATIVE}

    def setUp(self):
        super().setUp()
        p = mock.patch.dict(os.environ, {"GISSTACK_VERIFY_TIMEOUT": "0"})
        p.start()
        self.addCleanup(p.stop)
        self.cfg = config.resolve(self.BODY["config"], persist_secrets=False, generate_secrets=False)

    def seed(self, outcome, domain):
        c = dict(self.cfg, domain=domain)
        rec = deployments.begin(c, c["deploy_target"], "local", "localhost")
        deployments.finish(rec, outcome)
        return rec["id"]

    def audit(self):
        p = Path(self.td) / "audit.log"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def wait(self, jid):
        for _ in range(300):
            d = self.data(self.call("GET", f"/jobs/{jid}"))
            if d["done"]:
                return d
            time.sleep(0.02)
        self.fail("job did not finish")

    def test_history_and_plan_permissions_and_contract(self):
        good = self.seed("ok", "a.example.org")
        self.seed("failed", "b.example.org")
        self.assertEqual(self.call("GET", "/deployments/history", None).status_code, 401)
        h = self.data(self.call("GET", "/deployments/history", self.tokens["read"]))
        self.conforms("/deployments/history", "GET", h)
        self.assertEqual([r["outcome"] for r in h], ["failed", "ok"])
        self.assertNotIn("password", json.dumps(h).lower())
        self.assertEqual(self.call("POST", "/deployments/rollback/plan", self.tokens["read"], body=self.BODY).status_code, 403)
        plan = self.data(self.call("POST", "/deployments/rollback/plan", self.tokens["operate"], body=self.BODY))
        self.conforms("/deployments/rollback/plan", "POST", plan)
        self.assertTrue(plan["eligible"])
        self.assertEqual(plan["target"]["id"], good)

    def test_rollback_needs_admin_confirm_and_a_recovery_point(self):
        good = self.seed("ok", "a.example.org")
        body = {**self.BODY, "record": good, "confirm": True}
        for scope in ("read", "operate"):
            self.assertEqual(self.call("POST", "/deployments/rollback", self.tokens[scope], body=body).status_code, 403)
        self.assertEqual(self.call("POST", "/deployments/rollback", body={**self.BODY, "record": good, "confirm": False}).status_code, 400)
        r = self.call("POST", "/deployments/rollback", body=body)                      # only one deployment: nothing earlier / already there
        self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (409, "not_ready"))
        self.assertFalse(r.get_json()["error"]["details"]["eligible"])
        refused = [a for a in self.audit() if a["event"] == "deploy.rollback"]
        self.assertEqual((refused[-1]["ok"], refused[-1]["error"]), (False, "refused"))

    def test_rollback_job_success_failure_and_audit(self):
        good = self.seed("ok", "a.example.org")
        self.seed("failed", "b.example.org")
        body = {**self.BODY, "record": good, "confirm": True}
        ran = []

        def entry(cfg, bundle, target, mode, ssh, entry, args, op="run"):
            ran.append((entry, args, cfg["domain"]))
            yield "re-applying"
        status = {"ok": True}
        with mock.patch.object(deploy, "_run_entry", entry), mock.patch.object(deploy, "preflight", return_value=[]), mock.patch.object(deploy, "_check_bundle"), \
                mock.patch.object(ops, "status", lambda *a, **k: {"rows": rows(status["ok"])}):
            jid = self.data(self.call("POST", "/deployments/rollback", body=body))["job"]
            d = self.wait(jid)
            self.assertTrue(d["ok"], d["lines"])
            self.assertEqual(ran, [("install.sh", [], "a.example.org")])                # earlier settings re-applied, plain install
            self.assertEqual(self.call("POST", "/deployments/rollback", body=body).status_code, 409)       # idempotent: already there
            # a second incident, and a rollback that ends unhealthy
            self.seed("failed", "c.example.org")
            status["ok"] = False
            d = self.wait(self.data(self.call("POST", "/deployments/rollback", body=body))["job"])
            self.assertFalse(d["ok"])
            self.assertTrue(any("not verified healthy" in x for x in d["lines"]), d["lines"])
        events = [(a["event"], a.get("ok"), a.get("phase")) for a in self.audit() if a["event"] == "deploy.rollback"]
        self.assertEqual(events.count(("deploy.rollback", True, "start")), 2)
        self.assertNotIn("password", json.dumps(self.audit()).lower().replace("passwords", ""))

    def test_plain_deploy_is_audited_and_recorded(self):
        ran = []

        def entry(cfg, bundle, target, mode, ssh, entry, args, op="run"):
            ran.append(op)
            yield "x"
        with mock.patch.object(deploy, "_run_entry", entry), mock.patch.object(deploy, "preflight", return_value=[]), mock.patch.object(deploy, "_check_bundle"), \
                mock.patch.object(ops, "status", lambda *a, **k: {"rows": rows(True)}), mock.patch.object(services, "ensure_ready"), \
                mock.patch.object(services, "find_conflicts", return_value=None):
            d = self.wait(self.data(self.call("POST", "/deployments", body={**self.BODY, "skip_preflight": True}))["job"])
        self.assertTrue(d["ok"], d["lines"])
        self.assertIn("deploy.start", [a["event"] for a in self.audit()])
        self.assertEqual(self.data(self.call("GET", "/deployments/history"))[0]["outcome"], "ok")


if __name__ == "__main__":
    unittest.main()


class NeverBreakADeployTests(Env):
    def test_odd_status_failures_and_unwritable_history_do_not_stop_the_deployment(self):
        def weird(*a, **k):
            raise KeyError("components")
        with mock.patch.object(ops, "status", weird):
            lines = self.run_deploy(cfg_for())
        self.assertIn("installing", lines)
        rec = deployments.records("demo")[0]
        self.assertEqual(rec["outcome"], "unverified")
        self.assertIn("unavailable", rec["before"])
        with mock.patch.object(deployments, "begin", side_effect=PermissionError(13, "Permission denied")):
            lines = self.run_deploy(cfg_for())
        self.assertIn("installing", lines)
        self.assertTrue(any(x.startswith("!!") for x in lines))
