"""
Live LibreQoS queue statistics from tc, for the Overview's queue view.

LibreQoS builds one HTB tree per CPU queue on each shaping interface: an mq
root (7FFF:), and per queue q an HTB qdisc with major q+1 whose root class
X:1 carries that core's traffic (X:2 is the default class). Under it come a
class per network node and per circuit, each circuit with a cake / fq_codel
leaf qdisc. Download is shaped on the interface facing the subscribers
(to_network), upload on the one facing the internet (to_internet); on a
stick both share one interface and upload uses majors q+1+queues.

QueueSampler reads `tc -s class show` and `tc -s qdisc show` every few
seconds, turns byte and drop counters into rates, and names the classes from
LibreQoS's own records: queuingStructure.json (network nodes with their
circuits and class ids) and statsByCircuit.json (the flat circuit list).
Without them every leaf class is shown by its class id.

A queue's traffic is read three ways — the per-CPU HTB qdisc's counters, its
root class X:1, and the sum of its leaf classes — and the largest is used,
so a counter the kernel does not keep up to date cannot hide the traffic.
"""
import json
import re
import subprocess
import time
from collections import defaultdict, deque
from pathlib import Path

ACTIVE_BPS = 100_000            # a circuit moving more than this (down + up) counts as active
SATURATED = 0.9                 # … and over this share of its download plan as saturated
TOP_CIRCUITS = 25
MAX_GAP = 15                    # seconds: an older previous sample is not used for rates (at least)
STATE_TTL = 60                  # seconds a found / missing state-file path is trusted
MQ_MAJOR = 0x7FFF
# Share-of-plan bands of the Overview's histogram: [low, high) of download / plan
UTIL_BANDS = [(0, .1), (.1, .25), (.25, .5), (.5, .75), (.75, .9), (.9, None)]

# A class or qdisc line and the "Sent" line tc -s prints right after it
_CLASS = re.compile(r"^class htb (\S+) (?:parent (\S+)|root)([^\n]*)\n Sent (\d+) bytes (\d+) pkt \(dropped (\d+)", re.M)
_QDISC = re.compile(r"^qdisc (\S+) (\S+) (?:parent (\S+)|root)[^\n]*\n Sent (\d+) bytes (\d+) pkt \(dropped (\d+)", re.M)
_LEAF = re.compile(r" leaf (\S+)")


def parse_handle(text):
    """'3:5', '0x3:0x5' or '3:' → (3, 5) / (3, None); None if unreadable."""
    if not isinstance(text, str) or ":" not in text:
        return None
    major, minor = text.split(":", 1)
    try:
        return int(major, 16), (int(minor, 16) if minor else None)
    except ValueError:
        return None


def parse_classes(text):
    """`tc -s class show` → {(major, minor): {parent, leaf, bytes, packets, drops}} of the HTB classes."""
    out = {}
    for m in _CLASS.finditer(text):
        cid = parse_handle(m.group(1))
        if cid:
            leaf = _LEAF.search(m.group(3))
            out[cid] = {"parent": parse_handle(m.group(2)), "leaf": leaf.group(1) if leaf else "",
                        "bytes": int(m.group(4)), "packets": int(m.group(5)), "drops": int(m.group(6))}
    return out


def parse_qdiscs(text):
    """`tc -s qdisc show` → {parent classid: {kind, bytes, drops}} of the qdiscs hanging off a class."""
    out = {}
    for m in _QDISC.finditer(text):
        parent = parse_handle(m.group(3)) if m.group(3) else None
        if parent and parent[1] is not None:
            out[parent] = {"kind": m.group(1), "bytes": int(m.group(4)), "drops": int(m.group(6))}
    return out


# ── LibreQoS's records of what each class is ────────────────────────────────

def find_state_file(libreqos_dir, name):
    """Where LibreQoS keeps queuingStructure.json / statsByCircuit.json (it moved between versions)."""
    d = Path(libreqos_dir)
    for base in (d, d / "state" / "shaping", d / "state" / "stats", d / "state",
                 Path("/var/lib/libreqos/shaping"), Path("/var/lib/libreqos/stats"), Path("/var/lib/libreqos")):
        p = base / name
        if p.is_file():
            return p
    for pattern in (f"*/{name}", f"*/*/{name}"):
        found = sorted(d.glob(pattern))
        if found:
            return found[0]
    return None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


class Mapping:
    """Which circuit or network node each class is, from LibreQoS's JSON files."""
    def __init__(self):
        self.circuits = {}            # download classid (major, minor) → {id, name, parent, plan_*, down, up}
        self.node_parent = {}         # node name → parent node name (None at the top)
        self.node_cap = {}            # node name → (download Mbps, upload Mbps) it is shaped to
        self.top_of_major = {}        # download major → [top-level node names]
        self.up_major = {}            # download major → upload major
        self.source = []

    def load(self, structure_path, circuits_path):
        if structure_path:
            try:
                data = json.loads(Path(structure_path).read_text())
            except (OSError, ValueError):
                data = None
            if isinstance(data, dict):
                self._walk(data.get("Network", data), None)
                self.source.append(Path(structure_path).name)
        if circuits_path:
            try:
                rows = json.loads(Path(circuits_path).read_text())
            except (OSError, ValueError):
                rows = None
            if isinstance(rows, list):
                for c in rows:
                    self._add_circuit(c, None)
                self.source.append(Path(circuits_path).name)
        return self

    def _add_circuit(self, c, node):
        if not isinstance(c, dict):
            return
        down = parse_handle(c.get("classid"))
        if not down or down in self.circuits:
            return
        self.circuits[down] = {
            "id": str(c.get("circuitID", "")), "name": str(c.get("circuitName") or c.get("circuitID") or ""),
            "parent": str(c.get("ParentNode") or c.get("parentNode") or node or ""),
            "plan_down": _num(c.get("maxDownload")), "plan_up": _num(c.get("maxUpload")),
            "down": down, "up": parse_handle(c.get("up_classid")),
        }

    def _walk(self, nodes, parent):
        if not isinstance(nodes, dict):
            return
        for name, node in nodes.items():
            if not isinstance(node, dict):
                continue
            self.node_parent[name] = parent
            self.node_cap[name] = (_num(node.get("downloadBandwidthMbps")), _num(node.get("uploadBandwidthMbps")))
            cid = parse_handle(node.get("classid"))
            if parent is None and cid:
                self.top_of_major.setdefault(cid[0], []).append(name)
                up = parse_handle(node.get("up_classid"))
                if up:
                    self.up_major[cid[0]] = up[0]
            for c in node.get("circuits") or []:
                self._add_circuit(c, name)
            self._walk(node.get("children"), name)

    def queue_name(self, major):
        names = self.top_of_major.get(major) or []
        if not names:
            return f"queue {major}"
        return names[0] if len(names) == 1 else f"{names[0]} +{len(names) - 1}"

    def path(self, node):
        """Node names from the top-level node down to node."""
        out, seen = [], set()
        while node and node not in seen:
            seen.add(node)
            out.append(node)
            node = self.node_parent.get(node)
        return out[::-1]


# ── Sampling ────────────────────────────────────────────────────────────────

def run_tc(args):
    try:
        r = subprocess.run(["tc", *args], capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        raise RuntimeError("tc is not installed")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"tc {' '.join(args)} timed out")
    if r.returncode != 0:
        raise RuntimeError(f"tc {' '.join(args)}: {r.stderr.strip() or 'failed'}")
    return r.stdout


def _rate(now, before, dt):
    return (now - before) * 8 / dt if before is not None and now >= before else 0.0


class QueueSampler:
    """
    Rates of LibreQoS's queues from two tc samples. Call sample() every few
    seconds; `latest` is the newest view for the GUI.

    libreqos_dir() and interfaces() are called on every sample, so edits to
    the settings or lqos.conf apply without a restart; interfaces() returns
    (download interface, upload interface, on a stick).
    """
    def __init__(self, libreqos_dir, interfaces, history=90, runner=None):
        self.libreqos_dir = libreqos_dir
        self.interfaces = interfaces
        self.runner = runner or run_tc
        self.prev = None                      # (time, {"down": parsed, "up": parsed})
        self.history = deque(maxlen=history)
        self.took = 0.0                       # seconds the last sample took
        self.latest = {"available": False, "error": "no sample yet"}
        self._paths, self._paths_at = (None, None), 0.0
        self._mapping, self._mapping_key = Mapping(), None

    def max_gap(self):
        """Oldest previous sample still used for a rate: a slow tc (a huge tree) samples less often."""
        return max(MAX_GAP, self.took * 10 + 5)

    def _load_mapping(self):
        if time.time() - self._paths_at > STATE_TTL:
            d = self.libreqos_dir()
            self._paths = (find_state_file(d, "queuingStructure.json"), find_state_file(d, "statsByCircuit.json"))
            self._paths_at = time.time()
        key = []
        for p in self._paths:
            try:
                key.append((str(p), p.stat().st_mtime) if p else None)
            except OSError:
                key.append(None)
                self._paths_at = 0.0          # gone: look again next time
        if key != self._mapping_key:
            self._mapping = Mapping().load(*[p if k else None for p, k in zip(self._paths, key)])
            self._mapping_key = key
        return self._mapping

    def _read(self, iface):
        return (parse_classes(self.runner(["-s", "class", "show", "dev", iface])),
                parse_qdiscs(self.runner(["-s", "qdisc", "show", "dev", iface])))

    def sample(self):
        started = time.monotonic()
        try:
            self.latest = self._sample()
        finally:
            self.took = time.monotonic() - started
        return self.latest

    def _sample(self):
        down_if, up_if, stick = self.interfaces()
        if not down_if:
            self.prev = None
            return {"available": False, "error": "no shaping interfaces in lqos.conf"}
        t = time.time()
        try:
            data = {"down": self._read(down_if)}
            data["up"] = data["down"] if (stick or not up_if or up_if == down_if) else self._read(up_if)
        except RuntimeError as e:
            self.prev = None
            return {"available": False, "error": str(e), "interfaces": [down_if, up_if]}
        prev, self.prev = self.prev, (t, data)
        if not any(minor == 1 for _, minor in data["down"][0]):
            return {"available": False, "interfaces": [down_if, up_if],
                    "error": f"no LibreQoS queues on {down_if} (is lqosd running and shaping?)"}
        if not prev or t - prev[0] > self.max_gap() or t <= prev[0]:
            return {"available": False, "error": "measuring…", "interfaces": [down_if, up_if]}
        return self._snapshot(t, t - prev[0], data, prev[1], (down_if, up_if), stick)

    @staticmethod
    def _rates(now, before, dt):
        """
        ({classid: (bits/s, drops/s)}, {major: {"qdisc", "root", "leaves"} bits/s}) between two
        parsed samples. A class's bytes are the larger of its own and its leaf qdisc's counters.
        """
        classes, qdiscs = now
        old_c, old_q = before
        out, queues = {}, defaultdict(lambda: {"qdisc": 0.0, "root": 0.0, "leaves": 0.0})
        for cid, c in classes.items():
            o, q, oq = old_c.get(cid), qdiscs.get(cid), old_q.get(cid)
            bps = max(_rate(c["bytes"], o and o["bytes"], dt), _rate(q["bytes"], oq["bytes"], dt) if q and oq else 0.0)
            drops = (q["drops"] - oq["drops"]) / dt if q and oq and q["drops"] >= oq["drops"] else 0.0
            out[cid] = (bps, drops)
            if cid[1] == 1:
                queues[cid[0]]["root"] = bps
            elif c["leaf"]:
                queues[cid[0]]["leaves"] += bps
        for parent, q in qdiscs.items():          # the per-CPU HTB qdiscs hang off mq 7fff:X
            oq = old_q.get(parent)
            if parent[0] == MQ_MAJOR and q["kind"] == "htb" and oq:
                queues[parent[1]]["qdisc"] = _rate(q["bytes"], oq["bytes"], dt)
        return out, queues

    def _snapshot(self, t, dt, data, prev, ifaces, stick):
        m = self._load_mapping()
        down, down_q = self._rates(data["down"], prev["down"], dt)
        up, up_q = self._rates(data["up"], prev["up"], dt)
        roots = sorted(major for major, minor in data["down"][0] if minor == 1)
        if stick:                              # on a stick the upload majors are the second half
            half = len(roots) // 2
            down_roots = roots[:half] or roots
            up_of = {major: m.up_major.get(major, major + half) for major in down_roots}
        else:
            down_roots = roots
            up_of = {major: m.up_major.get(major, major) for major in down_roots}
        per_queue = {major: {"circuits": 0, "active": 0, "saturated": 0, "drops_ps": 0.0} for major in down_roots}

        circuits = []
        if m.circuits:
            for c in m.circuits.values():
                d_bps, d_drops = down.get(c["down"], (0.0, 0.0))
                u_bps, u_drops = up.get(c["up"], (0.0, 0.0)) if c["up"] else (0.0, 0.0)
                circuits.append({"id": c["id"], "name": c["name"], "parent": c["parent"],
                                 "plan_down": c["plan_down"], "plan_up": c["plan_up"],
                                 "down_bps": d_bps, "up_bps": u_bps, "drops_ps": d_drops + u_drops,
                                 "util": d_bps / (c["plan_down"] * 1e6) if c["plan_down"] else 0.0,
                                 "queue": c["down"][0]})
        else:                                  # no LibreQoS records: every leaf class is a circuit
            classes = data["down"][0]
            for cid, (d_bps, d_drops) in down.items():
                if cid[1] is not None and cid[1] >= 3 and classes[cid]["leaf"] and cid[0] in per_queue:
                    circuits.append({"id": "", "name": f"{cid[0]:x}:{cid[1]:x}", "parent": "",
                                     "plan_down": 0, "plan_up": 0, "down_bps": d_bps, "up_bps": 0.0,
                                     "drops_ps": d_drops, "util": 0.0, "queue": cid[0]})

        totals = {"down_bps": 0.0, "up_bps": 0.0, "drops_ps": 0.0, "active": 0, "saturated": 0,
                  "circuits": len(circuits)}
        hist = [0] * len(UTIL_BANDS)
        for c in circuits:
            c["active"] = c["down_bps"] + c["up_bps"] > ACTIVE_BPS
            saturated = c["util"] >= SATURATED
            q = per_queue.get(c["queue"])
            if q is not None:
                q["circuits"] += 1
                q["active"] += c["active"]
                q["saturated"] += saturated
                q["drops_ps"] += c["drops_ps"]
            totals["drops_ps"] += c["drops_ps"]
            totals["active"] += c["active"]
            totals["saturated"] += saturated
            if c["active"] and c["plan_down"]:
                hist[next(i for i, (lo, hi) in enumerate(UTIL_BANDS) if hi is None or c["util"] < hi)] += 1

        queues = []
        for major in down_roots:
            measured = down_q.get(major, {"qdisc": 0.0, "root": 0.0, "leaves": 0.0})
            d_bps = max(measured.values())
            u_bps = max(up_q.get(up_of[major], {"x": 0.0}).values())
            totals["down_bps"] += d_bps
            totals["up_bps"] += u_bps
            queues.append({"major": major, "core": major - 1, "name": m.queue_name(major),
                           "down_bps": d_bps, "up_bps": u_bps, "measured": measured, **per_queue[major]})

        self.history.append((t, totals["down_bps"], totals["up_bps"]))
        busiest = sorted(circuits, key=lambda c: -(c["down_bps"] + c["up_bps"]))
        return {
            "available": True, "error": "", "at": t, "interval": round(dt, 2), "interfaces": list(ifaces),
            "stick": bool(stick), "mapping": " + ".join(m.source), "totals": totals, "queues": queues,
            "top": [{k: v for k, v in c.items() if k != "active"} for c in busiest[:TOP_CIRCUITS]
                    if c["down_bps"] + c["up_bps"] > 0],
            "nodes": self._nodes(m, circuits, {q["major"]: q["name"] for q in queues}),
            "util_bands": [{"low": lo, "high": hi, "circuits": n} for (lo, hi), n in zip(UTIL_BANDS, hist)],
            "history": {"t": [h[0] for h in self.history], "down": [h[1] for h in self.history],
                        "up": [h[2] for h in self.history]},
        }

    @staticmethod
    def _nodes(m, circuits, queue_names):
        """
        Every network node that has circuits, as a tree: its traffic (the sum of
        its circuits'), the capacity LibreQoS shapes it to, and circuit counts.
        """
        stats = {}
        for c in circuits:
            for name in m.path(c["parent"]) if c["parent"] else []:
                s = stats.get(name)
                if s is None:
                    cap = m.node_cap.get(name, (0.0, 0.0))
                    s = stats[name] = {"name": name, "down_bps": 0.0, "up_bps": 0.0, "cap_down": cap[0],
                                       "cap_up": cap[1], "circuits": 0, "active": 0, "saturated": 0,
                                       "cpu": queue_names.get(c["queue"], ""), "children": []}
                s["down_bps"] += c["down_bps"]
                s["up_bps"] += c["up_bps"]
                s["circuits"] += 1
                s["active"] += c["active"]
                s["saturated"] += c["util"] >= SATURATED
        roots = []
        for name, s in stats.items():
            parent = m.node_parent.get(name)
            (stats[parent]["children"] if parent in stats else roots).append(s)
        for s in stats.values():
            s["children"].sort(key=lambda n: -n["down_bps"])
        return sorted(roots, key=lambda n: -n["down_bps"])
