"""Phase 8: alert state machine, rule overrides and notification providers (real HTTP and SMTP receivers on localhost)."""
import base64
import json
import os
import socketserver
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from gisstack.monitoring import alerts, collect, notify, store

FIX = Path(__file__).parent / "fixtures/monitor/native_ubuntu2404_pg18.txt"


class Base(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.td, True))
        self.s = store.Store(self.td / "a.sqlite")
        self.addCleanup(self.s.close)

    def step(self, rules, ts, metrics, ctx=None):
        return alerts.evaluate(self.s, rules, metrics, ts, ctx)


def one(rule_id="disk_high", **over):
    r = {**alerts.DEFAULT_RULES[rule_id], "enabled": True, **over}
    return {rule_id: r}


class StateMachineTests(Base):
    def test_fires_after_n_consecutive_samples_once_and_resolves_on_the_first_good_one(self):
        rules = one("cpu_high", **{"for": 3})
        m = lambda v: [("host_cpu_percent", {}, v)]  # noqa: E731
        self.assertEqual(self.step(rules, 1, m(95)), [])
        self.assertEqual(self.step(rules, 2, m(96)), [])
        fired = self.step(rules, 3, m(97))
        self.assertEqual([(n["state"], n["level"], n["rule"]) for n in fired], [("firing", "warn", "cpu_high")])
        self.assertIn("97 %", fired[0]["message"])
        self.assertEqual(self.step(rules, 4, m(98)), [])                              # still firing: no repeat
        done = self.step(rules, 5, m(10))
        self.assertEqual([(n["state"], n["level"]) for n in done], [("resolved", "info")])
        self.assertTrue(done[0]["message"].startswith("resolved:"))
        self.assertEqual(self.step(rules, 6, m(10)), [])
        self.assertEqual([e["kind"] for e in self.s.events()], ["alert.resolved", "alert.firing"])

    def test_a_blip_resets_the_streak(self):
        rules = one("cpu_high", **{"for": 3})
        for ts, v in enumerate((95, 95, 10, 95, 95), 1):
            self.assertEqual(self.step(rules, ts, [("host_cpu_percent", {}, float(v))]), [])
        self.assertEqual(len(self.step(rules, 6, [("host_cpu_percent", {}, 95.0)])), 1)

    def test_every_series_has_its_own_state(self):
        rules = one("disk_high")
        n = self.step(rules, 1, [("disk_used_percent", {"mount": "/"}, 90.0), ("disk_used_percent", {"mount": "/var"}, 10.0)])
        self.assertEqual([x["subject"] for x in n], ["/"])
        n = self.step(rules, 2, [("disk_used_percent", {"mount": "/"}, 90.0), ("disk_used_percent", {"mount": "/var"}, 99.0)])
        self.assertEqual([x["subject"] for x in n], ["/var"])
        n = self.step(rules, 3, [("disk_used_percent", {"mount": "/"}, 10.0), ("disk_used_percent", {"mount": "/var"}, 99.0)])
        self.assertEqual([(x["subject"], x["state"]) for x in n], [("/", "resolved")])

    def test_no_data_neither_fires_nor_resolves(self):
        rules = one("disk_high")
        self.step(rules, 1, [("disk_used_percent", {"mount": "/"}, 99.0)])
        self.assertEqual(self.step(rules, 2, [("something_else", {}, 1.0)]), [])         # collector delivered nothing for this series
        self.assertEqual(self.s.alert_state()[0]["state"], "firing")
        self.assertEqual(self.step(one("cpu_high"), 3, []), [])
        self.assertEqual(self.step(rules, 4, [("disk_used_percent", {"mount": "/"}, 10.0)])[0]["state"], "resolved")

    def test_reminders_and_disabled_rules(self):
        rules = one("disk_high", renotify=100.0)
        self.step(rules, 0, [("disk_used_percent", {"mount": "/"}, 99.0)])
        self.assertEqual(self.step(rules, 50, [("disk_used_percent", {"mount": "/"}, 99.0)]), [])
        rem = self.step(rules, 101, [("disk_used_percent", {"mount": "/"}, 99.0)])
        self.assertEqual([(n["state"], n.get("reminder")) for n in rem], [("firing", True)])
        self.assertEqual(self.step(rules, 150, [("disk_used_percent", {"mount": "/"}, 99.0)]), [])
        off = one("disk_high", enabled=False)
        self.assertEqual(self.step(off, 200, [("disk_used_percent", {"mount": "/x"}, 99.0)]), [])

    def test_state_survives_a_restart(self):
        rules = one("cpu_high", **{"for": 3})
        self.step(rules, 1, [("host_cpu_percent", {}, 99.0)])
        self.step(rules, 2, [("host_cpu_percent", {}, 99.0)])
        self.s.close()
        s2 = store.Store(self.td / "a.sqlite")
        self.addCleanup(s2.close)
        fired = alerts.evaluate(s2, rules, [("host_cpu_percent", {}, 99.0)], 3)
        self.assertEqual(len(fired), 1)

    def test_context_is_attached_to_notifications(self):
        n = self.step(one("disk_critical"), 1, [("disk_used_percent", {"mount": "/"}, 99.0)], {"project": "p", "host": "srv"})
        self.assertEqual((n[0]["project"], n[0]["host"], n[0]["level"]), ("p", "srv", "crit"))

    def test_real_sample_service_down_and_probe_rules(self):
        from gisstack import config
        cfg = config.resolve({"targets": ["native"], "deploy_target": "native", "postgres": {"version": "18"}})
        rows_ok = [{"id": "caddy", "state": "running", "restarts": 0, "enabled": True}]
        rows_bad = [{"id": "caddy", "state": "failed", "restarts": 3, "enabled": True}]
        rules = {k: dict(v, enabled=True) for k, v in alerts.DEFAULT_RULES.items()}
        sent = []
        for i, rows in enumerate((rows_ok, rows_bad, rows_bad, rows_bad)):
            sample = collect.build_sample(cfg, "native", FIX.read_text(), rows, 1.0)
            sent += alerts.evaluate(self.s, rules, collect.flatten(sample), 100.0 + i * 30)
        down = [n for n in sent if n["rule"] == "service_down"]
        self.assertEqual([(n["subject"], n["state"], n["level"]) for n in down], [("caddy", "firing", "crit")])      # needs 2 consecutive samples
        self.assertEqual([n for n in sent if n["rule"] in ("probe_failing", "cpu_high", "disk_high", "postgres_down")], [])    # the healthy fixture stays quiet


class RuleFileTests(unittest.TestCase):
    def write(self, data):
        p = Path(tempfile.mkdtemp()) / "rules.json"
        p.write_text(data if isinstance(data, str) else json.dumps(data))
        return p

    def test_defaults_are_complete_and_valid(self):
        rules = alerts.load_rules(None)
        self.assertEqual(set(rules), set(alerts.DEFAULT_RULES))
        for r in rules.values():
            self.assertIn(r["op"], alerts.OPS)
            self.assertIn(r["level"], ("warn", "crit"))
            self.assertGreaterEqual(r["for"], 1)
        self.assertEqual(alerts.load_rules(Path("/does/not/exist")), rules)

    def test_defaults_only_reference_metrics_the_collector_can_emit(self):
        from gisstack import config
        cfg = config.resolve({"targets": ["native"], "deploy_target": "native", "postgres": {"version": "18"}})
        sample = collect.build_sample(cfg, "native", FIX.read_text(), [{"id": "caddy", "state": "running", "restarts": 0, "enabled": True}], 1.0)
        names = {n for n, _, _ in collect.flatten(sample)} | {"host_swap_used_percent"}      # swap exists only on hosts that have swap
        for rid, r in alerts.DEFAULT_RULES.items():
            self.assertIn(r["metric"], names, rid)

    def test_overrides(self):
        r = alerts.load_rules(self.write({"rules": {"cpu_high": {"threshold": 75, "for": 2, "level": "crit"}, "swap_high": {"enabled": False}}}))
        self.assertEqual((r["cpu_high"]["threshold"], r["cpu_high"]["for"], r["cpu_high"]["level"]), (75.0, 2, "crit"))
        self.assertFalse(r["swap_high"]["enabled"])
        self.assertTrue(r["disk_high"]["enabled"])

    def test_bad_files_are_rejected_with_a_message(self):
        for bad in ('{"rules": {"nope": {}}}', '{"rules": {"cpu_high": {"metric": "x"}}}', '{"rules": {"cpu_high": {"threshold": "high"}}}',
                    '{"rules": {"cpu_high": {"threshold": true}}}', '{"rules": {"cpu_high": {"for": 0}}}', '{"rules": {"cpu_high": {"for": 1.5}}}',
                    '{"rules": {"cpu_high": {"level": "info"}}}', '{"rules": {"cpu_high": {"op": "~"}}}', '{"rules": {"cpu_high": {"enabled": "yes"}}}',
                    '{"rules": []}', '[1]', 'not json', '{"rules": {"cpu_high": 5}}'):
            with self.assertRaises(alerts.RulesError, msg=bad):
                alerts.load_rules(self.write(bad))


class Receiver:
    """A local HTTP receiver that records requests and answers with a scripted sequence of statuses."""

    def __init__(self, statuses=(200,), redirect_to=None):
        self.requests, self.statuses, outer = [], list(statuses), self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.requests.append((self.path, self.headers.get("Content-Type"), body))
                if redirect_to:
                    self.send_response(302)
                    self.send_header("Location", redirect_to)
                    self.end_headers()
                    return
                code = outer.statuses.pop(0) if len(outer.statuses) > 1 else outer.statuses[0]
                self.send_response(code)
                self.end_headers()

            def log_message(self, *a):
                pass
        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}/hook/SECRET-TOKEN"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


NOTE = {"rule": "disk_high", "subject": "/", "level": "crit", "state": "firing", "message": "disk / is 99 % full", "value": 99.0, "project": "p", "host": "srv", "ts": 1.0}


class WebhookTests(unittest.TestCase):
    def test_posts_json_with_text_and_alert(self):
        r = Receiver()
        self.addCleanup(r.close)
        notify.WebhookProvider(r.url).send(NOTE)
        (path, ctype, body), = r.requests
        data = json.loads(body)
        self.assertEqual(path, "/hook/SECRET-TOKEN")
        self.assertIn("application/json", ctype)
        self.assertIn("disk / is 99 % full", data["text"])
        self.assertIn("(p @ srv)", data["text"])
        self.assertEqual(data["alert"]["rule"], "disk_high")

    def test_transient_failures_are_retried_client_errors_are_not(self):
        r = Receiver([500, 503, 200])
        self.addCleanup(r.close)
        sleeps = []
        notify.WebhookProvider(r.url, sleep=sleeps.append).send(NOTE)
        self.assertEqual(len(r.requests), 3)
        self.assertEqual(sleeps, [0.5, 1.0])
        r2 = Receiver([404])
        self.addCleanup(r2.close)
        with self.assertRaises(notify.NotifyError) as cm:
            notify.WebhookProvider(r2.url, sleep=lambda s: None).send(NOTE)
        self.assertEqual(len(r2.requests), 1)
        self.assertIn("404", str(cm.exception))
        self.assertNotIn("SECRET-TOKEN", str(cm.exception))                               # the URL (a credential) never appears in errors

    def test_unreachable_receiver_raises_cleanly(self):
        with self.assertRaises(notify.NotifyError):
            notify.WebhookProvider("http://127.0.0.1:9/x", retries=0, timeout=2).send(NOTE)

    def test_redirects_are_not_followed(self):
        target = Receiver()
        self.addCleanup(target.close)
        r = Receiver(redirect_to=target.url)
        self.addCleanup(r.close)
        with self.assertRaises(notify.NotifyError):
            notify.WebhookProvider(r.url, retries=0).send(NOTE)
        self.assertEqual(target.requests, [])

    def test_only_https_or_loopback_http_and_no_other_schemes(self):
        for url in ("http://example.com/h", "ftp://example.com/h", "file:///etc/passwd", "javascript:alert(1)", "https:///nohost", ""):
            with self.assertRaises(notify.NotifyError, msg=url):
                notify.WebhookProvider(url)
        notify.WebhookProvider("https://hooks.example.com/x")
        notify.WebhookProvider("http://example.com/h", allow_insecure=True)
        notify.WebhookProvider("http://[::1]:8080/h")


class SmtpSink(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        self.mails, self.auth = [], []
        outer = self

        class H(socketserver.StreamRequestHandler):
            def reply(self, text):
                self.wfile.write(text.encode() + b"\r\n")

            def handle(self):
                self.reply("220 sink ready")
                mail = {"to": [], "data": ""}
                while True:
                    line = self.rfile.readline().decode().rstrip("\r\n")
                    cmd = line.upper()
                    if cmd.startswith("EHLO"):
                        self.wfile.write(b"250-sink\r\n250 AUTH PLAIN\r\n")
                    elif cmd.startswith("AUTH PLAIN"):
                        outer.auth.append(base64.b64decode(line.split()[2]).split(b"\0")[1:])
                        self.reply("235 ok")
                    elif cmd.startswith("MAIL FROM"):
                        mail["from"] = line[10:]
                        self.reply("250 ok")
                    elif cmd.startswith("RCPT TO"):
                        mail["to"].append(line[8:])
                        self.reply("250 ok")
                    elif cmd == "DATA":
                        self.reply("354 go")
                        buf = []
                        while True:
                            d = self.rfile.readline().decode()
                            if d.rstrip("\r\n") == ".":
                                break
                            buf.append(d)
                        mail["data"] = "".join(buf)
                        outer.mails.append(mail)
                        self.reply("250 queued")
                    elif cmd == "QUIT":
                        self.reply("221 bye")
                        return
                    elif not line:
                        return
                    else:
                        self.reply("250 ok")
        super().__init__(("127.0.0.1", 0), H)
        threading.Thread(target=self.serve_forever, daemon=True).start()


class EmailTests(unittest.TestCase):
    def setUp(self):
        self.sink = SmtpSink()
        self.addCleanup(lambda: (self.sink.shutdown(), self.sink.server_close()))

    def test_sends_a_plain_mail_and_logs_in_with_the_password_from_the_environment(self):
        p = notify.EmailProvider("127.0.0.1", self.sink.server_address[1], "gis@example.ge", ["ops@example.ge", "dev@example.ge"], user="gis")
        with mock.patch.dict(os.environ, {"GISSTACK_SMTP_PASSWORD": "S3cretPass"}):
            p.send(NOTE)
        (m,) = self.sink.mails
        self.assertEqual(m["to"], ["<ops@example.ge>", "<dev@example.ge>"])
        self.assertIn("Subject: [gisstack] CRIT firing: disk / is 99 % full", m["data"])
        self.assertIn("disk / is 99 % full", m["data"])
        self.assertEqual(self.sink.auth, [[b"gis", b"S3cretPass"]])

    def test_header_injection_through_the_alert_text_is_neutralised(self):
        p = notify.EmailProvider("127.0.0.1", self.sink.server_address[1], "gis@example.ge", ["ops@example.ge"])
        p.send({**NOTE, "message": "x\r\nBcc: attacker@evil.example\r\nSubject: pwned"})
        header_lines = self.sink.mails[0]["data"].split("\r\n\r\n")[0].lower().splitlines()
        self.assertFalse([h for h in header_lines if h.startswith("bcc:")])                  # no injected header line ...
        self.assertEqual(len([h for h in header_lines if h.startswith("subject:")]), 1)     # ... only one Subject; the text stays inside it

    def test_bad_addresses_are_rejected_up_front(self):
        for sender, to in (("a@b.c\nBcc: x@y.z", ["o@p.q"]), ("a@b.c", ["o@p.q,x@y.z"]), ("nobody", ["o@p.q"]), ("a@b.c", [])):
            with self.assertRaises(notify.NotifyError, msg=(sender, to)):
                notify.EmailProvider("127.0.0.1", 25, sender, to)

    def test_server_errors_do_not_leak_server_text(self):
        with self.assertRaises(notify.NotifyError) as cm:
            notify.EmailProvider("127.0.0.1", 9, "a@b.c", ["o@p.q"], timeout=2).send(NOTE)
        self.assertEqual(str(cm.exception), "email:127.0.0.1: ConnectionRefusedError")

    def test_starttls_is_default_for_remote_hosts_only(self):
        self.assertFalse(notify.EmailProvider("localhost", 25, "a@b.c", ["o@p.q"]).starttls)
        self.assertTrue(notify.EmailProvider("smtp.example.com", 587, "a@b.c", ["o@p.q"]).starttls)


class ConfigAndDispatchTests(unittest.TestCase):
    def write(self, data):
        p = Path(tempfile.mkdtemp()) / "notify.json"
        p.write_text(json.dumps(data))
        return p

    def test_load_providers(self):
        self.assertEqual(notify.load_providers(None), [])
        ps = notify.load_providers(self.write({"webhooks": [{"url": "https://hooks.example.com/x"}], "email": {"host": "smtp.example.com", "from": "a@b.c", "to": "o@p.q"}}))
        self.assertEqual([p.name for p in ps], ["webhook:hooks.example.com", "email:smtp.example.com"])

    def test_config_files_must_not_contain_secrets_or_unknown_keys(self):
        for bad in ({"email": {"host": "h", "from": "a@b.c", "to": "o@p.q", "password": "x"}}, {"smtp": {}}, {"webhooks": [{"nourl": 1}]},
                    {"webhooks": [{"url": "http://example.com/x"}]}, {"email": {"host": "h"}}):
            with self.assertRaises(notify.NotifyError, msg=str(bad)):
                notify.load_providers(self.write(bad))

    def test_a_failing_provider_does_not_stop_the_others_and_is_reported(self):
        good = Receiver()
        self.addCleanup(good.close)
        failures = []
        provs = [notify.WebhookProvider("http://127.0.0.1:9/dead", retries=0, timeout=1), notify.WebhookProvider(good.url)]
        res = notify.dispatch([NOTE, {**NOTE, "state": "resolved"}], provs, on_failure=lambda n, m: failures.append((n, m)))
        self.assertEqual([r["ok"] for r in res], [False, True, False, True])
        self.assertEqual(len(good.requests), 2)
        self.assertEqual(len(failures), 2)
        self.assertNotIn("SECRET-TOKEN", json.dumps(res))


if __name__ == "__main__":
    unittest.main()


class SuppressionTests(Base):
    """One outage must produce one page, not five (a down Caddy makes every ':public' health check fail)."""

    def rules(self):
        return {k: {**v, "enabled": True, "for": 1} for k, v in alerts.DEFAULT_RULES.items() if k in ("service_down", "probe_failing", "postgres_down")}

    def m(self, caddy_up, public_up, pg_service_up=1.0, pg_up=1.0):
        return [("service_up", {"service": "caddy"}, caddy_up), ("service_up", {"service": "postgis"}, pg_service_up),
                ("probe_up", {"probe": "caddy:public"}, public_up), ("probe_up", {"probe": "geoserver:public"}, public_up),
                ("probe_up", {"probe": "tomcat:http_8080"}, 1.0), ("pg_up", {}, pg_up)]

    def test_symptoms_of_a_reported_cause_are_held_back_and_resolve_silently(self):
        rules = self.rules()
        sent = self.step(rules, 1, self.m(0.0, 0.0))
        self.assertEqual([(n["rule"], n["subject"]) for n in sent], [("service_down", "caddy")])
        states = {(a["rule"], a["subject"]): a for a in self.s.alert_state()}
        self.assertEqual(states[("probe_failing", "caddy:public")]["state"], "firing")           # recorded, not announced
        self.assertEqual(states[("probe_failing", "caddy:public")]["notified"], alerts.SUPPRESSED)
        self.assertIn("alert.suppressed", [e["kind"] for e in self.s.events()])
        done = self.step(rules, 2, self.m(1.0, 1.0))
        self.assertEqual([(n["rule"], n["state"]) for n in done], [("service_down", "resolved")])    # one page up, one page down

    def test_a_symptom_that_outlives_its_cause_is_reported_then(self):
        rules = self.rules()
        self.step(rules, 1, self.m(0.0, 0.0))
        later = self.step(rules, 2, self.m(1.0, 0.0))                                           # Caddy is back, the checks still fail
        self.assertEqual(sorted((n["rule"], n["subject"], n["state"]) for n in later),
                         [("probe_failing", "caddy:public", "firing"), ("probe_failing", "geoserver:public", "firing"), ("service_down", "caddy", "resolved")])

    def test_unrelated_failures_are_not_suppressed(self):
        rules = self.rules()
        self.step(rules, 1, self.m(0.0, 1.0))
        sent = self.step(rules, 2, [("service_up", {"service": "caddy"}, 0.0), ("probe_up", {"probe": "tomcat:http_8080"}, 0.0)])
        self.assertEqual([(n["rule"], n["subject"]) for n in sent], [("probe_failing", "tomcat:http_8080")])

    def test_postgres_symptoms_follow_the_postgis_service(self):
        rules = self.rules()
        sent = self.step(rules, 1, self.m(1.0, 1.0, pg_service_up=0.0, pg_up=0.0))
        self.assertEqual([(n["rule"], n["subject"]) for n in sent], [("service_down", "postgis")])
        self.assertEqual([n["rule"] for n in self.step(rules, 2, self.m(1.0, 1.0, pg_up=0.0))].count("postgres_down"), 1)    # service back, DB still silent

    def test_reminders_are_not_sent_for_held_back_alerts(self):
        rules = {k: {**v, "renotify": 10.0} for k, v in self.rules().items()}
        self.step(rules, 1, self.m(0.0, 0.0))
        again = self.step(rules, 100, self.m(0.0, 0.0))
        self.assertEqual([(n["rule"], n.get("reminder")) for n in again], [("service_down", True)])
