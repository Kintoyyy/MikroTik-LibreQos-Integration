import heapq
import json
import logging
import os

from settings import (
    PARENT_BANDWIDTH_MULTIPLIER, SKEW_WARN_RATIO, STICKY_CPU_ASSIGNMENT,
    CPU_REBALANCE_RATIO, NODE_IDS, VIRTUAL_SITE_NODES, write_if_changed,
)

logger = logging.getLogger(__name__)

# Integration strategies (LibreQoS Scale Planning)
STRATEGY_FLAT    = 'flat'     # No parent hierarchy; empty network.json (max perf, min visibility)
STRATEGY_AP_ONLY = 'ap_only'  # Devices grouped under their router as parent node
STRATEGY_AP_SITE = 'ap_site'  # Devices grouped under site → router hierarchy
STRATEGY_FULL    = 'full'     # Full path shaping; pair with promote_to_root if single-core saturates
STRATEGY_CPU     = 'cpu'      # Bin-pack across CPU nodes (current default)

ALL_STRATEGIES = {STRATEGY_FLAT, STRATEGY_AP_ONLY, STRATEGY_AP_SITE, STRATEGY_FULL, STRATEGY_CPU}


def _node(kind, name, dl, ul, node_type, virtual=False):
    """One network.json node sized at the sum of its devices' max rates x multiplier."""
    node = {
        "downloadBandwidthMbps": max(int(dl * PARENT_BANDWIDTH_MULTIPLIER), 1),
        "uploadBandwidthMbps":   max(int(ul * PARENT_BANDWIDTH_MULTIPLIER), 1),
        "type": node_type,
        "children": {},
    }
    if NODE_IDS:
        # Stable namespaced id; LibreQoS prefers it over the name for saved overrides.
        node["id"] = f"mikrotik:{kind}:{name}"
    if virtual:
        node["virtual"] = True
    return node


def _node_names(network_config):
    names = set()
    for name, node in network_config.items():
        names.add(name)
        names |= _node_names(node.get("children", {}))
    return names


class NodeAssigner:
    def __init__(self, network_json_path='network.json'):
        self.network_json_path = network_json_path

    # ── Public API ──────────────────────────────────────────────────────────

    def assign(self, conn, strategy, routers, queues, promote_to_root) -> bool:
        """
        Set every device's parent_node for the strategy and write network.json.
        Returns True if network.json changed on disk.
        """
        if strategy == STRATEGY_CPU and queues is None:
            self._drop_unknown_parents(conn, {})
            conn.commit()
            logger.info("Skipping network.json (queues=false)")
            return False

        if strategy == STRATEGY_FLAT:
            network_config = {}

        elif strategy == STRATEGY_AP_ONLY:
            router_totals = self._assign_router_nodes(conn, routers)
            self.check_distribution_skew(router_totals, label="router")
            network_config = {
                name: _node("router", name, dl, ul, "ap")
                for name, (dl, ul) in router_totals.items()
            }

        elif strategy in (STRATEGY_AP_SITE, STRATEGY_FULL) and not (
                promote_to_root and strategy == STRATEGY_FULL):
            network_config = self._build_site_tree(conn, routers)

        else:
            # cpu, or full + promote_to_root. NOTE: the latter replaces the
            # site hierarchy with CPU nodes, so its result equals 'cpu'.
            effective_queues = queues or (os.cpu_count() or 4)
            cpu_totals = self._assign_cpu_nodes(conn, effective_queues)
            self.check_distribution_skew(cpu_totals, label="CPU")
            network_config = {
                cpu: _node("cpu", cpu, dl, ul, "site")
                for cpu, (dl, ul) in cpu_totals.items()
            }

        self._drop_unknown_parents(conn, network_config)
        conn.commit()
        return self.write_network_json(network_config)

    @staticmethod
    def check_distribution_skew(totals: dict, label: str = "node"):
        """
        Warn if the max/min load ratio across nodes exceeds SKEW_WARN_RATIO.
        Skewed distribution signals one core will saturate while others stay idle.
        """
        loads = {k: dl + ul for k, (dl, ul) in totals.items()}
        if not loads:
            return
        max_load = max(loads.values())
        min_load = min(loads.values())
        if min_load > 0 and max_load / min_load > SKEW_WARN_RATIO:
            worst = max(loads, key=loads.get)
            logger.warning(
                f"Load skew detected across {label}s: {worst} has {max_load:.0f} Mbps vs "
                f"min {min_load:.0f} Mbps (ratio {max_load/min_load:.1f}x). "
                "Review topology or use promote_to_root."
            )

    def read_network_json(self):
        try:
            if os.path.exists(self.network_json_path):
                with open(self.network_json_path, 'r') as f:
                    return json.load(f)
            logger.info("Network JSON not found, will create new.")
            return {}
        except Exception as e:
            logger.error(f"Error reading network JSON: {e}")
            return {}

    def write_network_json(self, data) -> bool:
        """Replace network.json only if its content changes. Returns True if written."""
        try:
            if write_if_changed(self.network_json_path, json.dumps(data, indent=4)):
                logger.info(f"Wrote network config to {self.network_json_path}")
                return True
        except Exception as e:
            logger.error(f"Error writing network JSON: {e}")
        return False

    @staticmethod
    def _drop_unknown_parents(conn, network_config):
        """
        Blank parent_node values that are not a node in network_config, so no
        device points at a node LibreQoS cannot find (e.g. 'CPU3' after a
        strategy or queue-count change, or a manual device's stale parent).
        """
        names = list(_node_names(network_config))
        sql = "UPDATE devices SET parent_node = '' WHERE parent_node != ''"
        if names:
            sql += f" AND parent_node NOT IN ({','.join('?' * len(names))})"
        cur = conn.execute(sql, names)
        if cur.rowcount:
            logger.info(f"Cleared parent node of {cur.rowcount} device(s) not in network.json")

    # ── Private — node assignment ───────────────────────────────────────────

    def _assign_cpu_nodes(self, conn, cpu_count):
        """
        Distribute devices across CPU0..CPU{n-1}.
        Returns {cpu_name: (total_dl_mbps, total_ul_mbps)}.

        Sticky mode (default): devices keep their current CPU node, which
        keeps their queues stable (LibreQoS Scale Planning: prefer stable
        parent relationships to reduce queue churn). New devices go to the
        least loaded node, and devices are moved only when the busiest node
        carries more than CPU_REBALANCE_RATIO x the lightest one's load.
        Otherwise every device is re-packed from scratch each time.
        """
        cpu_names = [f"CPU{i}" for i in range(cpu_count)]
        loads     = {name: [0, 0] for name in cpu_names}
        members   = {name: [] for name in cpu_names}
        unplaced  = []

        for code, parent, dl, ul in conn.execute(
            "SELECT code, parent_node, download_max_mbps, upload_max_mbps FROM devices "
            "ORDER BY weight DESC, code"
        ):
            if STICKY_CPU_ASSIGNMENT and parent in loads:
                loads[parent][0] += dl
                loads[parent][1] += ul
                members[parent].append((code, dl, ul))
            else:
                unplaced.append((code, dl, ul))

        heap = [(sum(loads[name]), i) for i, name in enumerate(cpu_names)]
        heapq.heapify(heap)
        assignments = []
        for code, dl, ul in unplaced:
            load, idx = heapq.heappop(heap)
            name = cpu_names[idx]
            loads[name][0] += dl
            loads[name][1] += ul
            assignments.append((name, code))
            heapq.heappush(heap, (load + dl + ul, idx))

        moves = self._cpu_rebalance_moves(loads, members) if STICKY_CPU_ASSIGNMENT else []
        assignments.extend(moves)

        conn.executemany("UPDATE devices SET parent_node = ? WHERE code = ?", assignments)
        if unplaced or moves:
            logger.info(
                f"CPU nodes: placed {len(unplaced)} device(s), moved {len(moves)} "
                f"across {cpu_count} CPUs"
            )
        return {k: tuple(v) for k, v in loads.items()}

    @staticmethod
    def _cpu_rebalance_moves(loads, members):
        """
        Move as few devices as possible from the busiest CPU node to the
        lightest until their load ratio is within CPU_REBALANCE_RATIO.
        Each move rebuilds that circuit's queues. Returns [(cpu_name, code)].
        """
        if len(loads) < 2:
            return []

        def total(name):
            return loads[name][0] + loads[name][1]

        moves, moved = [], set()
        while True:
            hi = max(loads, key=total)
            lo = min(loads, key=total)
            l_hi, l_lo = total(hi), total(lo)
            if l_hi == 0 or (l_lo > 0 and l_hi / l_lo <= CPU_REBALANCE_RATIO):
                break
            # Moving more than half the gap only flips the imbalance.
            target = (l_hi - l_lo) / 2
            pick = next(
                (d for d in members[hi] if d[0] not in moved and d[1] + d[2] <= target),
                None
            )
            if pick is None:
                break
            code, dl, ul = pick
            loads[hi][0] -= dl
            loads[hi][1] -= ul
            loads[lo][0] += dl
            loads[lo][1] += ul
            moved.add(code)
            moves.append((lo, code))
        return moves

    def _assign_router_nodes(self, conn, routers):
        """
        ap_only strategy: parent_node = router name for all devices from that router.
        Returns {router_name: (total_dl_mbps, total_ul_mbps)}.
        """
        router_totals = {}
        for router in routers:
            name = router['name']
            row = conn.execute(
                "SELECT SUM(download_max_mbps), SUM(upload_max_mbps) FROM devices WHERE router = ?",
                (name,)
            ).fetchone()
            dl, ul = (row[0] or 0), (row[1] or 0)
            router_totals[name] = (dl, ul)
            conn.execute("UPDATE devices SET parent_node = ? WHERE router = ?", (name, name))
        logger.info(f"Assigned router-level parent nodes for {len(routers)} routers")
        return router_totals

    def _build_site_tree(self, conn, routers):
        """
        ap_site / full strategy: parent_node = router name, routers grouped
        under their optional 'site'. Returns the network.json dict.

        Node names must be unique across the whole tree, so a site named like
        a router gets a ' (site)' suffix.
        """
        router_names = {r['name'] for r in routers}
        tree = {}
        top_totals = {}   # top-level node → (dl, ul); top-level nodes are what LibreQoS spreads over CPUs
        for router in routers:
            name = router['name']
            row = conn.execute(
                "SELECT SUM(download_max_mbps), SUM(upload_max_mbps) FROM devices WHERE router = ?",
                (name,)
            ).fetchone()
            dl, ul = (row[0] or 0), (row[1] or 0)
            conn.execute("UPDATE devices SET parent_node = ? WHERE router = ?", (name, name))

            router_node = _node("router", name, dl, ul, "ap")
            site = router.get('site', '')
            if not site:
                tree[name] = router_node
                top_totals[name] = (dl, ul)
                continue

            site_name = f"{site} (site)" if site in router_names else site
            if site_name not in tree:
                tree[site_name] = _node("site", site_name, 0, 0, "site", virtual=VIRTUAL_SITE_NODES)
                top_totals[site_name] = (0, 0)
            tree[site_name]["children"][name] = router_node
            s_dl, s_ul = top_totals[site_name]
            top_totals[site_name] = (s_dl + dl, s_ul + ul)

        # Size each site from its routers' totals
        for name, node in tree.items():
            if node["type"] == "site":
                dl, ul = top_totals[name]
                node["downloadBandwidthMbps"] = max(int(dl * PARENT_BANDWIDTH_MULTIPLIER), 1)
                node["uploadBandwidthMbps"]   = max(int(ul * PARENT_BANDWIDTH_MULTIPLIER), 1)
        self.check_distribution_skew(top_totals, label="site/router")

        logger.info(f"Assigned site/router-level parent nodes for {len(routers)} routers")
        return tree
