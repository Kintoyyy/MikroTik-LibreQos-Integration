import csv
import json
import logging
import time
import random
import string
import subprocess
import sqlite3
import routeros_api
import os
import re
import heapq
from datetime import datetime

# --- Constants ---
CONFIG_JSON = 'config.json'
SHAPED_DEVICES_CSV = 'ShapedDevices.csv'
NETWORK_JSON = 'network.json'
DB_FILE = 'devices.db'

FIELDNAMES = [
    'Circuit ID', 'Circuit Name', 'Device ID', 'Device Name', 'Parent Node',
    'MAC', 'IPv4', 'IPv6', 'Download Min Mbps', 'Upload Min Mbps',
    'Download Max Mbps', 'Upload Max Mbps', 'Comment'
]

SCAN_INTERVAL = 600
ERROR_RETRY_INTERVAL = 30
MIN_RATE_PERCENTAGE = 0.5
MAX_RATE_PERCENTAGE = 1.15
ID_LENGTH = 8
DEFAULT_BANDWIDTH = 1000

# Integration strategies (LibreQoS Scale Planning)
STRATEGY_FLAT    = 'flat'     # No parent hierarchy; empty network.json (max perf, min visibility)
STRATEGY_AP_ONLY = 'ap_only'  # Devices grouped under their router as parent node
STRATEGY_AP_SITE = 'ap_site'  # Devices grouped under site → router hierarchy
STRATEGY_FULL    = 'full'     # Full path shaping; pair with promote_to_root if single-core saturates
STRATEGY_CPU     = 'cpu'      # Greedy bin-pack across CPU nodes (current default)

TC_U16_WARN_THRESHOLD   = 60_000  # Warn before approaching TC classifier u16 overflow (~65535)
WAN_REBALANCE_THRESHOLD = 1.10    # Trigger full WAN rebalance when any WAN load exceeds 110% of its limit

# Compiled once at module level
RE_LIST_RATE = re.compile(r'(\d+(?:\.\d+)?[kmgKMG])/(\d+(?:\.\d+)?[kmgKMG])')
RE_BANDWIDTH = re.compile(r'(\d+(?:\.\d+)?)([kmgKMG])?')

# Higher number = higher priority. If same IP exists from a higher-priority source,
# the lower-priority source is skipped.
SOURCE_PRIORITY = {'pppoe': 4, 'hotspot': 3, 'dhcp': 2, 'address_list': 1}

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────────────

def generate_short_id(length=ID_LENGTH):
    return ''.join(random.choices(string.digits + string.ascii_uppercase, k=length))


def convert_to_mbps(value_str):
    try:
        if not value_str or value_str == '0':
            return '0'
        m = RE_BANDWIDTH.match(value_str.strip())
        if not m:
            return '0'
        number = float(m.group(1))
        unit = (m.group(2) or '').lower()
        if unit == 'k':
            return str(round(number / 1000, 2))
        elif unit == 'g':
            return str(round(number * 1000, 2))
        return str(round(number, 2))
    except Exception as e:
        logger.warning(f"Could not convert bandwidth '{value_str}': {e}")
        return '0'


def is_valid_rate(rx, tx):
    """Return True only if both values parse as positive numbers."""
    try:
        return float(rx) > 0 and float(tx) > 0
    except (ValueError, TypeError):
        return False


def parse_rate(rate_str):
    """
    Parse a rate string like '50M/50M'. The entire string must match.
    Returns (rx_mbps, tx_mbps) if valid, else None.
    """
    if not rate_str:
        return None
    m = RE_LIST_RATE.fullmatch(rate_str.strip())
    if m:
        rx = convert_to_mbps(m.group(1))
        tx = convert_to_mbps(m.group(2))
        if is_valid_rate(rx, tx):
            return rx, tx
    return None


def calculate_min_rates(max_rx, max_tx):
    try:
        rx, tx = float(max_rx), float(max_tx)
    except (ValueError, TypeError):
        rx, tx = 0, 0
    return max(int(rx * MIN_RATE_PERCENTAGE), 1), max(int(tx * MIN_RATE_PERCENTAGE), 1)


def calculate_max_rates(rx, tx):
    try:
        rx_f, tx_f = float(rx), float(tx)
    except (ValueError, TypeError):
        rx_f, tx_f = 0, 0
    return max(int(rx_f * MAX_RATE_PERCENTAGE), 1), max(int(tx_f * MAX_RATE_PERCENTAGE), 1)


def build_comment(source, rate_str, rate_failed, scan_time):
    """Format: 'source | rate | YYYY-MM-DD HH:MM:SS'"""
    rate_label = '[default]' if rate_failed else (rate_str or '[default]')
    ts = datetime.fromtimestamp(scan_time).strftime('%Y-%m-%d %H:%M:%S')
    return f"{source} | {rate_label} | {ts}"


def extract_first_rate(text):
    """
    Return the first X/X rate token found in a free-form string, or '' if none.
    Handles comma/space separated lists and MikroTik rate-limit strings.
    """
    if not text:
        return ''
    for token in re.split(r'[\s,]+', text.strip()):
        token = token.strip()
        if token and parse_rate(token):
            return token
    return ''


def resolve_rate_with_fallback(list_name, comment_str, rate_limit_str, default_dl, default_ul):
    """
    Rate resolution fallback chain:
      1. address-list name  (e.g. '50M/50M')
      2. comment field
      3. rate-limit / rate field
      4. config default
    Returns (rx_max, tx_max, rx_min, tx_min, rate_failed, rate_source, rate_str_used).
    """
    for source, raw in [
        ('address_list', list_name),
        ('comment',      comment_str),
        ('rate_limit',   rate_limit_str),
    ]:
        token = extract_first_rate(raw)
        if token:
            rx_max, tx_max, rx_min, tx_min, failed = resolve_rates(token, default_dl, default_ul)
            return rx_max, tx_max, rx_min, tx_min, failed, source, token

    rx_max, tx_max, rx_min, tx_min, _ = resolve_rates('', default_dl, default_ul)
    return rx_max, tx_max, rx_min, tx_min, True, 'default', ''


def resolve_rates(rate_str, default_dl, default_ul):
    """
    Try to parse rate_str. If valid, apply MAX/MIN multipliers.
    Falls back to config defaults.
    Returns (rx_max, tx_max, rx_min, tx_min, rate_failed).
    rate_failed is True when rate_str could not be parsed and defaults were used.
    """
    rate = parse_rate(rate_str)
    rate_failed = rate is None
    rx_raw, tx_raw = rate if rate else (str(default_dl), str(default_ul))
    rx_max, tx_max = calculate_max_rates(rx_raw, tx_raw)
    rx_min, tx_min = calculate_min_rates(rx_max, tx_max)
    return rx_max, tx_max, rx_min, tx_min, rate_failed


# ── Config ───────────────────────────────────────────────────────────────────

def read_config_json():
    """
    Returns (routers, strategy, queues, promote_to_root).

    Strategy resolution order:
      1. Explicit 'strategy' key in config.json
      2. Legacy 'no_parent: true'  → STRATEGY_FLAT
      3. Legacy 'queues: false'    → no network.json (queues=None, strategy=STRATEGY_FLAT)
      4. Default                   → STRATEGY_CPU with auto cpu_count
    """
    try:
        with open(CONFIG_JSON, 'r') as f:
            config = json.load(f)
        routers = config.get('bras', [])
        cores = config.get('cores', [])
        promote_to_root = config.get('promote_to_root', False)

        # Strategy resolution
        if 'strategy' in config:
            strategy = config['strategy']
            valid = {STRATEGY_FLAT, STRATEGY_AP_ONLY, STRATEGY_AP_SITE, STRATEGY_FULL, STRATEGY_CPU}
            if strategy not in valid:
                logger.warning(f"Unknown strategy '{strategy}', falling back to '{STRATEGY_CPU}'")
                strategy = STRATEGY_CPU
        elif config.get('no_parent', False):
            strategy = STRATEGY_FLAT
        else:
            strategy = STRATEGY_CPU

        # Queue count (only relevant for STRATEGY_CPU / promote_to_root)
        queues_raw = config.get('queues', True)
        if queues_raw is False:
            queues = None  # skip network.json entirely
        elif queues_raw is True:
            queues = os.cpu_count() or 4
        else:
            queues = int(queues_raw)

        # Auto-increment duplicate router names
        seen_names = {}
        for router in routers:
            base = router['name']
            if base in seen_names:
                seen_names[base] += 1
                router['name'] = f"{base} {seen_names[base]}"
            else:
                seen_names[base] = 1

        wan_cfg = config.get('wan_assignment', {})
        wan_sources = {
            'include_hotspot': wan_cfg.get('include_hotspot', False),
            'include_dhcp':    wan_cfg.get('include_dhcp',    False),
        }

        logger.info(f"Read {len(routers)} routers, {len(cores)} cores from {CONFIG_JSON} (strategy={strategy})")
        return routers, strategy, queues, promote_to_root, cores, wan_sources
    except FileNotFoundError:
        logger.error(f"Config file not found: {CONFIG_JSON}")
        return [], STRATEGY_CPU, None, False, [], {}
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in config: {e}")
        return [], STRATEGY_CPU, None, False, [], {}
    except Exception as e:
        logger.error(f"Error reading config: {e}")
        return [], STRATEGY_CPU, None, False, [], {}


# ── Network JSON ─────────────────────────────────────────────────────────────

def read_network_json():
    try:
        if os.path.exists(NETWORK_JSON):
            with open(NETWORK_JSON, 'r') as f:
                return json.load(f)
        logger.info(f"Network JSON not found, will create new.")
        return {}
    except Exception as e:
        logger.error(f"Error reading network JSON: {e}")
        return {}


def write_network_json(data):
    try:
        with open(NETWORK_JSON, 'w') as f:
            json.dump(data, f, indent=4)
        logger.info(f"Wrote network config to {NETWORK_JSON}")
    except Exception as e:
        logger.error(f"Error writing network JSON: {e}")


def assign_cpu_nodes(conn, cpu_count):
    """
    Distribute all devices across CPU0..CPU{n-1} parent nodes using greedy
    bin-packing: sort by weight descending, assign each device to the
    least-loaded CPU. Updates parent_node in DB.
    Returns {cpu_name: (total_dl_mbps, total_ul_mbps)}.
    """
    devices = conn.execute(
        "SELECT code, download_max_mbps, upload_max_mbps FROM devices ORDER BY weight DESC"
    ).fetchall()

    # Min-heap: (current_total_load, cpu_index)
    heap = [(0, i) for i in range(cpu_count)]
    heapq.heapify(heap)

    cpu_totals = {f"CPU{i}": [0, 0] for i in range(cpu_count)}
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


def assign_wan_nodes(conn, cores, wan_sources=None):
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
                if util > WAN_REBALANCE_THRESHOLD:
                    logger.info(
                        f"WAN {w['wan']} on {w['core']} utilization {util:.0%} exceeds "
                        f"threshold {WAN_REBALANCE_THRESHOLD:.0%} — triggering rebalance"
                    )
                    rebalance_needed = True
                    break

        if not rebalance_needed:
            return totals

        # Full rebalance: clear all WAN assignments so every device gets reassigned
        logger.info("Rebalancing all WAN assignments...")
        conn.execute("UPDATE devices SET core_name='', wan_name=''")
        conn.commit()
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


def check_wan_capacity(wan_totals, cores):
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


# Per-core cache: {core_address: {(list_name, ip): entry_id}}
# Populated on first contact, updated incrementally — avoids re-fetching every cycle.
_wan_cache: dict = {}


def _build_wan_cache(api, core):
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


def sync_wan_address_lists(conn, cores):
    """
    Sync address-list entries on each core router.
    Uses an in-memory cache so the router is only queried once per process start.
    Subsequent cycles only push actual deltas (add/remove).
    """
    global _wan_cache

    for core in cores:
        core_key = core['address']
        api = connect_to_router(core)
        if api is None:
            logger.warning(f"Skipping core {core['name']} — connection failed.")
            _wan_cache.pop(core_key, None)   # invalidate so next success re-fetches
            continue

        try:
            resource = api.get_resource('/ip/firewall/address-list')

            # Build cache on first contact with this core
            if core_key not in _wan_cache:
                _wan_cache[core_key] = _build_wan_cache(api, core)
            cache = _wan_cache[core_key]

            # Build target state from DB: {(wan_name, ip)}
            target = set()
            for i, _ in enumerate(core.get('wans', []), start=1):
                wan_name = f"WAN{i}"
                for (ip,) in conn.execute(
                    "SELECT ipv4 FROM devices WHERE core_name=? AND wan_name=? AND ipv4 IS NOT NULL",
                    (core['name'], wan_name)
                ):
                    target.add((wan_name, ip))

            current = set(cache)
            to_add    = target  - current
            to_remove = current - target

            # Remove stale entries
            for key in to_remove:
                list_name, ip = key
                try:
                    resource.remove(id=cache[key])
                    logger.info(f"Removed {ip} from {list_name} on {core['name']}")
                except Exception as ex:
                    logger.warning(f"Failed to remove {ip} from {list_name}: {ex}")
                cache.pop(key, None)

            # Add new entries
            for key in to_add:
                list_name, ip = key
                try:
                    new_id = resource.add(list=list_name, address=ip, comment='libreqos-managed')
                    cache[key] = new_id
                    logger.info(f"Added {ip} to {list_name} on {core['name']}")
                except Exception as ex:
                    if 'already have such entry' in str(ex):
                        cache[key] = None   # exists on router, mark in cache to skip next time
                        logger.debug(f"Already exists: {ip} in {list_name} — cached")
                    else:
                        logger.warning(f"Failed to add {ip} to {list_name}: {ex}")

            if to_add or to_remove:
                logger.info(f"{core['name']} WAN sync: +{len(to_add)} / -{len(to_remove)}")

        except Exception as ex:
            logger.error(f"Error syncing address lists on {core['name']}: {ex}")
            _wan_cache.pop(core_key, None)   # invalidate cache so next cycle re-fetches


def backup_files():
    """
    Rollout checklist: back up network.json and ShapedDevices.csv before any write.
    Backups are named *.bak and overwritten each cycle.
    """
    for path in (NETWORK_JSON, SHAPED_DEVICES_CSV):
        if os.path.exists(path):
            try:
                import shutil
                shutil.copy2(path, path + '.bak')
                logger.debug(f"Backed up {path} → {path}.bak")
            except Exception as e:
                logger.warning(f"Could not back up {path}: {e}")


def check_tc_u16_overflow(conn):
    """
    Warn when total device count approaches the TC u16 classifier limit (~65535).
    See: LibreQoS Troubleshooting — TC_U16_OVERFLOW urgent code.
    """
    total = conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
    if total >= TC_U16_WARN_THRESHOLD:
        logger.warning(
            f"TC_U16_OVERFLOW RISK: {total} devices in DB (threshold={TC_U16_WARN_THRESHOLD}). "
            "Consider reducing topology depth (full → ap_site/ap_only) or increasing queue parallelism."
        )
    return total


def check_distribution_skew(totals: dict, label: str = "node"):
    """
    Warn if the max/min load ratio across nodes is greater than 2:1.
    Skewed distribution is a sign that one core will saturate while others stay idle.
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


def assign_router_nodes(conn, routers):
    """
    ap_only strategy: assign parent_node = router name for all devices from that router.
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
        conn.execute(
            "UPDATE devices SET parent_node = ? WHERE router = ?", (name, name)
        )
    conn.commit()
    logger.info(f"Assigned router-level parent nodes for {len(routers)} routers")
    return router_totals


def assign_site_nodes(conn, routers):
    """
    ap_site / full strategy: assign parent_node = router name, with an optional
    site node above. Routers may have a 'site' key in config.json.
    Returns {node_name: (total_dl_mbps, total_ul_mbps)} for every site and router node.
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
            # Router node is under the site
            node_totals[name] = (dl, ul, site)  # (dl, ul, parent_site)
            if site not in node_totals:
                node_totals[site] = (0, 0, '')
            # Accumulate into site totals
            s_dl, s_ul, _ = node_totals[site]
            node_totals[site] = (s_dl + dl, s_ul + ul, '')
        else:
            node_totals[name] = (dl, ul, '')

    conn.commit()
    logger.info(f"Assigned site/router-level parent nodes for {len(routers)} routers")
    return node_totals


def update_network_json_by_router(router_totals):
    """Build network.json with each router as a top-level node (ap_only strategy)."""
    network_config = {
        name: {
            "downloadBandwidthMbps": max(int(dl * 1.1), 1),
            "uploadBandwidthMbps":   max(int(ul * 1.1), 1),
            "type": "ap",
            "children": {}
        }
        for name, (dl, ul) in router_totals.items()
    }
    write_network_json(network_config)
    return network_config


def update_network_json_by_site(node_totals):
    """
    Build network.json with site → router hierarchy (ap_site / full strategy).
    node_totals values: (dl, ul, parent_site_name_or_empty).
    """
    network_config = {}

    # First pass: create all site nodes
    for name, (dl, ul, parent) in node_totals.items():
        if not parent:  # top-level (site or router without a site)
            network_config[name] = {
                "downloadBandwidthMbps": max(int(dl * 1.1), 1),
                "uploadBandwidthMbps":   max(int(ul * 1.1), 1),
                "type": "site",
                "children": {}
            }

    # Second pass: nest routers under their site
    for name, (dl, ul, parent) in node_totals.items():
        if parent and parent in network_config:
            network_config[parent]["children"][name] = {
                "downloadBandwidthMbps": max(int(dl * 1.1), 1),
                "uploadBandwidthMbps":   max(int(ul * 1.1), 1),
                "type": "ap",
                "children": {}
            }

    write_network_json(network_config)
    return network_config


def update_network_json(cpu_totals):
    network_config = {
        cpu: {
            "downloadBandwidthMbps": max(int(dl * 1.1), 1),
            "uploadBandwidthMbps": max(int(ul * 1.1), 1),
            "type": "site",
            "children": {}
        }
        for cpu, (dl, ul) in cpu_totals.items()
    }
    write_network_json(network_config)
    return network_config


# ── Router connection ─────────────────────────────────────────────────────────

def connect_to_router(router, retries=3):
    for attempt in range(retries):
        try:
            api = routeros_api.RouterOsApiPool(
                router['address'],
                username=router['username'],
                password=router['password'],
                port=router['port'],
                plaintext_login=True,
            ).get_api()
            logger.info(f"Connected to {router['name']} ({router['address']}) [attempt {attempt+1}]")
            return api
        except Exception as e:
            logger.warning(f"Connection error to {router['name']} [attempt {attempt+1}/{retries}]: {e}")
            if attempt == retries - 1:
                logger.error(f"Failed to connect to {router['name']}")
                return None
            time.sleep(5)


def get_resource_data(api, resource_path):
    try:
        return api.get_resource(resource_path).get()
    except Exception as e:
        logger.error(f"Failed to fetch {resource_path}: {e}")
        return []


# ── SQLite ────────────────────────────────────────────────────────────────────

_CREATE_DEVICES_SQL = """
    CREATE TABLE IF NOT EXISTS devices (
        code            TEXT PRIMARY KEY,
        circuit_id      TEXT NOT NULL UNIQUE,
        device_id       TEXT NOT NULL UNIQUE,
        parent_node     TEXT DEFAULT '',
        mac             TEXT DEFAULT '',
        ipv4            TEXT UNIQUE,
        ipv6            TEXT UNIQUE,
        download_min_mbps INT NOT NULL DEFAULT 0 CHECK(download_min_mbps > 0),
        upload_min_mbps   INT NOT NULL DEFAULT 0 CHECK(upload_min_mbps > 0),
        download_max_mbps INT NOT NULL DEFAULT 0 CHECK(download_max_mbps > 0),
        upload_max_mbps   INT NOT NULL DEFAULT 0 CHECK(upload_max_mbps > 0),
        comment         TEXT DEFAULT '',
        source          TEXT DEFAULT '',
        router          TEXT DEFAULT '',
        last_seen       REAL DEFAULT 0,
        is_static       INTEGER DEFAULT 0,
        weight          INT NOT NULL DEFAULT 0,
        core_name       TEXT DEFAULT '',
        wan_name        TEXT DEFAULT ''
    )
"""

def open_db():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")

    # Check if existing table has UNIQUE constraints; migrate if not
    schema_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='devices'"
    ).fetchone()

    needs_migration = schema_row and (
        'UNIQUE' not in schema_row[0] or "CHECK" not in schema_row[0]
    )
    if needs_migration:
        logger.info("Migrating devices table schema...")
        conn.execute("ALTER TABLE devices RENAME TO devices_backup")
        conn.execute(_CREATE_DEVICES_SQL)
        # NULLIF converts '' → NULL (required for UNIQUE on ipv4/ipv6)
        # MAX(..., 1) ensures CHECK(> 0) is satisfied when casting old TEXT '0' values
        conn.execute("""
            INSERT OR IGNORE INTO devices
            SELECT code, circuit_id, device_id, parent_node, mac,
                   NULLIF(ipv4, ''), NULLIF(ipv6, ''),
                   MAX(CAST(download_min_mbps AS INT), 1),
                   MAX(CAST(upload_min_mbps   AS INT), 1),
                   MAX(CAST(download_max_mbps AS INT), 1),
                   MAX(CAST(upload_max_mbps   AS INT), 1),
                   comment, source, router, last_seen, is_static
            FROM devices_backup
        """)
        conn.execute("DROP TABLE devices_backup")
        logger.info("Migration complete.")
    else:
        conn.execute(_CREATE_DEVICES_SQL)

    # Add missing columns non-destructively
    cols = {row[1] for row in conn.execute("PRAGMA table_info(devices)")}
    if 'weight' not in cols:
        conn.execute("ALTER TABLE devices ADD COLUMN weight INT NOT NULL DEFAULT 0")
        conn.execute("UPDATE devices SET weight = download_max_mbps + upload_max_mbps")
        logger.info("Added weight column to devices table")
    if 'core_name' not in cols:
        conn.execute("ALTER TABLE devices ADD COLUMN core_name TEXT DEFAULT ''")
        logger.info("Added core_name column to devices table")
    if 'wan_name' not in cols:
        conn.execute("ALTER TABLE devices ADD COLUMN wan_name TEXT DEFAULT ''")
        logger.info("Added wan_name column to devices table")

    conn.commit()
    return conn


def upsert_device(conn, code, parent_node, mac, ipv4, comment, source, router_name,
                  rx_max, tx_max, rx_min, tx_min, scan_time):
    """
    Insert or update a device. Returns True if data changed.

    IPv4 conflict resolution: if the same IP already exists under a different code,
    the entry with the higher SOURCE_PRIORITY wins. Lower-priority source is skipped.
    If priorities are equal the existing entry is kept.
    """
    new_priority = SOURCE_PRIORITY.get(source, 0)

    # ── Check for IPv4 conflict with a different code ──────────────────────
    if ipv4:
        conflict = conn.execute(
            "SELECT code, source FROM devices WHERE ipv4 = ? AND code != ?",
            (ipv4, code)
        ).fetchone()

        if conflict:
            conflict_code, conflict_source = conflict
            existing_priority = SOURCE_PRIORITY.get(conflict_source, 0)

            if new_priority > existing_priority:
                # New source wins — remove the lower-priority duplicate
                conn.execute("DELETE FROM devices WHERE code = ?", (conflict_code,))
                logger.info(f"Replaced {conflict_code} ({conflict_source}) with {code} ({source}) for IP {ipv4}")
            else:
                # Existing source wins — skip this entry
                logger.debug(f"Skipping {code} ({source}) — IP {ipv4} already owned by {conflict_code} ({conflict_source})")
                return False

    # ── Normal upsert ──────────────────────────────────────────────────────
    row = conn.execute(
        "SELECT circuit_id, device_id, parent_node, mac, ipv4, comment, "
        "download_max_mbps, upload_max_mbps, download_min_mbps, upload_min_mbps, is_static "
        "FROM devices WHERE code = ?", (code,)
    ).fetchone()

    if row:
        (circuit_id, device_id, old_parent, old_mac, old_ipv4, old_comment,
         old_dlmax, old_ulmax, old_dlmin, old_ulmin, is_static) = row

        conn.execute("UPDATE devices SET last_seen = ? WHERE code = ?", (scan_time, code))

        if is_static:
            return False

        new_vals = (parent_node, mac, ipv4, comment, rx_max, tx_max, rx_min, tx_min)
        old_vals = (old_parent, old_mac, old_ipv4, old_comment, old_dlmax, old_ulmax, old_dlmin, old_ulmin)
        if new_vals != old_vals:
            conn.execute("""
                UPDATE devices
                SET parent_node=?, mac=?, ipv4=?, comment=?, source=?, router=?,
                    download_max_mbps=?, upload_max_mbps=?, download_min_mbps=?, upload_min_mbps=?,
                    weight=?
                WHERE code = ?
            """, (parent_node, mac, ipv4, comment, source, router_name,
                  rx_max, tx_max, rx_min, tx_min, rx_max + tx_max, code))
            logger.debug(f"Updated {code}")
            return True
        return False
    else:
        conn.execute("""
            INSERT INTO devices (code, circuit_id, device_id, parent_node, mac, ipv4, ipv6,
                comment, source, router, download_max_mbps, upload_max_mbps,
                download_min_mbps, upload_min_mbps, last_seen, is_static, weight)
            VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
        """, (code, generate_short_id(), generate_short_id(), parent_node, mac,
              ipv4 or None,  # NULL instead of '' so UNIQUE allows multiple "no IP" rows
              comment, source, router_name, rx_max, tx_max, rx_min, tx_min, scan_time,
              rx_max + tx_max))
        logger.info(f"New device: {code} (source={source}, IP={ipv4})")
        return True


def remove_inactive(conn, scan_time):
    """Remove devices not seen in the current scan (excluding static entries)."""
    cur = conn.execute(
        "DELETE FROM devices WHERE last_seen < ? AND is_static = 0", (scan_time,))
    count = cur.rowcount
    conn.commit()
    if count:
        logger.info(f"Removed {count} inactive device(s)")
    return count > 0


def export_to_csv(conn):
    """Export all devices from SQLite to ShapedDevices.csv."""
    rows = conn.execute("""
        SELECT circuit_id, code, device_id, code, parent_node, mac, ipv4, ipv6,
               download_min_mbps, upload_min_mbps, download_max_mbps, upload_max_mbps, comment
        FROM devices
        ORDER BY source, code
    """).fetchall()
    with open(SHAPED_DEVICES_CSV, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(FIELDNAMES)
        writer.writerows(rows)
    logger.info(f"Exported {len(rows)} devices to {SHAPED_DEVICES_CSV}")


# ── Data processors ───────────────────────────────────────────────────────────

def process_pppoe_users(api, conn, router, ip_to_list, parent_node, scan_time):
    """
    Active PPPoE sessions: get NAME, CALLER-ID (MAC), ADDRESS.
    Rate is looked up by IP in the address list. Falls back to config default.
    """
    if not router.get('pppoe', {}).get('enabled', False):
        logger.info(f"PPPoE disabled for {router['name']}")
        return False

    router_name = router['name']
    default_dl = router.get('pppoe', {}).get('default_download_limit', 10)
    default_ul = router.get('pppoe', {}).get('default_upload_limit', 10)
    changed = False

    sessions = get_resource_data(api, '/ppp/active')
    logger.info(f"PPPoE: {len(sessions)} active sessions on {router_name}")

    for session in sessions:
        name      = session.get('name', '')
        address   = session.get('address', '')
        caller_id = session.get('caller-id', '')

        if not address:
            continue

        mac  = caller_id.upper()
        code = f"PPP-{name}"

        list_name      = ip_to_list.get(address, '')
        comment_field  = session.get('comment', '')
        rate_limit_str = session.get('rate', '')

        rx_max, tx_max, rx_min, tx_min, rate_failed, rate_src, rate_used = \
            resolve_rate_with_fallback(list_name, comment_field, rate_limit_str, default_dl, default_ul)
        logger.debug(f"PPPoE {code}: IP={address} src={rate_src} rate='{rate_used}' → {rx_max}/{tx_max} Mbps")

        comment = build_comment('pppoe', rate_used or list_name, rate_failed, scan_time)

        if upsert_device(conn, code, parent_node, mac, address,
                         comment, 'pppoe', router_name,
                         rx_max, tx_max, rx_min, tx_min, scan_time):
            changed = True

    return changed


def process_hotspot_users(api, conn, router, ip_to_list, parent_node, scan_time):
    """
    Active hotspot sessions: get USER, MAC-ADDRESS, ADDRESS.
    Rate is looked up by IP in the address list. Falls back to config default.
    """
    if not router.get('hotspot', {}).get('enabled', False):
        logger.info(f"Hotspot disabled for {router['name']}")
        return False

    router_name = router['name']
    default_dl = router.get('hotspot', {}).get('default_download_limit', 10)
    default_ul = router.get('hotspot', {}).get('default_upload_limit', 10)
    changed = False

    users = get_resource_data(api, '/ip/hotspot/active')
    logger.info(f"Hotspot: {len(users)} active users on {router_name}")

    for user in users:
        username = user.get('user', '')
        mac      = user.get('mac-address', '').upper()
        address  = user.get('address', '')

        if not username and not mac:
            continue

        if not address:
            continue

        mac_clean = mac.replace(':', '')
        code = f"HS-{mac_clean}" if mac_clean else f"HS-{username}"

        list_name      = ip_to_list.get(address, '')
        comment_field  = user.get('comment', '')
        rate_limit_str = user.get('rate', '')

        rx_max, tx_max, rx_min, tx_min, rate_failed, rate_src, rate_used = \
            resolve_rate_with_fallback(list_name, comment_field, rate_limit_str, default_dl, default_ul)
        logger.debug(f"Hotspot {code}: IP={address} src={rate_src} rate='{rate_used}' → {rx_max}/{tx_max} Mbps")

        comment = build_comment('hotspot', rate_used or list_name, rate_failed, scan_time)

        if upsert_device(conn, code, parent_node, mac, address,
                         comment, 'hotspot', router_name,
                         rx_max, tx_max, rx_min, tx_min, scan_time):
            changed = True

    return changed


def process_dhcp_leases(api, conn, router, ip_to_list, parent_node, scan_time):
    """
    DHCP leases: only include leases whose address-list field contains a valid
    X/X rate AND whose IP is present in the firewall address list.
    """
    if not router.get('dhcp', {}).get('enabled', False):
        logger.info(f"DHCP disabled for {router['name']}")
        return False

    router_name = router['name']
    default_dl  = router.get('dhcp', {}).get('default_download_limit', 1000)
    default_ul  = router.get('dhcp', {}).get('default_upload_limit', 1000)
    changed = False

    leases = get_resource_data(api, '/ip/dhcp-server/lease')
    logger.info(f"DHCP: {len(leases)} leases on {router_name}")

    for lease in leases:
        mac = lease.get('mac-address', '').upper()
        if not mac:
            continue

        address  = lease.get('address', '')
        hostname = lease.get('host-name', '')

        # address-list field on the lease (e.g. "50M/50M")
        addr_list_field = lease.get('address-list', lease.get('address-lists', ''))
        comment_field   = lease.get('comment', '')
        rate_limit_str  = lease.get('rate-limit', '')

        rx_max, tx_max, rx_min, tx_min, rate_failed, rate_src, rate_used = \
            resolve_rate_with_fallback(addr_list_field, comment_field, rate_limit_str, default_dl, default_ul)
        logger.debug(f"DHCP {mac}: IP={address} src={rate_src} rate='{rate_used}' → {rx_max}/{tx_max} Mbps")

        mac_clean = mac.replace(':', '')
        code    = f"DHCP-{hostname}" if hostname else f"DHCP-{mac_clean}"
        comment = build_comment('dhcp', rate_used or addr_list_field, rate_failed, scan_time)

        if upsert_device(conn, code, parent_node, mac, address,
                         comment, 'dhcp', router_name,
                         rx_max, tx_max, rx_min, tx_min, scan_time):
            changed = True

    return changed


def process_address_list(conn, router, addr_list_entries, parent_node, scan_time):
    """
    Standalone address list entries: rate comes directly from the list name.
    Falls back to config default.
    """
    router_name = router['name']
    default_dl  = router.get('address_list', {}).get('default_download_limit', 100)
    default_ul  = router.get('address_list', {}).get('default_upload_limit', 100)
    changed = False

    entries = [
        e for e in addr_list_entries
        if e.get('disabled', 'false') != 'true'
        and e.get('address')
        and parse_rate(e.get('list', ''))
    ]
    logger.info(f"Address list: {len(entries)} entries for {router_name}")

    for entry in entries:
        address   = entry.get('address', '')
        list_name = entry.get('list', '')
        comment   = entry.get('comment', '')
        code = comment if comment else f"ADDR-{address}"

        rx_max, tx_max, rx_min, tx_min, rate_failed = resolve_rates(list_name, default_dl, default_ul)
        logger.debug(f"AddrList {code}: list='{list_name}' → {rx_max}/{tx_max} Mbps")

        entry_comment = build_comment('address_list', list_name, rate_failed, scan_time)

        if upsert_device(conn, code, parent_node, '', address,
                         entry_comment, 'address_list', router_name,
                         rx_max, tx_max, rx_min, tx_min, scan_time):
            changed = True

    return changed


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    logger.info("Starting MikroTik-LibreQoS integration")
    conn = open_db()

    while True:
        try:
            routers, strategy, queues, promote_to_root, cores, wan_sources = read_config_json()

            scan_time   = time.time()
            any_changes = False

            for router in routers:
                logger.info(f"Processing router: {router['name']} ({router['address']})")

                api = connect_to_router(router)
                if api is None:
                    logger.warning(f"Skipping {router['name']} — connection failed.")
                    continue

                try:
                    # ── Fetch all data at once ──────────────────────────────
                    addr_list_entries = get_resource_data(api, '/ip/firewall/address-list')

                    # Build IP→list_name map for PPPoE and Hotspot rate lookups
                    ip_to_list = {
                        e['address']: e.get('list', '')
                        for e in addr_list_entries
                        if e.get('address') and e.get('disabled', 'false') != 'true'
                    }

                    # For cpu strategy, parent_node is set later by assign_cpu_nodes.
                    # For ap_only/ap_site/full, we tag by router name so assign_*_nodes
                    # can group them correctly.
                    parent_node = '' if strategy == STRATEGY_CPU else ''

                    # ── Aggregate into SQLite ───────────────────────────────
                    if process_pppoe_users(api, conn, router, ip_to_list, parent_node, scan_time):
                        any_changes = True
                    if process_hotspot_users(api, conn, router, ip_to_list, parent_node, scan_time):
                        any_changes = True
                    if process_dhcp_leases(api, conn, router, ip_to_list, parent_node, scan_time):
                        any_changes = True
                    if process_address_list(conn, router, addr_list_entries, parent_node, scan_time):
                        any_changes = True

                    conn.commit()

                except Exception as e:
                    logger.error(f"Error processing router {router['name']}: {e}")
                    conn.rollback()

            # Remove devices not seen this scan
            if remove_inactive(conn, scan_time):
                any_changes = True

            if any_changes:
                # Rollout checklist: back up before any write
                backup_files()

                # TC_U16_OVERFLOW guard
                check_tc_u16_overflow(conn)

                # ── Build network.json and assign parent nodes by strategy ──
                if strategy == STRATEGY_FLAT:
                    write_network_json({})

                elif strategy == STRATEGY_AP_ONLY:
                    router_totals = assign_router_nodes(conn, routers)
                    check_distribution_skew(router_totals, label="router")
                    update_network_json_by_router(router_totals)

                elif strategy in (STRATEGY_AP_SITE, STRATEGY_FULL):
                    node_totals = assign_site_nodes(conn, routers)
                    # Build a (dl, ul) dict for skew check (ignore parent field)
                    flat_totals = {k: (dl, ul) for k, (dl, ul, *_) in node_totals.items()}
                    check_distribution_skew(flat_totals, label="site/router")
                    update_network_json_by_site(node_totals)
                    if promote_to_root and strategy == STRATEGY_FULL:
                        # promote_to_root: additionally distribute across CPU nodes
                        # to avoid single-core saturation on full-strategy networks
                        effective_queues = queues or (os.cpu_count() or 4)
                        cpu_totals = assign_cpu_nodes(conn, effective_queues)
                        check_distribution_skew(cpu_totals, label="CPU")
                        update_network_json(cpu_totals)

                elif strategy == STRATEGY_CPU:
                    if queues is not None:
                        cpu_totals = assign_cpu_nodes(conn, queues)
                        check_distribution_skew(cpu_totals, label="CPU")
                        update_network_json(cpu_totals)
                    else:
                        # queues: false — skip network.json
                        logger.info("Skipping network.json (queues=false)")

                # ── WAN assignment across cores ─────────────────────────────
                if cores:
                    wan_totals = assign_wan_nodes(conn, cores, wan_sources)
                    check_wan_capacity(wan_totals, cores)
                    sync_wan_address_lists(conn, cores)

                export_to_csv(conn)
                try:
                    result = subprocess.run(
                        ["/usr/bin/sudo", "/opt/libreqos/src/LibreQoS.py", "--updateonly"],
                        capture_output=True, text=True, check=True
                    )
                    logger.info("LibreQoS updated: " + result.stdout.strip())
                except subprocess.CalledProcessError as e:
                    logger.error(f"LibreQoS update failed: {e.stderr}")
                except Exception as e:
                    logger.error(f"Unexpected error running LibreQoS: {e}")
            else:
                logger.info("No changes detected.")

            logger.info(f"Scan complete. Next in {SCAN_INTERVAL}s.")
            time.sleep(SCAN_INTERVAL)

        except Exception as e:
            logger.error(f"Error in main loop: {e}")
            time.sleep(ERROR_RETRY_INTERVAL)


if __name__ == "__main__":
    main()
