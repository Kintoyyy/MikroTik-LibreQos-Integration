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
"""
import json
import re
import subprocess
import time
from collections import deque
from pathlib import Path

ACTIVE_BPS = 100_000            # a circuit moving more than this (down + up) counts as active
SATURATED = 0.9                 # … and over this share of its download plan as saturated
TOP_CIRCUITS = 25
TREE_CIRCUITS = 600             # busiest circuits drawn in the treemap; the rest are summed per node
MAX_GAP = 15                    # seconds: an older previous sample is not used for rates
STATE_TTL = 60                  # seconds a found / missing state-file path is trusted

_CLASS = re.compile(r"^class htb (\S+) (?:parent (\S+)|root)(?:.*? leaf (\S+))?")
_QDISC = re.compile(r"^qdisc (\S+) (\S+) (?:parent (\S+)|root)")
_SENT = re.compile(r"Sent (\d+) bytes (\d+) pkt \(dropped (\d+)")


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
    out, current = {}, None
    for line in text.splitlines():
        if line.startswith("class "):
            m = _CLASS.match(line)
            cid = parse_handle(m.group(1)) if m else None
            current = None
            if cid:
                current = out[cid] = {"parent": parse_handle(m.group(2)), "leaf": m.group(3) or "",
                                      "bytes": 0, "packets": 0, "drops": 0}
        elif current is not None:
            s = _SENT.search(line)
            if s:
                current.update(bytes=int(s.group(1)), packets=int(s.group(2)), drops=int(s.group(3)))
    return out


def parse_qdiscs(text):
    """`tc -s qdisc show` → {parent classid: {kind, bytes, drops}} of the qdiscs hanging off a class."""
    out, current = {}, None
    for line in text.splitlines():
        if line.startswith("qdisc "):
            m = _QDISC.match(line)
            parent = parse_handle(m.group(3)) if m and m.group(3) else None
            current = None
            if parent and parent[1] is not None:
                current = out[parent] = {"kind": m.group(1), "bytes": 0, "drops": 0}
        elif current is not None:
            s = _SENT.search(line)
            if s:
                current.update(bytes=int(s.group(1)), drops=int(s.group(3)))
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
        if not prev or t - prev[0] > MAX_GAP or t <= prev[0]:
            return {"available": False, "error": "measuring…", "interfaces": [down_if, up_if]}
        return self._snapshot(t, t - prev[0], data, prev[1], (down_if, up_if), stick)

    @staticmethod
    def _rates(now, before, dt):
        """{classid: (bits/s, drops/s)} between two parsed samples (classes, leaf qdiscs)."""
        classes, qdiscs = now
        old_c, old_q = before
        out = {}
        for cid, c in classes.items():
            o = old_c.get(cid)
            bps = (c["bytes"] - o["bytes"]) * 8 / dt if o and c["bytes"] >= o["bytes"] else 0.0
            q, oq = qdiscs.get(cid), old_q.get(cid)
            drops = (q["drops"] - oq["drops"]) / dt if q and oq and q["drops"] >= oq["drops"] else 0.0
            out[cid] = (bps, drops)
        return out

    def _snapshot(self, t, dt, data, prev, ifaces, stick):
        m = self._load_mapping()
        down = self._rates(data["down"], prev["down"], dt)
        up = self._rates(data["up"], prev["up"], dt)
        roots = sorted(major for major, minor in data["down"][0] if minor == 1)
        if stick:                              # on a stick the upload majors are the second half
            half = len(roots) // 2
            down_roots = roots[:half] or roots
            up_of = {major: m.up_major.get(major, major + half) for major in down_roots}
        else:
            down_roots = roots
            up_of = {major: m.up_major.get(major, major) for major in down_roots}
        per_queue = {major: {"circuits": 0, "active": 0, "drops_ps": 0.0} for major in down_roots}

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
        for c in circuits:
            active = c["down_bps"] + c["up_bps"] > ACTIVE_BPS
            q = per_queue.get(c["queue"])
            if q is not None:
                q["circuits"] += 1
                q["active"] += active
                q["drops_ps"] += c["drops_ps"]
            totals["drops_ps"] += c["drops_ps"]
            totals["active"] += active
            totals["saturated"] += c["util"] >= SATURATED

        queues = []
        for major in down_roots:
            d_bps = down.get((major, 1), (0.0, 0.0))[0]
            u_bps = up.get((up_of[major], 1), (0.0, 0.0))[0]
            totals["down_bps"] += d_bps
            totals["up_bps"] += u_bps
            queues.append({"major": major, "core": major - 1, "name": m.queue_name(major),
                           "down_bps": d_bps, "up_bps": u_bps, **per_queue[major]})

        self.history.append((t, totals["down_bps"], totals["up_bps"], [q["down_bps"] for q in queues]))
        busiest = sorted(circuits, key=lambda c: -(c["down_bps"] + c["up_bps"]))
        return {
            "available": True, "error": "", "at": t, "interval": round(dt, 2), "interfaces": list(ifaces),
            "stick": bool(stick), "mapping": " + ".join(m.source), "totals": totals, "queues": queues,
            "top": [c for c in busiest[:TOP_CIRCUITS] if c["down_bps"] + c["up_bps"] > 0],
            "tree": self._tree(m, circuits, queues),
            "history": {"t": [h[0] for h in self.history], "down": [h[1] for h in self.history],
                        "up": [h[2] for h in self.history], "queues": [h[3] for h in self.history],
                        "names": [q["name"] for q in queues]},
        }

    @staticmethod
    def _tree(m, circuits, queues):
        """
        CPU queue → network nodes → circuits by download rate, for a treemap.
        Idle circuits are left out; past the TREE_CIRCUITS busiest, the rest
        of a node's circuits become one "N more" tile.
        """
        names = {q["major"]: q["name"] for q in queues}
        moving = sorted((c for c in circuits if c["down_bps"] > 0), key=lambda c: -c["down_bps"])
        root = {}
        for rank, c in enumerate(moving):
            tops = m.top_of_major.get(c["queue"]) or []
            path = m.path(c["parent"]) if c["parent"] else []
            if len(tops) == 1 and path and path[0] == tops[0]:
                path = path[1:]                # the queue's only top-level node is the queue itself
            node = root.setdefault(names.get(c["queue"], f"queue {c['queue']}"), {})
            for p in path:
                node = node.setdefault(p, {})
            leaf = node.setdefault("\0", {"shown": [], "more": 0, "more_bps": 0.0})
            if rank < TREE_CIRCUITS:
                leaf["shown"].append(c)
            else:
                leaf["more"] += 1
                leaf["more_bps"] += c["down_bps"]

        def build(name, node, kind):
            leaf = node.get("\0") or {"shown": [], "more": 0, "more_bps": 0.0}
            children = [build(k, v, "node") for k, v in sorted(node.items()) if k != "\0"]
            children += [{"name": c["name"], "value": round(c["down_bps"]), "util": round(c["util"], 3),
                          "plan": c["plan_down"], "kind": "circuit"} for c in leaf["shown"]]
            if leaf["more"]:
                children.append({"name": f"{leaf['more']} more", "value": round(leaf["more_bps"]),
                                 "util": 0, "kind": "others"})
            return {"name": name, "value": sum(ch["value"] for ch in children), "kind": kind,
                    "children": children}

        order = {q["name"]: i for i, q in enumerate(queues)}
        return [build(k, root[k], "queue") for k in sorted(root, key=lambda k: order.get(k, len(order)))]
