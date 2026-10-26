#!/usr/bin/env python3
"""
Dummy preview server: the real LQ-Sync GUI against fake data.

Runs gui.py's Flask app with a sandbox state directory (.preview/), a fake
lqos.conf and sysfs (two 8-queue NICs, 8 CPUs of which 2 are E-cores, so
LibreQoS would use 6 queues), about 1500 seeded subscribers on five fake
BRAS routers and simulated systemd services. The Overview's live queues come
from a simulated LibreQoS (tc output and queuingStructure.json laid out from
the generated network.json), and this host's SNMP from a fake snmpd. No
MikroTik, LibreQoS or root is needed, and nothing outside .preview/ is
written.

    python3 preview_server.py [--reset] [--port 5050] [--devices 1500]

Log in with password "admin". Restarting updatecsv (Save in the Builder,
Routers or Services) runs one sync cycle in-process: a fake scan, then the
real build → network.json and ShapedDevices.csv in .preview/libreqos/.
"""
import argparse
import collections
import datetime
import json
import logging
import math
import os
import random
import shutil
import subprocess
import sys
import threading
import time
import zlib

REPO = os.path.dirname(os.path.abspath(__file__))

# name, address (TEST-NET-1, never routable), share of the subscribers
ROUTERS = [
    ("BRAS-Core", "192.0.2.10", 0.40),
    ("BRAS-East", "192.0.2.11", 0.14),
    ("BRAS-N1",   "192.0.2.12", 0.13),
    ("BRAS-S1",   "192.0.2.13", 0.09),
    ("BRAS-S2",   "192.0.2.14", 0.09),
]
# (download, upload) plan Mbps and how common each is
PLANS = [((10, 5), .15), ((25, 10), .25), ((50, 20), .30), ((100, 40), .20), ((200, 100), .08),
         ((500, 250), .02)]
SOURCES = [("pppoe", .70), ("hotspot", .20), ("dhcp", .10)]
STATIC_SHARE = 0.05


def _service_defaults():
    return {"enabled": True, "default_download_limit": 100, "default_upload_limit": 100}


SEED_CONFIG = {
    "strategy": "auto_cpu",
    "queues": True,
    "promote_to_root": False,
    "bras": [
        {"name": name, "address": addr, "username": "api-read", "password": "preview", "port": 8728,
         "pppoe": _service_defaults(), "hotspot": _service_defaults(),
         "dhcp": {**_service_defaults(), "enabled": False},
         "address_list": {"default_download_limit": 100, "default_upload_limit": 100}}
        for name, addr, _ in ROUTERS
    ],
    "cores": [],
    "wan_assignment": {"enabled": False, "include_hotspot": False, "include_dhcp": False, "interval": 300},
    # url is filled in from --port when the sandbox is seeded
    "webhook": {"enabled": True, "url": ""},
    "topology": {
        "version": 1,
        "nodes": [
            {"id": "n_north", "name": "Region North", "type": "site", "download_mbps": 0, "upload_mbps": 0,
             "virtual": True, "promote": False, "pin_cpu": 0, "routers": [],
             "children": [
                 {"id": "n_tower_n1", "name": "Tower N1", "type": "ap", "download_mbps": 4000,
                  "upload_mbps": 2000, "virtual": False, "promote": False, "pin_cpu": None,
                  "routers": ["BRAS-N1"], "children": []}]},
            {"id": "n_south", "name": "Region South", "type": "site", "download_mbps": 5000,
             "upload_mbps": 5000, "virtual": False, "promote": False, "pin_cpu": None,
             "routers": ["BRAS-S1", "BRAS-S2"], "children": []},
        ],
        "router_options": {},
    },
}

SEED_LQOS_CONF = """\
version = "1.5"
lqos_directory = "{libreqos}"
node_name = "lqos-preview"
packet_capture_time = 10
queue_check_period_ms = 1000
exclude_efficiency_cores = true

[usage_stats]
send_anonymous = false

[tuning]
stop_irq_balance = true
netdev_budget_usecs = 8000
netdev_budget_packets = 300
rx_usecs = 0
tx_usecs = 0
disable_rxvlan = true
disable_txvlan = true
disable_offload = ["gso", "tso", "lro", "sg", "gro"]

[bridge]
use_xdp_bridge = false
to_internet = "ens17"
to_network = "ens16"

[queues]
default_sqm = "cake diffserv4"
monitor_only = false
uplink_bandwidth_mbps = {link}
downlink_bandwidth_mbps = {link}
generated_pn_download_mbps = 3000
generated_pn_upload_mbps = 3000
dry_run = false
sudo = false
use_binpacking = false

[ip_ranges]
ignore_subnets = []
allow_subnets = ["100.64.0.0/10", "10.0.0.0/8"]

[integration_common]
circuit_name_as_address = false
always_overwrite_network_json = true
queue_refresh_interval_mins = 30

[stormguard]
enabled = false
targets = ["CPU0", "CPU1", "CPU2", "CPU3", "CPU4", "CPU5", "CPU6", "CPU7"]
"""

SEED_NETPLAN = {
    "libreqos.yaml": "network:\n  version: 2\n  ethernets:\n    ens16: {}\n    ens17: {}\n",
    "50-cloud-init.yaml": "network:\n  version: 2\n  ethernets:\n    ens18:\n      dhcp4: true\n",
}


# ── Fake RouterOS API ───────────────────────────────────────────────────────

class FakeRouterOS:
    """
    Just enough of a routeros_api connection (pool and api in one) for the
    webhook installer and the PPP lookup: in-memory tables with get(filters),
    add, set, remove, and "run" of a /system script (on_run is called with it).
    Rows use the keys routeros_api returns ("id", "on-up", ...).
    """
    def __init__(self, tables=None, on_run=None):
        self.tables = {path: [dict(r) for r in rows] for path, rows in (tables or {}).items()}
        self.on_run = on_run
        self._next = 1
        for rows in self.tables.values():
            for r in rows:
                r.setdefault("id", self._new_id())

    def _new_id(self):
        self._next += 1
        return f"*{self._next:X}"

    def get_api(self):
        return self

    def disconnect(self):
        pass

    def get_resource(self, path):
        return _FakeResource(self, "/" + path.strip("/"))


class _FakeResource:
    def __init__(self, ros, path):
        self.ros, self.rows = ros, ros.tables.setdefault(path, [])

    @staticmethod
    def _keys(kwargs):
        return {k.replace("_", "-"): v for k, v in kwargs.items()}

    def get(self, **filters):
        filters = self._keys(filters)
        return [dict(r) for r in self.rows if all(str(r.get(k, "")) == str(v) for k, v in filters.items())]

    def add(self, **kwargs):
        row = {"id": self.ros._new_id(), **self._keys(kwargs)}
        self.rows.append(row)
        return row["id"]

    def set(self, id, **kwargs):
        for r in self.rows:
            if r["id"] == id:
                r.update(self._keys(kwargs))
                return []
        raise RuntimeError(f"no such item ({id})")

    def remove(self, id):
        self.rows[:] = [r for r in self.rows if r["id"] != id]
        return []

    def call(self, command, arguments=None, queries=None):
        arguments = self._keys(arguments or {})
        if command == "print":
            rows = self.get(**(queries or {}))
            if "count-only" in arguments:
                return _FakeReply([], {"ret": str(len(rows))})
            if "proplist" in arguments:
                keep = arguments["proplist"].split(",")
                rows = [{k: v for k, v in r.items() if k in keep} for r in rows]
            return _FakeReply(rows, {})
        if command == "run" and self.ros.on_run:
            name = arguments.get("number")
            for r in self.rows:
                if r.get("name") == name or r["id"] == name:
                    self.ros.on_run(r)
        return []


class _FakeReply(list):
    """A reply list with the !done attributes, as routeros_api returns it."""
    def __init__(self, rows, done):
        super().__init__(rows)
        self.done_message = done


# ── Fake SNMP agent ─────────────────────────────────────────────────────────

class FakeSnmpAgent:
    """
    SNMP v2c agent on a local UDP port answering GET / GETNEXT / GETBULK
    from mib(), a callable returning {oid: value} (values typed with the
    snmp module's Counter64, TimeTicks, … classes). Wrong communities get
    no answer, as on a real agent.
    """
    def __init__(self, mib, community="public", host="127.0.0.1"):
        import socket
        self.mib, self.community = mib, community
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((host, 0))
        self.port = self.sock.getsockname()[1]
        self.requests = 0
        threading.Thread(target=self._serve, daemon=True).start()

    def close(self):
        self.sock.close()

    def _serve(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(65535)
            except OSError:
                return
            try:
                reply = self._answer(data)
            except Exception:
                continue
            if reply:
                self.sock.sendto(reply, addr)

    def _answer(self, data):
        import bisect
        import snmp
        _, community, pdu, request_id, a, b, varbinds = snmp.decode_message(data)
        if community != self.community.encode():
            return None
        self.requests += 1
        table = sorted((snmp.oid_tuple(o), v) for o, v in self.mib().items() if v is not None)
        keys, values = [k for k, _ in table], dict(table)

        def after(oid):
            i = bisect.bisect_right(keys, oid)
            return (keys[i], values[keys[i]]) if i < len(keys) else (oid, snmp.END_OF_MIB_VIEW)

        out = []
        if pdu == snmp.GET:
            out = [(o, values.get(snmp.oid_tuple(o), snmp.NO_SUCH_OBJECT)) for o, _ in varbinds]
        elif pdu == snmp.GETNEXT:
            out = [(snmp.oid_str(k), v) for k, v in (after(snmp.oid_tuple(o)) for o, _ in varbinds)]
        elif pdu == snmp.GETBULK:                  # a = non-repeaters (not used here), b = max-repetitions
            for o, _ in varbinds:
                current = snmp.oid_tuple(o)
                for _ in range(max(1, b)):
                    current, v = after(current)
                    out.append((snmp.oid_str(current), v))
                    if v is snmp.END_OF_MIB_VIEW:
                        break
        return snmp.encode_message(community, snmp.RESPONSE, request_id, out)


SNMP_STATIC_IFACES = [   # (ifIndex, name, ifType, running) of a simulated BRAS
    (1, "ether1", 6, True), (2, "ether2", 6, True), (3, "ether3", 6, False), (4, "sfp-sfpplus1", 6, True),
    (5, "sfp-sfpplus2", 6, False), (6, "vlan100-pppoe", 135, True), (7, "bridge-access", 209, True),
]


def fake_snmp_mib(name, sessions, counters, t, ros7=True, static=None):
    """
    The MIB a simulated MikroTik BRAS answers with at time t: system and
    health objects (RouterOS 7 gauge table, or RouterOS 6 scalars), CPU and
    memory, one "<pppoe-…>" interface per active session, and IF-MIB
    counters (counters(iface) → {rx_bytes, …}).
    """
    import snmp
    h = fake_health(name, t)
    static = static if static is not None else SNMP_STATIC_IFACES
    mib = {
        "1.3.6.1.2.1.1.1.0": "RouterOS CCR2116-12G-4S+",
        "1.3.6.1.2.1.1.3.0": snmp.TimeTicks(int(t * 100) % 2 ** 32),
        "1.3.6.1.2.1.1.5.0": name,
        "1.3.6.1.2.1.2.1.0": len(static) + sessions,
        "1.3.6.1.2.1.25.1.1.0": snmp.TimeTicks(200_226_900),         # 3w2d04:11:09
        "1.3.6.1.4.1.14988.1.1.4.4.0": "7.16.2" if ros7 else "6.49.17",
        "1.3.6.1.2.1.25.2.3.1.3.65536": "main memory", "1.3.6.1.2.1.25.2.3.1.4.65536": 1024,
        "1.3.6.1.2.1.25.2.3.1.5.65536": 4194304, "1.3.6.1.2.1.25.2.3.1.6.65536": int(4194304 * h["mem"] / 100),
        "1.3.6.1.2.1.25.2.3.1.3.131072": "system disk", "1.3.6.1.2.1.25.2.3.1.5.131072": 131072,
    }
    for cpu in range(4):
        mib[f"1.3.6.1.2.1.25.3.3.1.2.{cpu + 1}"] = int(h["cpu"]) + cpu
    if ros7:
        for i, (gauge, value, unit) in enumerate([("cpu-temperature", int(h["temp"]), 1), ("fan1-speed", 4200, 2),
                                                  ("voltage", int(h["volt"] * 10), 3)], start=1):
            mib[f"1.3.6.1.4.1.14988.1.1.3.100.1.2.{i}"] = gauge
            mib[f"1.3.6.1.4.1.14988.1.1.3.100.1.3.{i}"] = value
            mib[f"1.3.6.1.4.1.14988.1.1.3.100.1.4.{i}"] = unit
    else:
        mib["1.3.6.1.4.1.14988.1.1.3.8.0"] = int(h["volt"] * 10)
        mib["1.3.6.1.4.1.14988.1.1.3.10.0"] = int(h["temp"] * 10)
    interfaces = [(i, n, typ, up) for i, n, typ, up in static] + \
        [(1000 + s, f"<pppoe-sub{s:04d}>", 23, True) for s in range(sessions)]
    for index, iname, typ, up in interfaces:
        mib[f"1.3.6.1.2.1.31.1.1.1.1.{index}"] = iname
        mib[f"1.3.6.1.2.1.2.2.1.3.{index}"] = typ
        mib[f"1.3.6.1.2.1.2.2.1.8.{index}"] = 1 if up else 2
        if iname.startswith("<"):
            continue
        c = counters(iname) if up else dict.fromkeys(("rx_bytes", "tx_bytes", "rx_errors", "tx_errors",
                                                       "rx_drops", "tx_drops"), 0)
        mib[f"1.3.6.1.2.1.31.1.1.1.6.{index}"] = snmp.Counter64(c["rx_bytes"])
        mib[f"1.3.6.1.2.1.31.1.1.1.10.{index}"] = snmp.Counter64(c["tx_bytes"])
        mib[f"1.3.6.1.2.1.2.2.1.14.{index}"] = snmp.Counter32(c["rx_errors"] % 2 ** 32)
        mib[f"1.3.6.1.2.1.2.2.1.20.{index}"] = snmp.Counter32(c["tx_errors"] % 2 ** 32)
        mib[f"1.3.6.1.2.1.2.2.1.13.{index}"] = snmp.Counter32(c["rx_drops"] % 2 ** 32)
        mib[f"1.3.6.1.2.1.2.2.1.19.{index}"] = snmp.Counter32(c["tx_drops"] % 2 ** 32)
    return mib


# ── Sandbox ─────────────────────────────────────────────────────────────────

def _write_new(path, text):
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(text)


def seed_sandbox(sandbox, port, nic_queues=8, cpus=8, ecores=2, link_mbps=10000):
    """Create the sandbox files that are missing; never overwrite edits made in the GUI."""
    libreqos = os.path.join(sandbox, "libreqos")
    netplan = os.path.join(sandbox, "netplan")
    for d in (sandbox, libreqos, netplan):
        os.makedirs(d, exist_ok=True)

    config = json.loads(json.dumps(SEED_CONFIG))
    config["webhook"]["url"] = f"http://localhost:{port}"
    for r in config["bras"]:
        r["webhook_token"] = os.urandom(16).hex()
        if r["name"] != "BRAS-S2":           # one router stays on the API, to show both
            r["snmp"] = {"enabled": True, "community": "public", "port": 161}
    _write_new(os.path.join(sandbox, "config.json"), json.dumps(config, indent=4))
    _write_new(os.path.join(sandbox, "lqos.conf"), SEED_LQOS_CONF.format(libreqos=libreqos, link=link_mbps))
    _write_new(os.path.join(libreqos, "VERSION_STRING"), "1.5\n")
    for name, text in SEED_NETPLAN.items():
        _write_new(os.path.join(netplan, name), text)

    # settings.json: keep the user's edits, but always point LibreQoS at the
    # sandbox and never run a real LibreQoS update command.
    path = os.path.join(sandbox, "settings.json")
    try:
        with open(path) as f:
            settings = json.load(f)
    except (OSError, ValueError):
        settings = {}
    scanner = settings.setdefault("scanner", {})
    scanner.update(libreqos_dir=libreqos, libreqos_update_command="", scan_interval=600)
    with open(path, "w") as f:
        json.dump(settings, f, indent=4)

    # Fake sysfs: ens16/ens17 with nic_queues tx/rx queues each; the last `ecores` of the CPUs are E-cores.
    for nic in ("ens16", "ens17"):
        qdir = os.path.join(sandbox, "sys", "class", "net", nic, "queues")
        os.makedirs(qdir, exist_ok=True)
        for entry in os.listdir(qdir):
            if int(entry.split("-")[1]) >= nic_queues:
                os.rmdir(os.path.join(qdir, entry))
        for i in range(nic_queues):
            for kind in ("tx", "rx"):
                os.makedirs(os.path.join(qdir, f"{kind}-{i}"), exist_ok=True)
    atom = os.path.join(sandbox, "sys", "devices", "cpu_atom")
    os.makedirs(atom, exist_ok=True)
    with open(os.path.join(atom, "cpus"), "w") as f:
        f.write(f"{cpus - ecores}-{cpus - 1}\n" if 0 < ecores < cpus else "\n")
    return netplan


# ── Fake subscribers ────────────────────────────────────────────────────────

class IpPool:
    """Unused addresses from 100.64.0.0/10, in order."""
    def __init__(self, conn):
        self.used = {ip for (ip,) in conn.execute("SELECT ipv4 FROM devices WHERE ipv4 IS NOT NULL")}
        self.next = 1

    def take(self):
        while True:
            n = self.next
            self.next += 1
            ip = f"100.{64 + (n >> 16) % 64}.{(n >> 8) & 255}.{n & 255}"
            if n & 255 and ip not in self.used:
                self.used.add(ip)
                return ip


def _pick(rng, weighted):
    return rng.choices([v for v, _ in weighted], [w for _, w in weighted])[0]


def _mac(rng):
    return ":".join(f"{rng.randrange(256):02X}" for _ in range(6))


def add_dynamic(db, rng, router, ips, scan_time, ppp_user=None, ip=None):
    """A random subscriber on router; ppp_user/ip make it that PPPoE user (a webhook "up")."""
    from rate_resolver import RateResolver
    source = "pppoe" if ppp_user else _pick(rng, SOURCES)
    dl, ul = _pick(rng, PLANS)
    mac = _mac(rng)
    code = {"pppoe": f"PPP-{ppp_user or router.lower() + '-' + format(rng.getrandbits(24), '06x')}",
            "hotspot": f"HS-{mac}",
            "dhcp": "DHCP-" + mac.replace(":", "")}[source]
    if not ip or ip in ips.used:
        ip = ips.take()
    ips.used.add(ip)
    rx_max, tx_max = RateResolver.calculate_max_rates(dl, ul)
    rx_min, tx_min = RateResolver.calculate_min_rates(rx_max, tx_max)
    comment = RateResolver.build_comment(source, f"{dl}M/{ul}M", False)
    db.upsert_device(code, mac if source != "pppoe" else "", ip, comment, source, router,
                     rx_max, tx_max, rx_min, tx_min, scan_time)


def seed_devices(db, count, seed):
    rng = random.Random(seed)
    ips = IpPool(db.conn)
    now = time.time()
    routers = [(name, share) for name, _, share in ROUTERS]
    static = int(count * STATIC_SHARE)
    for _ in range(count - static):
        add_dynamic(db, rng, _pick(rng, routers), ips, now)
    for i in range(static):
        dl, ul = _pick(rng, PLANS)
        cid, did = db._new_ids()
        # Half are loose manual devices (no router), half hang off a site node.
        parent = "Tower N1" if i % 2 else ""
        db.conn.execute("""
            INSERT INTO devices (code, circuit_id, device_id, parent_node, mac, ipv4, ipv6,
                download_min_mbps, upload_min_mbps, download_max_mbps, upload_max_mbps,
                comment, source, router, last_seen, is_static, weight, core_name, wan_name)
            VALUES (?, ?, ?, ?, ?, ?, NULL, 1, 1, ?, ?, ?, 'address_list', '', ?, 1, ?, '', '')
        """, (f"MANUAL-{i:03d}", cid, did, parent, _mac(rng), ips.take(), dl, ul,
              "manual | preview", now, dl + ul))
    db.conn.commit()


class FakeScanner:
    """
    RouterScanner stand-in for the simulated routers. A full scan marks the
    router's devices as seen (seeding a router that has none); the webhook
    fast path treats each user's latest webhook event as the router's
    session table: "up" adds the PPPoE user, "down" removes it.
    """
    def __init__(self, db, rng, churn=0):
        self.db, self.rng, self.churn = db, rng, churn
        self.ips = IpPool(db.conn)
        self.last_error = ""

    def scan_router(self, router, scan_time):
        db, name = self.db, router["name"]
        count = db.conn.execute(
            "SELECT COUNT(*) FROM devices WHERE router = ? AND is_static = 0", (name,)).fetchone()[0]
        if count == 0:
            seeded = random.Random(zlib.crc32(name.encode()))
            for _ in range(120):
                add_dynamic(db, seeded, name, self.ips, scan_time)
        db.conn.execute("UPDATE devices SET last_seen = ? WHERE router = ? AND is_static = 0",
                        (scan_time, name))
        for _ in range(self.churn):
            # One subscriber leaves (remove_inactive deletes it), one joins.
            db.conn.execute(
                "UPDATE devices SET last_seen = ? WHERE code = (SELECT code FROM devices "
                "WHERE router = ? AND is_static = 0 ORDER BY RANDOM() LIMIT 1)", (scan_time - 1, name))
            add_dynamic(db, self.rng, name, self.ips, scan_time)
        db.conn.commit()
        return True, True

    def apply_ppp_events(self, router, users, scan_time):
        db, name = self.db, router["name"]
        for user in users:
            last = db.conn.execute(
                "SELECT event, ip FROM webhook_events WHERE router = ? AND user = ? "
                "AND event IN ('up', 'down') ORDER BY id DESC LIMIT 1", (name, user)).fetchone()
            code = f"PPP-{user}"
            if last and last[0] == "up":
                if db.conn.execute("SELECT 1 FROM devices WHERE code = ?", (code,)).fetchone():
                    db.conn.execute("UPDATE devices SET last_seen = ? WHERE code = ?", (scan_time, code))
                else:
                    add_dynamic(db, self.rng, name, self.ips, scan_time, ppp_user=user, ip=last[1])
            else:
                db.remove_device(code, name)
        db.conn.commit()
        return True, True


def fake_scan(db, routers, scan_time, churn, rng):
    """A full scan of every configured router, recorded like updatecsv records its scans."""
    from device_database import timed_job, prune_jobs
    from settings import SCAN_INTERVAL
    scanner = FakeScanner(db, rng, churn)
    for r in routers:
        with timed_job(db.conn, f"scan {r['name']}", "poller", "updatecsv", SCAN_INTERVAL):
            scanner.scan_router(r, scan_time)
    prune_jobs(db.conn, "updatecsv", "poller", [f"scan {r['name']}" for r in routers])


# ── Fakes for gui.py ────────────────────────────────────────────────────────

class RingLog(logging.Handler):
    """journalctl stand-in: the last lines each service logged, in short-iso format."""
    def __init__(self):
        super().__init__()
        self.lines = collections.defaultdict(lambda: collections.deque(maxlen=5000))

    def emit(self, record):
        unit = "gui" if record.name.startswith(("werkzeug", "flask", "gui")) else "updatecsv"
        ts = datetime.datetime.fromtimestamp(record.created).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        self.lines[unit].append(f"{ts} lqos-preview {unit}[{os.getpid()}]: {record.levelname} - {msg}")

    def tail(self, unit, n):
        return "\n".join(list(self.lines[unit.removesuffix(".service")])[-n:])


class FakeSystemd:
    """systemctl stand-in that keeps unit state in memory."""
    def __init__(self, units, on_restart, on_gui_restart=None):
        now = datetime.datetime.now().astimezone().strftime("%a %Y-%m-%d %H:%M:%S %Z")
        self.units = {f"{u}.service": {"active": "active", "enabled": "enabled", "since": now, "pid": 4000 + i}
                      for i, u in enumerate(units)}
        self.units["wan_service.service"].update(active="inactive", enabled="disabled", pid=0)
        self.on_restart = on_restart
        self.on_gui_restart = on_gui_restart

    def _unit(self, name):
        name = name if name.endswith(".service") else name + ".service"
        return self.units.setdefault(name, {"active": "inactive", "enabled": "disabled", "since": "", "pid": 0})

    def _start(self, unit):
        u = self._unit(unit)
        u.update(active="active", since=datetime.datetime.now().astimezone().strftime("%a %Y-%m-%d %H:%M:%S %Z"),
                 pid=random.randint(5000, 60000))
        if unit.startswith("updatecsv"):
            self.on_restart()
        if unit.startswith("gui") and self.on_gui_restart:
            self.on_gui_restart()

    def __call__(self, *args, **kwargs):
        args = [str(a) for a in args]
        cmd, rest = args[0], [a for a in args[1:] if not a.startswith("--")]
        out = ""
        if cmd == "is-active":
            out = "\n".join(self._unit(u)["active"] for u in rest) + "\n"
        elif cmd == "show":
            u = self._unit(rest[0])
            out = (f"ActiveState={u['active']}\nSubState={'running' if u['active'] == 'active' else 'dead'}\n"
                   f"MainPID={u['pid']}\nExecMainStartTimestamp={u['since']}\nLoadState=loaded\n"
                   f"UnitFileState={u['enabled']}\n")
        elif cmd in ("start", "restart", "try-restart"):
            for unit in rest:
                self._start(unit)
        elif cmd == "stop":
            for unit in rest:
                self._unit(unit).update(active="inactive", pid=0)
        elif cmd in ("enable", "disable"):
            for unit in rest:
                self._unit(unit)["enabled"] = "enabled" if cmd == "enable" else "disabled"
                if "--now" in args:
                    if cmd == "enable":
                        self._start(unit)
                    else:
                        self._unit(unit).update(active="inactive", pid=0)
        elif cmd == "reboot":
            logging.getLogger("gui").warning("preview: host reboot ignored")
        stdout = out if kwargs.get("text") else out.encode()
        return subprocess.CompletedProcess(["systemctl"] + args, 0, stdout, "" if kwargs.get("text") else b"")


FAKE_IFACES = ["sfp-sfpplus1", "ether2"]     # graphed when a BRAS has no monitor_interfaces


def _busy(t):
    """Share of peak traffic at time t: highest around 21:00, lowest around 09:00."""
    hour = time.localtime(t).tm_hour + time.localtime(t).tm_min / 60
    return 0.55 + 0.45 * math.cos(2 * math.pi * (hour - 21) / 24)


def fake_health(name, t):
    rng = random.Random(f"{name}{int(t // 60)}")
    return {"cpu": rng.uniform(4, 20) + 30 * _busy(t), "mem": rng.uniform(30, 45),
            "temp": rng.uniform(40, 46) + 6 * _busy(t), "volt": 24.1, "api_ms": rng.uniform(4, 20)}


def fake_ppp(subs, name, t):
    """Active sessions: most subscribers are online, a few more in the evening."""
    return max(0, int(subs * (0.82 + 0.15 * _busy(t)) + random.Random(f"ppp{name}{int(t // 60)}").randint(-4, 4)))


def fake_rates(subs, name, iface, t):
    """(rx bytes/s, tx bytes/s, rx/tx errors per s, rx/tx drops per s) of a simulated interface."""
    rng = random.Random(f"{name}{iface}{int(t // 60)}")
    down = subs * 1.6e6 / 8 * _busy(t) * rng.uniform(0.85, 1.15)     # ~1.6 Mbps per subscriber at peak
    up = down * rng.uniform(0.10, 0.16)
    uplink = iface.startswith("sfp") or iface == FAKE_IFACES[0]
    rx, tx = (down, up) if uplink else (up, down)                       # the access side mirrors the uplink
    burst = rng.random() < 0.03                                         # now and then a dirty optic or port
    errors = (rng.uniform(0.05, 0.8) if burst else 0.0, rng.uniform(0, 0.05) if burst and not uplink else 0.0)
    drops = (rng.uniform(0.2, 3) if _busy(t) > 0.9 and rng.random() < 0.3 else 0.0, 0.0)
    return rx, tx, errors[0], errors[1], drops[0], drops[1]


class FakeCounters:
    """Ever-growing interface counters of the simulated routers, as /interface reports them."""
    FIELDS = ("rx_bytes", "tx_bytes", "rx_errors", "tx_errors", "rx_drops", "tx_drops")

    def __init__(self):
        self.state = {}       # (router, iface) → (t, [counters as floats])

    def read(self, name, iface, subs, t):
        last_t, values = self.state.get((name, iface), (t - 60, [random.uniform(1e12, 5e12), random.uniform(1e11, 5e11),
                                                                   1000.0, 10.0, 5000.0, 0.0]))
        dt = max(0.0, t - last_t)
        values = [v + r * dt for v, r in zip(values, fake_rates(subs, name, iface, t))]
        self.state[(name, iface)] = (t, values)
        return {f: int(v) for f, v in zip(self.FIELDS, values)}


# ── Fake LibreQoS queues (tc) and this host's snmpd ─────────────────────────

PREVIEW_QUEUES = 6        # what LibreQoS would use here: 8 queues per NIC, 8 CPUs minus 2 E-cores
HEAVY_SHARE = 0.015       # share of subscribers that run flat out at their plan


class FakeShaper:
    """
    LibreQoS's queues as tc shows them, laid out from the sandbox's
    network.json and ShapedDevices.csv the way LibreQoS.py does it (top-level
    nodes round-robin onto CPU queues, an HTB class per node and circuit with
    a cake leaf), with traffic that follows each plan. Also writes the
    queuingStructure.json LibreQoS keeps, which names the classes.
    """
    def __init__(self, libreqos, down_if="ens16", up_if="ens17", queues=PREVIEW_QUEUES, link_bps=10e9):
        self.dir, self.down_if, self.up_if, self.queues = libreqos, down_if, up_if, queues
        self.link_bps = link_bps          # lqos.conf's downlink_bandwidth_mbps
        self.lock = threading.Lock()
        self.key = None
        self.circuits = []            # [{id, name, parent, dl, ul, major, minor, parent_minor, qdisc, p}]
        self.nodes = {}               # (major, minor) → (parent minor, name, rate Mbps)
        self.bytes = collections.defaultdict(float)      # (direction, circuit id) → bytes sent
        self.dropped = collections.defaultdict(float)
        self.rates = {}               # circuit id → (down bps, up bps) at the last advance
        self.at = None

    def _files(self):
        return os.path.join(self.dir, "network.json"), os.path.join(self.dir, "ShapedDevices.csv")

    def _layout(self):
        import csv
        net_path, csv_path = self._files()
        try:
            with open(net_path) as f:
                network = json.load(f)
        except (OSError, ValueError):
            network = {}
        circuits = {}
        try:
            with open(csv_path, newline="") as f:
                for row in csv.DictReader(f):
                    cid = row.get("Circuit ID") or ""
                    if cid and cid not in circuits:
                        circuits[cid] = {"id": cid, "name": row.get("Circuit Name") or cid,
                                         "parent": row.get("Parent Node") or "",
                                         "dl": float(row.get("Download Max Mbps") or 0),
                                         "ul": float(row.get("Upload Max Mbps") or 0)}
        except OSError:
            pass

        known, stack = set(), [network]
        while stack:
            for name, node in stack.pop().items():
                known.add(name)
                stack.append(node.get("children") or {})
        top, by_parent = dict(network), collections.defaultdict(list)
        for i, c in enumerate(sorted(circuits.values(), key=lambda c: c["id"])):
            if c["parent"] not in known:              # LibreQoS's generated parent nodes
                c["parent"] = f"Generated_PN_{i % self.queues + 1}"
                top.setdefault(c["parent"], {"downloadBandwidthMbps": 3000, "uploadBandwidthMbps": 3000})
            by_parent[c["parent"]].append(c)

        self.circuits, self.nodes, minors = [], {}, {}

        def place(name, node, major, parent_minor):
            minor = minors[major] = minors.get(major, 2) + 1
            cid = f"0x{major:x}:0x{minor:x}"
            self.nodes[(major, minor)] = (parent_minor, name, node.get("downloadBandwidthMbps") or 10000)
            entry = {"classid": cid, "up_classid": cid, "classMajor": hex(major), "classMinor": hex(minor),
                     "cpuNum": hex(major - 1), "parentClassID": f"0x{major:x}:0x{parent_minor:x}",
                     "downloadBandwidthMbps": node.get("downloadBandwidthMbps"),
                     "uploadBandwidthMbps": node.get("uploadBandwidthMbps"), "children": {}, "circuits": []}
            for child in sorted(node.get("children") or {}):
                entry["children"][child] = place(child, node["children"][child], major, minor)
            for c in by_parent.get(name, []):
                cm = minors[major] = minors[major] + 1
                c.update(major=major, minor=cm, parent_minor=minor, qdisc=0x8000 + len(self.circuits),
                         p=zlib.crc32(c["id"].encode()) % 1000 / 1000)
                self.circuits.append(c)
                entry["circuits"].append({"circuitID": c["id"], "circuitName": c["name"], "ParentNode": name,
                                          "classid": f"0x{major:x}:0x{cm:x}", "up_classid": f"0x{major:x}:0x{cm:x}",
                                          "maxDownload": c["dl"], "maxUpload": c["ul"]})
            return entry

        structure = {name: place(name, top[name], i % self.queues + 1, 1) for i, name in enumerate(sorted(top))}
        tmp = os.path.join(self.dir, "queuingStructure.json.tmp")
        with open(tmp, "w") as f:
            json.dump({"Network": structure}, f)
        os.replace(tmp, os.path.join(self.dir, "queuingStructure.json"))

    @staticmethod
    def activity(c, t):
        """Share of its plan circuit c uses at time t: most are light or idle, a few run flat out."""
        p = c["p"]
        if p < HEAVY_SHARE:
            return 0.93 + 0.07 * math.sin(t / 5 + p * 50)
        wave = (0.5 + 0.5 * math.sin(t / (40 + 80 * p) + p * 20)) * (0.5 + 0.5 * math.sin(t / (6 + 9 * p) + p * 7))
        return min(1.0, wave ** 2 * _busy(t) * (0.04 if p > 0.6 else 1.0))

    def advance(self, t=None):
        """Bring the byte counters up to time t (the files are re-read when the sync rewrote them)."""
        t = t or time.time()
        with self.lock:
            key = []
            for path in self._files():
                try:
                    key.append(os.stat(path).st_mtime)
                except OSError:
                    key.append(None)
            if key != self.key:
                self._layout()
                self.key = key
            dt = 0.0 if self.at is None else max(0.0, t - self.at)
            self.at = t
            # Fit the link (45-80% busy, by time of day): flat-out users get at most 40% of it, the rest share what is left.
            acts = [self.activity(c, t) for c in self.circuits]
            heavy = sum(c["dl"] * 1e6 * a for c, a in zip(self.circuits, acts) if c["p"] < HEAVY_SHARE)
            light = sum(c["dl"] * 1e6 * a for c, a in zip(self.circuits, acts) if c["p"] >= HEAVY_SHARE)
            busy = self.link_bps * (0.45 + 0.35 * _busy(t))
            heavy_scale = min(1.0, busy * 0.4 / heavy) if heavy else 1.0
            scale = min(1.0, (busy - heavy * heavy_scale) / light) if light else 1.0
            for c, a in zip(self.circuits, acts):
                a = a * (heavy_scale if c["p"] < HEAVY_SHARE else scale)
                down, up = c["dl"] * 1e6 * a, c["ul"] * 1e6 * a * 0.35
                self.rates[c["id"]] = (down, up)
                self.bytes[("down", c["id"])] += down / 8 * dt
                self.bytes[("up", c["id"])] += up / 8 * dt
                if a > 0.9:
                    self.dropped[c["id"]] += (a - 0.9) * 120 * dt
            return t

    def queue_bps(self):
        """Download bits/s per CPU queue (index = queue) at the last advance."""
        out = [0.0] * self.queues
        with self.lock:
            for c in self.circuits:
                out[c["major"] - 1] += self.rates.get(c["id"], (0, 0))[0]
        return out

    def totals(self):
        """Bytes sent so far: (download, upload)."""
        with self.lock:
            return (sum(self.bytes[("down", c["id"])] for c in self.circuits),
                    sum(self.bytes[("up", c["id"])] for c in self.circuits))

    def tc(self, args):
        """Stand-in for run_tc: `-s class show dev X` and `-s qdisc show dev X`."""
        dev = args[-1]
        if dev not in (self.down_if, self.up_if):
            raise RuntimeError(f'tc {" ".join(args)}: Cannot find device "{dev}"')
        self.advance()
        direction = "down" if dev == self.down_if else "up"
        with self.lock:
            sent = collections.defaultdict(float)
            for c in self.circuits:
                b = self.bytes[(direction, c["id"])]
                sent[(c["major"], c["minor"])] += b
                node = (c["major"], c["parent_minor"])
                while node in self.nodes:
                    sent[node] += b
                    node = (node[0], self.nodes[node][0])
                sent[(c["major"], 1)] += b
            stats = lambda b, d=0: (f" Sent {int(b)} bytes {int(b // 1100)} pkt (dropped {int(d)}, overlimits 0 requeues 0) \n"
                                    " backlog 0b 0p requeues 0\n")
            out = []
            if args[1] == "class":
                for q in range(1, self.queues + 1):
                    out.append(f"class htb {q:x}:1 root rate 10Gbit ceil 10Gbit burst 1600b cburst 1600b \n"
                               + stats(sent[(q, 1)]))
                    out.append(f"class htb {q:x}:2 parent {q:x}:1 prio 3 rate 10Mbit ceil 10Gbit burst 1600b cburst 1600b \n"
                               + stats(0))
                for (major, minor), (parent, _, rate) in sorted(self.nodes.items()):
                    out.append(f"class htb {major:x}:{minor:x} parent {major:x}:{parent:x} prio 3 rate {int(rate)}Mbit "
                               f"ceil {int(rate)}Mbit burst 1600b cburst 1600b \n" + stats(sent[(major, minor)]))
                for c in self.circuits:
                    rate = c["dl"] if direction == "down" else c["ul"]
                    out.append(f"class htb {c['major']:x}:{c['minor']:x} parent {c['major']:x}:{c['parent_minor']:x} "
                               f"leaf {c['qdisc']:x}: prio 0 rate {max(1, int(rate * .8))}Mbit ceil {int(rate * 1.15)}Mbit "
                               "burst 1600b cburst 1600b \n" + stats(sent[(c["major"], c["minor"])]))
            else:
                out.append("qdisc mq 7fff: root \n" + stats(sent[(1, 1)]))
                for q in range(1, self.queues + 1):
                    out.append(f"qdisc htb {q:x}: parent 7fff:{q:x} r2q 10 default 0x2 direct_packets_stat 0 "
                               "direct_qlen 1000\n" + stats(sent[(q, 1)]))
                for c in self.circuits:
                    out.append(f"qdisc cake {c['qdisc']:x}: parent {c['major']:x}:{c['minor']:x} bandwidth unlimited "
                               "diffserv4 triple-isolate nonat nowash no-ack-filter split-gso rtt 100ms raw overhead 0 \n"
                               + stats(sent[(c["major"], c["minor"])], self.dropped[c["id"]] if direction == "down" else 0))
            return "".join(out)


class FakeHost:
    """
    The MIB this host's snmpd (net-snmp) would answer with: UCD raw CPU
    ticks and memory, HOST-RESOURCES per-core load and uptime, IF-MIB
    counters of the shaping interfaces. The cores LibreQoS shapes on follow
    their queue's traffic.
    """
    IFACES = [(1, "lo"), (2, "ens16"), (3, "ens17"), (4, "ens18")]

    def __init__(self, shaper, cores=8):
        self.shaper, self.cores = shaper, cores
        self.ticks = dict.fromkeys(("user", "nice", "system", "idle", "wait", "interrupt", "softirq", "steal"), 0.0)
        self.at = time.time()
        self.boot = self.at - (9 * 86400 + 5 * 3600)
        self.lock = threading.Lock()

    def loads(self, t):
        queues = self.shaper.queue_bps()
        out = []
        for core in range(self.cores):
            idle = 0.03 + 0.02 * math.sin(t / 13 + core)
            bps = queues[core] if core < len(queues) else 0.0
            out.append(min(0.98, idle + bps / 3e9 * 0.85))       # a core shapes about 3 Gbps
        return out

    def mib(self):
        import snmp
        with self.lock:
            t = self.shaper.advance()
            loads = self.loads(t)
            dt, self.at = max(0.0, t - self.at), t
            for load in loads:
                busy, rest = load * 100 * dt, (1 - load) * 100 * dt
                for k, share in (("user", .12), ("system", .13), ("softirq", .72), ("interrupt", .03)):
                    self.ticks[k] += busy * share
                self.ticks["idle"] += rest * .98
                self.ticks["wait"] += rest * .02
            down, up = self.shaper.totals()
        ucd = "1.3.6.1.4.1.2021"
        raw = {"user": 50, "nice": 51, "system": 52, "idle": 53, "wait": 54, "interrupt": 56, "softirq": 61, "steal": 64}
        mib = {
            "1.3.6.1.2.1.1.1.0": "Linux lqos-preview 6.8.0-45-generic #45-Ubuntu SMP x86_64",
            "1.3.6.1.2.1.1.5.0": "lqos-preview",
            "1.3.6.1.2.1.25.1.1.0": snmp.TimeTicks(int((t - self.boot) * 100) % 2 ** 32),
            f"{ucd}.4.5.0": 16 * 1024 * 1024, f"{ucd}.4.6.0": 9_300_000,
            f"{ucd}.4.14.0": 420_000, f"{ucd}.4.15.0": 3_100_000 + int(200_000 * _busy(t)),
        }
        for k, n in raw.items():
            mib[f"{ucd}.11.{n}.0"] = snmp.Counter32(int(self.ticks[k]) % 2 ** 32)
        total = sum(loads)
        for i, minutes in enumerate((1, 5, 15), start=1):
            mib[f"{ucd}.10.1.5.{i}"] = int(total * (1 + 0.05 * i) * 100)
        for core, load in enumerate(loads):
            mib[f"1.3.6.1.2.1.25.3.3.1.2.{196608 + core}"] = int(load * 100)
        counters = {"ens16": (up, down), "ens17": (down, up), "ens18": (t * 2e4, t * 6e4), "lo": (t * 1e3, t * 1e3)}
        for index, name in self.IFACES:
            rx, tx = counters[name]
            mib[f"1.3.6.1.2.1.31.1.1.1.1.{index}"] = name
            mib[f"1.3.6.1.2.1.31.1.1.1.6.{index}"] = snmp.Counter64(int(rx))
            mib[f"1.3.6.1.2.1.31.1.1.1.10.{index}"] = snmp.Counter64(int(tx))
        return mib


def fake_subscribers(db_path, router):
    try:
        import sqlite3
        con = sqlite3.connect(db_path, timeout=5)
        try:
            return con.execute("SELECT COUNT(*) FROM devices WHERE router = ? AND source = 'pppoe'",
                               (router,)).fetchone()[0]
        finally:
            con.close()
    except Exception:
        return 0


def make_fake_poll(db_path, counters):
    def poll(kind, router):
        name = router.get("name") or router.get("address", "")
        t = time.time()
        info = {"name": name, "kind": kind, "address": router.get("address", ""), "ok": True, "error": "",
                "at": t, "via": "api", "uptime": "3w2d04:11:09", "identity": name, "version": "7.16.2 (stable)",
                "board": "CCR2116-12G-4S+", **fake_health(name, t)}
        if kind == "bras":
            subs = fake_subscribers(db_path, name)
            info["ppp"] = fake_ppp(subs, name, t)
            ifaces = [n for n in router.get("monitor_interfaces") or [] if n] or FAKE_IFACES
            info["ifaces"] = {i: {**counters.read(name, i, subs, t), "running": True} for i in ifaces}
        return info
    return poll


def seed_rrd_history(gui, routers, db_path, counters, hours=6):
    """Back-fill the simulated routers' graphs so the preview shows history at once."""
    if not gui.RRDTOOL:
        return
    now = int(time.time()) // 60 * 60
    start = now - hours * 3600
    for router in routers:
        name = router["name"]
        key = gui._router_key("bras", name)
        subs = fake_subscribers(db_path, name)
        series = {key: ("router", lambda t: [fake_health(name, t)[k] for k in gui.RRD_SCHEMAS["router"]]),
                  gui._rrd_ppp(key): ("ppp", lambda t: [fake_ppp(subs, name, t)])}
        for iface in [n for n in router.get("monitor_interfaces") or [] if n] or FAKE_IFACES:
            series[gui._rrd_iface(key, iface)] = (
                "iface", lambda t, i=iface: [v for f, v in counters.read(name, i, subs, t).items()])
        for rrd, (schema, sample) in series.items():
            path = gui._rrd_path(rrd)
            if path.exists():
                continue
            gui._rrd_create(path, schema, start=start - 60)
            rows = [f"{t}:" + ":".join(gui._rrd_value(v) for v in sample(t)) for t in range(start, now, 60)]
            for i in range(0, len(rows), 120):
                subprocess.run([gui.RRDTOOL, "update", str(path)] + rows[i:i + 120],
                               capture_output=True, text=True, timeout=30)


def fake_router_api(name):
    """
    A simulated router for the webhook installer: three PPP profiles (one
    with a script of its own, to show it is kept). Running the installed
    script POSTs a test call to LQ-Sync, the way the real script does.
    """
    def run(script):
        import re
        import urllib.request
        source = script.get("source", "")
        url = re.search(r':local url "([^"]*)"', source)
        token = re.search(r':local token "([^"]*)"', source)
        if not (url and token):
            return
        req = urllib.request.Request(
            url.group(1), data=json.dumps({"event": "test", "service": "ppp"}).encode(), method="POST",
            headers={"Content-Type": "application/json", "X-LQSync-Token": token.group(1)})

        def send():
            try:
                urllib.request.urlopen(req, timeout=5).read()
            except Exception as e:
                logging.getLogger("gui").warning(f"preview: {name} could not call {url.group(1)}: {e}")
        threading.Thread(target=send, daemon=True).start()

    def iface(name, kind="ether", running=True, comment="", dynamic=False):
        return {"name": name, "type": kind, "running": str(running).lower(), "disabled": "false",
                "comment": comment, "dynamic": str(dynamic).lower()}

    return FakeRouterOS({
        "/ppp/profile": [
            {"name": "default", "on-up": "", "on-down": ""},
            {"name": "default-encryption", "on-up": "", "on-down": ""},
            {"name": "PPPoE-plans", "on-up": ':log info ("PPP up " . $user)', "on-down": ""},
        ],
        "/interface": [
            iface("ether1", comment="management"), iface("ether2", comment="access / OLT"),
            iface("ether3", running=False), iface("sfp-sfpplus1", comment="uplink to LibreQoS"),
            iface("sfp-sfpplus2", running=False), iface("vlan100-pppoe", "vlan", comment="PPPoE VLAN"),
            iface("bridge-access", "bridge"),
            iface("<pppoe-alice>", "pppoe-in", dynamic=True), iface("<pppoe-bob>", "pppoe-in", dynamic=True),
        ],
    }, on_run=run)


def install_fakes(gui, ring, systemd, netplan, counters, shaper, cores=8):
    from flask import jsonify, request

    # LibreQoS's queues: tc output from the fake shaper. This host's snmpd: a
    # fake agent, used when host SNMP points at the default 127.0.0.1:161.
    gui._queue_sampler.runner = shaper.tc
    host_agent = FakeSnmpAgent(FakeHost(shaper, cores).mib)
    gui._host_snmp_target = lambda s: (("127.0.0.1", host_agent.port)
                                       if s["address"] in ("127.0.0.1", "localhost") and s["port"] == 161
                                       else (s["address"], s["port"]))

    def raise_preview(*_a, **_k):
        raise RuntimeError("preview: routers and root actions are simulated")

    routers = {}

    def router_connect(router, username=None, password=None):
        if router["name"] not in routers:
            routers[router["name"]] = fake_router_api(router["name"])
        return routers[router["name"]]

    def restart_libreqos(action="restart"):
        systemd(action, "lqosd.service")
        systemd("restart", "lqos_scheduler.service")
        return {"ok": True, "lqosd": "active", "lqos_scheduler": "active"}

    gui._systemctl = systemd
    gui._restart_libreqos = restart_libreqos
    gui._journal_tail = lambda unit, lines=15: ring.tail(unit, lines)
    # Routers with SNMP on are polled by the real SNMP code, against one fake agent each.
    agents = {}

    def snmp_target(router):
        name = router.get("name") or router.get("address", "")
        if name not in agents:
            ros7 = zlib.crc32(name.encode()) % 2 == 0          # some answer like RouterOS 7, some like 6

            def mib(name=name, ros7=ros7):
                t = time.time()
                subs = fake_subscribers(str(gui.DB_PATH), name)
                return fake_snmp_mib(name, fake_ppp(subs, name, t), lambda i: counters.read(name, i, subs, t), t, ros7)
            agents[name] = FakeSnmpAgent(mib)
        return "127.0.0.1", agents[name].port

    api_poll, real_poll = make_fake_poll(str(gui.DB_PATH), counters), gui._poll_router

    def poll(kind, router):
        return real_poll(kind, router) if gui.snmp_monitor.settings(router) else api_poll(kind, router)

    gui._connect_router_api = raise_preview
    gui._router_connect = router_connect
    gui._snmp_target = snmp_target
    gui._as_root = raise_preview
    gui._poll_router = poll
    gui.YAML_FILES["libreqos"] = gui.Path(netplan) / "libreqos.yaml"
    gui.YAML_FILES["network"] = gui.Path(netplan) / "50-cloud-init.yaml"

    def logs(name):
        if name not in gui.MANAGED_SERVICES:
            return jsonify({"ok": False, "error": "unknown service"}), 400
        try:
            n = min(max(int(request.args.get("lines", 500)), 50), 5000)
        except ValueError:
            n = 500
        return jsonify({"ok": True, "logs": ring.tail(name, n) or "-- No entries --"})

    def no_update():
        return jsonify({"ok": False, "error": "Disabled in the preview: it would overwrite this checkout"}), 400

    gui.app.view_functions["service_logs"] = gui.require_auth(logs)
    gui.app.view_functions["update_app"] = gui.require_auth(no_update)
    gui.app.view_functions["rollback_app"] = gui.require_auth(no_update)


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5050)
    ap.add_argument("--dir", default=os.path.join(REPO, ".preview"), help="sandbox directory")
    ap.add_argument("--reset", action="store_true", help="delete the sandbox and seed it again")
    ap.add_argument("--devices", type=int, default=1500, help="subscribers to seed")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--cpus", type=int, default=8, help="CPU count the queue detection sees")
    ap.add_argument("--nic-queues", type=int, default=8, help="tx/rx queues of each fake shaping NIC")
    ap.add_argument("--ecores", type=int, default=2, help="how many of the CPUs are E-cores (excluded)")
    ap.add_argument("--interval", type=int, default=600, help="seconds between background sync cycles")
    ap.add_argument("--churn", type=int, default=0, help="subscribers leaving/joining per router per cycle")
    args = ap.parse_args()

    sandbox = os.path.abspath(args.dir)
    if args.reset and os.path.isdir(sandbox):
        shutil.rmtree(sandbox)
    link_mbps = max(10000, -(-args.devices * 5 // 1000) * 1000)     # about 5 Mbps per subscriber at peak
    netplan = seed_sandbox(sandbox, args.port, args.nic_queues, args.cpus, args.ecores, link_mbps)

    # Before any app import: settings.py, updatecsv.py and gui.py read these at import.
    os.environ["LQSYNC_HOME"] = sandbox
    os.environ["LQSYNC_LQOS_CONF"] = os.path.join(sandbox, "lqos.conf")
    os.environ["LQSYNC_SYSFS"] = os.path.join(sandbox, "sys")
    sys.path.insert(0, REPO)
    os.chdir(sandbox)          # anything relative lands in the sandbox

    # The services log at INFO to stderr (journald); keep that, plus a copy for the Logs view.
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    ring = RingLog()
    logging.getLogger().addHandler(ring)

    import cpu_planner
    import updatecsv
    from device_database import DeviceDatabase, take_webhook_events
    from node_assigner import NodeAssigner
    cpu_planner.CPU_COUNT = args.cpus
    cpu_planner.SYSFS_ROOT = os.environ["LQSYNC_SYSFS"]

    db = DeviceDatabase(updatecsv.DB_FILE, updatecsv.SHAPED_DEVICES_CSV)
    db.open()
    if db.conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 0:
        seed_devices(db, args.devices, args.seed)
        logging.info(f"preview: seeded {args.devices} fake subscribers")
    db.close()

    lock = threading.Lock()
    churn_rng = random.Random(args.seed + 1)
    log = logging.getLogger("updatecsv")

    def rebuild(reason, events=None):
        """
        One sync cycle against the fake routers, as updatecsv.py's main loop
        runs it: a webhook cycle for events, else a full scan (which also
        takes any events still waiting, like the real one).
        """
        with lock:
            log.info(f"preview: sync cycle ({reason})")
            try:
                cfg = updatecsv.read_config_json()
            except updatecsv.ConfigError as e:
                log.error(f"Skipping cycle — {e}")
                return
            cycle_db = DeviceDatabase(updatecsv.DB_FILE, updatecsv.SHAPED_DEVICES_CSV)
            cycle_db.open()     # sqlite connections belong to one thread: one per cycle
            try:
                assigner = NodeAssigner(updatecsv.NETWORK_JSON)
                if events is not None:
                    updatecsv.run_webhook_cycle(cycle_db, FakeScanner(cycle_db, churn_rng), assigner, cfg, events)
                    return
                t = time.time()
                waiting = take_webhook_events(cycle_db.conn)
                if waiting:     # the simulated routers' sessions follow the events they sent
                    scanner = FakeScanner(cycle_db, churn_rng)
                    for r in cfg.routers:
                        users = {e["user"] for e in waiting if e["router"] == r["name"] and e["user"]}
                        if users:
                            scanner.apply_ppp_events(r, users, t)
                fake_scan(cycle_db, cfg.routers, t, args.churn if reason == "timer" else 0, churn_rng)
                updatecsv.build_and_apply(cycle_db, assigner, cfg, t, [r["name"] for r in cfg.routers])
            except Exception as e:
                log.error(f"preview: sync cycle failed: {e}")
            finally:
                cycle_db.close()

    import gui
    def gui_restarted():
        gui._STARTED_AT = time.time()      # the page sees a new GUI process, as after a real restart

    systemd = FakeSystemd(gui.MANAGED_SERVICES + ["lqosd", "lqos_scheduler"], lambda: rebuild("restart"),
                          gui_restarted)
    counters = FakeCounters()
    conf = cpu_planner.load_lqos_conf(os.environ["LQSYNC_LQOS_CONF"])
    link_bps = float((conf.get("queues") or {}).get("downlink_bandwidth_mbps") or 10000) * 1e6
    shaper = FakeShaper(str(gui.libreqos_dir()), queues=cpu_planner.detect_queues(conf)["queues"], link_bps=link_bps)
    install_fakes(gui, ring, systemd, netplan, counters, shaper, args.cpus)
    rebuild("startup")
    try:
        seed_rrd_history(gui, updatecsv.read_config_json().routers, str(gui.DB_PATH), counters)
    except Exception as e:
        log.warning(f"preview: could not back-fill graphs: {e}")

    def sync_loop():
        """updatecsv.py's wait: a full cycle every --interval, webhook cycles as events arrive."""
        import sqlite3
        conn = sqlite3.connect(updatecsv.DB_FILE, timeout=30)
        next_full_at, last_cycle_at = time.time() + max(30, args.interval), 0.0
        while True:
            try:
                events = updatecsv.wait_for_work(conn, next_full_at, last_cycle_at)
                last_cycle_at = time.time()
                if events is None:
                    rebuild("timer")
                    next_full_at = time.time() + max(30, args.interval)
                else:
                    rebuild(f"webhook, {len(events)} event(s)", events)
            except Exception as e:
                log.error(f"preview: sync loop: {e}")
                time.sleep(5)

    threading.Thread(target=sync_loop, daemon=True).start()
    if gui.HAS_PSUTIL:
        threading.Thread(target=gui._monitor_loop, args=(gui._stats_job, "stats"), daemon=True).start()
    threading.Thread(target=gui._monitor_loop, args=(gui._poll_routers, "router polls"), daemon=True).start()

    print(f"\n  LQ-Sync preview  →  http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{args.port}"
          f"\n  password: admin    sandbox: {sandbox}\n", flush=True)
    gui.app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
