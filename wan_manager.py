import heapq
import ipaddress
import logging
import time

from settings import WAN_REBALANCE_IMBALANCE, WAN_FULL_RECONCILE_INTERVAL

logger = logging.getLogger(__name__)

MANAGED_COMMENT = 'libreqos-managed'


def _wan_list(core):
    """[(wan_name, wan_cfg)] for a core; wan_name is the address-list name."""
    return [
        (wan.get('address_list') or f"WAN{i}", wan)
        for i, wan in enumerate(core.get('wans', []), start=1)
    ]


class WANManager:
    WAN_REBALANCE_IMBALANCE = WAN_REBALANCE_IMBALANCE
    # Even when the in-memory cache says nothing changed, force a live
    # re-verify against the router at least this often. This is the safety
    # net against drift: manual router edits, another process touching the
    # same address-list, or a partial failure that desynced the cache all
    # get self-healed on the next periodic reconcile instead of persisting
    # (and building up) forever.
    FULL_RECONCILE_INTERVAL = WAN_FULL_RECONCILE_INTERVAL

    def __init__(self, connect_fn):
        # connect_fn(router) is a context manager yielding a RouterOS API object
        # (or None) and closing the connection on exit — e.g. RouterScanner.open_api.
        # Injected to avoid circular imports.
        self._connect = connect_fn
        # Per-core cache: {core_address: {(list_name, ip): entry_id}}
        # Populated on first contact, updated incrementally — avoids re-fetching every cycle.
        # Only ever holds entries this manager owns (comment == MANAGED_COMMENT),
        # so it never tracks (and therefore never touches) unrelated address-list entries.
        self._cache: dict = {}
        # Per-core timestamp of the last live reconciliation against the router.
        self._last_verified: dict = {}

    @staticmethod
    def _utilization(w):
        cap = w['dl_limit'] + w['ul_limit']
        return (w['used_dl'] + w['used_ul']) / cap if cap > 0 else float('inf')

    def assign_wan_nodes(self, conn, cores, wan_sources=None):
        """
        Assign NEW (unassigned) devices to WANs using greedy bin-packing
        weighted by WAN capacity. Existing assignments are kept, so the
        address lists on core routers only receive incremental adds/removes.

        When there are no new devices and the WANs are out of balance (the
        busiest WAN's utilization is more than WAN_REBALANCE_IMBALANCE times
        the least busy one's), as few devices as possible are moved to even
        them out — every move changes a subscriber's exit WAN and breaks
        their NAT sessions.

        Returns {(core_name, wan_name): (total_dl_mbps, total_ul_mbps)}.
        """
        wans = []
        for core in cores:
            for wan_name, wan in _wan_list(core):
                wans.append({
                    'core':     core['name'],
                    'wan':      wan_name,
                    'dl_limit': wan.get('download_limit', 1000),
                    'ul_limit': wan.get('upload_limit', 1000),
                    'used_dl':  0,
                    'used_ul':  0,
                })

        if not wans:
            logger.info("No cores/WANs defined — skipping WAN assignment")
            return {}

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

            # Devices of a source that is now excluded leave the WAN lists.
            cur = conn.execute(
                f"UPDATE devices SET core_name='', wan_name='' WHERE source IN ({placeholders}) "
                "AND (COALESCE(core_name, '') != '' OR COALESCE(wan_name, '') != '')",
                excluded
            )
            if cur.rowcount:
                logger.info(f"Cleared WAN assignment of {cur.rowcount} device(s) from excluded sources")

        # Devices pinned to a WAN that is no longer configured get reassigned.
        configured = {(w['core'], w['wan']) for w in wans}
        for core_name, wan_name in conn.execute(
            "SELECT DISTINCT core_name, wan_name FROM devices "
            "WHERE COALESCE(core_name, '') != '' OR COALESCE(wan_name, '') != ''"
        ).fetchall():
            if (core_name, wan_name) not in configured:
                cur = conn.execute(
                    "UPDATE devices SET core_name='', wan_name='' WHERE core_name IS ? AND wan_name IS ?",
                    (core_name, wan_name)
                )
                logger.info(
                    f"WAN {core_name}/{wan_name} is no longer configured — "
                    f"reassigning {cur.rowcount} device(s)"
                )

        # Seed each WAN's current load from the DB so new devices are balanced
        # against what is already assigned, not from zero.
        for w in wans:
            row = conn.execute(
                "SELECT COALESCE(SUM(download_max_mbps),0), COALESCE(SUM(upload_max_mbps),0) "
                "FROM devices WHERE core_name=? AND wan_name=?" + excl_sql,
                [w['core'], w['wan']] + excluded
            ).fetchone()
            w['used_dl'], w['used_ul'] = row[0], row[1]

        # Only process devices that have not been assigned yet
        new_devices = conn.execute(
            "SELECT code, download_max_mbps, upload_max_mbps FROM devices "
            "WHERE (core_name IS NULL OR core_name = '' OR wan_name IS NULL OR wan_name = '')"
            + excl_sql + " ORDER BY weight DESC, code",
            excluded
        ).fetchall()

        if new_devices:
            heap = [(self._utilization(w), i) for i, w in enumerate(wans)]
            heapq.heapify(heap)

            assignments = []
            for code, dl, ul in new_devices:
                _, idx = heapq.heappop(heap)
                wan = wans[idx]
                assignments.append((wan['core'], wan['wan'], code))
                wan['used_dl'] += dl
                wan['used_ul'] += ul
                heapq.heappush(heap, (self._utilization(wan), idx))
            logger.info(f"Assigned {len(new_devices)} new device(s) across {len(wans)} WAN(s)")
        else:
            assignments = self._rebalance_moves(conn, wans, excl_sql, excluded)

        conn.executemany(
            "UPDATE devices SET core_name=?, wan_name=? WHERE code=?",
            assignments
        )
        conn.commit()

        return {(w['core'], w['wan']): (w['used_dl'], w['used_ul']) for w in wans}

    def _rebalance_moves(self, conn, wans, excl_sql, excluded):
        """
        Move as few devices as possible from the busiest WAN to the least busy
        one until their utilization ratio is within WAN_REBALANCE_IMBALANCE.
        Updates the in-memory WAN loads and returns [(core, wan, code)] moves.
        """
        usable = [w for w in wans if w['dl_limit'] + w['ul_limit'] > 0]
        if len(usable) < 2:
            return []

        devices_by_wan = {}
        for w in usable:
            devices_by_wan[(w['core'], w['wan'])] = conn.execute(
                "SELECT code, download_max_mbps, upload_max_mbps FROM devices "
                "WHERE core_name=? AND wan_name=?" + excl_sql + " ORDER BY weight DESC, code",
                [w['core'], w['wan']] + excluded
            ).fetchall()

        moves, moved = [], set()
        while True:
            hi = max(usable, key=self._utilization)
            lo = min(usable, key=self._utilization)
            u_hi, u_lo = self._utilization(hi), self._utilization(lo)
            if u_hi == 0 or (u_lo > 0 and u_hi / u_lo <= self.WAN_REBALANCE_IMBALANCE):
                break

            if not moves:
                logger.info(
                    f"WAN imbalance: {hi['core']}/{hi['wan']} at {u_hi:.0%} vs "
                    f"{lo['core']}/{lo['wan']} at {u_lo:.0%} "
                    f"(limit {self.WAN_REBALANCE_IMBALANCE:g}x) — moving devices"
                )

            # Load that, moved from hi to lo, would equalize their utilization.
            # Moving more than this only flips the imbalance the other way.
            cap_hi = hi['dl_limit'] + hi['ul_limit']
            cap_lo = lo['dl_limit'] + lo['ul_limit']
            target = ((hi['used_dl'] + hi['used_ul']) * cap_lo
                      - (lo['used_dl'] + lo['used_ul']) * cap_hi) / (cap_hi + cap_lo)

            pick = next(
                (d for d in devices_by_wan[(hi['core'], hi['wan'])]
                 if d[0] not in moved and d[1] + d[2] <= target),
                None
            )
            if pick is None:
                break

            code, dl, ul = pick
            hi['used_dl'] -= dl
            hi['used_ul'] -= ul
            lo['used_dl'] += dl
            lo['used_ul'] += ul
            moved.add(code)
            moves.append((lo['core'], lo['wan'], code))

        if moves:
            logger.info(f"Rebalance moved {len(moves)} device(s)")
        return moves

    def check_wan_capacity(self, wan_totals, cores):
        """
        Warn if any WAN's assigned load exceeds its configured limit.
        wan_totals: {(core_name, wan_name): (total_dl, total_ul)}
        """
        wan_limits = {}
        for core in cores:
            for wan_name, wan in _wan_list(core):
                wan_limits[(core['name'], wan_name)] = (
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
        for wan_name, _ in _wan_list(core):
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
        resource = api.get_resource('/ip/firewall/address-list')
        for wan_name, _ in _wan_list(core):
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
            with self._connect(core) as api:
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

            with self._connect(core) as api:
                if api is None:
                    results.append({'core': core_name, 'removed': 0, 'error': 'connection failed'})
                    continue

                try:
                    resource = api.get_resource('/ip/firewall/address-list')
                    for wan_name in {name for name, _ in _wan_list(core)}:
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
