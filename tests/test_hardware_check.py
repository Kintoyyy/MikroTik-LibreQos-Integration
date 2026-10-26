"""Hardware check against LibreQoS v2.0's requirements: the tables, reading a host, the verdicts, the route."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import hardware_check as hw
from preview_server import FAKE_NICS, fake_hw_run, seed_fake_hardware

CONF = {"bridge": {"to_internet": "ens17", "to_network": "ens16"}, "queues": {"downlink_bandwidth_mbps": 10000}}


def cpuinfo(cores, threads_per_core=1):
    blocks = []
    for i in range(cores * threads_per_core):
        blocks.append(f"processor\t: {i}\nmodel name\t: Intel(R) Xeon(R) E-2456\nflags\t\t: fpu sse2\n"
                      f"physical id\t: 0\ncore id\t\t: {i % cores}\n")
    return "\n".join(blocks) + "\n"


class Host:
    """A fake server: sysfs (NICs, queues, SMT), /proc (cpuinfo, meminfo) and /etc/os-release in a temp dir."""
    def __init__(self, cores=8, queues=8, mem_gb=32, os_version="24.04"):
        self.root = tempfile.mkdtemp(prefix="lqsync-hw-")
        self.sysfs = os.path.join(self.root, "sys")
        seed_fake_hardware(self.sysfs)
        for nic in ("ens16", "ens17"):
            for i in range(queues):
                os.makedirs(os.path.join(self.sysfs, "class", "net", nic, "queues", f"tx-{i}"))
        self.proc = os.path.join(self.root, "proc")
        self.etc = os.path.join(self.root, "etc")
        os.makedirs(self.proc)
        os.makedirs(self.etc)
        with open(os.path.join(self.proc, "cpuinfo"), "w") as f:
            f.write(cpuinfo(cores))
        with open(os.path.join(self.proc, "meminfo"), "w") as f:
            f.write(f"MemTotal:       {mem_gb * 1024 * 1024} kB\n")
        with open(os.path.join(self.etc, "os-release"), "w") as f:
            f.write(f'ID=ubuntu\nVERSION_ID="{os_version}"\nPRETTY_NAME="Ubuntu {os_version} LTS"\n')
        self.cores = cores

    def gather(self, conf=CONF):
        with mock.patch.object(hw.cpu_planner, "CPU_COUNT", self.cores):
            facts = hw.gather(conf, sysfs=self.sysfs, proc=self.proc, etc=self.etc, run=fake_hw_run(self.sysfs))
        facts["addresses"] = {"lo": ["127.0.0.1"], "ens18": ["192.0.2.5"]}
        return facts


def checks(result):
    return {c["title"]: c for s in result["sections"] for c in s["checks"]}


def nic_named(facts, name):
    return next(n for n in facts["shaping"] if n["name"] == name)


class TablesTest(unittest.TestCase):
    def test_lookups(self):
        self.assertEqual(hw.single_thread_limits(2500), (3, 1000))
        self.assertEqual(hw.single_thread_limits(4500), (5, 3000))
        self.assertIsNone(hw.single_thread_limits(900))
        self.assertEqual(hw.aggregate_capacity(8, 2000), 10)
        self.assertEqual(hw.aggregate_capacity(12, 2000), 15.5)          # between 8 and 16 cores
        self.assertEqual(hw.aggregate_capacity(8, 2500), 14)             # between scores 2000 and 3000
        self.assertEqual(hw.aggregate_capacity(128, 4000), 288)          # no 4000 column at 128 cores: the last known
        self.assertIsNone(hw.aggregate_capacity(8, 500))
        self.assertEqual(hw.ram_subscribers(8), 1000)
        self.assertEqual(hw.ram_subscribers(24), 3750)
        self.assertEqual(hw.ram_subscribers(256), 40000)
        self.assertEqual(hw.profile_for(800, 1)[0], "Small")
        self.assertEqual(hw.profile_for(3000, 10)[0], "Medium")
        self.assertEqual(hw.profile_for(30000, 10)[0], "High-throughput")
        self.assertEqual(hw.supported_nic("Ethernet Controller XXV710 for 25GbE SFP28")[0], "Intel XXV710")
        self.assertEqual(hw.supported_nic("MT2894 Family [ConnectX-6 Lx]")[0], "Mellanox ConnectX-6")
        self.assertIsNone(hw.supported_nic("Ethernet Controller E810-C for SFP"))


class EvaluateTest(unittest.TestCase):
    def setUp(self):
        self.host = Host()
        self.addCleanup(shutil.rmtree, self.host.root)

    def test_a_good_server(self):
        facts = self.host.gather()
        nic = nic_named(facts, "ens16")
        self.assertEqual((nic["name"], nic["driver"], nic["pci"], nic["speed"], nic["tx_queues"], nic["max_queues"]),
                         ("ens16", "i40e", "0000:03:00.0", 10000, 8, 8))
        self.assertEqual(nic["model"], "Intel Corporation Ethernet Controller X710 for 10GbE SFP+")
        r = hw.evaluate(facts, subscribers=3000, max_plan_mbps=500, link_mbps=10000, score=3000)
        c = checks(r)
        self.assertEqual(r["counts"]["fail"], 0, [x for x in c.values() if x["status"] == "fail"])
        self.assertEqual(c["Operating system"]["status"], "pass")
        self.assertEqual(c["Hyper-Threading (SMT)"]["status"], "pass")
        self.assertEqual(c["ens16: NIC"]["status"], "pass")
        self.assertEqual(c["ens16: XDP"]["status"], "pass")
        self.assertEqual(c["ens16: queues"]["status"], "pass")
        self.assertEqual(c["Dedicated shaping NIC"]["status"], "pass")
        self.assertEqual(c["Management interface"]["status"], "pass")
        self.assertIn("4 Gbps per top-level node", c["Single-thread score"]["found"])
        self.assertEqual(c["Total capacity"]["status"], "pass")                       # 8 cores at 3000: ~18 Gbps
        self.assertEqual(r["profile"], "Medium")

    def test_problems_are_found(self):
        host = Host(cores=16, queues=4, mem_gb=8, os_version="22.04")
        self.addCleanup(shutil.rmtree, host.root)
        os.makedirs(os.path.join(host.sysfs, "class", "net", "ens17", "bonding"))      # a bond
        facts = host.gather()
        facts["virt"] = "kvm"
        facts["addresses"] = {"ens16": ["192.0.2.9"]}                                  # managed over a shaping port
        r = hw.evaluate(facts, subscribers=2000, max_plan_mbps=1000, link_mbps=40000, score=1600)
        c = checks(r)
        self.assertEqual(c["Operating system"]["status"], "warn")
        self.assertEqual(c["Bare metal or VM"]["status"], "warn")
        self.assertEqual(c["RAM for the subscriber count"]["status"], "fail")         # 8 GB: about 1,000
        self.assertEqual(c["ens16: queues"]["status"], "warn")                         # 4 queues, 16 cores
        self.assertEqual(c["ens17: bonding"]["status"], "fail")
        self.assertEqual(c["Management interface"]["status"], "warn")
        self.assertEqual(c["Single-thread score"]["status"], "warn")                   # 1000 Mbps plan > 500
        self.assertEqual(c["Total capacity"]["status"], "warn")                        # 40 Gbps link
        self.assertGreater(r["counts"]["fail"], 0)

    def test_hyperthreading_counts_real_cores(self):
        facts = self.host.gather()
        facts["cpu"].update(logical=16, physical=8, smt_active="1")
        facts["queues"]["shaping_cpus"] = 16                          # LibreQoS opens a queue per thread
        c = checks(hw.evaluate(facts, subscribers=100, link_mbps=1000, score=2000))
        self.assertEqual(c["Hyper-Threading (SMT)"]["status"], "warn")
        self.assertIn("on 8 core(s)", c["Total capacity"]["found"])  # capacity: real cores only
        self.assertIn("at least 16", c["ens16: queues"]["needs"])

    def test_unsupported_and_desktop_nics(self):
        facts = self.host.gather()
        nic_named(facts, "ens16").update(model="Realtek RTL8111", driver="r8169")
        nic_named(facts, "ens17").update(model="Ethernet Controller E810-C for SFP", driver="ice")
        c = checks(hw.evaluate(facts))
        self.assertEqual(c["ens16: NIC"]["status"], "fail")
        self.assertEqual(c["ens16: XDP"]["status"], "warn")                            # r8169: generic XDP only
        self.assertEqual(c["ens17: NIC"]["status"], "warn")                            # works, not on the list
        self.assertEqual(c["ens17: XDP"]["status"], "pass")

    def test_missing_interface_and_no_score(self):
        facts = self.host.gather({"bridge": {"to_internet": "eth9", "to_network": "ens16"}})
        c = checks(hw.evaluate(facts))
        self.assertEqual(c["eth9: interface"]["status"], "fail")
        self.assertEqual(c["Single-thread score"]["status"], "info")
        self.assertNotIn("Total capacity", c)


class EstimateTest(unittest.TestCase):
    def test_the_cpu_test_runs_in_its_own_process(self):
        e = hw.estimate_single_thread(sysfs="/nonexistent", rounds=1, slice_seconds=0.01)
        self.assertGreater(e["score"], 0)
        self.assertEqual(e["score"] % 10, 0)                                          # rounded: it is an estimate
        self.assertEqual(set(e["rates"]), {"compression", "sorting", "integer", "floating", "hashing"})
        self.assertIn(e["cpu"], os.sched_getaffinity(0))
        self.assertTrue(0 <= e["spread"] < 1 and isinstance(e["steady"], bool))

    def test_estimated_scores_are_labelled(self):
        host = Host()
        self.addCleanup(shutil.rmtree, host.root)
        c = checks(hw.evaluate(host.gather(), link_mbps=1000, score=3300, score_source="estimated"))
        self.assertTrue(c["Single-thread score"]["found"].startswith("≈3,300 (estimated)"))
        self.assertIn("±20%", c["Single-thread score"]["detail"])
        self.assertIn("≈3,300", c["Total capacity"]["found"])
        rough = checks(hw.evaluate(host.gather(), score=3300, score_source="estimated", score_rough=True))
        self.assertTrue(rough["Single-thread score"]["found"].startswith("≈3,300 (estimated, rough)"))
        self.assertIn("speed changed", rough["Single-thread score"]["detail"])


class RouteTest(unittest.TestCase):
    def test_estimate_runs_once_per_cpu_and_a_typed_score_wins(self):
        import gui
        host = Host()
        self.addCleanup(shutil.rmtree, host.root)
        saved = gui.SETTINGS_PATH.read_text() if gui.SETTINGS_PATH.exists() else None
        self.addCleanup(lambda: gui.SETTINGS_PATH.write_text(saved) if saved is not None else gui.SETTINGS_PATH.unlink())
        calls = []

        def fake_estimate(sysfs):
            calls.append(sysfs)
            return {"score": 3300, "cpu": 0, "load": 4.0, "geomean": 145.8, "rates": {}, "seconds": 3.1,
                    "python": "3.12.3", "at": 1.0}
        c = gui.app.test_client()
        with c.session_transaction() as s:
            s["authed"] = True
        post = lambda body: c.post("/api/troubleshoot/hardware", json=body, headers={"X-Requested-With": "fetch"}).get_json()
        with mock.patch.object(gui.cpu_planner, "SYSFS_ROOT", host.sysfs), \
                mock.patch.object(gui.cpu_planner, "CPU_COUNT", host.cores), \
                mock.patch.object(gui, "_hw_run", fake_hw_run(host.sysfs)), \
                mock.patch.object(gui, "_hw_estimate", fake_estimate), \
                mock.patch.object(gui, "HW_PATHS", {"proc": host.proc, "etc": host.etc}), \
                mock.patch.object(gui, "_lqos_conf_dict", lambda: CONF):
            d = post({"score": 0})
            self.assertEqual((d["score"], d["score_source"], len(calls)), (3300, "estimated", 1))
            self.assertEqual(d["estimate"]["cpu_model"], "Intel(R) Xeon(R) E-2456")
            self.assertEqual(post({"score": 0})["score"], 3300)
            self.assertEqual(len(calls), 1)                                            # kept: same CPU
            post({"score": 0, "detect": True})
            self.assertEqual(len(calls), 2)                                            # Detect runs it again
            d = post({"score": 2500})
            self.assertEqual((d["score"], d["score_source"], d["estimate"]["score"]), (2500, "manual", 3300))
            self.assertEqual(post({"score": 0})["score_source"], "estimated")          # cleared: the estimate again
        hw_saved = json.loads(gui.SETTINGS_PATH.read_text())["hardware"]
        self.assertEqual((hw_saved["single_thread_score"], hw_saved["estimate"]["score"]), (0, 3300))


    def test_route_saves_the_score(self):
        import gui
        host = Host()
        self.addCleanup(shutil.rmtree, host.root)
        saved = gui.SETTINGS_PATH.read_text() if gui.SETTINGS_PATH.exists() else None
        self.addCleanup(lambda: gui.SETTINGS_PATH.write_text(saved) if saved is not None else gui.SETTINGS_PATH.unlink())
        c = gui.app.test_client()
        with c.session_transaction() as s:
            s["authed"] = True
        with mock.patch.object(gui.cpu_planner, "SYSFS_ROOT", host.sysfs), \
                mock.patch.object(gui.cpu_planner, "CPU_COUNT", host.cores), \
                mock.patch.object(gui, "_hw_run", fake_hw_run(host.sysfs)), \
                mock.patch.object(gui, "HW_PATHS", {"proc": host.proc, "etc": host.etc}), \
                mock.patch.object(gui, "_lqos_conf_dict", lambda: CONF):
            d = c.post("/api/troubleshoot/hardware", json={"score": 2600}, headers={"X-Requested-With": "fetch"}).get_json()
            self.assertTrue(d["ok"], d)
            self.assertEqual(d["score"], 2600)
            self.assertEqual(json.loads(gui.SETTINGS_PATH.read_text())["hardware"]["single_thread_score"], 2600)
            again = c.post("/api/troubleshoot/hardware", json={}, headers={"X-Requested-With": "fetch"}).get_json()
            self.assertEqual(again["score"], 2600)                                     # remembered
            bad = c.post("/api/troubleshoot/hardware", json={"score": "fast"}, headers={"X-Requested-With": "fetch"})
            self.assertEqual(bad.status_code, 400)
        self.assertEqual([s["name"] for s in d["sections"]], ["System", "CPU", "Memory & disk", "Network", "Capacity & sizing"])
        self.assertEqual(d["cpu_model"], "Intel(R) Xeon(R) E-2456")
        self.assertIn("ens16: NIC", checks(d))


if __name__ == "__main__":
    unittest.main()
