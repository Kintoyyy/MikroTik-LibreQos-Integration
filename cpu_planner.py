"""
auto_cpu topology planner (LibreQoS CPU / queue planning).

LibreQoS shapes each top-level network.json node, with everything below it,
on one CPU core (one HTB per queue). Without Insight it hands top-level
nodes to queues round-robin, so exactly N real top-level nodes on N queues
map one-to-one onto cores. This planner:

  - keeps the site → AP → router hierarchy from config.json "topology",
  - dissolves ("promotes to root") a node too heavy for one core, so its
    children can spread across cores (LibreQoS promote_to_root),
  - splits a router too heavy for one core into shards "R", "R #2", ...,
  - packs the resulting subtrees into CPU0..CPU{N-1} with a stable greedy
    bin-pack: items keep their CPU between runs and only move, a few at a
    time, when the load ratio exceeds the rebalance ratio,
  - detects N the way LibreQoS does (NIC tx queues, E-cores, override).

It is pure: no settings import, no database, no file writes. NodeAssigner
(the sync loop) and the GUI (dry-run preview) build Devices and Params and
call plan().
"""
from __future__ import annotations

import heapq
import math
import os
import re
from dataclasses import dataclass, field

try:
    import tomllib
except ImportError:          # Python < 3.11
    tomllib = None

RESERVED = re.compile(r'^(CPU\d+|Generated_PN_\d+)$')
NODE_ID = re.compile(r'^[A-Za-z0-9_.:-]{1,48}$')
NODE_TYPES = ('site', 'ap')
# HTB has 8 levels: CPU bin + 6 node levels + router.
MAX_NODE_DEPTH = 6
MAX_NODES = 1000
# Undo a promotion or a shard split only once the load falls below this
# share of the limit, so a load hovering at the limit does not flap.
STRUCT_HYSTERESIS = 0.8
# A split router's device stays on its CPU while that CPU is at most this
# much over the level every CPU should reach; beyond it, devices move.
SHARD_SLACK = 1.05
DEFAULT_CAPACITY_MBPS = 3000

# The preview server points these at a fake sysfs / CPU count.
SYSFS_ROOT = None    # None → $LQSYNC_SYSFS or /sys, read at call time
CPU_COUNT = None     # None → os.cpu_count()


# ── Inputs ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Device:
    code: str
    router: str
    dl: int
    ul: int
    parent: str = ''
    is_static: bool = False
    has_ip: bool = True


@dataclass(frozen=True)
class Params:
    bins: int
    capacity_mbps: int = DEFAULT_CAPACITY_MBPS
    link_dl_mbps: int = 0
    split_ratio: float = 1.0
    rebalance_ratio: float = 1.5
    move_budget: int = 0
    multiplier: float = 1.1
    sticky: bool = True
    emit_ids: bool = False
    skew_warn_ratio: float = 2.0
    tc_u16_warn: int = 60000
    queue_info: dict | None = None
    n_source: str = ''
    capacity_source: str = ''


@dataclass
class Prior:
    """What the network.json on disk (what LibreQoS loaded) says about the last plan."""
    bin_count: int = 0
    places: dict = field(default_factory=dict)   # node name → (bin name, parent name)
    ids: dict = field(default_factory=dict)      # node "id" → node name


# ── Output ──────────────────────────────────────────────────────────────────

@dataclass
class Plan:
    network: dict
    parents: dict          # device code → parent node
    bins: list
    items: list
    promoted: list
    shards: dict
    moves: list
    warnings: list
    summary: dict
    kinds: dict            # network.json node name → bin | site | ap | router | shard
    node_index: dict       # topology node id → status for the Builder
    router_index: dict     # router name → status for the Builder
    device_changes: int

    def bin_circuits(self) -> dict:
        return {b["name"]: b["circuits"] for b in self.bins}

    def bin_totals(self) -> dict:
        return {b["name"]: (b["dl"], b["ul"]) for b in self.bins}

    def summary_line(self) -> str:
        s = self.summary
        busiest = max(self.bins, key=lambda b: b["dl"], default=None)
        line = (f"auto_cpu: {s['n']} CPUs ({s['n_source']}), {s['totals']['items']} subtrees, "
                f"{len(self.promoted)} promoted, {len(self.shards)} sharded router(s), "
                f"{len(self.moves)} move(s), {self.device_changes} device parent change(s)")
        if busiest:
            skew = "n/a" if s['skew'] is None else f"{s['skew']}x"
            line += f"; busiest {busiest['name']} {busiest['dl']} Mbps of plans, skew {skew}"
        return line

    def to_json(self) -> dict:
        return {
            **self.summary,
            "bins": self.bins, "items": self.items, "promoted": self.promoted,
            "shards": self.shards, "moves": self.moves, "warnings": self.warnings,
            "kinds": self.kinds, "node_index": self.node_index,
            "router_index": self.router_index, "device_changes": self.device_changes,
            "network": self.network,
        }


# ── Config helpers ──────────────────────────────────────────────────────────

def dedupe_router_names(names):
    """Same renaming as read_config_json: a repeated name gets ' 2', ' 3', ..."""
    seen, out = {}, []
    for base in names:
        if base in seen:
            seen[base] += 1
            out.append(f"{base} {seen[base]}")
        else:
            seen[base] = 1
            out.append(base)
    return out


def _is_int(v, minimum=0):
    return isinstance(v, int) and not isinstance(v, bool) and v >= minimum


def topology_errors(topology, router_names):
    """
    Why config.json "topology" is unusable, or None. Unknown router names are
    not an error (a router may have just been removed); the planner warns.
    """
    if topology is None:
        return None
    if not isinstance(topology, dict):
        return "'topology' must be a JSON object"
    nodes = topology.get("nodes", [])
    if not isinstance(nodes, list):
        return "'topology.nodes' must be a list"
    routers = set(router_names)
    ids, names, placed = set(), set(), {}
    count = 0

    def check(node, depth, path):
        nonlocal count
        where = f"topology node {path}"
        if not isinstance(node, dict):
            return f"{where} must be an object"
        count += 1
        if count > MAX_NODES:
            return f"topology has more than {MAX_NODES} nodes"
        nid = node.get("id")
        if not isinstance(nid, str) or not NODE_ID.match(nid):
            return f"{where}: id must be 1-48 letters, digits or _ . : -"
        if nid in ids:
            return f"{where}: duplicate id '{nid}'"
        ids.add(nid)
        name = node.get("name")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 64 \
                or any(ord(c) < 32 for c in name):
            return f"{where}: name must be 1-64 printable characters"
        name = name.strip()
        where = f"topology node '{name}'"
        if name in names:
            return f"{where}: duplicate name"
        if name in routers:
            return f"{where}: a node cannot be named like a router"
        if RESERVED.match(name):
            return f"{where}: names CPU<n> and Generated_PN_<n> are reserved"
        names.add(name)
        if node.get("type", "site") not in NODE_TYPES:
            return f"{where}: type must be 'site' or 'ap'"
        for key in ("download_mbps", "upload_mbps"):
            if not _is_int(node.get(key, 0)):
                return f"{where}: {key} must be a whole number >= 0"
        for key in ("virtual", "promote"):
            if not isinstance(node.get(key, False), bool):
                return f"{where}: {key} must be true or false"
        pin = node.get("pin_cpu")
        if pin is not None and not _is_int(pin):
            return f"{where}: pin_cpu must be empty or a CPU number >= 0"
        rlist = node.get("routers", [])
        if not isinstance(rlist, list) or not all(isinstance(r, str) for r in rlist):
            return f"{where}: routers must be a list of router names"
        for r in rlist:
            if r in placed:
                return f"router '{r}' is in both '{placed[r]}' and '{name}'"
            placed[r] = name
        children = node.get("children", [])
        if not isinstance(children, list):
            return f"{where}: children must be a list"
        if children and depth >= MAX_NODE_DEPTH:
            return f"{where}: nodes nest at most {MAX_NODE_DEPTH} levels deep"
        for i, child in enumerate(children):
            err = check(child, depth + 1, f"{name} › #{i + 1}")
            if err:
                return err
        return None

    for i, node in enumerate(nodes):
        err = check(node, 1, f"#{i + 1}")
        if err:
            return err

    ropts = topology.get("router_options", {})
    if not isinstance(ropts, dict):
        return "'topology.router_options' must be an object"
    for r, o in ropts.items():
        if not isinstance(o, dict):
            return f"router_options '{r}' must be an object"
        pin = o.get("pin_cpu")
        if pin is not None and not _is_int(pin):
            return f"router_options '{r}': pin_cpu must be empty or a CPU number >= 0"
        for key in ("download_mbps", "upload_mbps"):
            if not _is_int(o.get(key, 0)):
                return f"router_options '{r}': {key} must be a whole number >= 0"
        if not isinstance(o.get("shard", True), bool):
            return f"router_options '{r}': shard must be true or false"
    return None


def topology_from_legacy(routers, virtual_sites=False):
    """A topology equivalent to bras[].site (one site node per distinct site)."""
    router_names = {r["name"] for r in routers}
    nodes, by_site, ids = [], {}, set()
    for r in routers:
        site = str(r.get("site") or "").strip()
        if not site:
            continue
        if site not in by_site:
            base = "site-" + (re.sub(r'[^A-Za-z0-9_.-]+', '-', site).strip('-')[:40] or "x")
            nid, i = base, 2
            while nid in ids:
                nid, i = f"{base}-{i}", i + 1
            ids.add(nid)
            by_site[site] = {
                "id": nid, "name": f"{site} (site)" if site in router_names else site,
                "type": "site", "download_mbps": 0, "upload_mbps": 0,
                "virtual": bool(virtual_sites), "promote": False, "pin_cpu": None,
                "routers": [], "children": [],
            }
            nodes.append(by_site[site])
        by_site[site]["routers"].append(r["name"])
    return {"version": 1, "nodes": nodes, "router_options": {}}


def prior_from_network(network):
    """
    Read the previous plan back from network.json. Only a file made of
    top-level CPU0..CPU{k-1} bins counts as an auto_cpu/cpu plan; anything
    else (ap_site, flat, hand-edited) gives an empty Prior.
    """
    prior = Prior()
    if not isinstance(network, dict) or not network:
        return prior
    keys = list(network)
    if sorted(keys) != sorted(f"CPU{i}" for i in range(len(keys))):
        return prior
    prior.bin_count = len(keys)

    def walk(children, bin_name, parent):
        for name, node in (children or {}).items():
            if not isinstance(node, dict):
                continue
            prior.places[name] = (bin_name, parent)
            if node.get("id"):
                prior.ids[node["id"]] = name
            walk(node.get("children"), bin_name, name)

    for b in keys:
        walk((network[b] or {}).get("children"), b, b)
    return prior


def rebalance_moves(loads, members, ratio, budget=None):
    """
    Move whole items from busy bins to the lightest while their load ratio
    is above `ratio`, at most `budget` moves (None = no limit). The busiest
    bin with a movable item gives one up, so a bin holding only pinned items
    does not block the others. Each move takes the item closest to half the
    gap and never more than the gap, so the spread shrinks every step.
    Updates loads and members in place; members lists only movable items.
    members: {bin: [(key, load)]}. Returns [(key, from_bin, to_bin)].
    """
    moves, moved = [], set()
    order = list(loads)
    if len(order) < 2:
        return moves
    while budget is None or len(moves) < budget:
        lo = min(order, key=lambda b: (loads[b], order.index(b)))
        pick = None
        for hi in sorted(order, key=lambda b: (-loads[b], order.index(b))):
            if hi == lo or loads[hi] <= 0 or (loads[lo] > 0 and loads[hi] / loads[lo] <= ratio):
                break
            gap = loads[hi] - loads[lo]
            cands = [(k, w) for k, w in members[hi] if k not in moved and 0 < w < gap]
            if cands:
                pick = (hi,) + min(cands, key=lambda c: (abs(c[1] - gap / 2), c[0]))
                break
        if pick is None:
            break
        hi, k, w = pick
        members[hi].remove((k, w))
        members[lo].append((k, w))
        loads[hi] -= w
        loads[lo] += w
        moved.add(k)
        moves.append((k, hi, lo))
    return moves


# ── lqos.conf / queue detection ─────────────────────────────────────────────

def _mini_toml(text):
    """Scalar keys, one-line string arrays and [section.sub] headers (Python < 3.11)."""
    out = {}
    cur = out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        m = re.match(r'^\[\s*([^\]]+?)\s*\]', line)
        if m:
            cur = out
            for part in m.group(1).split('.'):
                cur = cur.setdefault(part.strip().strip('"'), {})
            continue
        m = re.match(r'^([A-Za-z0-9_-]+)\s*=\s*(.+)$', line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if val.startswith('"') or val.startswith("'"):
            q = val[0]
            end = val.find(q, 1)
            cur[key] = val[1:end] if end > 0 else val[1:]
        elif val.startswith('['):
            cur[key] = [a or b for a, b in re.findall(r'"([^"]*)"|\'([^\']*)\'', val)]
        else:
            val = val.split('#', 1)[0].strip()
            if val in ('true', 'false'):
                cur[key] = val == 'true'
            else:
                try:
                    cur[key] = int(val)
                except ValueError:
                    try:
                        cur[key] = float(val)
                    except ValueError:
                        cur[key] = val
    return out


def load_lqos_conf(path):
    """Parsed lqos.conf, or {} when missing or unreadable."""
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except OSError:
        return {}
    try:
        if tomllib is not None:
            return tomllib.loads(data.decode('utf-8', 'replace'))
        return _mini_toml(data.decode('utf-8', 'replace'))
    except Exception:
        return {}


def shaping_interfaces(conf):
    """(interfaces LibreQoS shapes on, on_a_stick) from lqos.conf, any version."""
    single = conf.get("single_interface") or {}
    if single.get("enabled") and single.get("interface"):
        return [single["interface"]], True
    bridge = conf.get("bridge") or {}
    names = [bridge.get("to_internet"), bridge.get("to_network")]
    names += [m.get("name") for m in bridge.get("interface_mapping") or [] if isinstance(m, dict)]  # 1.4
    return list(dict.fromkeys(n for n in names if n)), False


def _cpulist(text):
    """'0-3,6,8-9' → [0, 1, 2, 3, 6, 8, 9]."""
    cpus = []
    for part in (text or '').strip().split(','):
        part = part.strip()
        if not part:
            continue
        a, _, b = part.partition('-')
        try:
            cpus.extend(range(int(a), int(b or a) + 1))
        except ValueError:
            pass
    return cpus


def detect_queues(conf, sysfs=None, cpu_count=None):
    """
    The queue count LibreQoS will use, worked out like LibreQoS.py does:
    tx queues of each shaping interface ([queues] override_available_queues
    replaces them), capped at the shaping CPUs (E-cores excluded when
    exclude_efficiency_cores), halved on a stick.
    """
    sysfs = sysfs or SYSFS_ROOT or os.environ.get('LQSYNC_SYSFS') or '/sys'
    total = cpu_count or CPU_COUNT or os.cpu_count() or 1
    warnings = []

    ecores = 0
    if conf.get('exclude_efficiency_cores', True):
        try:
            with open(os.path.join(sysfs, 'devices', 'cpu_atom', 'cpus')) as f:
                ecores = len(_cpulist(f.read()))
        except OSError:
            pass
    cpus = max(1, total - ecores)

    ifaces, on_stick = shaping_interfaces(conf)
    try:
        override = int((conf.get('queues') or {}).get('override_available_queues') or 0)
    except (TypeError, ValueError):
        override = 0

    nic = {}
    for iface in ifaces:
        if override > 0:
            nic[iface] = override
            continue
        try:
            nic[iface] = sum(1 for e in os.listdir(os.path.join(sysfs, 'class', 'net', iface, 'queues'))
                             if e.startswith('tx-'))
        except OSError:
            nic[iface] = None
            warnings.append(f"could not read the queues of interface {iface}")

    known = [v for v in nic.values() if v]
    if known:
        queues = min(min(known), cpus)
        source = 'override' if override > 0 else 'sysfs'
    else:
        queues = cpus
        source = 'cpu_count'
        if not ifaces:
            warnings.append("no shaping interfaces in lqos.conf; using the CPU count")
    if on_stick:
        queues //= 2
    if queues < 2:
        warnings.append("LibreQoS needs at least 2 queues/CPUs to shape")
    return {
        "queues": max(1, queues), "source": source, "nic_queues": nic,
        "cpus_total": total, "e_cores": ecores, "shaping_cpus": cpus,
        "override": override, "on_a_stick": on_stick, "interfaces": ifaces,
        "warnings": warnings,
    }


def lqos_conf_warnings(conf, bins):
    """lqos.conf settings that work against one CPU node per core. Returns text lines."""
    out = []
    if (conf.get('queues') or {}).get('use_binpacking'):
        out.append("[queues] use_binpacking = true: LibreQoS (with Insight) re-packs the "
                   "CPU nodes auto_cpu already balanced; set it to false.")
    targets = (conf.get('stormguard') or {}).get('targets') or []
    extra = [t for t in targets if isinstance(t, str) and re.fullmatch(r'CPU\d+', t) and int(t[3:]) >= bins]
    if extra:
        out.append(f"[stormguard] targets {', '.join(extra)} do not exist: auto_cpu writes "
                   f"CPU0..CPU{bins - 1}.")
    links = (conf.get('treeguard') or {}).get('links') or {}
    if links.get('enabled') and links.get('top_level_auto_virtualize'):
        out.append("[treeguard.links] top_level_auto_virtualize = true can virtualize an idle "
                   "CPU node and move its subtrees onto other cores; set it to false.")
    return out


def resolve_params(conf, queues_cfg, ts, emit_ids=False, sysfs=None, cpu_count=None, n_source=None):
    """
    Params from lqos.conf, config.json "queues" and settings topology_settings().
    queues_cfg: True / None / 'auto' → detected queue count; an int → that many bins.
    """
    qi = detect_queues(conf, sysfs, cpu_count)
    if queues_cfg is None or isinstance(queues_cfg, bool) or queues_cfg == 'auto':
        bins, source = qi["queues"], qi["source"]
    else:
        bins, source = max(1, int(queues_cfg)), n_source or 'config'

    qsec = conf.get('queues') or {}
    capacity, cap_source = int(ts.get("cpu_capacity_mbps") or 0), "settings cpu_capacity_mbps"
    if capacity <= 0:
        try:
            capacity = int(qsec.get('generated_pn_download_mbps') or 0)
        except (TypeError, ValueError):
            capacity = 0
        cap_source = "lqos.conf generated_pn_download_mbps"
    if capacity <= 0:
        capacity, cap_source = DEFAULT_CAPACITY_MBPS, "default"
    try:
        link = int(qsec.get('downlink_bandwidth_mbps') or 0)
    except (TypeError, ValueError):
        link = 0

    return Params(
        bins=bins, capacity_mbps=capacity, link_dl_mbps=link,
        split_ratio=float(ts.get("split_ratio", 1.0)),
        rebalance_ratio=float(ts.get("rebalance_ratio", 1.5)),
        move_budget=int(ts.get("move_budget", 0)),
        multiplier=float(ts.get("multiplier", 1.1)),
        sticky=bool(ts.get("sticky", True)), emit_ids=emit_ids,
        skew_warn_ratio=float(ts.get("skew_warn_ratio", 2.0)),
        tc_u16_warn=int(ts.get("tc_u16_warn", 60000)),
        queue_info=qi, n_source=source, capacity_source=cap_source,
    )


# ── Planner internals ───────────────────────────────────────────────────────

@dataclass
class _TNode:
    id: str
    name: str
    type: str
    dl_cfg: int
    ul_cfg: int
    virtual: bool
    promote: bool
    pin: int | None
    routers: list
    children: list
    depth: int


@dataclass
class _Item:
    key: str
    kind: str              # node | router | shard
    name: str
    dl: int
    ul: int
    circuits: int
    node: _TNode | None = None
    router: str = ''
    devices: list = field(default_factory=list)
    pin: int | None = None
    bin: int | None = None
    sticky: bool = False
    prev: tuple | None = None


class _Names:
    """Network.json node names, unique across the whole tree."""
    def __init__(self, reserved):
        self.used = set(reserved)

    def claim(self, name, suffix):
        cand, i = name, 2
        if cand in self.used:
            cand = f"{name} {suffix}"
        while cand in self.used:
            cand, i = f"{name} {suffix} {i}", i + 1
        self.used.add(cand)
        return cand


def _water_level(loads, extra):
    """The level L where pouring `extra` into the emptiest bins fills them: Σ max(0, L - load) = extra."""
    ordered = sorted(loads)
    acc = 0.0
    for k, x in enumerate(ordered, 1):
        acc += x
        level = (extra + acc) / k
        if k == len(ordered) or level <= ordered[k]:
            return level
    return 0.0


def _sum(devs):
    dl = ul = n = 0
    for d in devs:
        if d.has_ip:
            dl += max(1, d.dl)
            ul += max(1, d.ul)
            n += 1
    return dl, ul, n


class _Planner:
    def __init__(self, devices, router_names, topology, params, prior):
        self.P = params
        self.N = max(1, int(params.bins))
        self.prior = prior or Prior()
        self.prior_ok = params.sticky and self.prior.bin_count == self.N
        self.W = []
        self.devices = devices
        self.routers = list(dict.fromkeys(router_names))
        self.topology = topology if isinstance(topology, dict) else {"nodes": []}
        self.bin_names = [f"CPU{i}" for i in range(self.N)]
        self.bin_index = {name: i for i, name in enumerate(self.bin_names)}
        self.names = _Names(self.bin_names)

    def warn(self, level, text):
        self.W.append({"level": level, "text": text})

    # ── 1. topology ────────────────────────────────────────────────────────
    def normalise(self):
        router_set = set(self.routers)
        self.rnode = {}
        for r in self.routers:
            self.rnode[r] = self.names.claim(f"{r} (router)" if RESERVED.match(r) else r, "(router)")
            if self.rnode[r] != r:
                self.warn("warn", f"router '{r}' uses a reserved name; its node is '{self.rnode[r]}'")

        self.by_id, self.by_name, self.placed, ids = {}, {}, {}, set()

        def pin_of(v, label):
            if v is None:
                return None
            if not _is_int(v):
                self.warn("warn", f"{label}: pin_cpu {v!r} ignored")
                return None
            if v >= self.N:
                self.warn("warn", f"{label}: pinned to CPU{v} but there are only {self.N} CPUs; pin ignored")
                return None
            return v

        def build(raw, depth, holder=None):
            if not isinstance(raw, dict):
                return None
            nid = str(raw.get("id") or "")
            if not nid or nid in ids:
                nid = f"auto-{len(ids) + 1}"
            ids.add(nid)
            base = str(raw.get("name") or nid).strip() or nid
            suffix = "(site)" if raw.get("type", "site") != "ap" else "(ap)"
            name = self.names.claim(base, suffix)
            if name != base:
                self.warn("warn", f"node '{base}' renamed '{name}' (names must be unique and not CPU<n>)")
            node = _TNode(
                id=nid, name=name, type=raw.get("type") if raw.get("type") in NODE_TYPES else "site",
                dl_cfg=raw.get("download_mbps") if _is_int(raw.get("download_mbps")) else 0,
                ul_cfg=raw.get("upload_mbps") if _is_int(raw.get("upload_mbps")) else 0,
                virtual=bool(raw.get("virtual", False)), promote=bool(raw.get("promote", False)),
                pin=None, routers=[], children=[], depth=depth)
            # Only a node that becomes a CPU item can be pinned; checked in expand.
            node.pin = pin_of(raw.get("pin_cpu"), f"node '{name}'")
            target = holder or node
            for r in raw.get("routers") or []:
                if r not in router_set:
                    self.warn("warn", f"node '{name}': router '{r}' is not configured")
                elif r in self.placed:
                    self.warn("warn", f"router '{r}' is in '{self.placed[r]}' and '{name}'; kept in the first")
                else:
                    self.placed[r] = target.name
                    target.routers.append(r)
            self.by_id[nid] = node
            if holder is None:
                self.by_name[name] = node
            kids = raw.get("children") or []
            if kids and depth >= MAX_NODE_DEPTH and holder is None:
                # Too deep for HTB: fold the subtree's routers into this node.
                self.warn("warn", f"node '{name}': nodes nest at most {MAX_NODE_DEPTH} levels; "
                                  "the routers of deeper nodes were moved into it")
            for c in kids:
                if depth >= MAX_NODE_DEPTH or holder is not None:
                    build(c, depth + 1, holder=target)
                else:
                    child = build(c, depth + 1)
                    if child is not None:
                        node.children.append(child)
            return node

        self.roots = []
        nodes = self.topology.get("nodes") or []
        for raw in nodes if isinstance(nodes, list) else []:
            n = build(raw, 1)
            if n is not None:
                self.roots.append(n)
        self.unplaced = [r for r in self.routers if r not in self.placed]

        self.ropts = {}
        raw_opts = self.topology.get("router_options") or {}
        for r in self.routers:
            o = raw_opts.get(r) if isinstance(raw_opts, dict) else None
            o = o if isinstance(o, dict) else {}
            self.ropts[r] = {
                "pin": pin_of(o.get("pin_cpu"), f"router '{r}'"),
                "dl": o.get("download_mbps") if _is_int(o.get("download_mbps")) else 0,
                "ul": o.get("upload_mbps") if _is_int(o.get("upload_mbps")) else 0,
                "shard": bool(o.get("shard", True)),
            }

    # ── 2. devices and loads ───────────────────────────────────────────────
    def classify(self):
        router_of_node = {v: k for k, v in self.rnode.items()}
        self.node_devs = {}
        self.by_router = {r: [] for r in self.routers}
        self.loose = []
        for d in self.devices:
            if d.is_static and d.parent in self.by_name:
                self.node_devs.setdefault(d.parent, []).append(d)      # manual placement honoured
            elif d.router in self.by_router:
                self.by_router[d.router].append(d)
            elif d.is_static and d.parent in router_of_node:
                self.by_router[router_of_node[d.parent]].append(d)
            elif d.is_static:
                self.loose.append(d)
            # Dynamic devices of a router no longer configured are skipped:
            # remove_inactive() deletes them in the same cycle.

        self.rload = {r: _sum(devs) for r, devs in self.by_router.items()}
        self.nload = {}

        def load(n):
            dl, ul, c = _sum(self.node_devs.get(n.name, []))
            for r in n.routers:
                a, b, k = self.rload[r]
                dl, ul, c = dl + a, ul + b, c + k
            for ch in n.children:
                a, b, k = load(ch)
                dl, ul, c = dl + a, ul + b, c + k
            self.nload[n.id] = (dl, ul, c)
            return dl, ul, c

        for n in self.roots:
            load(n)
        loose_dl = _sum(self.loose)[0]
        self.total = sum(self.rload[r][0] for r in self.unplaced) + \
            sum(self.nload[n.id][0] for n in self.roots) + loose_dl
        P = self.P
        self.scale = min(1.0, P.link_dl_mbps / self.total) if P.link_dl_mbps and self.total else 1.0
        self.fair = self.total / self.N
        self.limit = math.inf
        if self.N > 1 and self.total > 0:
            if P.split_ratio > 0:
                self.limit = self.fair * P.split_ratio
            if P.link_dl_mbps:
                self.limit = min(self.limit, P.capacity_mbps / self.scale)

    # ── 3. items ───────────────────────────────────────────────────────────
    def _prior_place(self, name, node_id=None):
        if node_id and self.P.emit_ids:
            name = self.prior.ids.get(f"mikrotik:node:{node_id}", name)
        return self.prior.places.get(name)

    def _was_promoted(self, n):
        """Dissolved last time: absent from network.json while a child sat directly under a CPU."""
        if not self.prior.bin_count or self._prior_place(n.name, n.id):
            return False
        kids = [c.name for c in n.children] + [self.rnode[r] for r in n.routers]
        for k in kids:
            p = self.prior.places.get(k)
            if p and p[0] == p[1]:
                return True
        return False

    def expand(self):
        self.items, self.promoted, self.shards, self.split_routers = [], [], {}, []
        self.status_node, self.status_router = {}, {}
        for n in self.roots:
            self.expand_node(n)
        for r in self.unplaced:
            self.expand_router(r)

    def expand_node(self, n):
        dl, ul, c = self.nload[n.id]
        if n.pin is not None:
            if n.promote:
                self.warn("warn", f"'{n.name}' is pinned to CPU{n.pin}; promote ignored")
            if dl > self.limit:
                self.warn("warn", f"'{n.name}' is pinned to CPU{n.pin} and kept whole although it "
                                  f"carries {dl} Mbps of plans (split limit {int(self.limit)})")
            self.items.append(_Item(f"node:{n.id}", "node", n.name, dl, ul, c, node=n, pin=n.pin))
            return
        h = STRUCT_HYSTERESIS if self._was_promoted(n) else 1.0
        reason = "flag" if n.promote else ("auto" if dl > self.limit * h else None)
        if reason is None:
            self.items.append(_Item(f"node:{n.id}", "node", n.name, dl, ul, c, node=n))
            return
        self.promoted.append({"id": n.id, "name": n.name, "reason": reason, "dl": dl, "circuits": c})
        self.status_node[n.id] = {"status": "promoted", "reason": reason}
        if n.dl_cfg or n.ul_cfg:
            self.warn("warn", f"'{n.name}' is split across CPUs, so its configured "
                              f"{n.dl_cfg or '-'}/{n.ul_cfg or '-'} Mbps limit is not enforced")
        own = self.node_devs.get(n.name, [])
        if own:
            self.loose.extend(own)
            self.warn("info", f"{len(own)} manual device(s) on '{n.name}' go to CPU nodes while it is promoted")
        for ch in n.children:
            self.expand_node(ch)
        for r in n.routers:
            self.expand_router(r)

    def _shard_names(self, r):
        """Matches the router's node name and its shard names ("R #2", "R #3 (shard)", …)."""
        return re.compile(re.escape(self.rnode[r]) + r"(?: #\d+)?(?: \(shard\)(?: \d+)?)?$")

    def _was_split(self, r):
        """Split over several CPUs last time: one of its devices sat in a "R #n" shard."""
        base, pat = self.rnode[r], self._shard_names(r)
        return any(d.parent and d.parent != base and pat.match(d.parent) for d in self.by_router[r])

    def expand_router(self, r):
        o = self.ropts[r]
        devs = self.by_router[r]
        dl, ul, c = self.rload[r]
        base = self.rnode[r]
        if o["pin"] is not None:
            if dl > self.limit:
                self.warn("warn", f"router '{r}' is pinned to CPU{o['pin']} and kept whole although it "
                                  f"carries {dl} Mbps of plans (split limit {int(self.limit)})")
            self.items.append(_Item(f"router:{r}", "router", base, dl, ul, c, router=r,
                                    devices=devs, pin=o["pin"]))
            return
        split = False
        if o["shard"] and self.limit < math.inf and len(devs) >= 2:
            split = dl > self.limit * (STRUCT_HYSTERESIS if self._was_split(r) else 1.0)
        if not split:
            if dl > self.limit:
                why = "sharding is off" if not o["shard"] else "it has too few devices or CPUs"
                self.warn("warn", f"router '{r}' carries {dl} Mbps of plans, above the split limit "
                                  f"{int(self.limit)}, but stays whole ({why})")
            self.items.append(_Item(f"router:{r}", "router", base, dl, ul, c, router=r, devices=devs))
            return
        # Spread over the CPUs in pack(), after the whole items (_fill_shards).
        self.split_routers.append(r)
        if o["dl"] or o["ul"]:
            self.warn("warn", f"router '{r}' is split across CPUs; its configured limit applies to each shard")
        if dl > self.N * self.limit:
            self.warn("error", f"router '{r}' alone needs more than all {self.N} CPUs")

    # ── 4. pack ────────────────────────────────────────────────────────────
    def pack(self):
        N = self.N
        self.bin_items = [[] for _ in range(N)]
        self.bin_dl = [0] * N
        free = []

        def put(it, b):
            it.bin = b
            self.bin_items[b].append(it)
            self.bin_dl[b] += it.dl

        for it in self.items:
            node_id = it.node.id if it.node else None
            it.prev = self._prior_place(it.name, node_id)
            if it.pin is not None:
                put(it, it.pin)
            elif self.prior_ok and it.prev and it.prev[0] == it.prev[1]:
                it.sticky = True
                put(it, int(it.prev[0][3:]))
            else:
                free.append(it)

        heap = [(self.bin_dl[b], len(self.bin_items[b]), b) for b in range(N)]
        heapq.heapify(heap)
        for it in sorted(free, key=lambda it: (-it.dl, it.name)):
            _, _, b = heapq.heappop(heap)
            put(it, b)
            heapq.heappush(heap, (self.bin_dl[b], len(self.bin_items[b]), b))

        budget = self.P.move_budget or max(1, min(32, math.ceil(len(self.items) / 100)))
        loads = {self.bin_names[b]: self.bin_dl[b] for b in range(N)}
        members = {self.bin_names[b]: [(it.key, it.dl) for it in self.bin_items[b] if it.pin is None]
                   for b in range(N)}
        by_key = {it.key: it for it in self.items}
        self.moves = []
        for key, src, dst in rebalance_moves(loads, members, self.P.rebalance_ratio, budget):
            it = by_key[key]
            s, d = int(src[3:]), int(dst[3:])
            self.bin_items[s].remove(it)
            self.bin_dl[s] -= it.dl
            put(it, d)
            self.moves.append({"name": it.name, "from": src, "to": dst, "reason": "rebalance"})
        moved = {m["name"] for m in self.moves}
        for it in self.items:
            p = it.prev
            if it.name in moved or not p:
                continue
            new_bin = self.bin_names[it.bin]
            if p[0] != new_bin or p[1] != p[0]:
                reason = "pin" if it.pin is not None else ("structure" if p[1] != p[0] else "repack")
                self.moves.append({"name": it.name, "from": p[0], "to": new_bin, "reason": reason})

        # Loose devices (manual devices with no router or node) go straight onto a CPU.
        self.loose_bin = {}
        counts = [len(x) for x in self.bin_items]
        for d in sorted(self.loose, key=lambda d: (-(max(1, d.dl) if d.has_ip else 0), d.code)):
            if self.prior_ok and d.parent in self.bin_names:
                b = int(d.parent[3:])
            else:
                b = min(range(N), key=lambda i: (self.bin_dl[i], counts[i], i))
            self.loose_bin[d.code] = b
            if d.has_ip:
                self.bin_dl[b] += max(1, d.dl)
            counts[b] += 1

        self._fill_shards()

    def _fill_shards(self):
        """
        Spread each split router's devices over the CPUs so that, together with
        the whole items already placed, every CPU ends up at the same load (the
        water level). A device stays on its CPU while that CPU is at most
        SHARD_SLACK over the level; new and displaced devices go to the
        router's own CPUs first, then to the emptiest ones, so a router uses as
        few CPUs as it can. Each (router, CPU) pair becomes one shard node.
        """
        if not self.split_routers:
            return
        N = self.N

        def wt(d):
            return max(1, d.dl) if d.has_ip else 0
        level = _water_level(self.bin_dl, sum(wt(d) for r in self.split_routers for d in self.by_router[r]))
        cap = level * SHARD_SLACK
        load = list(self.bin_dl)
        groups = {r: {} for r in self.split_routers}          # router → {CPU: [devices]}
        free = {r: [] for r in self.split_routers}
        sticky = []
        for r in self.split_routers:
            for d in self.by_router[r]:
                place = self.prior.places.get(d.parent) if self.prior_ok and d.parent else None
                b = self.bin_index.get(place[0]) if place else None
                if b is None:
                    free[r].append(d)
                else:
                    sticky.append((b, r, d))
        for b, r, d in sorted(sticky, key=lambda s: (s[0], s[2].code)):
            if load[b] + wt(d) <= cap:
                groups[r].setdefault(b, []).append(d)
                load[b] += wt(d)
            else:
                free[r].append(d)                              # its CPU is over the level: move it
        for r in sorted(self.split_routers, key=lambda r: (-sum(wt(d) for d in free[r]), r)):
            for d in sorted(free[r], key=lambda d: (-wt(d), d.code)):
                own = sorted(groups[r], key=lambda b: (load[b], b))
                rest = sorted((b for b in range(N) if b not in groups[r]), key=lambda b: (load[b], b))
                b = next((b for b in own + rest if load[b] < level and load[b] + wt(d) <= cap), None)
                if b is None:
                    b = min(range(N), key=lambda b: (load[b], b))
                groups[r].setdefault(b, []).append(d)
                load[b] += wt(d)

        for r in self.split_routers:
            names = self._name_shards(r, groups[r])
            kind = "shard" if len(groups[r]) > 1 else "router"
            items = []
            for b in sorted(groups[r]):
                devs = groups[r][b]
                sdl, sul, sc = _sum(devs)
                it = _Item(f"{kind}:{names[b]}", kind, names[b], sdl, sul, sc, router=r, devices=devs, bin=b)
                self.items.append(it)
                self.bin_items[b].append(it)
                self.bin_dl[b] += sdl
                items.append(it)
            if len(items) > 1:
                self.shards[r] = items

    def _name_shards(self, r, groups):
        """
        {CPU: shard name}: a CPU keeps the name most of its devices had there in
        the last plan; the others get the router's name, then "R #2", "R #3", ….
        """
        base, pat = self.rnode[r], self._shard_names(r)
        if len(groups) == 1:
            return {b: base for b in groups}
        names, used = {}, set()
        for b, devs in sorted(groups.items(), key=lambda g: (-len(g[1]), g[0])):
            seen = {}
            for d in devs:
                place = self.prior.places.get(d.parent) if d.parent and pat.match(d.parent) else None
                if place and place[0] == self.bin_names[b]:
                    seen[d.parent] = seen.get(d.parent, 0) + 1
            for name in sorted(seen, key=lambda n: (-seen[n], n)):
                if name not in used:
                    names[b] = name
                    used.add(name)
                    break
        k = 2
        for b in sorted(groups):
            if b in names:
                continue
            if base not in used:
                names[b] = base
            else:
                while f"{base} #{k}" in used:
                    k += 1
                names[b] = f"{base} #{k}"
            used.add(names[b])
        # The router's own name is already claimed for it; shard names are claimed here.
        return {b: (n if n == base else self.names.claim(n, "(shard)")) for b, n in names.items()}

    # ── 5. emit ────────────────────────────────────────────────────────────
    def _mk(self, kind, ident, dl, ul, ntype, virtual=False):
        node = {"downloadBandwidthMbps": dl, "uploadBandwidthMbps": ul, "type": ntype, "children": {}}
        if self.P.emit_ids:
            node["id"] = f"mikrotik:{kind}:{ident}"
        if virtual:
            node["virtual"] = True
        return node

    def _size(self, cfg, load, cap, label):
        v = cfg or max(1, int(load * self.P.multiplier))
        if cap is not None and v > cap:
            if cfg:
                self.warn("warn", f"'{label}': configured {cfg} Mbps is above its parent's {cap} Mbps; "
                                  "clamped")
            v = cap
        return v

    def emit_router(self, r, name, devs, caps, bin_name, kind):
        o = self.ropts[r]
        dl, ul, c = _sum(devs)
        self.kinds[name] = kind
        for d in devs:
            self.parents[d.code] = name
        self.bin_circuit[bin_name] += c
        return self._mk("router", name, self._size(o["dl"], dl, caps[0], name),
                        self._size(o["ul"], ul, caps[1], name), "ap")

    def emit_node(self, n, caps, bin_name):
        dl, ul, _ = self.nload[n.id]
        if n.virtual and (n.dl_cfg or n.ul_cfg):
            self.warn("warn", f"'{n.name}' is virtual: LibreQoS does not enforce its configured limit")
        size = (self._size(n.dl_cfg, dl, caps[0], n.name), self._size(n.ul_cfg, ul, caps[1], n.name))
        node = self._mk("node", n.id, size[0], size[1], n.type, n.virtual)
        self.kinds[n.name] = n.type
        self.status_node.setdefault(n.id, {"status": "nested", "bin": bin_name})
        for ch in n.children:
            if ch.pin is not None:
                self.warn("warn", f"pin on '{ch.name}' ignored: it is inside '{n.name}', which is placed whole")
            node["children"][ch.name] = self.emit_node(ch, size, bin_name)
        for r in n.routers:
            if self.ropts[r]["pin"] is not None:
                self.warn("warn", f"pin on router '{r}' ignored: it is inside '{n.name}', which is placed whole")
            node["children"][self.rnode[r]] = self.emit_router(
                r, self.rnode[r], self.by_router[r], size, bin_name, "router")
            self.status_router[r] = {"status": "nested", "bins": [bin_name]}
        own = self.node_devs.get(n.name, [])
        for d in own:
            self.parents[d.code] = n.name
        self.bin_circuit[bin_name] += _sum(own)[2]
        return node

    def emit(self):
        self.network, self.parents, self.kinds = {}, {}, {}
        self.bin_circuit = {b: 0 for b in self.bin_names}
        loose_by_bin = {}
        for d in self.loose:
            loose_by_bin.setdefault(self.loose_bin[d.code], []).append(d)

        for b, bname in enumerate(self.bin_names):
            items = sorted(self.bin_items[b], key=lambda it: (it.name.casefold(), it.name))
            kids = {}
            for it in items:
                if it.kind == "node":
                    kids[it.name] = self.emit_node(it.node, (None, None), bname)
                    self.status_node[it.node.id] = {"status": "item", "bin": bname,
                                                    "pinned": it.pin is not None}
                else:
                    kids[it.name] = self.emit_router(it.router, it.name, it.devices, (None, None), bname,
                                                     "router" if it.kind == "router" else "shard")
            loose = loose_by_bin.get(b, [])
            for d in loose:
                self.parents[d.code] = bname
            self.bin_circuit[bname] += _sum(loose)[2]

            ul = sum(it.ul for it in items) + _sum(loose)[1]
            dl_size = max([1, int(self.bin_dl[b] * self.P.multiplier)] +
                          [k["downloadBandwidthMbps"] for k in kids.values()])
            ul_size = max([1, int(ul * self.P.multiplier)] + [k["uploadBandwidthMbps"] for k in kids.values()])
            node = self._mk("cpu", bname, dl_size, ul_size, "site")
            node["children"] = kids
            self.network[bname] = node
            self.kinds[bname] = "bin"

        for r, shards in self.shards.items():
            self.status_router[r] = {"status": "sharded",
                                     "bins": [self.bin_names[s.bin] for s in shards]}
        for it in self.items:
            if it.kind == "router":
                self.status_router[it.router] = {"status": "item", "bins": [self.bin_names[it.bin]],
                                                 "pinned": it.pin is not None}

    # ── 6. report ──────────────────────────────────────────────────────────
    def report(self):
        P, N = self.P, self.N
        bins = []
        for b, bname in enumerate(self.bin_names):
            ul = sum(it.ul for it in self.bin_items[b]) + \
                sum(max(1, d.ul) for d in self.loose if d.has_ip and self.loose_bin[d.code] == b)
            bins.append({
                "name": bname, "index": b, "dl": self.bin_dl[b], "ul": ul,
                "projected_mbps": int(self.bin_dl[b] * self.scale),
                "circuits": self.bin_circuit[bname],
                "items": [it.name for it in sorted(self.bin_items[b], key=lambda it: -it.dl)],
                "size_dl": self.network[bname]["downloadBandwidthMbps"],
                "size_ul": self.network[bname]["uploadBandwidthMbps"],
            })
            if P.link_dl_mbps and bins[-1]["projected_mbps"] > P.capacity_mbps:
                self.warn("error", f"{bname} is projected at {bins[-1]['projected_mbps']} Mbps, above the "
                                   f"{P.capacity_mbps} Mbps one core can shape")
            if bins[-1]["circuits"] >= P.tc_u16_warn:
                self.warn("error", f"{bname} has {bins[-1]['circuits']} circuits: TC_U16_OVERFLOW risk "
                                   f"(threshold {P.tc_u16_warn})")

        loads = [b["dl"] for b in bins]
        skew = round(max(loads) / min(loads), 2) if loads and min(loads) > 0 else (0 if not any(loads) else None)
        if skew and skew > P.skew_warn_ratio:
            hi = max(bins, key=lambda b: b["dl"])
            lo = min(bins, key=lambda b: b["dl"])
            self.warn("warn", f"load skew {skew}x: {hi['name']} carries {hi['dl']} Mbps of plans, "
                              f"{lo['name']} {lo['dl']}. Pin or split nodes, or lower the split ratio.")
        if P.link_dl_mbps and N * P.capacity_mbps < P.link_dl_mbps:
            self.warn("warn", f"{N} CPUs × {P.capacity_mbps} Mbps is less than the "
                              f"{P.link_dl_mbps} Mbps link")

        qi = P.queue_info or {}
        if P.n_source not in ("config", "whatif"):
            for text in qi.get("warnings", []):
                self.warn("warn", f"queue detection: {text}")
        detected = qi.get("queues")
        if detected and P.n_source in ("config", "whatif"):
            if N > detected:
                self.warn("error", f"{N} CPU nodes but LibreQoS has {detected} queues: "
                                   f"CPU{detected}..CPU{N - 1} will share cores with others")
            elif N < detected:
                self.warn("info", f"only {N} of {detected} LibreQoS queues are used")
        if self.prior.bin_count and self.prior.bin_count != N:
            self.warn("info", f"CPU count changed {self.prior.bin_count} → {N}: everything was re-packed")

        current = {d.code: d.parent for d in self.devices}
        self.device_changes = sum(1 for c, p in self.parents.items() if current.get(c) != p)

        node_index = {}
        for nid, n in self.by_id.items():
            dl, ul, c = self.nload.get(nid, (0, 0, 0))
            st = dict(self.status_node.get(nid, {"status": "folded"}))
            node_index[nid] = {"name": n.name, "dl": dl, "ul": ul, "circuits": c, **st}
        router_index = {}
        for r in self.routers:
            dl, ul, c = self.rload[r]
            st = dict(self.status_router.get(r, {"status": "item", "bins": []}))
            router_index[r] = {"node": self.rnode[r], "dl": dl, "ul": ul, "circuits": c,
                               "shards": len(self.shards.get(r, [])) or 1, **st}

        items = [{"key": it.key, "kind": it.kind, "name": it.name, "bin": self.bin_names[it.bin],
                  "dl": it.dl, "ul": it.ul, "circuits": it.circuits, "pinned": it.pin is not None,
                  "sticky": it.sticky, "prev_bin": it.prev[0] if it.prev else None}
                 for it in sorted(self.items, key=lambda it: (it.bin, -it.dl, it.name))]
        shards = {r: [{"name": s.name, "bin": self.bin_names[s.bin], "dl": s.dl, "circuits": s.circuits}
                      for s in ss] for r, ss in self.shards.items()}
        summary = {
            "n": N, "n_source": P.n_source, "queues": qi,
            "capacity_mbps": P.capacity_mbps, "capacity_source": P.capacity_source,
            "link_dl_mbps": P.link_dl_mbps, "scale": round(self.scale, 4),
            "fair_share_mbps": int(self.fair),
            "split_limit_mbps": None if self.limit == math.inf else int(self.limit),
            "totals": {"dl": self.total, "ul": sum(b["ul"] for b in bins),
                       "circuits": sum(b["circuits"] for b in bins), "items": len(self.items)},
            "skew": skew,
        }
        return Plan(
            network=self.network, parents=self.parents, bins=bins, items=items,
            promoted=self.promoted, shards=shards, moves=self.moves, warnings=self.W,
            summary=summary, kinds=self.kinds, node_index=node_index, router_index=router_index,
            device_changes=self.device_changes,
        )


def plan(devices, router_names, topology, params, prior=None):
    """
    Plan network.json and every device's parent node for the auto_cpu strategy.
    devices: [Device]; router_names: configured routers (deduped); topology:
    config.json "topology" (None/{} = every router top-level); prior: from
    prior_from_network() for stable placement.
    """
    p = _Planner(devices, router_names, topology, params, prior)
    p.normalise()
    p.classify()
    p.expand()
    p.pack()
    p.emit()
    return p.report()
