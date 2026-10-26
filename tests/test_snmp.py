"""SNMP v2c client, the MikroTik SNMP poll, and the gui's choice between SNMP and the API."""
import time
import unittest
from unittest import mock

import snmp
import snmp_monitor
from preview_server import FakeSnmpAgent, FakeRouterOS, fake_snmp_mib, SNMP_STATIC_IFACES


def counters(rx=10**12, errors=3):
    return lambda iface: {"rx_bytes": rx, "tx_bytes": rx // 10, "rx_errors": errors, "tx_errors": 0,
                          "rx_drops": 1, "tx_drops": 0}


class CodecTest(unittest.TestCase):
    def test_round_trip(self):
        values = [0, 127, 128, -1, -129, 2**31 - 1, "text", "x" * 300, None, snmp.Counter32(2**32 - 1),
                  snmp.Gauge32(5), snmp.TimeTicks(123456), snmp.Counter64(2**64 - 1), snmp.NO_SUCH_OBJECT]
        varbinds = [(f"1.3.6.1.4.1.14988.1.{i}.{2**32 - 1}", v) for i, v in enumerate(values)]
        message = snmp.encode_message("püblic", snmp.RESPONSE, 2**31 - 1, varbinds)
        version, community, pdu, rid, status, index, decoded = snmp.decode_message(message)
        self.assertEqual((version, community.decode(), pdu, rid, status), (1, "püblic", snmp.RESPONSE, 2**31 - 1, 0))
        self.assertEqual([o for o, _ in decoded], [o for o, _ in varbinds])
        self.assertEqual([snmp.decode_value(*v) for _, v in decoded],
                         [int(v) if isinstance(v, int) else (None if v is None or v is snmp.NO_SUCH_OBJECT else v)
                          for v in values])

    def test_bad_input(self):
        with self.assertRaises(snmp.SnmpError):
            snmp.decode_message(b"\x30\x05\x02\x01")
        self.assertTrue(snmp.in_subtree("1.3.6.1.2.1.1.5.0", "1.3.6.1.2.1.1"))
        self.assertFalse(snmp.in_subtree("1.3.6.1.2.1.10", "1.3.6.1.2.1.1"))


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.mib = {"1.3.6.1.2.1.1.5.0": "core", "1.3.6.1.2.1.2.1.0": 3}
        self.mib.update({f"1.3.6.1.2.1.31.1.1.1.1.{i}": f"ether{i}" for i in range(1, 121)})
        self.mib["1.3.6.1.2.1.31.1.1.1.2.1"] = "next column"
        self.agent = FakeSnmpAgent(lambda: self.mib)
        self.addCleanup(self.agent.close)

    def client(self, community="public", timeout=1.0, retries=1):
        return snmp.Client("127.0.0.1", community, self.agent.port, timeout=timeout, retries=retries)

    def test_get(self):
        with self.client() as c:
            v = c.get([".1.3.6.1.2.1.1.5.0", "1.3.6.1.2.1.2.1.0", "1.3.6.1.2.1.1.6.0"])
        self.assertEqual(v, {"1.3.6.1.2.1.1.5.0": "core", "1.3.6.1.2.1.2.1.0": 3, "1.3.6.1.2.1.1.6.0": None})

    def test_get_many_is_chunked(self):
        oids = [f"1.3.6.1.2.1.31.1.1.1.1.{i}" for i in range(1, 101)]
        with self.client() as c:
            v = c.get(oids)
        self.assertEqual(len(v), 100)
        self.assertEqual(self.agent.requests, 4)       # 30 OIDs per request

    def test_walk_stops_at_subtree_end(self):
        with self.client() as c:
            rows = c.walk("1.3.6.1.2.1.31.1.1.1.1", max_repetitions=50)
        self.assertEqual(len(rows), 120)
        self.assertEqual(rows[0], ("1.3.6.1.2.1.31.1.1.1.1.1", "ether1"))

    def test_walk_to_end_of_mib(self):
        with self.client() as c:
            self.assertEqual(c.walk("1.3.6.1.2.1.31.1.1.1.2"), [("1.3.6.1.2.1.31.1.1.1.2.1", "next column")])
            self.assertEqual(c.walk("1.3.6.1.4"), [])

    def test_wrong_community_times_out(self):
        t0 = time.monotonic()
        with self.client("secret", timeout=0.2, retries=1) as c, self.assertRaises(snmp.SnmpError) as e:
            c.get(["1.3.6.1.2.1.1.5.0"])
        self.assertIn("timeout", str(e.exception))
        self.assertLess(time.monotonic() - t0, 2)


class MikrotikPollTest(unittest.TestCase):
    def agent(self, **kw):
        state = {"sessions": kw.pop("sessions", 37), "static": kw.pop("static", None)}
        args = {"ros7": True, **kw}
        agent = FakeSnmpAgent(lambda: fake_snmp_mib("BRAS-1", state["sessions"], counters(), time.time(),
                                                    static=state["static"], **args))
        self.addCleanup(agent.close)
        return agent, state

    def poll(self, agent, kind="bras", chosen=(), cache=None):
        return snmp_monitor.poll("127.0.0.1", agent.port, "public", kind, list(chosen),
                                 cache or snmp_monitor.RouterCache(), timeout=1.0)

    def test_ros7_health_ppp_and_auto_interfaces(self):
        agent, _ = self.agent()
        info = self.poll(agent)
        self.assertTrue(info["ok"], info["error"])
        self.assertEqual((info["identity"], info["board"], info["version"], info["via"]),
                         ("BRAS-1", "CCR2116-12G-4S+", "7.16.2", "snmp"))
        self.assertEqual(info["uptime"], "3w2d04:11:09")
        self.assertIsNotNone(info["cpu"])
        self.assertTrue(30 <= info["mem"] <= 50)
        self.assertTrue(40 <= info["temp"] <= 60)
        self.assertAlmostEqual(info["volt"], 24.1)
        self.assertEqual(info["ppp"], 37)
        self.assertEqual(sorted(info["ifaces"]), ["ether1", "ether2", "sfp-sfpplus1"])   # running ethernet only
        self.assertEqual(info["ifaces"]["ether2"], {"rx_bytes": 10**12, "tx_bytes": 10**11, "rx_errors": 3,
                                                    "tx_errors": 0, "rx_drops": 1, "tx_drops": 0, "running": True})

    def test_ros6_scalars(self):
        agent, _ = self.agent(ros7=False)
        info = self.poll(agent)
        self.assertAlmostEqual(info["volt"], 24.1)
        self.assertTrue(40 <= info["temp"] <= 60)
        self.assertEqual(info["version"], "6.49.17")

    def test_core_router_skips_bras_parts(self):
        agent, _ = self.agent()
        info = self.poll(agent, kind="core")
        self.assertTrue(info["ok"])
        self.assertNotIn("ppp", info)
        self.assertNotIn("ifaces", info)

    def test_chosen_interfaces_and_moved_ifindex(self):
        agent, state = self.agent()
        cache = snmp_monitor.RouterCache()
        info = self.poll(agent, chosen=["vlan100-pppoe", "ether3", "missing0"], cache=cache)
        self.assertEqual(sorted(info["ifaces"]), ["ether3", "vlan100-pppoe"])
        self.assertFalse(info["ifaces"]["ether3"]["running"])
        # The router renumbers its interfaces: the cached map is stale, the poll re-walks and still finds it
        state["static"] = [(i + 20, n, t, up) for i, n, t, up in SNMP_STATIC_IFACES]
        info = self.poll(agent, chosen=["vlan100-pppoe"], cache=cache)
        self.assertEqual(list(info["ifaces"]), ["vlan100-pppoe"])
        self.assertEqual(cache.ifindex["vlan100-pppoe"], 26)

    def test_ppp_count_follows_sessions_without_rewalk(self):
        agent, state = self.agent(sessions=10)
        cache = snmp_monitor.RouterCache()
        self.assertEqual(self.poll(agent, cache=cache)["ppp"], 10)
        walked = cache.mapped_at
        state["sessions"] = 250
        self.assertEqual(self.poll(agent, cache=cache)["ppp"], 250)
        self.assertEqual(cache.mapped_at, walked)            # ifNumber alone gave the new count

    def test_unreachable(self):
        info = snmp_monitor.poll("127.0.0.1", 9, "public", "bras", [], snmp_monitor.RouterCache(), timeout=0.2)
        self.assertFalse(info["ok"])
        self.assertTrue(info["error"].startswith("SNMP: "))

    def test_32bit_fallback(self):
        mib = fake_snmp_mib("B", 0, counters(rx=5000), time.time())
        for oid in [o for o in mib if o.startswith("1.3.6.1.2.1.31.1.1.1.6.") or o.startswith("1.3.6.1.2.1.31.1.1.1.10.")]:
            index = oid.rsplit(".", 1)[1]
            mib[f"1.3.6.1.2.1.2.2.1.10.{index}"] = snmp.Counter32(4000)
            del mib[oid]
        agent = FakeSnmpAgent(lambda: mib)
        self.addCleanup(agent.close)
        info = self.poll(agent, chosen=["ether2"])
        self.assertEqual(info["ifaces"]["ether2"]["rx_bytes"], 4000)
        self.assertIsNone(info["ifaces"]["ether2"]["tx_bytes"])

    def test_format_uptime(self):
        self.assertEqual(snmp_monitor.format_uptime(6100), "00:01:01")
        self.assertEqual(snmp_monitor.format_uptime(86400 * 100 + 5), "1d00:00:00")
        self.assertEqual(snmp_monitor.format_uptime(None), "")


class GuiDispatchTest(unittest.TestCase):
    def test_snmp_router_is_polled_over_snmp_and_checks_nat_rarely(self):
        import gui
        agent = FakeSnmpAgent(lambda: fake_snmp_mib("BRAS-1", 5, counters(), time.time()))
        self.addCleanup(agent.close)
        connects = []

        def connect(router, username=None, password=None):
            connects.append(router["name"])
            return FakeRouterOS({"/ip/firewall/nat": []})
        router = {"name": "BRAS-1", "address": "192.0.2.1", "snmp": {"enabled": True, "community": "public"},
                  "pppoe": {"enabled": True}}
        with mock.patch.object(gui, "_snmp_target", return_value=("127.0.0.1", agent.port)), \
                mock.patch.object(gui, "_router_connect", side_effect=connect), \
                mock.patch.object(gui, "_poll_router_api", side_effect=AssertionError("API used")):
            gui._nat_checked.clear()
            first = gui._poll_router("bras", router)
            second = gui._poll_router("bras", router)
            no_pppoe = gui._poll_router("bras", dict(router, pppoe={"enabled": False}))
        self.assertEqual((first["via"], first["ok"], first["ppp"]), ("snmp", True, 5))
        self.assertEqual(first["nat_rules"], [])
        self.assertNotIn("nat_rules", second)                  # NAT checked once per NAT_CHECK_INTERVAL
        self.assertEqual(connects, ["BRAS-1"])
        self.assertTrue(no_pppoe["ok"])
        self.assertNotIn("ppp", no_pppoe)                      # PPPoE not synced: no PPP count

    def test_api_router_still_uses_api(self):
        import gui
        with mock.patch.object(gui, "_poll_router_api", return_value={"via": "api"}) as api:
            self.assertEqual(gui._poll_router("bras", {"name": "x", "address": "a"})["via"], "api")
            self.assertEqual(gui._poll_router("bras", {"name": "x", "snmp": {"enabled": False}})["via"], "api")
        self.assertEqual(api.call_count, 2)


if __name__ == "__main__":
    unittest.main()
