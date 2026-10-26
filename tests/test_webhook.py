"""PPP webhook: router scripts, install/remove over the API, events, the queue and the fast path."""
import contextlib
import os
import time
import unittest
from unittest import mock

import webhook
from device_database import (
    DeviceDatabase, record_webhook_event, pending_webhook_events, take_webhook_events,
    prune_webhook_events,
)
from preview_server import FakeRouterOS
from router_scanner import RouterScanner

BASE = "http://10.0.0.5:5000"
TOKEN = "0123456789abcdef0123456789abcdef"
USER_SCRIPT = ':log info ("PPP up " . $user)\r\n/queue simple remove [find name=$user]'


def profiles():
    return [
        {"name": "default", "on-up": "", "on-down": ""},
        {"name": "PPPoE-100M", "on-up": USER_SCRIPT, "on-down": ':log info "bye"'},
    ]


class ScriptTest(unittest.TestCase):
    def test_ros_escape(self):
        self.assertEqual(webhook.ros_escape('a"b$c\\d?\r\n'), 'a\\"b\\$c\\\\d\\?\\r\\n')

    def test_hook_line(self):
        line = webhook.hook_line("up")
        self.assertIn(webhook.MARKER, line)
        self.assertTrue(line.startswith(':do {:local lq [:parse [/system script get lqsync-webhook source]];'))
        self.assertEqual(line.index(webhook.MARKER), webhook.MARKER_OFFSET)
        self.assertIn('event="up" user=$user ip=$"remote-address" cid=$"caller-id"', line)
        self.assertTrue(line.endswith("} on-error={}"))

    def test_with_and_without_hook(self):
        hooked = webhook.with_hook(USER_SCRIPT, "up")
        self.assertTrue(hooked.startswith(webhook.hook_line("up") + "\r\n"))
        self.assertTrue(hooked.endswith(USER_SCRIPT))
        self.assertEqual(webhook.with_hook(hooked, "up"), hooked)            # idempotent
        self.assertEqual(webhook.without_hook(hooked), USER_SCRIPT)          # restored exactly
        self.assertEqual(webhook.with_hook("", "down"), webhook.hook_line("down"))
        self.assertEqual(webhook.without_hook(webhook.hook_line("down")), "")
        self.assertEqual(webhook.without_hook(USER_SCRIPT), USER_SCRIPT)     # untouched without a hook

    def test_function_source(self):
        src = webhook.function_source(BASE + "/", TOKEN)
        self.assertIn(f':local url "{BASE}/api/webhook/mikrotik"', src)
        self.assertIn(f':local token "{TOKEN}"', src)
        self.assertIn("X-LQSync-Token: ", src)
        self.assertIn('/tool fetch url=$url http-method=post', src)
        self.assertIn(':if ([:len $ev] = 0) do={ :set ev "test" }', src)

    def test_terminal_scripts_are_balanced(self):
        for text in (webhook.terminal_install(BASE, TOKEN, "BRAS-1"), webhook.terminal_uninstall("BRAS-1")):
            code = [line for line in text.splitlines() if not line.startswith("#")]
            body = "\n".join(code)
            self.assertEqual(body.count("{"), body.count("}"))
            self.assertEqual(body.count("["), body.count("]"))
            self.assertTrue(code[0] == "{" and code[-1] == "}")
        install = webhook.terminal_install(BASE, TOKEN, "BRAS-1")
        self.assertIn('source="# LQ-Sync PPP webhook', install)
        self.assertIn("\\$lq event=\\\"up\\\"", install)                     # $ and " escaped in the string
        self.assertNotIn("\n:local token", install)                          # source stays one string


class ApiTest(unittest.TestCase):
    def test_install_status_uninstall(self):
        ros = FakeRouterOS({"/ppp/profile": profiles()})
        api = ros.get_api()
        self.assertEqual(webhook.status(api, BASE, TOKEN)["script"], "missing")

        result = webhook.install(api, BASE, TOKEN)
        self.assertEqual(sorted(result["changed"]), ["PPPoE-100M", "default"])
        st = webhook.status(api, BASE, TOKEN)
        self.assertEqual((st["script"], st["hooked_up"], st["hooked_down"], st["unhooked"]), ("current", 2, 2, []))
        p = api.get_resource("/ppp/profile").get(name="PPPoE-100M")[0]
        self.assertTrue(p["on-up"].endswith(USER_SCRIPT))

        self.assertEqual(webhook.install(api, BASE, TOKEN)["changed"], [])     # nothing to do twice
        self.assertEqual(len(api.get_resource("/system/script").get()), 1)
        self.assertEqual(webhook.status(api, BASE, "another-token")["script"], "outdated")

        webhook.uninstall(api)
        p = api.get_resource("/ppp/profile").get(name="PPPoE-100M")[0]
        self.assertEqual((p["on-up"], p["on-down"]), (USER_SCRIPT, ':log info "bye"'))
        self.assertEqual(webhook.status(api, BASE, TOKEN)["script"], "missing")

    def test_send_test_runs_script(self):
        ran = []
        ros = FakeRouterOS({"/ppp/profile": profiles()}, on_run=ran.append)
        webhook.install(ros, BASE, TOKEN)
        webhook.send_test(ros)
        self.assertEqual([r["name"] for r in ran], [webhook.SCRIPT_NAME])


class EventTest(unittest.TestCase):
    def test_parse_event(self):
        ev = webhook.parse_event(b'{"event":"UP","service":"ppp","user":"alice","ip":"100.64.0.9","caller_id":"AA:BB"}')
        self.assertEqual(ev, {"event": "up", "service": "ppp", "user": "alice", "ip": "100.64.0.9", "caller_id": "AA:BB"})
        self.assertEqual(webhook.parse_event(b'{"event":"down","user":"a"b"}')["event"], "unknown")
        self.assertEqual(webhook.parse_event(b'{"event":"up","ip":"nope"}')["ip"], "")
        self.assertEqual(webhook.parse_event(b'{"event":"reboot"}')["event"], "unknown")

    def test_router_for_token(self):
        routers = [{"name": "A", "webhook_token": "t1"}, {"name": "B", "webhook_token": "t2"}, {"name": "C"}]
        self.assertEqual(webhook.router_for_token(routers, "t2")["name"], "B")
        self.assertIsNone(webhook.router_for_token(routers, "t3"))
        self.assertIsNone(webhook.router_for_token(routers, ""))

    def test_queue(self):
        db = DeviceDatabase(":memory:", os.devnull)
        db.open()
        record_webhook_event(db.conn, "A", "test", now=100)
        self.assertEqual(pending_webhook_events(db.conn), 0)              # tests are never processed
        record_webhook_event(db.conn, "A", "up", user="alice", ip="100.64.0.9", now=101)
        record_webhook_event(db.conn, "B", "down", user="bob", now=102)
        self.assertEqual(pending_webhook_events(db.conn), 2)
        events = take_webhook_events(db.conn, now=103)
        self.assertEqual([(e["router"], e["event"], e["user"]) for e in events], [("A", "up", "alice"), ("B", "down", "bob")])
        self.assertEqual(take_webhook_events(db.conn), [])
        prune_webhook_events(db.conn, keep_seconds=1.5, now=103)
        self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM webhook_events").fetchone()[0], 1)


ROUTER = {"name": "BRAS-1", "address": "192.0.2.1",
          "pppoe": {"enabled": True, "default_download_limit": 10, "default_upload_limit": 5}}


class FastPathTest(unittest.TestCase):
    def setUp(self):
        self.db = DeviceDatabase(":memory:", os.devnull)
        self.db.open()
        self.scanner = RouterScanner(self.db)
        self.ros = FakeRouterOS({
            "/ppp/active": [{"name": "alice", "address": "100.64.0.9", "caller-id": "aa:bb:cc:dd:ee:ff"}],
            "/ip/firewall/address-list": [{"list": "50M/20M", "address": "100.64.0.9"},
                                          {"list": "5M/5M", "address": "100.64.0.10"}],
        })
        patcher = mock.patch.object(RouterScanner, "open_api", self._open_api)
        patcher.start()
        self.addCleanup(patcher.stop)

    @contextlib.contextmanager
    def _open_api(self, router, retries=None):
        yield self.ros

    def device(self, code):
        return self.db.conn.execute(
            "SELECT ipv4, download_max_mbps, upload_max_mbps, router FROM devices WHERE code = ?", (code,)
        ).fetchone()

    def test_up_adds_with_address_list_rate(self):
        ok, changed = self.scanner.apply_ppp_events(ROUTER, {"alice"}, time.time())
        self.assertTrue(ok and changed)
        ipv4, dl, ul, router = self.device("PPP-alice")
        self.assertEqual((ipv4, router), ("100.64.0.9", "BRAS-1"))
        self.assertEqual((dl, ul), (57, 23))   # 50M/20M × 1.15

    def test_down_removes_only_ended_sessions(self):
        self.scanner.apply_ppp_events(ROUTER, {"alice"}, time.time())
        self.db.upsert_device("PPP-bob", "", "100.64.0.10", "", "pppoe", "BRAS-1", 10, 5, 1, 1, time.time())
        self.db.upsert_device("PPP-carol", "", "100.64.0.11", "", "pppoe", "BRAS-2", 10, 5, 1, 1, time.time())
        ok, changed = self.scanner.apply_ppp_events(ROUTER, {"alice", "bob", "carol"}, time.time())
        self.assertTrue(ok and changed)
        self.assertIsNotNone(self.device("PPP-alice"))     # still active: kept
        self.assertIsNone(self.device("PPP-bob"))          # session gone: removed
        self.assertIsNotNone(self.device("PPP-carol"))     # other router's device: untouched

    def test_pppoe_disabled_is_ignored(self):
        ok, changed = self.scanner.apply_ppp_events({**ROUTER, "pppoe": {"enabled": False}}, {"alice"}, time.time())
        self.assertEqual((ok, changed), (True, False))
        self.assertIsNone(self.device("PPP-alice"))


class SyncLoopTest(unittest.TestCase):
    def test_run_webhook_cycle_targeted_and_full(self):
        import updatecsv

        class Scanner:
            last_error = ''
            calls = []

            def scan_router(self, router, t):
                self.calls.append(("full", router["name"]))
                return True, False

            def apply_ppp_events(self, router, users, t):
                self.calls.append(("users", router["name"], sorted(users)))
                return True, False

        cfg = updatecsv.SyncConfig([{"name": "A"}, {"name": "B"}], "flat", 4, False, None, False)
        events = [
            {"router": "A", "event": "up", "user": "alice"},
            {"router": "A", "event": "down", "user": "bob"},
            {"router": "B", "event": "unknown", "user": ""},      # unparseable call → full rescan
            {"router": "Z", "event": "up", "user": "zed"},        # unknown router → ignored
        ]
        db = DeviceDatabase(":memory:", os.devnull)
        db.open()
        scanner = Scanner()
        with mock.patch.object(updatecsv, "build_and_apply", return_value=False) as build:
            updatecsv.run_webhook_cycle(db, scanner, None, cfg, events)
        self.assertEqual(scanner.calls, [("users", "A", ["alice", "bob"]), ("full", "B")])
        self.assertEqual(build.call_args.args[4], ["B"])         # only the fully scanned router may lose devices

    def test_wait_for_work(self):
        import updatecsv
        db = DeviceDatabase(":memory:", os.devnull)
        db.open()
        fast = {"debounce": 0.01, "min_interval": 0, "full_scan_over": 50, "keep_seconds": 86400}
        with mock.patch.object(updatecsv, "webhook_settings", return_value=fast):
            self.assertIsNone(updatecsv.wait_for_work(db.conn, time.time() - 1, 0))     # full scan due
            record_webhook_event(db.conn, "A", "up", user="alice")
            events = updatecsv.wait_for_work(db.conn, time.time() + 60, 0)
            self.assertEqual([e["user"] for e in events], ["alice"])
            # A webhook cycle that would have to wait past the full scan just waits for the full scan
            record_webhook_event(db.conn, "A", "up", user="bob")
            slow = {**fast, "min_interval": 120}
            with mock.patch.object(updatecsv, "webhook_settings", return_value=slow):
                self.assertIsNone(updatecsv.wait_for_work(db.conn, time.time() + 0.05, time.time()))


if __name__ == "__main__":
    unittest.main()
