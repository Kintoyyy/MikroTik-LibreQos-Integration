"""Warning & error history: folding logged messages, opening / resolving problems, filters, the handler, the routes."""
import json
import logging
import os
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

import event_log


def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    event_log.ensure_tables(conn)
    return conn


class LogTest(unittest.TestCase):
    def test_repeats_fold_numbers_aside(self):
        c = db()
        a = event_log.record_log(c, "updatecsv", "WARNING", "updatecsv", "Router BRAS-1 timed out after 5 s", now=100)
        b = event_log.record_log(c, "updatecsv", "WARNING", "updatecsv", "Router BRAS-1 timed out after 7 s", now=160)
        self.assertEqual(a, b)
        e = event_log.get(c, a)
        self.assertEqual((e["count"], e["first_at"], e["last_at"]), (2, 100, 160))
        self.assertEqual(e["message"], "Router BRAS-1 timed out after 7 s")                  # the latest text
        self.assertEqual([o["message"] for o in e["occurrences"]],
                         ["Router BRAS-1 timed out after 7 s", "Router BRAS-1 timed out after 5 s"])
        other = event_log.record_log(c, "updatecsv", "ERROR", "updatecsv", "LibreQoS update failed", now=170)
        self.assertNotEqual(other, a)
        later = event_log.record_log(c, "updatecsv", "WARNING", "updatecsv", "Router BRAS-1 timed out after 9 s",
                                     now=160 + event_log.FOLD_SECONDS + 1)
        self.assertNotEqual(later, a)                                                         # a new day, a new entry

    def test_occurrences_are_capped(self):
        c = db()
        for i in range(event_log.MAX_OCCURRENCES + 10):
            eid = event_log.record_log(c, "gui", "WARNING", "gui", f"slow poll {i}", now=1000 + i)
        e = event_log.get(c, eid)
        self.assertEqual(e["count"], event_log.MAX_OCCURRENCES + 10)
        self.assertEqual(len(e["occurrences"]), event_log.MAX_OCCURRENCES)


class ConditionTest(unittest.TestCase):
    def cond(self, msg="BRAS-1 (192.0.2.1): timed out"):
        return [{"key": "bras:BRAS-1:down", "level": "error", "title": "BRAS-1 does not answer", "message": msg,
                 "fix": "Check it", "context": {"router": "BRAS-1"}}]

    def test_open_change_resolve_reopen(self):
        c = db()
        event_log.sync_conditions(c, "gui", "router", self.cond(), now=100)
        event_log.sync_conditions(c, "gui", "router", self.cond(), now=160)                 # unchanged: no new line
        event_log.sync_conditions(c, "gui", "router", self.cond("BRAS-1: connection refused"), now=220)
        rows, _ = event_log.query(c, state="active")
        self.assertEqual(len(rows), 1)
        e = event_log.get(c, rows[0]["id"])
        self.assertEqual((e["state"], e["first_at"], e["last_at"], e["fix"]), ("active", 100, 220, "Check it"))
        self.assertEqual(e["context"], {"router": "BRAS-1"})
        self.assertEqual(len(e["occurrences"]), 2)

        event_log.sync_conditions(c, "gui", "job", [], now=230)                             # other category: untouched
        self.assertEqual(event_log.get(c, e["id"])["state"], "active")
        event_log.sync_conditions(c, "gui", "router", [], now=280)
        e = event_log.get(c, e["id"])
        self.assertEqual((e["state"], e["resolved_at"]), ("resolved", 280))
        self.assertEqual(e["occurrences"][0]["message"], "resolved")

        event_log.sync_conditions(c, "gui", "router", self.cond(), now=300)                 # back soon: same entry
        e = event_log.get(c, e["id"])
        self.assertEqual((e["state"], e["count"]), ("active", 2))
        event_log.sync_conditions(c, "gui", "router", [], now=400)
        event_log.sync_conditions(c, "gui", "router", self.cond(), now=400 + event_log.REOPEN_SECONDS + 1)
        rows, total = event_log.query(c)
        self.assertEqual(total, 2)                                                          # much later: a new one
        self.assertEqual(rows[0]["state"], "active")                                        # open problems first

    def test_summary_prune_and_filters(self):
        c = db()
        now = time.time()
        event_log.sync_conditions(c, "gui", "check", [{"key": "k", "level": "warn", "title": "lqosd is failed"}],
                                  now=now - 40 * 86400)
        event_log.record_log(c, "wan_service", "ERROR", "wan_service", "old error", now=now - 40 * 86400)
        event_log.record_log(c, "gui", "WARNING", "gui", "router RRD x: busy", now=now - 60)
        s = event_log.summary(c, now)
        self.assertEqual(s["active"], {"error": 0, "warning": 1})
        self.assertEqual(s["day"]["warning"], 1)
        self.assertEqual(event_log.query(c, q="RRD")[1], 1)
        self.assertEqual(event_log.query(c, kind="log", level="error")[1], 1)
        self.assertEqual(event_log.query(c, since=now - 3600)[1], 1)
        event_log.prune(c, keep_days=30, now=now)
        rows, total = event_log.query(c)
        self.assertEqual(sorted(r["title"] for r in rows), ["lqosd is failed", "router RRD x: busy"])   # open one kept


class HandlerTest(unittest.TestCase):
    def test_warnings_and_tracebacks_are_kept_with_where(self):
        path = os.path.join(tempfile.mkdtemp(prefix="lqsync-ev-"), "devices.db")
        log = logging.getLogger("test.events")
        log.propagate = False
        handler = event_log.DBLogHandler(path, "updatecsv")
        log.addHandler(handler)
        self.addCleanup(log.removeHandler, handler)
        log.info("not kept")
        log.warning("Router %s timed out", "BRAS-1")
        try:
            1 / 0
        except ZeroDivisionError:
            log.exception("LibreQoS update failed")
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        rows, total = event_log.query(c)
        self.assertEqual(total, 2)
        err = next(r for r in rows if r["level"] == "error")
        self.assertTrue(err["detail"].startswith("Traceback"))
        self.assertIn("ZeroDivisionError", err["detail"])
        self.assertEqual(err["context"]["function"], "test_warnings_and_tracebacks_are_kept_with_where")
        self.assertTrue(err["context"]["file"].startswith("test_event_log.py:"))
        self.assertEqual((err["source"], err["category"]), ("updatecsv", "test.events"))

    def test_a_broken_database_never_breaks_logging(self):
        log = logging.getLogger("test.events.broken")
        log.propagate = False
        log.addHandler(event_log.DBLogHandler("/nonexistent/dir/devices.db", "gui"))
        log.error("still fine")                                                             # no exception


class GuiTest(unittest.TestCase):
    def setUp(self):
        import gui
        self.gui = gui
        self.client = gui.app.test_client()
        with self.client.session_transaction() as s:
            s["authed"] = True
        con = gui._events_con()
        con.execute("DELETE FROM events")
        con.commit()
        con.close()

    def test_router_and_job_conditions(self):
        latest = {"router-bras-A": {"name": "A", "kind": "bras", "address": "192.0.2.1", "ok": False, "error": "timed out"},
                  "router-bras-B": {"name": "B", "kind": "bras", "address": "192.0.2.2", "ok": True, "via": "snmp",
                                    "ifaces": {"ether1": {"running": True, "rx_errors_min": 12.0, "tx_errors_min": 0},
                                               "ether2": {"running": False}}}}
        conds = {c["key"]: c for c in self.gui._router_conditions(latest)}
        self.assertEqual(set(conds), {"router-bras-A:down", "router-bras-B:if:ether1:errors", "router-bras-B:if:ether2:down"})
        self.assertIn("timed out", conds["router-bras-A:down"]["message"])
        self.assertEqual(conds["router-bras-B:if:ether1:errors"]["context"]["interface"], "ether1")

        con = sqlite3.connect(str(self.gui.DB_PATH))
        from device_database import ensure_jobs_table, record_job
        ensure_jobs_table(con)
        now = time.time()
        record_job(con, "scan X", "poller", "updatecsv", 600, now - 30, 1.0, 0.1, "connection refused")
        record_job(con, "build topology", "build", "updatecsv", 600, now - 7200, 1.0, 0.1)
        jobs = {c["key"]: c for c in self.gui._job_conditions(con, now)}
        self.assertEqual(jobs["job:scan X:failing"]["message"], "connection refused")
        self.assertIn("job:build topology:late", jobs)
        con.execute("DELETE FROM jobs")
        con.commit()
        con.close()

    def test_routes(self):
        con = self.gui._events_con()
        event_log.sync_conditions(con, "gui", "router", [{"key": "a", "level": "error", "title": "A does not answer",
                                                         "message": "A: timed out", "context": {"router": "A"}}])
        event_log.record_log(con, "updatecsv", "WARNING", "updatecsv", "auto_cpu: CPU3 over capacity",
                             detail="", context={"file": "updatecsv.py:12"})
        con.close()
        d = self.client.get("/api/events?range=all").get_json()
        self.assertEqual(d["total"], 2)
        self.assertEqual(d["summary"]["active"]["error"], 1)
        self.assertEqual(d["events"][0]["title"], "A does not answer")                      # open problems first
        self.assertEqual(d["sources"], ["gui", "updatecsv"])
        one = self.client.get(f"/api/events/{d['events'][1]['id']}").get_json()["event"]
        self.assertEqual(one["context"], {"file": "updatecsv.py:12"})
        self.assertEqual(len(one["occurrences"]), 1)
        self.assertEqual(self.client.get("/api/events?range=all&state=active").get_json()["total"], 1)
        self.assertEqual(self.client.get("/api/events?range=all&level=warning&q=auto_cpu").get_json()["total"], 1)
        csv_text = self.client.get("/api/events.csv?range=all").get_data(as_text=True)
        self.assertIn("A does not answer", csv_text)
        self.assertIn('"{""router"": ""A""}"', csv_text)
        r = self.client.post("/api/events/clear", json={"keep_open": True}, headers={"X-Requested-With": "fetch"})
        self.assertEqual(r.get_json()["deleted"], 1)                                         # the open problem stays
        self.assertEqual(self.client.get("/api/events?range=all").get_json()["total"], 1)
        self.assertEqual(self.client.get("/api/events/999999").status_code, 404)
        self.assertIn("events", self.client.get("/api/monitor").get_json())


if __name__ == "__main__":
    unittest.main()
