"""This host over SNMP (net-snmp's UCD / HOST-RESOURCES / IF-MIB) and the Overview's merge with psutil."""
import unittest
from unittest import mock

import host_snmp
import snmp
from preview_server import FakeSnmpAgent

UCD = "1.3.6.1.4.1.2021"


class Host:
    """A settable snmpd MIB: CPU tick counters, memory, load, cores, uptime and two interfaces."""
    def __init__(self, full=True):
        self.full = full
        self.ticks = {"user": 1000, "nice": 0, "system": 1000, "idle": 8000, "wait": 0,
                      "interrupt": 0, "softirq": 0, "steal": 0}
        self.octets = {"ens16": (10_000, 20_000), "ens17": (5_000, 6_000)}

    def mib(self):
        m = {"1.3.6.1.2.1.1.5.0": "lqos-box", "1.3.6.1.2.1.1.1.0": "Linux lqos-box",
             "1.3.6.1.2.1.25.1.1.0": snmp.TimeTicks(360000)}
        if not self.full:                       # Ubuntu's default view: the system group only
            return m
        cols = {"user": 50, "nice": 51, "system": 52, "idle": 53, "wait": 54, "interrupt": 56, "softirq": 61, "steal": 64}
        m.update({f"{UCD}.11.{n}.0": snmp.Counter32(self.ticks[k] % 2 ** 32) for k, n in cols.items()})
        m.update({f"{UCD}.4.5.0": 1000, f"{UCD}.4.6.0": 400, f"{UCD}.4.14.0": 50, f"{UCD}.4.15.0": 150,
                  f"{UCD}.10.1.5.1": 125, f"{UCD}.10.1.5.2": 100, f"{UCD}.10.1.5.3": 75,
                  "1.3.6.1.2.1.25.3.3.1.2.196608": 30, "1.3.6.1.2.1.25.3.3.1.2.196609": 50})
        for i, (name, (rx, tx)) in enumerate([("lo", (1, 1))] + list(self.octets.items()), start=1):
            m[f"1.3.6.1.2.1.31.1.1.1.1.{i}"] = name
            m[f"1.3.6.1.2.1.31.1.1.1.6.{i}"] = snmp.Counter64(rx)
            m[f"1.3.6.1.2.1.31.1.1.1.10.{i}"] = snmp.Counter64(tx)
        return m


class HostSnmpTest(unittest.TestCase):
    def setUp(self):
        self.host = Host()
        self.agent = FakeSnmpAgent(self.host.mib)
        self.addCleanup(self.agent.close)
        self.clock = [1000.0]
        patcher = mock.patch("time.time", lambda: self.clock[0])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = host_snmp.HostSnmp("127.0.0.1", self.agent.port, "public")

    def test_first_sample(self):
        s = self.client.sample(exclude={"lo"})
        self.assertTrue(s["ok"], s["error"])
        self.assertEqual(s["hostname"], "lqos-box")
        self.assertEqual(s["uptime_seconds"], 3600)
        self.assertEqual(s["cpu_per"], [30, 50])
        self.assertEqual(s["cpu_avg"], 40.0)            # the per-core average until the ticks have moved
        self.assertEqual((s["mem_total"], s["mem_used"]), (1000 * 1024, 400 * 1024))   # total - free - buffers - cached
        self.assertEqual(s["mem_percent"], 40.0)
        self.assertEqual(s["load"], [1.25, 1.0, 0.75])
        self.assertEqual(s["net"], {})                  # rates need a second reading
        self.assertTrue(s["cpu_counters"])

    def test_cpu_from_ticks_counts_softirq(self):
        self.client.sample()
        for k, v in {"user": 100, "system": 100, "softirq": 600, "idle": 150, "wait": 50}.items():
            self.host.ticks[k] += v                     # 1000 ticks: 800 busy (softirq is LibreQoS's work)
        self.assertEqual(self.client.sample()["cpu_avg"], 80.0)
        self.assertEqual(self.client.sample()["cpu_avg"], 80.0)     # unchanged counters (snmpd cache): kept

    def test_cpu_counter_wrap(self):
        self.host.ticks["idle"] = 2 ** 32 - 100
        self.client.sample()
        self.host.ticks["idle"] += 900                  # wraps past 2^32
        self.host.ticks["user"] += 100
        self.assertEqual(self.client.sample()["cpu_avg"], 10.0)

    def test_interface_rates_hold_between_snmpd_cache_refreshes(self):
        self.client.sample()
        self.clock[0] += 2
        self.host.octets["ens16"] = (10_000 + 250_000, 20_000 + 500_000)
        net = self.client.sample(exclude={"lo"})["net"]
        self.assertEqual(net["ens16"], {"rx_mbps": 1.0, "tx_mbps": 2.0})
        self.assertNotIn("lo", net)
        self.clock[0] += 1                              # snmpd has not refreshed: same counters, same rate
        self.assertEqual(self.client.sample()["net"]["ens16"], {"rx_mbps": 1.0, "tx_mbps": 2.0})
        self.clock[0] += host_snmp.STALE + 1
        self.assertNotIn("ens16", self.client.sample()["net"])

    def test_system_group_only(self):
        self.host.full = False
        s = self.client.sample()
        self.assertTrue(s["ok"])
        self.assertEqual(sorted(s["fields"]), ["hostname", "uptime_seconds"])

    def test_no_answer(self):
        quiet = host_snmp.HostSnmp("127.0.0.1", self.agent.port, "wrong", timeout=0.2)
        s = quiet.sample()
        self.assertFalse(s["ok"])
        self.assertIn("timeout", s["error"])
        self.assertEqual(s["fields"], [])

    def test_settings(self):
        self.assertEqual(host_snmp.settings({"enabled": True, "port": "x"}),
                         {"enabled": True, "address": "127.0.0.1", "port": 161, "community": "public"})
        self.assertFalse(host_snmp.settings(None)["enabled"])


class GuiMergeTest(unittest.TestCase):
    """gui._host_metrics: snmpd's answers win, psutil fills the gaps, a silent snmpd is retried later."""
    def setUp(self):
        import gui
        self.gui = gui
        self.host = Host()
        self.agent = FakeSnmpAgent(self.host.mib)
        self.addCleanup(self.agent.close)
        for name, value in (("host_snmp_settings", lambda: {"enabled": True, "address": "127.0.0.1",
                                                             "port": self.agent.port, "community": "public"}),
                            ("_host", {"client": None, "key": None, "retry_at": 0.0, "status": None})):
            p = mock.patch.object(gui, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_snmp_values_win(self):
        m = self.gui._host_metrics()
        self.assertEqual(m["snmp"]["ok"], True)
        self.assertEqual(m["cpu_per"], [30, 50])
        self.assertEqual(m["mem_percent"], 40.0)
        self.assertEqual(m["hostname"], "lqos-box")
        self.assertEqual(m["sources"]["cpu_per"], "snmp")
        if self.gui.HAS_PSUTIL:
            self.assertEqual(m["sources"]["net"], "snmp")

    def test_psutil_fills_what_snmpd_hides(self):
        if not self.gui.HAS_PSUTIL:
            self.skipTest("psutil not installed")
        self.host.full = False
        m = self.gui._host_metrics()
        self.assertEqual(m["source"], "snmp + psutil")
        self.assertEqual(m["sources"]["uptime_seconds"], "snmp")
        self.assertEqual(m["sources"]["mem_used"], "psutil")
        self.assertEqual(len(m["cpu_per"]), len(self.gui.psutil.cpu_percent(percpu=True)))

    def test_silent_snmpd_is_retried_later(self):
        with mock.patch.object(self.gui, "host_snmp_settings", lambda: {
                "enabled": True, "address": "127.0.0.1", "port": self.agent.port, "community": "nope"}), \
                mock.patch.object(self.gui.host_snmp.HostSnmp, "__init__", _quick_init):
            m = self.gui._host_metrics()
            self.assertFalse(m["snmp"]["ok"])
            # inside the retry window snmpd is not asked again
            self.gui._host["client"] = mock.Mock(sample=mock.Mock(side_effect=AssertionError("asked again")))
            self.assertFalse(self.gui._host_metrics()["snmp"]["ok"])
        if self.gui.HAS_PSUTIL:
            self.assertEqual(m["source"], "psutil")

    def test_test_route(self):
        c = self.gui.app.test_client()
        with c.session_transaction() as s:
            s["authed"] = True
        d = c.post("/api/host-snmp/test", json={"address": "127.0.0.1", "port": self.agent.port},
                   headers={"X-Requested-With": "fetch"}).get_json()
        self.assertTrue(d["answered"], d)
        self.assertEqual(d["missing"], [])
        self.assertEqual(d["interfaces"], ["ens16", "ens17"])
        self.host.full = False
        d = c.post("/api/host-snmp/test", json={"address": "127.0.0.1", "port": self.agent.port},
                   headers={"X-Requested-With": "fetch"}).get_json()
        self.assertEqual(d["missing"], ["cpu", "cores", "memory", "load", "interfaces"])


_real_init = host_snmp.HostSnmp.__init__


def _quick_init(self, host="127.0.0.1", port=161, community="public", timeout=1.0):
    _real_init(self, host, port, community, timeout=0.2)


if __name__ == "__main__":
    unittest.main()
