"""auto_cpu planner: placement, promotion, sharding, stickiness and queue detection."""
import json
import os
import tempfile
import unittest

import cpu_planner as cp
from cpu_planner import Device, Params, plan, prior_from_network


def devs(router, n, dl=100, ul=50, start=0, parent='', prefix=None):
    return [Device(f"{prefix or router}-{i:04d}", router, dl, ul, parent) for i in range(start, start + n)]


def node(nid, name, routers=(), children=(), **kw):
    return {"id": nid, "name": name, "type": kw.pop("type", "site"), "download_mbps": kw.pop("dl", 0),
            "upload_mbps": kw.pop("ul", 0), "virtual": kw.pop("virtual", False),
            "promote": kw.pop("promote", False), "pin_cpu": kw.pop("pin", None),
            "routers": list(routers), "children": list(children)}


def topo(*nodes, router_options=None):
    return {"version": 1, "nodes": list(nodes), "router_options": router_options or {}}


def with_parents(devices, parents):
    return [Device(d.code, d.router, d.dl, d.ul, parents.get(d.code, d.parent), d.is_static, d.has_ip)
            for d in devices]


class Invariants:
    def check(self, p, devices, routers):
        names = []

        def walk(children, depth, cap):
            for name, n in children.items():
                names.append(name)
                self.assertLessEqual(depth, 8, name)
                if cap:
                    self.assertLessEqual(n["downloadBandwidthMbps"], cap[0], name)
                    self.assertLessEqual(n["uploadBandwidthMbps"], cap[1], name)
                self.assertGreaterEqual(n["downloadBandwidthMbps"], 1)
                walk(n["children"], depth + 1, (n["downloadBandwidthMbps"], n["uploadBandwidthMbps"]))

        for name, n in p.network.items():
            self.assertRegex(name, r'^CPU\d+$')
            self.assertNotIn("virtual", n)
        self.assertEqual(list(p.network), [f"CPU{i}" for i in range(len(p.network))])
        walk(p.network, 1, None)
        self.assertEqual(len(names), len(set(names)), "node names must be unique")
        nodeset = set(names)
        for code, parent in p.parents.items():
            self.assertIn(parent, nodeset, code)
        placed = [d for d in devices if d.router in routers or d.is_static]
        self.assertEqual(set(p.parents), {d.code for d in placed})
        self.assertEqual(sum(p.bin_circuits().values()), sum(1 for d in placed if d.has_ip))
        json.dumps(p.to_json())   # serialisable


class PlannerTest(Invariants, unittest.TestCase):
    def test_equal_routers_one_per_bin(self):
        routers = ["R1", "R2", "R3", "R4"]
        devices = [d for r in routers for d in devs(r, 50)]
        p = plan(devices, routers, None, Params(bins=4))
        self.check(p, devices, routers)
        self.assertEqual(sorted(len(b["items"]) for b in p.bins), [1, 1, 1, 1])
        self.assertEqual(p.shards, {})
        self.assertEqual(p.promoted, [])

    def test_oversized_router_is_sharded_and_stable(self):
        routers = ["BIG", "S1", "S2"]
        devices = devs("BIG", 400) + devs("S1", 100) + devs("S2", 100)
        p = plan(devices, routers, None, Params(bins=4))
        self.check(p, devices, routers)
        self.assertIn("BIG", p.shards)
        names = [s["name"] for s in p.shards["BIG"]]
        self.assertEqual(names[0], "BIG")
        self.assertTrue(all(n == "BIG" or n.startswith("BIG #") for n in names))
        self.assertEqual(sum(s["circuits"] for s in p.shards["BIG"]), 400)
        self.assertEqual(len({s["bin"] for s in p.shards["BIG"]}), len(names))   # shards on different CPUs

        again = plan(with_parents(devices, p.parents), routers, None, Params(bins=4),
                     prior_from_network(p.network))
        self.assertEqual(again.device_changes, 0)
        self.assertEqual(again.moves, [])
        self.assertEqual(again.network, p.network)

    def test_heavy_site_is_promoted(self):
        routers = ["A", "B", "C", "D", "E"]
        devices = devs("A", 200) + devs("B", 200) + devs("C", 100) + devs("D", 100) + devs("E", 100)
        t = topo(node("s1", "Big Site", routers=["A", "B"], dl=5000, ul=5000))
        p = plan(devices, routers, t, Params(bins=4))
        self.check(p, devices, routers)
        self.assertEqual([x["name"] for x in p.promoted], ["Big Site"])
        self.assertEqual(p.promoted[0]["reason"], "auto")
        self.assertNotIn("Big Site", p.kinds)
        self.assertTrue(any("not enforced" in w["text"] for w in p.warnings))
        self.assertEqual(p.node_index["s1"]["status"], "promoted")

    def test_promote_flag_dissolves_small_node(self):
        routers = ["A", "B", "C", "D"]
        devices = [d for r in routers for d in devs(r, 20)]
        t = topo(node("s1", "Small", routers=["A", "B"], promote=True))
        p = plan(devices, routers, t, Params(bins=4))
        self.check(p, devices, routers)
        self.assertEqual(p.promoted[0]["reason"], "flag")
        self.assertEqual({it["name"] for it in p.items}, {"A", "B", "C", "D"})

    def test_nested_site_kept_whole(self):
        routers = ["A", "B", "C", "D"]
        devices = [d for r in routers for d in devs(r, 20)]
        t = topo(node("s1", "North", children=[node("t1", "Tower", routers=["A"], type="ap")], routers=["B"]))
        p = plan(devices, routers, t, Params(bins=2))   # North = exactly one fair share
        self.check(p, devices, routers)
        north_bin = p.node_index["s1"]["bin"]
        self.assertIn("Tower", p.network[north_bin]["children"]["North"]["children"])
        self.assertEqual(p.parents["A-0000"], "A")
        self.assertEqual(p.node_index["t1"]["status"], "nested")

    def test_pins(self):
        routers = ["A", "B", "C"]
        devices = [d for r in routers for d in devs(r, 30)]
        t = topo(node("s1", "Site", routers=["A"], pin=2),
                 router_options={"B": {"pin_cpu": 0}, "C": {"pin_cpu": 9}})
        p = plan(devices, routers, t, Params(bins=3))
        self.check(p, devices, routers)
        self.assertIn("Site", p.network["CPU2"]["children"])
        self.assertIn("B", p.network["CPU0"]["children"])
        self.assertTrue(any("CPU9" in w["text"] for w in p.warnings))

    def test_sticky_new_router_only_placed(self):
        routers = [f"R{i}" for i in range(6)]
        devices = [d for i, r in enumerate(routers) for d in devs(r, 20 + i * 7)]
        p = plan(devices, routers, None, Params(bins=4))
        prior = prior_from_network(p.network)
        routers2 = routers + ["NEW"]
        devices2 = with_parents(devices, p.parents) + devs("NEW", 10)
        p2 = plan(devices2, routers2, None, Params(bins=4), prior)
        self.check(p2, devices2, routers2)
        for it in p2.items:
            if it["name"] != "NEW":
                self.assertTrue(it["sticky"], it)
        self.assertEqual(p2.moves, [])
        self.assertEqual(p2.device_changes, 10)

    def test_rebalance_respects_budget(self):
        routers = [f"R{i}" for i in range(8)]
        devices = [d for r in routers for d in devs(r, 10)]
        # Everything on CPU0 last time
        prior_net = {f"CPU{i}": {"children": {}} for i in range(4)}
        prior_net["CPU0"]["children"] = {r: {"children": {}} for r in routers}
        p = plan(devices, routers, None, Params(bins=4, move_budget=2), prior_from_network(prior_net))
        self.check(p, devices, routers)
        self.assertEqual(len([m for m in p.moves if m["reason"] == "rebalance"]), 2)
        p_all = plan(devices, routers, None, Params(bins=4, move_budget=50), prior_from_network(prior_net))
        self.assertEqual(sorted(len(b["items"]) for b in p_all.bins), [2, 2, 2, 2])

    def test_pinned_busiest_bin_does_not_block_rebalance(self):
        # CPU0 holds only a heavy pinned site; CPU1 is empty and CPU2 holds two routers.
        routers = ["P", "A", "B"]
        devices = devs("P", 300) + devs("A", 50) + devs("B", 50)
        t = topo(node("s1", "Pinned", routers=["P"], pin=0))
        prior_net = {"CPU0": {"children": {"Pinned": {"children": {"P": {"children": {}}}}}},
                     "CPU1": {"children": {}},
                     "CPU2": {"children": {"A": {"children": {}}, "B": {"children": {}}}}}
        p = plan(devices, routers, t, Params(bins=3), prior_from_network(prior_net))
        self.check(p, devices, routers)
        self.assertEqual(sorted(len(b["items"]) for b in p.bins), [1, 1, 1])
        self.assertEqual([m["reason"] for m in p.moves], ["rebalance"])

    def test_bin_count_change_repacks(self):
        routers = [f"R{i}" for i in range(6)]
        devices = [d for r in routers for d in devs(r, 25)]
        p = plan(devices, routers, None, Params(bins=4))
        p2 = plan(with_parents(devices, p.parents), routers, None, Params(bins=6),
                  prior_from_network(p.network))
        self.check(p2, devices, routers)
        self.assertEqual(len(p2.network), 6)
        self.assertTrue(any("re-packed" in w["text"] for w in p2.warnings))
        self.assertEqual(sorted(len(b["items"]) for b in p2.bins), [1] * 6)

    def test_router_in_two_nodes_and_stale_routers(self):
        routers = ["A", "B"]
        devices = devs("A", 10) + devs("B", 10)
        t = topo(node("s1", "One", routers=["A", "GONE"]), node("s2", "Two", routers=["A", "B"]))
        self.assertIn("both", cp.topology_errors(t, routers))
        p = plan(devices, routers, t, Params(bins=2))
        self.check(p, devices, routers)
        texts = " ".join(w["text"] for w in p.warnings)
        self.assertIn("GONE", texts)
        self.assertIn("kept in the first", texts)

    def test_static_devices(self):
        routers = ["A", "B"]
        devices = devs("A", 10) + devs("B", 10) + [
            Device("MAN-1", "", 50, 50, "Site", is_static=True),
            Device("MAN-2", "", 50, 50, "nowhere", is_static=True),
            Device("MAN-3", "", 50, 50, "B", is_static=True),
            Device("DYN-X", "REMOVED", 50, 50, "CPU0"),
        ]
        t = topo(node("s1", "Site", routers=["A"]))
        p = plan(devices, routers, t, Params(bins=2))
        self.check(p, devices, routers)
        self.assertEqual(p.parents["MAN-1"], "Site")
        self.assertRegex(p.parents["MAN-2"], r'^CPU\d$')
        self.assertEqual(p.parents["MAN-3"], "B")
        self.assertNotIn("DYN-X", p.parents)

    def test_depth_limit(self):
        deep = node("n7", "L7", routers=["A"])
        for i in range(6, 0, -1):
            deep = node(f"n{i}", f"L{i}", children=[deep])
        t = topo(deep)
        self.assertIn("6 levels", cp.topology_errors(t, ["A"]))
        devices = devs("A", 10) + devs("B", 10)
        p = plan(devices, ["A", "B"], t, Params(bins=2))
        self.check(p, devices, ["A", "B"])
        self.assertEqual(p.node_index["n7"]["status"], "folded")
        self.assertTrue(any("levels" in w["text"] for w in p.warnings))

    def test_child_capacity_clamped(self):
        routers = ["A", "B"]
        devices = devs("A", 10) + devs("B", 10)
        t = topo(node("s1", "Parent", dl=500, ul=500, children=[node("c1", "Child", routers=["A"], dl=900)]))
        p = plan(devices, routers, t, Params(bins=1))
        self.check(p, devices, routers)
        child = p.network["CPU0"]["children"]["Parent"]["children"]["Child"]
        self.assertEqual(child["downloadBandwidthMbps"], 500)
        self.assertTrue(any("clamped" in w["text"] for w in p.warnings))

    def test_reserved_router_name(self):
        routers = ["CPU1", "B"]
        devices = devs("CPU1", 10) + devs("B", 10)
        p = plan(devices, routers, None, Params(bins=2))
        self.check(p, devices, routers)
        self.assertEqual(p.parents["CPU1-0000"], "CPU1 (router)")

    def test_deterministic(self):
        routers = ["A", "B", "C", "BIG"]
        devices = devs("A", 30) + devs("B", 30, dl=50) + devs("C", 10) + devs("BIG", 300, dl=25)
        t = topo(node("s1", "Site", routers=["A", "B"]))
        a = plan(devices, routers, t, Params(bins=3))
        b = plan(list(reversed(devices)), routers, t, Params(bins=3))
        self.assertEqual(json.dumps(a.network), json.dumps(b.network))
        self.assertEqual(a.parents, b.parents)

    def test_shrunk_router_goes_back_to_one_cpu(self):
        routers = ["BIG", "S1", "S2"]
        devices = devs("BIG", 120) + devs("S1", 50) + devs("S2", 50)
        p = plan(devices, routers, None, Params(bins=2))
        self.assertEqual(len(p.shards.get("BIG", [])), 2)
        # BIG shrinks well below one core's share: it becomes one router node again
        smaller = with_parents(devices, p.parents)[:40] + devs("S1", 50) + devs("S2", 50)
        p2 = plan(smaller, routers, None, Params(bins=2), prior_from_network(p.network))
        self.check(p2, smaller, routers)
        self.assertNotIn("BIG", p2.shards)
        self.assertEqual({p2.parents[d.code] for d in smaller if d.router == "BIG"}, {"BIG"})

    def test_two_big_routers_fill_every_cpu_evenly(self):
        # A split site whose two routers each need several cores: the shards must
        # fill all 16 CPUs evenly, never two shards of one router on one CPU.
        import random
        rng = random.Random(5)
        plans = [10, 25, 50, 50, 100, 100, 200]
        devices = [Device(f"A-{i:04d}", "AC 1", rng.choice(plans), 10) for i in range(860)] + \
                  [Device(f"B-{i:04d}", "AC 2", rng.choice(plans), 10) for i in range(682)]
        routers = ["AC 1", "AC 2"]
        t = topo(node("s1", "san-poli", routers=routers))
        p = plan(devices, routers, t, Params(bins=16))
        self.check(p, devices, routers)
        loads = [b["dl"] for b in p.bins]
        self.assertLess(max(loads) / min(loads), 1.1)
        for r in routers:
            self.assertEqual(len({s["bin"] for s in p.shards[r]}), len(p.shards[r]))
        self.assertLessEqual(len(p.shards["AC 1"]) + len(p.shards["AC 2"]), 16 + 1)
        again = plan(with_parents(devices, p.parents), routers, t, Params(bins=16), prior_from_network(p.network))
        self.assertEqual(again.device_changes, 0)
        self.assertEqual(again.network, p.network)

    def test_two_shards_on_one_cpu_are_rebalanced(self):
        # An older plan left 5 shards on 4 CPUs (CPU3 held two): only the overflow moves.
        devices = [Device(f"BIG-{i:04d}", "BIG", 100, 10) for i in range(400)]
        parents, net = {}, {f"CPU{i}": {"children": {}} for i in range(4)}
        for i, d in enumerate(devices):
            shard = i // 80
            name = "BIG" if shard == 0 else f"BIG #{shard + 1}"
            parents[d.code] = name
            net[f"CPU{min(shard, 3)}"]["children"][name] = {"children": {}}
        devices = with_parents(devices, parents)
        p = plan(devices, ["BIG"], None, Params(bins=4), prior_from_network(net))
        self.check(p, devices, ["BIG"])
        loads = [b["dl"] for b in p.bins]
        self.assertLess(max(loads) / min(loads), 1.12)
        self.assertEqual(len(p.shards["BIG"]), 4)
        self.assertLess(p.device_changes, 120)                # of 400: the rest stayed put

    def test_zero_loads_spread(self):
        routers = [f"R{i}" for i in range(4)]
        p = plan([], routers, None, Params(bins=4))
        self.assertEqual(sorted(len(b["items"]) for b in p.bins), [1, 1, 1, 1])

    def test_projection_and_capacity(self):
        routers = ["A", "B"]
        devices = devs("A", 100, dl=100) + devs("B", 100, dl=100)   # 20 Gbps of plans
        p = plan(devices, routers, None, Params(bins=2, link_dl_mbps=10000, capacity_mbps=3000))
        self.assertAlmostEqual(p.summary["scale"], 0.5)
        self.assertTrue(any(w["level"] == "error" and "one core" in w["text"] for w in p.warnings))
        self.assertTrue(any("less than the" in w["text"] for w in p.warnings))

    def test_queue_count_warning(self):
        p = plan([], ["A"], None, Params(bins=8, n_source="whatif", queue_info={"queues": 6, "warnings": []}))
        self.assertTrue(any("share cores" in w["text"] for w in p.warnings))

    def test_ids_keep_renamed_node_on_cpu(self):
        routers = ["A", "B", "C"]
        devices = [d for r in routers for d in devs(r, 20)]
        t = topo(node("s1", "Old Name", routers=["A"]))
        p = plan(devices, routers, t, Params(bins=3, emit_ids=True))
        t2 = topo(node("s1", "New Name", routers=["A"]))
        p2 = plan(with_parents(devices, p.parents), routers, t2, Params(bins=3, emit_ids=True),
                  prior_from_network(p.network))
        self.assertEqual(p2.node_index["s1"]["bin"], p.node_index["s1"]["bin"])
        self.assertEqual(p2.network["CPU0"]["id"], "mikrotik:cpu:CPU0")


class AssignerIntegrationTest(unittest.TestCase):
    def test_assign_auto_cpu_writes_and_is_stable(self):
        from node_assigner import NodeAssigner
        from tests.legacy_fixture import build_db, ROUTERS
        path = os.path.join(tempfile.mkdtemp(prefix='lqsync-auto-'), 'network.json')
        assigner = NodeAssigner(path)
        db = build_db()
        routers = [dict(r) for r in ROUTERS]
        t = topo(node("n1", "North", routers=["BRAS-A", "BRAS-B"]))
        self.assertTrue(assigner.assign(db.conn, "auto_cpu", routers, 3, False, t, False))
        net = assigner.read_network_json()
        self.assertEqual(list(net), ["CPU0", "CPU1", "CPU2"])
        self.assertEqual(sum(assigner.bin_circuits.values()), 181)
        names = set()

        def walk(children):
            for name, n in children.items():
                names.add(name)
                walk(n["children"])
        walk(net)
        parents = {p for (p,) in db.conn.execute("SELECT parent_node FROM devices")}
        self.assertTrue(parents <= names, parents - names)
        self.assertEqual(db.conn.execute("SELECT parent_node FROM devices WHERE code='MAN-1'").fetchone()[0][:3],
                         "CPU")
        # Same input again: nothing to write
        self.assertFalse(assigner.assign(db.conn, "auto_cpu", routers, 3, False, t, False))
        db.check_limits(3, assigner.bin_circuits)
        db.close()


class TopologyConfigTest(unittest.TestCase):
    def test_valid_topology(self):
        t = topo(node("s1", "North", routers=["A"], children=[node("t1", "Tower", type="ap")]))
        self.assertIsNone(cp.topology_errors(t, ["A"]))
        self.assertIsNone(cp.topology_errors(None, ["A"]))

    def test_invalid_topologies(self):
        cases = [
            (topo(node("s1", "A")), "named like a router"),
            (topo(node("s1", "CPU3")), "reserved"),
            (topo(node("s1", "X"), node("s1", "Y")), "duplicate id"),
            (topo(node("s1", "X"), node("s2", "X")), "duplicate name"),
            (topo(node("bad id", "X")), "id must"),
            (topo(node("s1", "X", dl=-1)), "download_mbps"),
            (topo(node("s1", "X", pin="2")), "pin_cpu"),
            (topo(node("s1", "X", type="tower")), "type"),
            ({"nodes": "x"}, "must be a list"),
        ]
        for t, msg in cases:
            with self.subTest(msg):
                self.assertIn(msg, cp.topology_errors(t, ["A"]) or "")

    def test_from_legacy(self):
        routers = [{"name": "R1", "site": "North"}, {"name": "R2", "site": "North"},
                   {"name": "R3"}, {"name": "R4", "site": "R3"}]
        t = cp.topology_from_legacy(routers, virtual_sites=True)
        self.assertEqual([n["name"] for n in t["nodes"]], ["North", "R3 (site)"])
        self.assertEqual(t["nodes"][0]["routers"], ["R1", "R2"])
        self.assertTrue(t["nodes"][0]["virtual"])
        self.assertIsNone(cp.topology_errors(t, [r["name"] for r in routers]))

    def test_dedupe(self):
        self.assertEqual(cp.dedupe_router_names(["A", "A", "B", "A"]), ["A", "A 2", "B", "A 3"])

    def test_prior_ignores_non_cpu_networks(self):
        self.assertEqual(prior_from_network({"Site": {"children": {}}}).bin_count, 0)
        self.assertEqual(prior_from_network({"CPU0": {"children": {}}, "CPU1": {"children": {}}}).bin_count, 2)


class QueueDetectionTest(unittest.TestCase):
    def sysfs(self, nics, ecores=None):
        root = tempfile.mkdtemp(prefix='lqsync-sysfs-')
        for nic, n in nics.items():
            for i in range(n):
                os.makedirs(os.path.join(root, 'class', 'net', nic, 'queues', f'tx-{i}'))
                os.makedirs(os.path.join(root, 'class', 'net', nic, 'queues', f'rx-{i}'))
        if ecores:
            os.makedirs(os.path.join(root, 'devices', 'cpu_atom'))
            with open(os.path.join(root, 'devices', 'cpu_atom', 'cpus'), 'w') as f:
                f.write(ecores + "\n")
        return root

    BRIDGE = {"bridge": {"to_internet": "eth1", "to_network": "eth2"}}

    def test_nic_and_ecores(self):
        root = self.sysfs({"eth1": 8, "eth2": 6}, ecores="12-15")
        q = cp.detect_queues(self.BRIDGE, sysfs=root, cpu_count=16)
        self.assertEqual((q["queues"], q["source"], q["e_cores"]), (6, "sysfs", 4))
        q = cp.detect_queues({**self.BRIDGE, "exclude_efficiency_cores": False}, sysfs=root, cpu_count=4)
        self.assertEqual(q["queues"], 4)

    def test_override_and_stick(self):
        root = self.sysfs({"eth1": 16})
        conf = {"single_interface": {"enabled": True, "interface": "eth1"},
                "queues": {"override_available_queues": 12}}
        q = cp.detect_queues(conf, sysfs=root, cpu_count=32)
        self.assertEqual((q["queues"], q["source"], q["on_a_stick"]), (6, "override", True))

    def test_unreadable_nic_falls_back_to_cpus(self):
        q = cp.detect_queues(self.BRIDGE, sysfs=self.sysfs({}), cpu_count=8)
        self.assertEqual((q["queues"], q["source"]), (8, "cpu_count"))
        self.assertTrue(q["warnings"])

    def test_resolve_params(self):
        root = self.sysfs({"eth1": 4, "eth2": 4})
        conf = {**self.BRIDGE, "queues": {"generated_pn_download_mbps": 2500, "downlink_bandwidth_mbps": 9000}}
        ts = {"cpu_capacity_mbps": 0, "split_ratio": 1.0}
        p = cp.resolve_params(conf, True, ts, sysfs=root, cpu_count=8)
        self.assertEqual((p.bins, p.capacity_mbps, p.link_dl_mbps), (4, 2500, 9000))
        p = cp.resolve_params(conf, 6, {**ts, "cpu_capacity_mbps": 4000}, sysfs=root, cpu_count=8)
        self.assertEqual((p.bins, p.n_source, p.capacity_mbps), (6, "config", 4000))

    def test_mini_toml(self):
        text = ('version = "1.5"\nexclude_efficiency_cores = true\n[bridge]\nto_internet = "a" # c\n'
                '[queues]\noverride_available_queues = 12\n[stormguard]\ntargets = ["CPU0", "CPU1"]\n'
                '[treeguard.links]\ntop_level_auto_virtualize = false\n')
        conf = cp._mini_toml(text)
        self.assertEqual(conf["bridge"]["to_internet"], "a")
        self.assertEqual(conf["queues"]["override_available_queues"], 12)
        self.assertEqual(conf["stormguard"]["targets"], ["CPU0", "CPU1"])
        self.assertIs(conf["treeguard"]["links"]["top_level_auto_virtualize"], False)
        self.assertIs(conf["exclude_efficiency_cores"], True)


if __name__ == "__main__":
    unittest.main()
