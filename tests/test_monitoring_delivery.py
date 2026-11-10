"""Alert DELIVERY against a real HTTP receiver on loopback: the receiver is down when the alert fires, comes back later, answers slowly, returns errors.
A notification that could not be delivered must not be lost (outbox with back-off), must arrive after recovery in order, must not be duplicated,
and must not stall the monitoring loop.  No mocks of the network: real sockets, real timeouts."""
import json
import socket
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
from test_monitoring_service import Base, ROWS, cfg  # noqa: E402

from gisstack.monitoring import notify, service  # noqa: E402

DOWN = [dict(r, state="stopped") if r["id"] == "caddy" else dict(r) for r in ROWS]            # one failing service: one alert


class Receiver:
    """A webhook receiver whose behaviour can be switched: ok | fail500 | slow | closed (nothing listens)."""

    def __init__(self):
        self.mode = "ok"
        self.got = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if outer.mode == "slow":
                    time.sleep(3)
                if outer.mode == "fail500":
                    self.send_response(500)
                    self.end_headers()
                    return
                outer.got.append(json.loads(body))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.H = H
        self.srv = None

    def start(self):
        self.srv = HTTPServer(("127.0.0.1", self.port), self.H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()
            self.srv = None

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/hook"

    def texts(self):
        return [g["alert"]["rule"] + ":" + g["alert"]["subject"] + ":" + g["alert"]["state"] for g in self.got]


class DeliveryTests(Base):
    def setUp(self):
        super().setUp()
        self.rx = Receiver()
        self.addCleanup(self.rx.stop)
        self.now = [1_800_000_000.0]

    def monitor(self, retries=0, timeout=1.0):
        prov = notify.WebhookProvider(self.rx.url, timeout=timeout, retries=retries, sleep=lambda s: None)
        mon = service.Monitor(cfg(), "local", None, providers=[prov], clock=lambda: self.now[0])
        self.addCleanup(mon.store.close)
        return mon

    def tick(self, mon, rows):
        self.now[0] += 60
        with self.fake_collect(rows=rows):
            return mon.tick()

    def outage_two_ticks(self, mon):
        """service_down needs two consecutive bad samples to fire."""
        self.tick(mon, DOWN)
        return self.tick(mon, DOWN)

    def test_a_notification_lost_to_a_down_receiver_is_delivered_when_it_comes_back_exactly_once(self):
        mon = self.monitor()                                                                # receiver is NOT started: connection refused
        res = self.outage_two_ticks(mon)
        self.assertTrue(any(d["ok"] is False for d in res["delivery"]), res["delivery"])
        self.assertEqual([e["kind"] for e in mon.store.events() if e["kind"] == "notify.failed"][:1], ["notify.failed"])
        self.assertEqual(self.rx.got, [])
        self.rx.start()                                                                     # the receiver recovers
        for _ in range(6):
            self.tick(mon, DOWN)
        firing = [t for t in self.rx.texts() if t.startswith("service_down:caddy") and t.endswith("firing")]
        self.assertEqual(len(firing), 1, self.rx.texts())                                   # delivered late, and only once
        n = len(self.rx.got)
        for _ in range(4):
            self.tick(mon, DOWN)
        self.assertEqual(len(self.rx.got), n, "nothing is re-sent after a successful delivery")

    def test_the_resolved_notice_follows_the_firing_notice_in_order_after_an_outage(self):
        mon = self.monitor()
        self.outage_two_ticks(mon)                                                          # firing: receiver down
        self.tick(mon, ROWS)                                                                # recovered machine: resolved, receiver still down
        self.rx.start()
        for _ in range(8):
            self.tick(mon, ROWS)
        order = [t for t in self.rx.texts() if t.startswith("service_down:caddy")]
        self.assertEqual(order, ["service_down:caddy:firing", "service_down:caddy:resolved"], self.rx.texts())

    def test_retries_back_off_instead_of_hammering_a_dead_receiver(self):
        mon = self.monitor()
        self.outage_two_ticks(mon)
        calls = []
        orig = notify.WebhookProvider.send
        with mock.patch.object(notify.WebhookProvider, "send", lambda self_, n: (calls.append(n["rule"]), orig(self_, n))[1]):
            for _ in range(5):                                                              # 5 more minutes of a dead receiver
                self.tick(mon, DOWN)
        self.assertLess(len(calls), 5, f"retried on every tick: {len(calls)}")              # back-off: fewer attempts than ticks

    def test_http_500_is_retried_later_and_delivered_once(self):
        self.rx.start()
        self.rx.mode = "fail500"
        mon = self.monitor()
        self.outage_two_ticks(mon)
        self.assertEqual(self.rx.got, [])
        self.rx.mode = "ok"
        for _ in range(6):
            self.tick(mon, DOWN)
        self.assertEqual([t for t in self.rx.texts() if t.endswith("firing")], ["service_down:caddy:firing"])

    def test_a_slow_receiver_does_not_stall_the_loop_longer_than_the_timeout(self):
        self.rx.start()
        self.rx.mode = "slow"
        mon = self.monitor(timeout=0.5)
        t0 = time.monotonic()
        self.outage_two_ticks(mon)
        self.assertLess(time.monotonic() - t0, 6, "a 3 s receiver with a 0.5 s timeout must not block the loop")
        self.assertTrue([e for e in mon.store.events() if e["kind"] == "notify.failed"])

    def test_pending_notifications_survive_a_restart_of_the_monitor(self):
        mon = self.monitor()
        self.outage_two_ticks(mon)
        mon.store.close()
        self.rx.start()
        mon2 = self.monitor()                                                               # a new process: same history directory
        for _ in range(6):
            self.tick(mon2, DOWN)
        self.assertIn("service_down:caddy:firing", self.rx.texts())

    def test_old_undeliverable_notifications_are_dropped_with_a_visible_event(self):
        mon = self.monitor()
        self.outage_two_ticks(mon)
        self.now[0] += 3 * 86400                                                            # three days later the receiver is still dead
        self.tick(mon, DOWN)
        self.assertTrue([e for e in mon.store.events() if e["kind"] == "notify.dropped"], mon.store.events())
        self.rx.start()
        for _ in range(4):
            self.tick(mon, DOWN)
        self.assertEqual([t for t in self.rx.texts() if t.endswith("firing")][:1], [], "a three-day-old firing notice is not delivered as if it were new")


if __name__ == "__main__":
    unittest.main()
