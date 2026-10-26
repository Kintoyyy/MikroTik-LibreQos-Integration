"""BRAS monitoring: active PPP sessions and graphed interfaces (counters, rates, RRDs, graphs)."""
import subprocess
import unittest
from unittest import mock

import gui
from preview_server import FakeRouterOS


def iface(name, kind="ether", running=True, **counters):
    row = {"name": name, "type": kind, "running": str(running).lower(), "disabled": "false", "dynamic": "false"}
    row.update({k.replace("_", "-"): str(v) for k, v in counters.items()})
    return row


def ros():
    return FakeRouterOS({
        "/ppp/active": [{"name": f"user{i}"} for i in range(7)],
        "/interface": [
            iface("sfp-sfpplus1", rx_byte=1000, tx_byte=500, rx_error=3, tx_error=0, rx_drop=1, tx_drop=0),
            iface("ether2", rx_byte=10, tx_byte=20),             # no error/drop counters reported
            iface("ether3", running=False),
            iface("vlan100", "vlan", rx_byte=1, tx_byte=1),
        ],
    })


class PppCountTest(unittest.TestCase):
    def test_count_only(self):
        self.assertEqual(gui._ppp_active_count(ros()), 7)

    def test_falls_back_to_listing(self):
        api = ros()
        resource = api.get_resource("/ppp/active")
        original = resource.call

        class NoRet:
            def __init__(self, inner):
                self.inner = inner

            def call(self, command, arguments=None, queries=None):
                reply = original(command, arguments, queries)
                if "count-only" in (arguments or {}):
                    reply.done_message = {}            # router answered without ret
                return reply
        api.get_resource = lambda path: NoRet(resource) if path == "/ppp/active" else FakeRouterOS.get_resource(api, path)
        self.assertEqual(gui._ppp_active_count(api), 7)


class InterfaceTest(unittest.TestCase):
    def test_auto_picks_running_ethernet_ports(self):
        out = gui._read_interfaces(ros(), {"name": "B"})
        self.assertEqual(sorted(out), ["ether2", "sfp-sfpplus1"])
        self.assertEqual(out["sfp-sfpplus1"], {"rx_bytes": 1000, "tx_bytes": 500, "rx_errors": 3,
                                               "tx_errors": 0, "rx_drops": 1, "tx_drops": 0, "running": True})
        self.assertIsNone(out["ether2"]["rx_errors"])

    def test_configured_list_wins_and_missing_are_skipped(self):
        out = gui._read_interfaces(ros(), {"name": "B", "monitor_interfaces": ["vlan100", "gone0"]})
        self.assertEqual(list(out), ["vlan100"])

    def test_rates(self):
        prev = {"at": 100, "ifaces": {"x": {"rx_bytes": 0, "tx_bytes": 1000, "rx_errors": 0, "tx_errors": 5,
                                            "rx_drops": None, "tx_drops": 0}}}
        info = {"at": 160, "ifaces": {"x": {"rx_bytes": 7_500_000, "tx_bytes": 500, "rx_errors": 2,
                                            "tx_errors": 5, "rx_drops": 3, "tx_drops": 0}}}
        gui._add_interface_rates(info, prev)
        c = info["ifaces"]["x"]
        self.assertAlmostEqual(c["rx_bps"], 1_000_000)          # 7.5 MB in 60 s = 1 Mbps
        self.assertIsNone(c["tx_bps"])                           # counter went backwards (reset)
        self.assertAlmostEqual(c["rx_errors_min"], 2)
        self.assertEqual(c["tx_errors_min"], 0)
        self.assertIsNone(c["rx_drops_min"])                     # no previous value

    def test_first_poll_has_no_rates(self):
        info = {"at": 160, "ifaces": {"x": {"rx_bytes": 1}}}
        gui._add_interface_rates(info, None)
        self.assertNotIn("rx_bps", info["ifaces"]["x"])


@unittest.skipUnless(gui.RRDTOOL, "rrdtool is not installed")
class RrdTest(unittest.TestCase):
    ROUTER = {"name": "BRAS 1", "address": "192.0.2.1", "monitor_interfaces": ["sfp-sfpplus1", "ether 2"]}

    def test_values(self):
        self.assertEqual(gui._rrd_value(None), "U")
        self.assertEqual(gui._rrd_value(12345678901234), "12345678901234")
        self.assertEqual(gui._rrd_value(1.5), "1.5000")

    def test_counter_rrd_and_graphs_render(self):
        key = gui._router_key("bras", self.ROUTER["name"])
        gui._rrd_update(key, "router", [10.0, 20.0, 40.0, 24.0, 5.0])
        gui._rrd_update(gui._rrd_ppp(key), "ppp", [123])
        for name in self.ROUTER["monitor_interfaces"]:
            gui._rrd_update(gui._rrd_iface(key, name), "iface", [10**12, 10**11, 5, 0, 7, 0])

        info = subprocess.run([gui.RRDTOOL, "info", str(gui._rrd_path(gui._rrd_iface(key, "sfp-sfpplus1")))],
                              capture_output=True, text=True).stdout
        self.assertIn('ds[rx_bytes].type = "DERIVE"', info)
        self.assertIn("ds[rx_bytes].min = 0", info)
        info = subprocess.run([gui.RRDTOOL, "info", str(gui._rrd_path(gui._rrd_ppp(key)))],
                              capture_output=True, text=True).stdout
        self.assertIn('ds[ppp].type = "GAUGE"', info)

        defs = gui._graph_defs({"bras": [self.ROUTER]})
        ids = [g for g in defs if g.startswith(key)]
        self.assertEqual(ids, [key, f"{key}-ppp", f"{key}-if-sfp-sfpplus1", f"{key}-if-ether_2"])
        for gid in ids:
            d = defs[gid]
            r = subprocess.run([gui.RRDTOOL, "graph", "-", "--imgformat", "PNG", "--start", "end-3h",
                                "--end", "now", "--title", d["title"], "--vertical-label", d["vlabel"]]
                               + gui._GRAPH_STYLE + d["args"], capture_output=True)
            self.assertEqual(r.returncode, 0, r.stderr.decode())
            self.assertTrue(r.stdout.startswith(b"\x89PNG"))
        args = " ".join(defs[f"{key}-if-sfp-sfpplus1"]["args"])
        self.assertIn("CDEF:v0=v0r,8,*,1000000,/", args)
        self.assertIn("TICK:v2#ef444488:1: rx errors /min", args)

    def test_host_interfaces_stacked_graph_and_groups(self):
        for name, (rx, tx) in {"ens16": (10**12, 10**11), "ens17": (10**11, 10**12)}.items():
            gui._rrd_update(gui._rrd_host_iface(name), "hostif", [rx, tx])
        info = subprocess.run([gui.RRDTOOL, "info", str(gui._rrd_path(gui._rrd_host_iface("ens16")))],
                              capture_output=True, text=True).stdout
        self.assertIn('ds[tx_bytes].type = "DERIVE"', info)

        defs = gui._graph_defs({"bras": [self.ROUTER]})
        d = defs["host-ifaces"]
        args = d["args"]
        # received stacks above zero, sent (negated) below it, one colour per interface
        self.assertIn("AREA:i0#3987e5cc:ens16       ", args)
        self.assertIn("AREA:i1#d95926cc:ens17       :STACK", args)
        self.assertIn("CDEF:o1n=o1,-1,*", args)
        self.assertEqual(args[-3:], ["AREA:o0n#3987e588", "AREA:o1n#d9592688:STACK", "HRULE:0#8b949e"])
        r = subprocess.run([gui.RRDTOOL, "graph", "-", "--imgformat", "PNG", "--start", "end-3h", "--end", "now",
                            "--title", d["title"], "--vertical-label", d["vlabel"]] + gui._GRAPH_STYLE + args,
                           capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertTrue(r.stdout.startswith(b"\x89PNG"))

        # this host's graphs first, then each router's together
        key = gui._router_key("bras", self.ROUTER["name"])
        groups = [defs[g]["group"][0] for g in defs]
        self.assertEqual(groups[:groups.index(key)], ["host"] * groups.index(key))
        self.assertEqual({g for g in defs if defs[g]["group"][0] == key},
                         {key, f"{key}-ppp", f"{key}-if-sfp-sfpplus1", f"{key}-if-ether_2"})
        client = gui.app.test_client()
        with client.session_transaction() as s:
            s["authed"] = True
        with mock.patch.object(gui, "_load_config", lambda: {"bras": [self.ROUTER]}):
            listing = client.get("/api/graphs").get_json()
        self.assertEqual([(g["id"], g["kind"]) for g in listing["groups"]], [("host", "host"), (key, "bras")])
        self.assertIn("host-ifaces", [g["id"] for g in listing["groups"][0]["graphs"]])
        self.assertEqual(len(listing["groups"][1]["graphs"]), 4)

    def test_host_interface_counters_prefer_snmpd(self):
        client = mock.Mock(if_prev={"ens16": (5, 6, 0.0), "lo": (1, 1, 0.0), "veth12": (2, 2, 0.0)})
        with mock.patch.dict(gui._host, {"client": client, "status": {"ok": True}}):
            self.assertEqual(gui._host_interface_counters(), {"ens16": (5, 6)})
        with mock.patch.dict(gui._host, {"client": client, "status": None}):       # SNMP on, no answer yet: wait
            self.assertEqual(gui._host_interface_counters(), {})
        if gui.HAS_PSUTIL:                       # snmpd not answering: psutil's counters
            with mock.patch.dict(gui._host, {"client": client, "status": {"ok": False}}):
                self.assertNotIn("lo", gui._host_interface_counters())


if __name__ == "__main__":
    unittest.main()
