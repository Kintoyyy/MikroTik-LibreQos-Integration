import heapq
import json
import logging
import os

logger = logging.getLogger(__name__)

# Integration strategies (LibreQoS Scale Planning)
STRATEGY_FLAT    = 'flat'     # No parent hierarchy; empty network.json (max perf, min visibility)
STRATEGY_AP_ONLY = 'ap_only'  # Devices grouped under their router as parent node
STRATEGY_AP_SITE = 'ap_site'  # Devices grouped under site → router hierarchy
STRATEGY_FULL    = 'full'     # Full path shaping; pair with promote_to_root if single-core saturates
STRATEGY_CPU     = 'cpu'      # Greedy bin-pack across CPU nodes (current default)

ALL_STRATEGIES = {STRATEGY_FLAT, STRATEGY_AP_ONLY, STRATEGY_AP_SITE, STRATEGY_FULL, STRATEGY_CPU}


class NodeAssigner:
    def __init__(self, network_json_path='network.json'):
        self.network_json_path = network_json_path

    # ── Public API ──────────────────────────────────────────────────────────

    def assign(self, conn, strategy, routers, queues, promote_to_root):
        """Dispatch to the correct strategy and write network.json."""
        if strategy == STRATEGY_FLAT:
            self.write_network_json({})

        elif strategy == STRATEGY_AP_ONLY:
            router_totals = self._assign_router_nodes(conn, routers)
            self.check_distribution_skew(router_totals, label="router")
            self._update_network_json_by_router(router_totals)

        elif strategy in (STRATEGY_AP_SITE, STRATEGY_FULL):
            node_totals = self._assign_site_nodes(conn, routers)
            flat_totals = {k: (dl, ul) for k, (dl, ul, *_) in node_totals.items()}
            self.check_distribution_skew(flat_totals, label="site/router")
            self._update_network_json_by_site(node_totals)
            if promote_to_root and strategy == STRATEGY_FULL:
                effective_queues = queues or (os.cpu_count() or 4)
                cpu_totals = self._assign_cpu_nodes(conn, effective_queues)
                self.check_distribution_skew(cpu_totals, label="CPU")
                self._update_network_json(cpu_totals)

        elif strategy == STRATEGY_CPU:
            if queues is not None:
                cpu_totals = self._assign_cpu_nodes(conn, queues)
                self.check_distribution_skew(cpu_totals, label="CPU")
                self._update_network_json(cpu_totals)
            else:
                logger.info("Skipping network.json (queues=false)")

    @staticmethod
    def check_distribution_skew(totals: dict, label: str = "node"):
        """
        Warn if the max/min load ratio across nodes exceeds 2:1.
        Skewed distribution signals one core will saturate while others stay idle.
        """
        loads = {k: dl + ul for k, (dl, ul) in totals.items()}
        if not loads:
            return
        max_load = max(loads.values())
        min_load = min(loads.values())
        if min_load > 0 and max_load / min_load > 2.0:
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

    def write_network_json(self, data):
        try:
            with open(self.network_json_path, 'w') as f:
                json.dump(data, f, indent=4)
            logger.info(f"Wrote network config to {self.network_json_path}")
        except Exception as e:
            logger.error(f"Error writing network JSON: {e}")

    # ── Private — node assignment ───────────────────────────────────────────

    def _assign_cpu_nodes(self, conn, cpu_count):
        """
        Distribute all devices across CPU0..CPU{n-1} using greedy bin-packing.
        Returns {cpu_name: (total_dl_mbps, total_ul_mbps)}.
        """
        devices = conn.execute(
            "SELECT code, download_max_mbps, upload_max_mbps FROM devices ORDER BY weight DESC"
        ).fetchall()

        heap = [(0, i) for i in range(cpu_count)]
        heapq.heapify(heap)

        cpu_totals  = {f"CPU{i}": [0, 0] for i in range(cpu_count)}
        assignments = []

        for code, dl, ul in devices:
            load, cpu_idx = heapq.heappop(heap)
            cpu_name = f"CPU{cpu_idx}"
            assignments.append((cpu_name, code))
            cpu_totals[cpu_name][0] += dl
            cpu_totals[cpu_name][1] += ul
            heapq.heappush(heap, (load + dl + ul, cpu_idx))

        conn.executemany("UPDATE devices SET parent_node = ? WHERE code = ?", assignments)
        conn.commit()
        logger.info(f"Assigned {len(devices)} devices across {cpu_count} CPUs")
        return {k: tuple(v) for k, v in cpu_totals.items()}

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
        conn.commit()
        logger.info(f"Assigned router-level parent nodes for {len(routers)} routers")
        return router_totals

    def _assign_site_nodes(self, conn, routers):
        """
        ap_site / full strategy: parent_node = router name, with optional site above.
        Returns {node_name: (total_dl_mbps, total_ul_mbps, parent_site_or_empty)}.
        """
        node_totals = {}
        for router in routers:
            name = router['name']
            row = conn.execute(
                "SELECT SUM(download_max_mbps), SUM(upload_max_mbps) FROM devices WHERE router = ?",
                (name,)
            ).fetchone()
            dl, ul = (row[0] or 0), (row[1] or 0)

            conn.execute("UPDATE devices SET parent_node = ? WHERE router = ?", (name, name))

            site = router.get('site', '')
            if site:
                node_totals[name] = (dl, ul, site)
                if site not in node_totals:
                    node_totals[site] = (0, 0, '')
                s_dl, s_ul, _ = node_totals[site]
                node_totals[site] = (s_dl + dl, s_ul + ul, '')
            else:
                node_totals[name] = (dl, ul, '')

        conn.commit()
        logger.info(f"Assigned site/router-level parent nodes for {len(routers)} routers")
        return node_totals

    # ── Private — network.json builders ────────────────────────────────────

    def _update_network_json_by_router(self, router_totals):
        """Build network.json with each router as a top-level node (ap_only)."""
        network_config = {
            name: {
                "downloadBandwidthMbps": max(int(dl * 1.1), 1),
                "uploadBandwidthMbps":   max(int(ul * 1.1), 1),
                "type": "ap",
                "children": {}
            }
            for name, (dl, ul) in router_totals.items()
        }
        self.write_network_json(network_config)

    def _update_network_json_by_site(self, node_totals):
        """Build network.json with site → router hierarchy (ap_site / full)."""
        network_config = {}

        for name, (dl, ul, parent) in node_totals.items():
            if not parent:
                network_config[name] = {
                    "downloadBandwidthMbps": max(int(dl * 1.1), 1),
                    "uploadBandwidthMbps":   max(int(ul * 1.1), 1),
                    "type": "site",
                    "children": {}
                }

        for name, (dl, ul, parent) in node_totals.items():
            if parent and parent in network_config:
                network_config[parent]["children"][name] = {
                    "downloadBandwidthMbps": max(int(dl * 1.1), 1),
                    "uploadBandwidthMbps":   max(int(ul * 1.1), 1),
                    "type": "ap",
                    "children": {}
                }

        self.write_network_json(network_config)

    def _update_network_json(self, cpu_totals):
        """Build network.json with CPU nodes (cpu strategy)."""
        network_config = {
            cpu: {
                "downloadBandwidthMbps": max(int(dl * 1.1), 1),
                "uploadBandwidthMbps":   max(int(ul * 1.1), 1),
                "type": "site",
                "children": {}
            }
            for cpu, (dl, ul) in cpu_totals.items()
        }
        self.write_network_json(network_config)
