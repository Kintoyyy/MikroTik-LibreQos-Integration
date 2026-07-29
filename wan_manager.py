import heapq
import ipaddress
import logging
import time

from settings import WAN_REBALANCE_THRESHOLD, WAN_FULL_RECONCILE_INTERVAL

logger = logging.getLogger(__name__)

MANAGED_COMMENT = 'libreqos-managed'


class WANManager:
    WAN_REBALANCE_THRESHOLD = WAN_REBALANCE_THRESHOLD
    # Even when the in-memory cache says nothing changed, force a live
    # re-verify against the router at least this often. This is the safety
    # net against drift: manual router edits, another process touching the
    # same address-list, or a partial failure that desynced the cache all
    # get self-healed on the next periodic reconcile instead of persisting
    # (and building up) forever.
    FULL_RECONCILE_INTERVAL = WAN_FULL_RECONCILE_INTERVAL

    def __init__(self, connect_fn):
        # connect_fn is connect_to_router from updatecsv.py, injected to avoid circular imports
        self._connect = connect_fn
        # Per-core cache: {core_address: {(list_name, ip): entry_id}}
        # Populated on first contact, updated incrementally — avoids re-fetching every cycle.
        # Only ever holds entries this manager owns (comment == MANAGED_COMMENT),
        # so it never tracks (and therefore never touches) unrelated address-list entries.
        self._cache: dict = {}
        # Per-core timestamp of the last live reconciliation against the router.
        self._last_verified: dict = {}

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
                    'wan':      wan.get('address_list', f"WAN{i}"),
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
                wan_limits[(core['name'], wan.get('address_list', f"WAN{i}"))] = (
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

    @staticmethod
    def _collapse_to_subnets(ips):
        """
        Collapse a list of IP strings into the minimal set of covering subnets.
        Uses ipaddress.collapse_addresses — only merges IPs that form complete
        CIDR blocks, so no unowned addresses are ever included.
        Returns a list of subnet strings (e.g. ['10.0.0.0/24', '10.0.2.0/23']).
        """
        networks = []
        for ip in ips:
            try:
                networks.append(ipaddress.ip_network(ip, strict=False))
            except ValueError:
                logger.debug(f"Skipping invalid IP for subnet calc: {ip}")
        if not networks:
            return []
        return [str(n) for n in ipaddress.collapse_addresses(networks)]

    def _build_target_subnets(self, conn, core):
        """
        Build target {(wan_name, subnet_str)} from DB IPs, collapsed per WAN.
        Collapsing means IPs within the same CIDR block become a single entry,
        so the address-list is smaller and changes less frequently.
        """
        target = set()
        for i, wan in enumerate(core.get('wans', []), start=1):
            wan_name = wan.get('address_list', f"WAN{i}")
            ips = [row[0] for row in conn.execute(
                "SELECT ipv4 FROM devices WHERE core_name=? AND wan_name=? AND ipv4 IS NOT NULL",
                (core['name'], wan_name)
            )]
            for subnet in self._collapse_to_subnets(ips):
                target.add((wan_name, subnet))
        return target

    @staticmethod
    def _normalize_address(addr: str) -> str:
        """
        Normalize an address string to canonical CIDR form so it can be compared
        against the output of _collapse_to_subnets.
        MikroTik may return bare host IPs ('10.0.0.1') while our target always
        produces CIDR notation ('10.0.0.1/32'). Normalizing both sides prevents
        false-positive diffs that would re-add already-present entries on every run.
        """
        try:
            return str(ipaddress.ip_network(addr, strict=False))
        except ValueError:
            return addr

    def _build_wan_cache(self, api, core):
        """
        Fetch current WAN address-list entries from the router.
        Addresses are normalized to CIDR form so cache keys always match the
        keys produced by _build_target_subnets / _collapse_to_subnets.

        Only entries tagged with MANAGED_COMMENT are cached. This scopes the
        cache to entries we actually own, so unrelated/manually-added entries
        in the same address-list are never adopted, diffed, or removed by us.
        """
        cache = {}
        wan_names = [
            wan.get('address_list', f"WAN{i}")
            for i, wan in enumerate(core.get('wans', []), start=1)
        ]
        resource = api.get_resource('/ip/firewall/address-list')
        for wan_name in wan_names:
            try:
                for e in resource.get(list=wan_name):
                    if e.get('address') and '.id' in e and e.get('comment') == MANAGED_COMMENT:
                        normalized = self._normalize_address(e['address'])
                        cache[(wan_name, normalized)] = e['.id']
            except Exception as ex:
                logger.warning(f"Cache init {wan_name} on {core['name']}: {ex}")
        logger.info(f"WAN cache built for {core['name']}: {len(cache)} entries")
        return cache

    def sync_wan_address_lists(self, conn, cores):
        """
        Sync address-list entries on each core router using subnet aggregation.

        IPs assigned to each WAN are collapsed into the minimal set of CIDR
        subnets before comparison. This means individual IP changes within the
        same subnet do not trigger any router update.

        Two-phase approach:
          1. Collapse DB IPs to subnets and diff against the in-memory cache.
             Skip the router entirely if the subnet set is unchanged AND we
             have verified against live router state recently (see
             FULL_RECONCILE_INTERVAL below).
          2. Otherwise: connect, re-fetch live state from the router,
             recompute the diff, then add/remove subnets as needed.

        A failed remove() is NOT dropped from the cache — if we did, the
        cache would believe the entry is gone while the router still has it,
        and it would never be retried, silently building up over time. Only
        confirmed removals are forgotten; confirmed adds are the only ones
        remembered. On top of that, every core is periodically forced through
        phase 2 regardless of whether the target looks unchanged, so any
        drift (a partial failure, a manual router edit, another process
        touching the same list) gets self-healed instead of accumulating
        indefinitely.

        Returns a summary dict: {ok, cores: [{core, added, removed, error}],
        total_added, total_removed, errors}.
        """
        summary = {'ok': True, 'cores': [], 'total_added': 0, 'total_removed': 0, 'errors': []}

        for core in cores:
            core_name = core['name']
            core_key = core['address']
            core_result = {'core': core_name, 'added': 0, 'removed': 0, 'error': ''}

            # Phase 1 — collapse IPs → subnets (free, no connection).
            target = self._build_target_subnets(conn, core)

            now = time.time()
            due_for_reconcile = (now - self._last_verified.get(core_key, 0)) > self.FULL_RECONCILE_INTERVAL
            if core_key in self._cache and not due_for_reconcile:
                if target == set(self._cache[core_key]):
                    summary['cores'].append(core_result)
                    continue

            # Phase 2 — subnets changed, or periodic reconcile is due; verify against live state.
            api = self._connect(core)
            if api is None:
                logger.warning(f"Skipping core {core_name} — connection failed.")
                self._cache.pop(core_key, None)
                core_result['error'] = 'connection failed'
                summary['ok'] = False
                summary['errors'].append(f"{core_name}: connection failed")
                summary['cores'].append(core_result)
                continue

            try:
                resource = api.get_resource('/ip/firewall/address-list')

                # Re-fetch from router so we never add subnets already present,
                # and so any entries added/removed by drift are picked up.
                self._cache[core_key] = self._build_wan_cache(api, core)
                cache = self._cache[core_key]

                to_add    = target - set(cache)
                to_remove = set(cache) - target

                for key in to_remove:
                    list_name, subnet = key
                    try:
                        resource.remove(id=cache[key])
                        cache.pop(key, None)
                        core_result['removed'] += 1
                        logger.info(f"Removed {subnet} from {list_name} on {core_name}")
                    except Exception as ex:
                        # Do NOT pop from cache — keep tracking it so the next
                        # cycle retries the removal instead of forgetting it.
                        logger.warning(f"Failed to remove {subnet} from {list_name}: {ex} — will retry")

                for key in to_add:
                    list_name, subnet = key
                    try:
                        new_id = resource.add(list=list_name, address=subnet, comment=MANAGED_COMMENT)
                        cache[key] = new_id
                        core_result['added'] += 1
                        logger.info(f"Added {subnet} to {list_name} on {core_name}")
                    except Exception as ex:
                        logger.warning(f"Failed to add {subnet} to {list_name}: {ex} — will retry")

                self._last_verified[core_key] = now

                if core_result['added'] or core_result['removed']:
                    logger.info(f"{core_name} WAN sync: +{core_result['added']} / -{core_result['removed']}")

            except Exception as ex:
                logger.error(f"Error syncing address lists on {core_name}: {ex}")
                self._cache.pop(core_key, None)
                core_result['error'] = str(ex)
                summary['ok'] = False
                summary['errors'].append(f"{core_name}: {ex}")

            summary['total_added'] += core_result['added']
            summary['total_removed'] += core_result['removed']
            summary['cores'].append(core_result)

        return summary

    def purge_wan_entries(self, cores):
        """
        Remove every MANAGED_COMMENT-tagged address-list entry from each core
        router (routing decisions belong in the DB, not the router, so a
        purge should only ever touch entries we own — never bare/unrelated
        address-list entries an admin may have added by hand).

        This is the only code path that should ever bulk-delete WAN
        address-list entries. Deleting through here (instead of a separate,
        parallel implementation) keeps this manager's cache — the source of
        truth for what's on the router — consistent with reality: the
        per-core cache is invalidated once the purge completes, forcing the
        next sync_wan_address_lists() call to do a full live reconcile rather
        than trusting stale in-memory state that may no longer match the
        router (which is exactly what let entries silently survive a
        purge+rebalance before).

        Returns a list of {core, removed, error} dicts, one per core.
        """
        results = []
        for core in cores:
            core_name = core['name']
            core_key = core['address']
            removed = 0
            error = None

            api = self._connect(core)
            if api is None:
                results.append({'core': core_name, 'removed': 0, 'error': 'connection failed'})
                continue

            try:
                resource = api.get_resource('/ip/firewall/address-list')
                wan_names = {
                    wan.get('address_list', f"WAN{i}")
                    for i, wan in enumerate(core.get('wans', []), start=1)
                }
                for wan_name in wan_names:
                    try:
                        entries = resource.get(list=wan_name)
                    except Exception as ex:
                        logger.warning(f"Purge fetch {wan_name} on {core_name}: {ex}")
                        continue
                    for e in entries:
                        if e.get('comment') == MANAGED_COMMENT and '.id' in e:
                            try:
                                resource.remove(id=e['.id'])
                                removed += 1
                            except Exception as ex:
                                logger.warning(f"Purge remove failed on {core_name}/{wan_name}: {ex}")
            except Exception as ex:
                error = str(ex)
                logger.error(f"Purge error on {core_name}: {ex}")
            finally:
                # Force the next sync to rebuild from live router state —
                # never trust the pre-purge cache again.
                self._cache.pop(core_key, None)
                self._last_verified.pop(core_key, None)

            result = {'core': core_name, 'removed': removed}
            if error:
                result['error'] = error
            results.append(result)
            logger.info(f"Purged {removed} managed WAN entries from {core_name}")

        return results
