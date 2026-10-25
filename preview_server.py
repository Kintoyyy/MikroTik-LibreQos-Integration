#!/usr/bin/env python3
"""
Dummy preview server: the real LQ-Sync GUI against fake data.

Runs gui.py's Flask app with a sandbox state directory (.preview/), a fake
lqos.conf and sysfs (two 8-queue NICs, 8 CPUs of which 2 are E-cores, so
LibreQoS would use 6 queues), about 1500 seeded subscribers on five fake
BRAS routers and simulated systemd services. No MikroTik, LibreQoS or root
is needed, and nothing outside .preview/ is written.

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
uplink_bandwidth_mbps = 10000
downlink_bandwidth_mbps = 10000
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


# ── Sandbox ─────────────────────────────────────────────────────────────────

def _write_new(path, text):
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(text)


def seed_sandbox(sandbox):
    """Create the sandbox files that are missing; never overwrite edits made in the GUI."""
    libreqos = os.path.join(sandbox, "libreqos")
    netplan = os.path.join(sandbox, "netplan")
    for d in (sandbox, libreqos, netplan):
        os.makedirs(d, exist_ok=True)

    _write_new(os.path.join(sandbox, "config.json"), json.dumps(SEED_CONFIG, indent=4))
    _write_new(os.path.join(sandbox, "lqos.conf"), SEED_LQOS_CONF.format(libreqos=libreqos))
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

    # Fake sysfs: ens16/ens17 with 8 tx/rx queues each, CPUs 6-7 are E-cores.
    for nic in ("ens16", "ens17"):
        for i in range(8):
            for kind in ("tx", "rx"):
                os.makedirs(os.path.join(sandbox, "sys", "class", "net", nic, "queues", f"{kind}-{i}"),
                            exist_ok=True)
    atom = os.path.join(sandbox, "sys", "devices", "cpu_atom")
    os.makedirs(atom, exist_ok=True)
    _write_new(os.path.join(atom, "cpus"), "6-7\n")
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


def add_dynamic(db, rng, router, ips, scan_time):
    from rate_resolver import RateResolver
    source = _pick(rng, SOURCES)
    dl, ul = _pick(rng, PLANS)
    mac = _mac(rng)
    code = {"pppoe": f"PPP-{router.lower()}-{rng.getrandbits(24):06x}",
            "hotspot": f"HS-{mac}",
            "dhcp": "DHCP-" + mac.replace(":", "")}[source]
    rx_max, tx_max = RateResolver.calculate_max_rates(dl, ul)
    rx_min, tx_min = RateResolver.calculate_min_rates(rx_max, tx_max)
    comment = RateResolver.build_comment(source, f"{dl}M/{ul}M", False)
    db.upsert_device(code, mac if source != "pppoe" else "", ips.take(), comment, source, router,
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


def fake_scan(db, routers, scan_time, churn, rng):
    """Mark every configured router's devices as seen; seed routers that have none."""
    from device_database import timed_job, prune_jobs
    from settings import SCAN_INTERVAL
    ips = IpPool(db.conn)
    for r in routers:
        name = r["name"]
        with timed_job(db.conn, f"scan {name}", "poller", "updatecsv", SCAN_INTERVAL):
            count = db.conn.execute(
                "SELECT COUNT(*) FROM devices WHERE router = ? AND is_static = 0", (name,)).fetchone()[0]
            if count == 0:
                seeded = random.Random(zlib.crc32(name.encode()))
                for _ in range(120):
                    add_dynamic(db, seeded, name, ips, scan_time)
            db.conn.execute("UPDATE devices SET last_seen = ? WHERE router = ? AND is_static = 0",
                            (scan_time, name))
            for _ in range(churn):
                # One subscriber leaves (remove_inactive deletes it), one joins.
                db.conn.execute(
                    "UPDATE devices SET last_seen = ? WHERE code = (SELECT code FROM devices "
                    "WHERE router = ? AND is_static = 0 ORDER BY RANDOM() LIMIT 1)", (scan_time - 1, name))
                add_dynamic(db, rng, name, ips, scan_time)
    db.conn.commit()
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
    def __init__(self, units, on_restart):
        now = datetime.datetime.now().astimezone().strftime("%a %Y-%m-%d %H:%M:%S %Z")
        self.units = {f"{u}.service": {"active": "active", "enabled": "enabled", "since": now, "pid": 4000 + i}
                      for i, u in enumerate(units)}
        self.units["wan_service.service"].update(active="inactive", enabled="disabled", pid=0)
        self.on_restart = on_restart

    def _unit(self, name):
        name = name if name.endswith(".service") else name + ".service"
        return self.units.setdefault(name, {"active": "inactive", "enabled": "disabled", "since": "", "pid": 0})

    def _start(self, unit):
        u = self._unit(unit)
        u.update(active="active", since=datetime.datetime.now().astimezone().strftime("%a %Y-%m-%d %H:%M:%S %Z"),
                 pid=random.randint(5000, 60000))
        if unit.startswith("updatecsv"):
            self.on_restart()

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


def fake_poll_router(kind, router):
    name = router.get("name") or router.get("address", "")
    rng = random.Random(f"{name}{int(time.time() // 60)}")
    return {"name": name, "kind": kind, "address": router.get("address", ""), "ok": True, "error": "",
            "at": time.time(), "cpu": rng.uniform(4, 45), "mem": rng.uniform(20, 60),
            "temp": rng.uniform(38, 55), "volt": 24.1, "uptime": "3w2d04:11:09", "identity": name,
            "version": "7.16.2 (stable)", "board": "CCR2116-12G-4S+", "api_ms": rng.uniform(4, 20)}


def install_fakes(gui, ring, systemd, netplan):
    from flask import jsonify, request

    def raise_preview(*_a, **_k):
        raise RuntimeError("preview: routers and root actions are simulated")

    def restart_libreqos(action="restart"):
        systemd(action, "lqosd.service")
        systemd("restart", "lqos_scheduler.service")
        return {"ok": True, "lqosd": "active", "lqos_scheduler": "active"}

    gui._systemctl = systemd
    gui._restart_libreqos = restart_libreqos
    gui._journal_tail = lambda unit, lines=15: ring.tail(unit, lines)
    gui._connect_router_api = raise_preview
    gui._as_root = raise_preview
    gui._poll_router = fake_poll_router
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
    ap.add_argument("--interval", type=int, default=600, help="seconds between background sync cycles")
    ap.add_argument("--churn", type=int, default=0, help="subscribers leaving/joining per router per cycle")
    args = ap.parse_args()

    sandbox = os.path.abspath(args.dir)
    if args.reset and os.path.isdir(sandbox):
        shutil.rmtree(sandbox)
    netplan = seed_sandbox(sandbox)

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
    from device_database import DeviceDatabase
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

    def rebuild(reason):
        """One sync cycle, as updatecsv.py's main loop runs it, against the fake routers."""
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
                t = time.time()
                fake_scan(cycle_db, cfg.routers, t, args.churn if reason == "timer" else 0, churn_rng)
                updatecsv.build_and_apply(cycle_db, NodeAssigner(updatecsv.NETWORK_JSON), cfg, t,
                                          [r["name"] for r in cfg.routers])
            except Exception as e:
                log.error(f"preview: sync cycle failed: {e}")
            finally:
                cycle_db.close()

    import gui
    systemd = FakeSystemd(gui.MANAGED_SERVICES + ["lqosd", "lqos_scheduler"], lambda: rebuild("restart"))
    install_fakes(gui, ring, systemd, netplan)
    rebuild("startup")

    def timer():
        while True:
            time.sleep(max(30, args.interval))
            rebuild("timer")

    threading.Thread(target=timer, daemon=True).start()
    if gui.HAS_PSUTIL:
        threading.Thread(target=gui._monitor_loop, args=(gui._stats_job, "stats"), daemon=True).start()
    threading.Thread(target=gui._monitor_loop, args=(gui._poll_routers, "router polls"), daemon=True).start()

    print(f"\n  LQ-Sync preview  →  http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{args.port}"
          f"\n  password: admin    sandbox: {sandbox}\n", flush=True)
    gui.app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
