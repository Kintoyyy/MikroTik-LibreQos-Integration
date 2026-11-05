"""Fix buttons and auto-fix: diagnosis rules, the RRD clock repair, router probes, GUI-requested rescans."""
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import gui
import job_fixer
from device_database import DeviceDatabase, record_job, take_webhook_events

# The errors from a real Pollers table
RRD_CLOCK = ("rrdtool update host.rrd: ERROR: /opt/lq-sync/rrd/host.rrd: illegal attempt to update using "
             "time 1793888873 when last update time is 1793896591 (minimum one second step)")
NO_ROUTE = "connection failed: [Errno 113] No route to host"


def job(**kw):
    return {"name": "x", "kind": "stats", "service": "gui", "status": "error", "last_error": "", **kw}


class DiagnoseTest(unittest.TestCase):
    def test_ok_job_needs_nothing(self):
        self.assertIsNone(job_fixer.diagnose(job(status="ok")))

    def test_rrd_clock_skew_is_auto(self):
        d = job_fixer.diagnose(job(last_error=RRD_CLOCK))
        self.assertEqual((d["id"], d["auto"]), ("rrd_clock", True))
        self.assertIn("2.1 h", d["cause"])

    def test_other_rrd_error_rebuilds_that_file(self):
        d = job_fixer.diagnose(job(last_error="rrdtool update app.rrd: ERROR: expected 9 data source readings (got 7)"))
        self.assertEqual((d["id"], d["auto"]), ("rrd_rebuild", True))

    def test_router_connection_is_diagnosed_not_auto(self):
        d = job_fixer.diagnose(job(name="scan AC 1", kind="poller", service="updatecsv", last_error=NO_ROUTE))
        self.assertEqual((d["id"], d["auto"]), ("router", False))

    def test_router_login(self):
        d = job_fixer.diagnose(job(kind="poller", last_error="connection failed: invalid user name or password (6)"))
        self.assertEqual(d["label"], "Test router login")

    def test_late_job_restarts_its_service(self):
        d = job_fixer.diagnose(job(status="late", service="updatecsv"))
        self.assertEqual((d["id"], d["label"], d["auto"]), ("restart_service", "Restart updatecsv", True))

    def test_apply(self):
        hung = job_fixer.diagnose(job(kind="apply", service="updatecsv", last_error="timed out after 300s"))
        self.assertEqual((hung["id"], hung["auto"]), ("apply", True))
        other = job_fixer.diagnose(job(kind="apply", service="updatecsv", last_error="KeyError: 'Circuit ID'"))
        self.assertEqual((other["label"], other["auto"]), ("Re-apply now", False))

    def test_disk_full_wins(self):
        d = job_fixer.diagnose(job(kind="build", last_error="[Errno 28] No space left on device"))
        self.assertEqual(d["id"], "free_disk")

    def test_config_and_fallback(self):
        self.assertEqual(job_fixer.diagnose(job(kind="config", last_error="invalid JSON"))["id"], "config")
        self.assertEqual(job_fixer.diagnose(job(kind="burst", last_error="RTNETLINK answers"))["id"], "retry")


class ShiftDumpTest(unittest.TestCase):
    XML = "<rrd><version>0003</version><step>60</step><lastupdate>10000</lastupdate><ds/></rrd>"

    def test_moves_back_whole_steps_to_before_now(self):
        xml, shift = job_fixer.shift_rrd_dump(self.XML, now=2500)
        self.assertEqual(shift % 60, 0)
        new_last = 10000 - shift
        self.assertLess(new_last, 2500)
        self.assertGreater(new_last, 2500 - 120)
        self.assertIn(f"<lastupdate>{new_last}</lastupdate>", xml)

    def test_no_change_when_already_in_the_past(self):
        self.assertEqual(job_fixer.shift_rrd_dump(self.XML, now=20000), (self.XML, 0))

    def test_rejects_other_text(self):
        with self.assertRaises(ValueError):
            job_fixer.shift_rrd_dump("<html/>", now=1)


class RouterVerdictTest(unittest.TestCase):
    BASE = {"address": "10.0.0.1", "port": 8728, "resolved": True, "ip": "10.0.0.1", "src": "10.0.0.5"}

    def test_pings_but_rejects_api_gives_router_commands(self):
        v = job_fixer.router_verdict({**self.BASE, "ping": True, "tcp": "unreachable"})
        self.assertFalse(v["ok"])
        self.assertIn("firewall", v["cause"])
        self.assertTrue(any("src-address=10.0.0.5" in c for c in v["commands"]))

    def test_snmp_health_counts_as_reachable(self):
        v = job_fixer.router_verdict({**self.BASE, "ping": False, "tcp": "refused", "health_ok": True,
                                      "health_via": "snmp"})
        self.assertIn("SNMP", v["cause"])

    def test_silent_router_is_down(self):
        v = job_fixer.router_verdict({**self.BASE, "ping": False, "tcp": "unreachable"})
        self.assertIn("does not answer", v["cause"])
        self.assertEqual(v["commands"], [])

    def test_answers_now(self):
        v = job_fixer.router_verdict({**self.BASE, "ping": True, "tcp": "ok", "login": "ok", "identity": "AC1"})
        self.assertTrue(v["ok"])

    def test_bad_login_and_dns(self):
        self.assertIn("refuses the login", job_fixer.router_verdict(
            {**self.BASE, "tcp": "ok", "login": "auth", "login_error": "invalid user"})["cause"])
        self.assertIn("does not resolve", job_fixer.router_verdict(
            {"address": "bras.lan", "resolved": False, "resolve_error": "x"})["cause"])


@unittest.skipUnless(shutil.which("rrdtool"), "rrdtool is not installed")
class RrdClockRepairTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.patches = [mock.patch.object(gui, "RRD_DIR", self.tmp),
                        mock.patch.object(gui, "RRD_BROKEN_DIR", self.tmp / "broken"),
                        mock.patch.object(gui, "RRDTOOL", shutil.which("rrdtool"))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp)

    def test_future_file_accepts_samples_again(self):
        now = int(time.time())
        future = now + 7200
        gui._rrd_create(gui._rrd_path("host"), "host", start=future - 600)
        for t in range(future - 540, future + 1, 60):         # samples "from the future"
            subprocess.run([gui.RRDTOOL, "update", str(gui._rrd_path("host")), f"{t}:1:2:3:4"], check=True)
        with self.assertRaisesRegex(RuntimeError, "illegal attempt to update"):
            gui._rrd_update("host", "host", [1, 2, 3, 4])

        result = gui._run_fix(job(name="host & app stats", last_error=RRD_CLOCK))
        self.assertTrue(result["ok"], result)
        self.assertLess(gui._rrd_last(gui._rrd_path("host")), now + 1)
        gui._rrd_update("host", "host", [1, 2, 3, 4])            # accepted now


class RouterFixTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = Path(self.tmp) / "devices.db"
        db = DeviceDatabase(str(self.db_path), os.devnull)
        db.open()
        db.conn.close()
        cfg = {"bras": [{"name": "AC", "address": "10.0.0.1"}, {"name": "AC", "address": "10.0.0.2"}]}
        self.patches = [mock.patch.object(gui, "DB_PATH", self.db_path),
                        mock.patch.object(gui, "_load_config", return_value=cfg),
                        mock.patch.object(gui, "_unit_state", return_value="active")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp)

    def scan_job(self):
        return job(name="scan AC 2", kind="poller", service="updatecsv", last_error=NO_ROUTE)

    def test_deduplicated_name_finds_second_router(self):
        (kind, router), = gui._job_routers(self.scan_job())
        self.assertEqual((kind, router["address"]), ("bras", "10.0.0.2"))

    def test_router_answering_again_queues_a_rescan(self):
        probe = {"address": "10.0.0.2", "port": 8728, "resolved": True, "ip": "10.0.0.2", "ping": True,
                 "tcp": "ok", "tcp_ms": 3.0, "login": "ok", "identity": "AC-2"}
        with mock.patch.object(gui, "_probe_router", return_value=probe):
            result = gui._run_fix(self.scan_job())
        self.assertTrue(result["ok"], result)
        con = sqlite3.connect(str(self.db_path))
        events = take_webhook_events(con)
        self.assertEqual([(e["router"], e["event"]) for e in events], [("AC 2", "rescan")])

    def test_unreachable_router_reports_commands(self):
        probe = {"address": "10.0.0.2", "port": 8728, "resolved": True, "ip": "10.0.0.2", "src": "10.0.0.5",
                 "ping": True, "tcp": "unreachable", "tcp_error": "[Errno 113] No route to host"}
        with mock.patch.object(gui, "_probe_router", return_value=probe):
            result = gui._run_fix(self.scan_job())
        self.assertFalse(result["ok"])
        self.assertTrue(result["commands"])
        self.assertEqual(take_webhook_events(sqlite3.connect(str(self.db_path))), [])


class RescanCycleTest(unittest.TestCase):
    def test_gui_rescan_is_recorded_as_the_scan_job(self):
        import updatecsv

        class Scanner:
            last_error = ''

            def scan_router(self, router, t):
                return True, False

        db = DeviceDatabase(":memory:", os.devnull)
        db.open()
        record_job(db.conn, "scan A", "poller", "updatecsv", 600, time.time() - 60, 1, 0, NO_ROUTE)
        cfg = updatecsv.SyncConfig([{"name": "A"}], "flat", 4, False, None, False)
        events = [{"router": "A", "event": "rescan", "user": ""}, {"router": "*", "event": "apply", "user": ""}]
        with mock.patch.object(updatecsv, "build_and_apply", return_value=False) as build:
            updatecsv.run_webhook_cycle(db, Scanner(), None, cfg, events)
        status, = db.conn.execute("SELECT status FROM jobs WHERE name = 'scan A'").fetchone()
        self.assertEqual(status, "ok")
        self.assertIsNone(db.conn.execute("SELECT 1 FROM jobs WHERE name = 'webhook A'").fetchone())
        self.assertTrue(build.call_args.args[5])                 # the apply request forces a LibreQoS update


if __name__ == "__main__":
    unittest.main()
