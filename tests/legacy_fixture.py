"""
Seeded device sets and runs of the pre-auto_cpu strategies, shared by
make_golden.py (records the expected output) and test_legacy_strategies.py
(checks the current code still produces it).
"""
import json
import os
import random
import tempfile

from device_database import DeviceDatabase
from node_assigner import NodeAssigner

ROUTERS = [
    {"name": "BRAS-A", "site": "North"},
    {"name": "BRAS-B", "site": "North"},
    {"name": "BRAS-C"},
    {"name": "BRAS-D", "site": "BRAS-C"},   # site named like a router → " (site)" suffix
]
PLANS = [(10, 5), (25, 10), (50, 20), (100, 40), (200, 100), (500, 250)]


def build_db(count=180, seed=1, extra=0):
    """In-memory DB with `count` devices spread over ROUTERS, plus `extra` more."""
    rng = random.Random(seed)
    db = DeviceDatabase(':memory:', os.devnull)
    db.open()
    for i in range(count + extra):
        router = ROUTERS[rng.randrange(len(ROUTERS))]["name"]
        dl, ul = PLANS[rng.randrange(len(PLANS))]
        db.upsert_device(f"PPP-{i:04d}", '', f"100.64.{i // 250}.{i % 250 + 1}", '', 'pppoe',
                         router, dl, ul, 1, 1, 1000.0)
    # A manual device with a hand-typed parent and one with no router
    db.conn.execute(
        "INSERT INTO devices (code, circuit_id, device_id, parent_node, ipv4, download_min_mbps, "
        "upload_min_mbps, download_max_mbps, upload_max_mbps, source, router, is_static, weight) "
        "VALUES ('MAN-1', 'm1c', 'm1d', 'Custom', '10.9.0.1', 1, 1, 100, 100, 'manual', '', 1, 200)")
    db.conn.commit()
    return db


def _parents(conn):
    return dict(conn.execute("SELECT code, parent_node FROM devices ORDER BY code").fetchall())


def run(strategy, queues=4, promote=False, extra_second_run=0):
    """
    Run one strategy on a fresh seeded DB. With extra_second_run, add that
    many devices and run again (exercises sticky CPU assignment).
    Returns {"runs": [{"changed", "network", "parents"}, ...]}.
    """
    tmp = tempfile.mkdtemp(prefix='lqsync-golden-')
    path = os.path.join(tmp, 'network.json')
    assigner = NodeAssigner(path)
    db = build_db()
    runs = []

    def once():
        changed = assigner.assign(db.conn, strategy, [dict(r) for r in ROUTERS], queues, promote)
        network = assigner.read_network_json() if os.path.exists(path) else None
        runs.append({"changed": changed, "network": network, "parents": _parents(db.conn)})

    once()
    if extra_second_run:
        rng = random.Random(99)
        for i in range(extra_second_run):
            dl, ul = PLANS[rng.randrange(len(PLANS))]
            db.upsert_device(f"NEW-{i:03d}", '', f"100.65.0.{i + 1}", '', 'pppoe',
                             ROUTERS[rng.randrange(len(ROUTERS))]["name"], dl, ul, 1, 1, 2000.0)
        db.conn.commit()
        once()
    db.close()
    return {"runs": runs}


SCENARIOS = {
    "cpu_q4_sticky": lambda: run("cpu", queues=4, extra_second_run=25),
    "cpu_q3":        lambda: run("cpu", queues=3),
    "cpu_noqueues":  lambda: run("cpu", queues=None),
    "flat":          lambda: run("flat"),
    "ap_only":       lambda: run("ap_only"),
    "ap_site":       lambda: run("ap_site"),
    "full":          lambda: run("full"),
    "full_promote":  lambda: run("full", promote=True),
}

GOLDEN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'golden')


def golden_path(name):
    return os.path.join(GOLDEN_DIR, f"legacy_{name}.json")


def dump(data):
    return json.dumps(data, indent=1, sort_keys=True)
