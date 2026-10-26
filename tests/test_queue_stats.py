"""Live LibreQoS queue statistics: tc parsing, naming from LibreQoS's files, rates, and the Overview routes."""
import csv
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

import queue_stats
from preview_server import FakeShaper

CLASSES = """\
class mq 7fff:1 root leaf 1:
class htb 1:1 root rate 10Gbit ceil 10Gbit burst 1600b cburst 1600b
 Sent 5000 bytes 10 pkt (dropped 0, overlimits 0 requeues 0)
 backlog 0b 0p requeues 0
 lended: 0 borrowed: 0 giants: 0
class htb 1:2 parent 1:1 prio 3 rate 10Mbit ceil 10Gbit burst 1600b cburst 1600b
 Sent 0 bytes 0 pkt (dropped 0, overlimits 0 requeues 0)
class htb 1:3 parent 1:1 prio 3 rate 3Gbit ceil 3Gbit burst 1600b cburst 1600b
 Sent 5000 bytes 10 pkt (dropped 2, overlimits 7 requeues 0)
class htb 1:4 parent 1:3 leaf 8004: prio 0 rate 80Mbit ceil 115Mbit burst 1600b cburst 1600b
 Sent 4000 bytes 8 pkt (dropped 1, overlimits 0 requeues 0)
class fq_codel 8004:1 parent 8004:
 (dropped 0, overlimits 0 requeues 0)
"""
QDISCS = """\
qdisc mq 7fff: root
 Sent 5000 bytes 10 pkt (dropped 0, overlimits 0 requeues 0)
qdisc htb 1: parent 7fff:1 r2q 10 default 0x2 direct_packets_stat 0 direct_qlen 1000
 Sent 5000 bytes 10 pkt (dropped 0, overlimits 0 requeues 0)
qdisc cake 8004: parent 1:4 bandwidth unlimited diffserv4 triple-isolate nonat nowash
 Sent 4000 bytes 8 pkt (dropped 3, overlimits 0 requeues 0)
 backlog 1.5Kb 2p requeues 0
"""


def tc_tree(queues, sent, leaves=(), drops=None):
    """tc text for `queues` HTB majors: root X:1 (sent[X]) and leaf circuits [(major, minor, bytes)]."""
    out = []
    for q in range(1, queues + 1):
        out.append(f"class htb {q:x}:1 root rate 10Gbit\n Sent {sent.get(q, 0)} bytes 1 pkt (dropped 0, overlimits 0 requeues 0)\n")
    for major, minor, b in leaves:
        out.append(f"class htb {major:x}:{minor:x} parent {major:x}:1 leaf {0x8000 + minor:x}: prio 0 rate 1Mbit\n"
                   f" Sent {b} bytes 1 pkt (dropped 0, overlimits 0 requeues 0)\n")
    return "".join(out)


class ParseTest(unittest.TestCase):
    def test_handles(self):
        self.assertEqual(queue_stats.parse_handle("0x3:0x1f"), (3, 31))
        self.assertEqual(queue_stats.parse_handle("a:10"), (10, 16))
        self.assertEqual(queue_stats.parse_handle("8004:"), (0x8004, None))
        self.assertIsNone(queue_stats.parse_handle("root"))
        self.assertIsNone(queue_stats.parse_handle(None))

    def test_classes(self):
        c = queue_stats.parse_classes(CLASSES)
        self.assertEqual(set(c), {(1, 1), (1, 2), (1, 3), (1, 4)})      # mq and fq_codel classes are skipped
        self.assertEqual(c[(1, 1)]["parent"], None)
        self.assertEqual(c[(1, 4)], {"parent": (1, 3), "leaf": "8004:", "bytes": 4000, "packets": 8, "drops": 1})
        self.assertEqual(c[(1, 3)]["leaf"], "")

    def test_qdiscs(self):
        q = queue_stats.parse_qdiscs(QDISCS)            # keyed by the class they hang off; the root mq is not
        self.assertEqual(q, {(1, 4): {"kind": "cake", "bytes": 4000, "drops": 3},
                             (0x7fff, 1): {"kind": "htb", "bytes": 5000, "drops": 0}})


class SamplerTest(unittest.TestCase):
    """The sampler against preview_server's simulated LibreQoS, which lays the queues out as LibreQoS.py does."""
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="lqsync-queues-")
        self.addCleanup(shutil.rmtree, self.dir)
        network = {"CPU0": {"downloadBandwidthMbps": 3000, "children": {"Site A": {"downloadBandwidthMbps": 1000}}},
                   "CPU1": {"downloadBandwidthMbps": 3000}}
        with open(os.path.join(self.dir, "network.json"), "w") as f:
            json.dump(network, f)
        with open(os.path.join(self.dir, "ShapedDevices.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["Circuit ID", "Circuit Name", "Device ID", "Device Name", "Parent Node", "MAC", "IPv4", "IPv6",
                        "Download Min Mbps", "Upload Min Mbps", "Download Max Mbps", "Upload Max Mbps", "Comment"])
            for i, (parent, plan) in enumerate([("Site A", 100), ("Site A", 50), ("CPU1", 200), ("Elsewhere", 25)]):
                w.writerow([f"c{i}", f"sub{i}", f"d{i}", f"sub{i}", parent, "", f"100.64.0.{i + 1}", "", 1, 1, plan, plan // 2])
        # three queues: CPU0, CPU1 and the generated parent of "Elsewhere" each get their own
        self.shaper = FakeShaper(self.dir, queues=3)
        self.clock = [1_800_000_000.0]
        patcher = mock.patch("time.time", lambda: self.clock[0])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sampler = queue_stats.QueueSampler(lambda: self.dir, lambda: ("ens16", "ens17", False),
                                                runner=self.shaper.tc)

    def two_samples(self, gap=2.0):
        first = self.sampler.sample()
        self.clock[0] += gap
        return first, self.sampler.sample()

    def test_rates_names_and_queues(self):
        first, snap = self.two_samples()
        self.assertEqual(first["error"], "measuring…")
        self.assertTrue(snap["available"], snap.get("error"))
        self.assertEqual(snap["mapping"], "queuingStructure.json")
        self.assertEqual([q["name"] for q in snap["queues"]], ["CPU0", "CPU1", "Generated_PN_1"])
        rates = self.shaper.rates
        down = {c["name"]: c["down_bps"] for c in snap["top"]}
        for c in self.shaper.circuits:
            if rates[c["id"]][0] > 0:
                self.assertAlmostEqual(down[c["name"]], rates[c["id"]][0], delta=10)
        total = sum(r[0] for r in rates.values())
        self.assertAlmostEqual(snap["totals"]["down_bps"], total, delta=50)
        self.assertAlmostEqual(snap["totals"]["up_bps"], sum(r[1] for r in rates.values()), delta=50)
        self.assertEqual(snap["totals"]["circuits"], 4)
        self.assertEqual(sum(q["circuits"] for q in snap["queues"]), 4)
        by_name = {c["name"]: c for c in snap["top"]}
        if "sub0" in by_name:
            self.assertEqual(by_name["sub0"]["parent"], "Site A")
            self.assertAlmostEqual(by_name["sub0"]["util"], rates["c0"][0] / 100e6, places=4)

    def test_nodes_follow_the_network(self):
        _, snap = self.two_samples()
        nodes = {n["name"]: n for n in snap["nodes"]}
        self.assertEqual(set(nodes), {"CPU0", "CPU1", "Generated_PN_1"})
        self.assertEqual([n["down_bps"] for n in snap["nodes"]], sorted((n["down_bps"] for n in snap["nodes"]), reverse=True))
        site = nodes["CPU0"]["children"][0]
        self.assertEqual((site["name"], site["circuits"], site["cap_down"], site["cpu"]), ("Site A", 2, 1000.0, "CPU0"))
        rates = self.shaper.rates
        self.assertAlmostEqual(site["down_bps"], rates["c0"][0] + rates["c1"][0], delta=20)
        self.assertEqual(nodes["CPU0"]["down_bps"], site["down_bps"])      # a node's traffic is its circuits'
        self.assertEqual(nodes["CPU1"]["children"], [])

    def test_share_of_plan_bands(self):
        _, snap = self.two_samples()
        active = sum(1 for c in self.shaper.circuits if sum(self.shaper.rates[c["id"]]) > queue_stats.ACTIVE_BPS)
        self.assertEqual(sum(b["circuits"] for b in snap["util_bands"]), active)
        self.assertEqual([(b["low"], b["high"]) for b in snap["util_bands"]], queue_stats.UTIL_BANDS)

    def test_history_and_gap(self):
        self.two_samples()
        self.clock[0] += 2
        snap = self.sampler.sample()
        self.assertEqual(len(snap["history"]["t"]), 2)
        self.clock[0] += queue_stats.MAX_GAP + 1          # a stale previous sample is not used for a rate
        self.assertEqual(self.sampler.sample()["error"], "measuring…")

    def test_tc_failure_and_no_queues(self):
        self.sampler.interfaces = lambda: ("ens99", "ens17", False)
        snap = self.sampler.sample()
        self.assertFalse(snap["available"])
        self.assertIn("Cannot find device", snap["error"])
        self.sampler.interfaces = lambda: ("", "", False)
        self.assertIn("no shaping interfaces", self.sampler.sample()["error"])
        empty = queue_stats.QueueSampler(lambda: self.dir, lambda: ("ens16", "ens17", False), runner=lambda a: "")
        self.assertIn("no LibreQoS queues on ens16", empty.sample()["error"])


class WithoutLibreQoSFilesTest(unittest.TestCase):
    def test_leaf_classes_named_by_class_id_and_stick_upload(self):
        empty = tempfile.mkdtemp(prefix="lqsync-nofiles-")
        self.addCleanup(shutil.rmtree, empty)
        texts = iter([tc_tree(4, {1: 0, 2: 0, 3: 0, 4: 0}, [(1, 3, 0), (2, 3, 0)]),
                      tc_tree(4, {1: 1000, 2: 500, 3: 250, 4: 125}, [(1, 3, 1000), (2, 3, 500)])])
        clock = [100.0]
        with mock.patch("time.time", lambda: clock[0]):
            s = queue_stats.QueueSampler(lambda: empty, lambda: ("eth0", "eth0", True),
                                         runner=lambda args: next(texts) if args[1] == "class" else "")
            s.sample()
            clock[0] += 1
            snap = s.sample()
        self.assertTrue(snap["available"], snap.get("error"))
        self.assertEqual(snap["mapping"], "")
        # on a stick majors 3 and 4 are the upload halves of queues 1 and 2
        self.assertEqual([(q["major"], q["down_bps"], q["up_bps"]) for q in snap["queues"]],
                         [(1, 8000.0, 2000.0), (2, 4000.0, 1000.0)])
        self.assertEqual(sorted(c["name"] for c in snap["top"]), ["1:3", "2:3"])

    def test_stuck_root_class_counters_do_not_hide_the_traffic(self):
        """A root class X:1 whose counters do not move: the per-CPU HTB qdisc and the leaves still show the traffic."""
        empty = tempfile.mkdtemp(prefix="lqsync-stuck-")
        self.addCleanup(shutil.rmtree, empty)
        classes = iter([tc_tree(1, {1: 500}, [(1, 2, 0), (1, 3, 0)]), tc_tree(1, {1: 500}, [(1, 2, 250), (1, 3, 1000)])])
        qdiscs = iter([f"qdisc htb 1: parent 7fff:1 r2q 10 default 0x2\n Sent {b} bytes 1 pkt (dropped 0, overlimits 0 requeues 0)\n"
                       for b in (0, 2000)])
        clock = [100.0]
        with mock.patch("time.time", lambda: clock[0]):
            s = queue_stats.QueueSampler(lambda: empty, lambda: ("eth1", "eth1", False),
                                         runner=lambda args: next(classes) if args[1] == "class" else next(qdiscs))
            s.sample()
            clock[0] += 1
            snap = s.sample()
        q = snap["queues"][0]
        self.assertEqual(q["measured"], {"qdisc": 16000.0, "root": 0.0, "leaves": 10000.0})
        self.assertEqual(q["down_bps"], 16000.0)
        self.assertEqual(snap["totals"]["down_bps"], 16000.0)

    def test_stats_by_circuit_file(self):
        d = tempfile.mkdtemp(prefix="lqsync-stats-")
        self.addCleanup(shutil.rmtree, d)
        os.makedirs(os.path.join(d, "state", "stats"))
        with open(os.path.join(d, "state", "stats", "statsByCircuit.json"), "w") as f:
            json.dump([{"circuitID": "x1", "circuitName": "Alice", "ParentNode": "Tower", "classid": "0x1:0x3",
                        "up_classid": "0x1:0x3", "maxDownload": 10, "maxUpload": 5}], f)
        self.assertEqual(queue_stats.find_state_file(d, "statsByCircuit.json").name, "statsByCircuit.json")
        m = queue_stats.Mapping().load(None, queue_stats.find_state_file(d, "statsByCircuit.json"))
        self.assertEqual(m.circuits[(1, 3)]["name"], "Alice")
        self.assertEqual(m.circuits[(1, 3)]["parent"], "Tower")
        self.assertEqual(m.queue_name(1), "queue 1")


class RouteTest(unittest.TestCase):
    def client(self):
        import gui
        c = gui.app.test_client()
        with c.session_transaction() as s:
            s["authed"] = True
        return gui, c

    def test_queues_live(self):
        gui, c = self.client()
        with mock.patch.object(gui._queue_sampler, "latest", {"available": False, "error": "measuring…"}), \
                mock.patch.dict(gui._queue_state, {"thread": object()}):     # no sampler thread in tests
            d = c.get("/api/queues/live").get_json()
            self.assertGreater(gui._queue_state["wanted_until"], 0)     # asking keeps the sampler going
        self.assertEqual((d["ok"], d["available"], d["error"]), (True, False, "measuring…"))
        self.assertIn("link", d)

    def test_overview_counts(self):
        gui, c = self.client()
        from device_database import DeviceDatabase, ensure_jobs_table, ensure_webhook_table, record_webhook_event
        db = DeviceDatabase(str(gui.DB_PATH), os.path.join(os.path.dirname(str(gui.DB_PATH)), "ShapedDevices.csv"))
        db.open()
        try:
            db.conn.execute("DELETE FROM devices")
            for i, (src, router, plan) in enumerate([("pppoe", "A", 50), ("pppoe", "A", 50), ("hotspot", "B", 10)]):
                db.conn.execute(
                    "INSERT INTO devices (code, circuit_id, device_id, ipv4, download_min_mbps, upload_min_mbps, "
                    "download_max_mbps, upload_max_mbps, source, router) VALUES (?, ?, ?, ?, 1, 1, ?, ?, ?, ?)",
                    (f"u{i}", f"c{i}", f"d{i}", f"100.64.1.{i}", plan, plan // 2, src, router))
            ensure_jobs_table(db.conn)
            ensure_webhook_table(db.conn)
            db.conn.commit()
            record_webhook_event(db.conn, "A", "up", "pppoe", "u0", "100.64.1.0", "", "192.0.2.1")
        finally:
            db.close()
        d = c.get("/api/overview").get_json()
        self.assertEqual(d["devices"], 3)
        self.assertEqual(d["sources"], {"pppoe": 2, "hotspot": 1})
        self.assertEqual([(r["name"], r["devices"]) for r in d["routers"]], [("A", 2), ("B", 1)])
        self.assertEqual(d["plans"][0], {"dl": 50, "ul": 25, "devices": 2})
        self.assertEqual(d["plan_dl"], 110)
        self.assertEqual(d["events"][0]["user"], "u0")


if __name__ == "__main__":
    unittest.main()
