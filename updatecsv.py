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
    try:
        with open(CONFIG_JSON, 'r') as f:
            config = json.load(f)
        routers = config.get('routers', [])
        flat_network = config.get('flat_network', False)
        no_parent = config.get('no_parent', False)
        preserve_network_config = config.get('preserve_network_config', False)
        logger.info(f"Read {len(routers)} routers from {CONFIG_JSON}")
        return routers, flat_network, no_parent, preserve_network_config
    except FileNotFoundError:
        logger.error(f"Config file not found: {CONFIG_JSON}")
        return [], False, False, False
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in config: {e}")
        return [], False, False, False
    except Exception as e:
        logger.error(f"Error reading config: {e}")
        return [], False, False, False


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


def update_network_json(routers, flat_network=False, no_parent=False, preserve_network_config=False):
    if preserve_network_config:
        logger.info("Preserving existing network.json.")
        return read_network_json()
    if no_parent:
        logger.info("No parent mode — clearing network config.")
        write_network_json({})
        return {}

    network_config = read_network_json()
    updated = False

    child_nodes = set()
    for cfg in network_config.values():
        if 'children' in cfg:
            child_nodes.update(cfg['children'].keys())
    for node in [n for n in list(network_config.keys()) if n in child_nodes]:
        del network_config[node]
        updated = True
        logger.info(f"Removed duplicate root node: {node}")

    for router in routers:
        node = f"ADDR-{router['name']}" if flat_network else router['name']
        if node not in network_config or not network_config[node].get('static', False):
            network_config[node] = {
                "downloadBandwidthMbps": DEFAULT_BANDWIDTH,
                "uploadBandwidthMbps": DEFAULT_BANDWIDTH,
                "type": "site",
                "children": {}
            }
            logger.info(f"Added node {node} to network config")
            updated = True

    if updated:
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
        is_static       INTEGER DEFAULT 0
    )
"""

def open_db():
    conn = sqlite3.connect(DB_FILE)

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
                    download_max_mbps=?, upload_max_mbps=?, download_min_mbps=?, upload_min_mbps=?
                WHERE code = ?
            """, (parent_node, mac, ipv4, comment, source, router_name,
                  rx_max, tx_max, rx_min, tx_min, code))
            logger.debug(f"Updated {code}")
            return True
        return False
    else:
        conn.execute("""
            INSERT INTO devices (code, circuit_id, device_id, parent_node, mac, ipv4, ipv6,
                comment, source, router, download_max_mbps, upload_max_mbps,
                download_min_mbps, upload_min_mbps, last_seen, is_static)
            VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """, (code, generate_short_id(), generate_short_id(), parent_node, mac,
              ipv4 or None,  # NULL instead of '' so UNIQUE allows multiple "no IP" rows
              comment, source, router_name, rx_max, tx_max, rx_min, tx_min, scan_time))
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

        if address not in ip_to_list:
            logger.debug(f"PPPoE skip {name}: IP={address} not in address list")
            continue

        mac  = caller_id.upper()
        code = f"PPP-{name}"

        list_name = ip_to_list.get(address, '')
        rx_max, tx_max, rx_min, tx_min, rate_failed = resolve_rates(list_name, default_dl, default_ul)
        logger.debug(f"PPPoE {code}: IP={address} list='{list_name}' → {rx_max}/{tx_max} Mbps")

        comment = build_comment('pppoe', list_name, rate_failed, scan_time)

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

        if address not in ip_to_list:
            logger.debug(f"Hotspot skip {username or mac}: IP={address} not in address list")
            continue

        mac_clean = mac.replace(':', '')
        code = f"HS-{mac_clean}" if mac_clean else f"HS-{username}"

        list_name = ip_to_list.get(address, '')
        rx_max, tx_max, rx_min, tx_min, rate_failed = resolve_rates(list_name, default_dl, default_ul)
        logger.debug(f"Hotspot {code}: IP={address} list='{list_name}' → {rx_max}/{tx_max} Mbps")

        comment = build_comment('hotspot', list_name, rate_failed, scan_time)

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

        # Pick first valid rate token from the field (may be comma/space separated)
        rate_name = ''
        for token in re.split(r'[,\s]+', addr_list_field):
            token = token.strip()
            if token and parse_rate(token):
                rate_name = token
                break

        # Skip leases without a valid rate format or whose IP isn't in the address list
        if not rate_name or address not in ip_to_list:
            logger.debug(f"DHCP skip {mac} (IP={address}): rate='{rate_name}' in_addr_list={address in ip_to_list}")
            continue

        rx_max, tx_max, rx_min, tx_min, rate_failed = resolve_rates(rate_name, default_dl, default_ul)
        logger.debug(f"DHCP {mac}: address-list='{rate_name}' → {rx_max}/{tx_max} Mbps")

        mac_clean = mac.replace(':', '')
        code    = f"DHCP-{hostname}" if hostname else f"DHCP-{mac_clean}"
        comment = build_comment('dhcp', rate_name, rate_failed, scan_time)

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
            routers, flat_network, no_parent, preserve_network_config = read_config_json()
            network_config = update_network_json(routers, flat_network, no_parent, preserve_network_config)

            scan_time   = time.time()
            any_changes = False

            for router in routers:
                logger.info(f"Processing router: {router['name']} ({router['address']})")

                api = connect_to_router(router)
                if api is None:
                    logger.warning(f"Skipping {router['name']} — connection failed.")
                    continue

                try:
                    router_name = router['name']
                    parent_node = '' if no_parent else (f"ADDR-{router_name}" if flat_network else router_name)

                    # ── Fetch all data at once ──────────────────────────────
                    addr_list_entries = get_resource_data(api, '/ip/firewall/address-list')

                    # Build IP→list_name map for PPPoE and Hotspot rate lookups
                    ip_to_list = {
                        e['address']: e.get('list', '')
                        for e in addr_list_entries
                        if e.get('address') and e.get('disabled', 'false') != 'true'
                    }

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
                # ── Export CSV → reload LibreQoS ────────────────────────────
                export_to_csv(conn)
                write_network_json(network_config)
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
