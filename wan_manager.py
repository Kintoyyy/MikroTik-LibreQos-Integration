import heapq
import logging

logger = logging.getLogger(__name__)


class WANManager:
    WAN_REBALANCE_THRESHOLD = 1.10  # Trigger full rebalance when any WAN load exceeds 110% of its limit

    def __init__(self, connect_fn):
        # connect_fn is connect_to_router from updatecsv.py, injected to avoid circular imports
        self._connect = connect_fn
        # Per-core cache: {core_address: {(list_name, ip): entry_id}}
        # Populated on first contact, updated incrementally — avoids re-fetching every cycle.
        self._cache: dict = {}

    def assign_wan_nodes(self, conn, cores, wan_sources=None):
        """
        Assign only NEW (unassigned) devices to WANs using greedy bin-packing
        weighted by WAN capacity. Existing assignments are never touched, so the
        address lists on core routers only receive incremental adds/removes.
        Returns {(core_name, wan_name): (total_dl_mbps, total_ul_mbps)}.
        """
        wans = []
        for core in cores:
            core_name = core['name']
            for i, wan in enumerate(core.get('wans', []), start=1):
                wans.append({
                    'core':     core_name,
                    'wan':      f"WAN{i}",
                    'dl_limit': wan.get('download_limit', 1000),
                    'ul_limit': wan.get('upload_limit', 1000),
                    'used_dl':  0,
                    'used_ul':  0,
                })

        if not wans:
            logger.info("No cores/WANs defined — skipping WAN assignment")
            return {}

        # Seed each WAN's current load from the DB so new devices are balanced
        # against what is already assigned, not from zero.
        for w in wans:
            row = conn.execute(
                "SELECT COALESCE(SUM(download_max_mbps),0), COALESCE(SUM(upload_max_mbps),0) "
                "FROM devices WHERE core_name=? AND wan_name=?",
                (w['core'], w['wan'])
            ).fetchone()
            w['used_dl'], w['used_ul'] = row[0], row[1]

        if wan_sources is None:
            wan_sources = {}
        excluded = []
        if not wan_sources.get('include_hotspot', False):
            excluded.append('hotspot')
        if not wan_sources.get('include_dhcp', False):
            excluded.append('dhcp')

        excl_sql = ""
        if excluded:
            placeholders = ','.join('?' * len(excluded))
            excl_sql = f" AND (source NOT IN ({placeholders}) OR source IS NULL)"

        # Only process devices that have not been assigned yet
        new_devices = conn.execute(
            "SELECT code, download_max_mbps, upload_max_mbps FROM devices "
            "WHERE (core_name IS NULL OR core_name = '' OR wan_name IS NULL OR wan_name = '')"
            + excl_sql + " ORDER BY weight DESC",
            excluded
        ).fetchall()

        totals = {(w['core'], w['wan']): (w['used_dl'], w['used_ul']) for w in wans}

        if not new_devices:
            # Check if any WAN is overloaded beyond the rebalance threshold
            rebalance_needed = False
            for w in wans:
                cap = w['dl_limit'] + w['ul_limit']
                if cap > 0:
                    util = (w['used_dl'] + w['used_ul']) / cap
                    if util > self.WAN_REBALANCE_THRESHOLD:
                        logger.info(
                            f"WAN {w['wan']} on {w['core']} utilization {util:.0%} exceeds "
                            f"threshold {self.WAN_REBALANCE_THRESHOLD:.0%} — triggering rebalance"
                        )
                        rebalance_needed = True
                        break

            if not rebalance_needed:
                return totals

            # Full rebalance without wiping assignments first: recompute all placements,
            # then update rows in place.
            logger.info("Rebalancing all WAN assignments (delta update mode)...")
            for w in wans:
                w['used_dl'] = w['used_ul'] = 0
            new_devices = conn.execute(
                "SELECT code, download_max_mbps, upload_max_mbps FROM devices"
                + (excl_sql.replace("AND (source", "WHERE (source") if excl_sql else "")
                + " ORDER BY weight DESC",
                excluded
            ).fetchall()

        def _utilization(w):
            cap = w['dl_limit'] + w['ul_limit']
            return (w['used_dl'] + w['used_ul']) / cap if cap > 0 else float('inf')

        heap = [(_utilization(w), i) for i, w in enumerate(wans)]
        heapq.heapify(heap)

        assignments = []
        for code, dl, ul in new_devices:
            ratio, idx = heapq.heappop(heap)
            wan = wans[idx]
            assignments.append((wan['core'], wan['wan'], code))
            wan['used_dl'] += dl
            wan['used_ul'] += ul
            heapq.heappush(heap, (_utilization(wan), idx))

        conn.executemany(
            "UPDATE devices SET core_name=?, wan_name=? WHERE code=?",
            assignments
        )
        conn.commit()
        logger.info(f"Assigned {len(new_devices)} new device(s) across {len(wans)} WAN(s)")

        return {(w['core'], w['wan']): (w['used_dl'], w['used_ul']) for w in wans}

    def check_wan_capacity(self, wan_totals, cores):
        """
        Warn if any WAN's assigned load exceeds its configured limit.
        wan_totals: {(core_name, wan_name): (total_dl, total_ul)}
        """
        wan_limits = {}
        for core in cores:
            for i, wan in enumerate(core.get('wans', []), start=1):
                wan_limits[(core['name'], f"WAN{i}")] = (
                    wan.get('download_limit', 1000),
                    wan.get('upload_limit', 1000),
                )
        for (core_name, wan_name), (dl, ul) in wan_totals.items():
            dl_limit, ul_limit = wan_limits.get((core_name, wan_name), (0, 0))
            if dl_limit and dl > dl_limit:
                logger.warning(
                    f"WAN OVERLOAD: {core_name}/{wan_name} DL {dl:.0f} Mbps > limit {dl_limit} Mbps"
                )
            if ul_limit and ul > ul_limit:
                logger.warning(
                    f"WAN OVERLOAD: {core_name}/{wan_name} UL {ul:.0f} Mbps > limit {ul_limit} Mbps"
                )

    def _build_wan_cache(self, api, core):
        """Fetch current WAN address-list entries from the router and return a cache dict."""
        cache = {}
        wan_names = [f"WAN{i}" for i in range(1, len(core.get('wans', [])) + 1)]
        resource = api.get_resource('/ip/firewall/address-list')
        for wan_name in wan_names:
            try:
                for e in resource.get(list=wan_name):
                    if e.get('address') and '.id' in e:
                        cache[(wan_name, e['address'])] = e['.id']
            except Exception as ex:
                logger.warning(f"Cache init {wan_name} on {core['name']}: {ex}")
        logger.info(f"WAN cache built for {core['name']}: {len(cache)} entries")
        return cache

    def sync_wan_address_lists(self, conn, cores):
        """
        Sync address-list entries on each core router.

        Two-phase approach to minimise API connections:
          1. Build target state from DB and diff against the in-memory cache.
             If nothing changed, skip the router entirely — no connection made.
          2. When a diff is detected (or there is no cache yet), connect to the
             router and fetch the CURRENT address-list state to recompute the diff
             against real router data before issuing any add/remove calls.
        """
        for core in cores:
            core_key = core['address']

            # Phase 1 — build target from DB (free, no connection).
            target = set()
            for i, _ in enumerate(core.get('wans', []), start=1):
                wan_name = f"WAN{i}"
                for (ip,) in conn.execute(
                    "SELECT ipv4 FROM devices WHERE core_name=? AND wan_name=? AND ipv4 IS NOT NULL",
                    (core['name'], wan_name)
                ):
                    target.add((wan_name, ip))

            # Quick cache diff — skip the router if nothing looks changed.
            if core_key in self._cache:
                cached = self._cache[core_key]
                if target == set(cached):
                    continue

            # Phase 2 — connect and fetch the live state before writing anything.
            api = self._connect(core)
            if api is None:
                logger.warning(f"Skipping core {core['name']} — connection failed.")
                self._cache.pop(core_key, None)
                continue

            try:
                resource = api.get_resource('/ip/firewall/address-list')

                # Always refresh the cache from the router so the diff is accurate
                # and we never add entries that are already present.
                self._cache[core_key] = self._build_wan_cache(api, core)
                cache = self._cache[core_key]

                to_add    = target - set(cache)
                to_remove = set(cache) - target

                if not to_add and not to_remove:
                    continue

                for key in to_remove:
                    list_name, ip = key
                    try:
                        resource.remove(id=cache[key])
                        logger.info(f"Removed {ip} from {list_name} on {core['name']}")
                    except Exception as ex:
                        logger.warning(f"Failed to remove {ip} from {list_name}: {ex}")
                    cache.pop(key, None)

                for key in to_add:
                    list_name, ip = key
                    try:
                        new_id = resource.add(list=list_name, address=ip, comment='libreqos-managed')
                        cache[key] = new_id
                        logger.info(f"Added {ip} to {list_name} on {core['name']}")
                    except Exception as ex:
                        logger.warning(f"Failed to add {ip} to {list_name}: {ex}")

                logger.info(f"{core['name']} WAN sync: +{len(to_add)} / -{len(to_remove)}")

            except Exception as ex:
                logger.error(f"Error syncing address lists on {core['name']}: {ex}")
                self._cache.pop(core_key, None)
