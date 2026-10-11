import csv
import io
import logging
import math
import os
import sqlite3

from rate_resolver import RateResolver
from settings import (
    SOURCE_PRIORITY, TC_U16_WARN_THRESHOLD, MAPPED_CIRCUIT_WARN_THRESHOLD, write_if_changed,
)

logger = logging.getLogger(__name__)

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

FIELDNAMES = [
    'Circuit ID', 'Circuit Name', 'Device ID', 'Device Name', 'Parent Node',
    'MAC', 'IPv4', 'IPv6', 'Download Min Mbps', 'Upload Min Mbps',
    'Download Max Mbps', 'Upload Max Mbps', 'Comment'
]


class DeviceDatabase:
    def __init__(self, db_path='devices.db', csv_path='ShapedDevices.csv'):
        self.db_path  = db_path
        self.csv_path = csv_path
        self.conn     = None
        self._over_circuit_cap = False

    def open(self):
        """Open the SQLite database, create/migrate schema, add missing columns."""
        self.conn = sqlite3.connect(self.db_path, timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")

        schema_row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='devices'"
        ).fetchone()

        needs_migration = schema_row and (
            'UNIQUE' not in schema_row[0] or 'CHECK' not in schema_row[0]
        )
        if needs_migration:
            logger.info("Migrating devices table schema...")
            self.conn.execute("ALTER TABLE devices RENAME TO devices_backup")
            self.conn.execute(_CREATE_DEVICES_SQL)
            self.conn.execute("""
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
            self.conn.execute("DROP TABLE devices_backup")
            logger.info("Migration complete.")
        else:
            self.conn.execute(_CREATE_DEVICES_SQL)

        # Add missing columns non-destructively
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(devices)")}
        if 'weight' not in cols:
            self.conn.execute("ALTER TABLE devices ADD COLUMN weight INT NOT NULL DEFAULT 0")
            self.conn.execute("UPDATE devices SET weight = download_max_mbps + upload_max_mbps")
            logger.info("Added weight column to devices table")
        if 'core_name' not in cols:
            self.conn.execute("ALTER TABLE devices ADD COLUMN core_name TEXT DEFAULT ''")
            logger.info("Added core_name column to devices table")
        if 'wan_name' not in cols:
            self.conn.execute("ALTER TABLE devices ADD COLUMN wan_name TEXT DEFAULT ''")
            logger.info("Added wan_name column to devices table")

        # DHCP devices used to be keyed by host-name, which is not unique
        # (many clients report the same name). Re-key them by MAC and keep
        # their circuit/device IDs. A row whose new code is already taken is
        # left as-is and ages out through remove_inactive().
        cur = self.conn.execute("""
            UPDATE OR IGNORE devices
            SET code = 'DHCP-' || REPLACE(mac, ':', '')
            WHERE source = 'dhcp' AND is_static = 0 AND mac != ''
              AND code != 'DHCP-' || REPLACE(mac, ':', '')
        """)
        if cur.rowcount > 0:
            logger.info(f"Re-keyed {cur.rowcount} DHCP device(s) by MAC address")

        self.conn.commit()

    def _new_ids(self):
        """Return a (circuit_id, device_id) pair not already in use."""
        while True:
            cid = RateResolver.generate_short_id()
            did = RateResolver.generate_short_id()
            taken = self.conn.execute(
                "SELECT 1 FROM devices WHERE circuit_id IN (?, ?) OR device_id IN (?, ?)",
                (cid, did, cid, did)
            ).fetchone()
            if cid != did and not taken:
                return cid, did

    def upsert_device(self, code, mac, ipv4, comment, source, router_name,
                      rx_max, tx_max, rx_min, tx_min, scan_time) -> bool:
        """
        Insert or update a device. Returns True if data changed.

        parent_node is not touched here: NodeAssigner owns that column.

        IPv4 conflict resolution when another row already holds the IP:
          - a static (manually added) row always keeps it;
          - a row not seen yet in this scan (e.g. a disconnected session whose
            IP was handed to a new one) or with lower SOURCE_PRIORITY gives
            the IP up — its ipv4 is cleared, but it keeps its IDs and either
            reappears with another IP or ages out;
          - otherwise this device is skipped.
        """
        ipv4 = ipv4 or None
        new_priority = SOURCE_PRIORITY.get(source, 0)

        if ipv4:
            conflict = self.conn.execute(
                "SELECT code, source, last_seen, is_static FROM devices "
                "WHERE ipv4 = ? AND code != ?",
                (ipv4, code)
            ).fetchone()

            if conflict:
                c_code, c_source, c_seen, c_static = conflict
                c_priority = SOURCE_PRIORITY.get(c_source, 0)
                stale = (c_seen or 0) < scan_time

                if not c_static and (stale or new_priority > c_priority):
                    self.conn.execute("UPDATE devices SET ipv4 = NULL WHERE code = ?", (c_code,))
                    logger.info(
                        f"IP {ipv4} moved from {c_code} ({c_source}"
                        f"{', stale' if stale else ''}) to {code} ({source})"
                    )
                else:
                    logger.debug(
                        f"Skipping {code} ({source}) — IP {ipv4} already owned by "
                        f"{c_code} ({c_source}{', static' if c_static else ''})"
                    )
                    return False

        row = self.conn.execute(
            "SELECT mac, ipv4, comment, source, router, "
            "download_max_mbps, upload_max_mbps, download_min_mbps, upload_min_mbps, is_static "
            "FROM devices WHERE code = ?", (code,)
        ).fetchone()

        if row:
            *old_vals, is_static = row

            self.conn.execute(
                "UPDATE devices SET last_seen = ? WHERE code = ?", (scan_time, code)
            )

            if is_static:
                return False

            new_vals = (mac, ipv4, comment, source, router_name, rx_max, tx_max, rx_min, tx_min)
            if new_vals != tuple(old_vals):
                self.conn.execute("""
                    UPDATE devices
                    SET mac=?, ipv4=?, comment=?, source=?, router=?,
                        download_max_mbps=?, upload_max_mbps=?, download_min_mbps=?,
                        upload_min_mbps=?, weight=?
                    WHERE code = ?
                """, (mac, ipv4, comment, source, router_name,
                      rx_max, tx_max, rx_min, tx_min, rx_max + tx_max, code))
                logger.debug(f"Updated {code}")
                return True
            return False
        else:
            circuit_id, device_id = self._new_ids()
            self.conn.execute("""
                INSERT INTO devices (code, circuit_id, device_id, parent_node, mac, ipv4, ipv6,
                    comment, source, router, download_max_mbps, upload_max_mbps,
                    download_min_mbps, upload_min_mbps, last_seen, is_static, weight)
                VALUES (?, ?, ?, '', ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """, (code, circuit_id, device_id, mac, ipv4,
                  comment, source, router_name,
                  rx_max, tx_max, rx_min, tx_min, scan_time,
                  rx_max + tx_max))
            logger.info(f"New device: {code} (source={source}, IP={ipv4})")
            return True

    def remove_inactive(self, scan_time, scanned_routers, configured_routers) -> bool:
        """
        Remove dynamic (non-static) devices that are gone:
          - devices of a router scanned successfully this cycle that it no
            longer reports, and
          - devices of routers no longer present in config.json.
        Devices of a router that could not be scanned are kept, so a router
        outage or API error never strips shaping from its subscribers.
        """
        count = 0
        for name in scanned_routers:
            count += self.conn.execute(
                "DELETE FROM devices WHERE is_static = 0 AND router = ? AND last_seen < ?",
                (name, scan_time)
            ).rowcount

        configured = list(configured_routers)
        sql = "DELETE FROM devices WHERE is_static = 0"
        if configured:
            sql += f" AND router NOT IN ({','.join('?' * len(configured))})"
        count += self.conn.execute(sql, configured).rowcount

        self.conn.commit()
        if count:
            logger.info(f"Removed {count} inactive device(s)")
        return count > 0

    def export_to_csv(self) -> bool:
        """
        Write all devices with an IP to ShapedDevices.csv.
        The file is only replaced when its content changes. Returns True if written.

        Rows are ordered manual devices first, then by source priority
        (PPPoE first): LibreQoS without a license admits only the first
        circuits in the file, so the least important ones are dropped first.
        """
        cases = ' '.join('WHEN ? THEN ?' for _ in SOURCE_PRIORITY)
        params = [v for item in SOURCE_PRIORITY.items() for v in item]
        rows = self.conn.execute(f"""
            SELECT circuit_id, code, device_id, code, parent_node, mac, ipv4, ipv6,
                   download_min_mbps, upload_min_mbps, download_max_mbps, upload_max_mbps, comment
            FROM devices
            WHERE ipv4 IS NOT NULL OR ipv6 IS NOT NULL
            ORDER BY is_static DESC, CASE source {cases} ELSE 0 END DESC, code
        """, params).fetchall()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(FIELDNAMES)
        writer.writerows(rows)

        if write_if_changed(self.csv_path, buf.getvalue()):
            logger.info(f"Exported {len(rows)} devices to {self.csv_path}")
            return True
        return False

    def check_limits(self, queues=None):
        """
        Warn about LibreQoS limits (see LibreQoS Troubleshooting):
          - TC_U16_OVERFLOW: tc class/qdisc minor IDs run out (~65535) per CPU
            queue. Estimated from the busiest parent node, or from the
            unparented devices spread over the queues.
          - MAPPED_CIRCUIT_LIMIT: without a license only the first
            MAPPED_CIRCUIT_WARN_THRESHOLD circuits are shaped.
        Returns the number of exported devices.
        """
        total = self.conn.execute(
            "SELECT COUNT(*) FROM devices WHERE ipv4 IS NOT NULL OR ipv6 IS NOT NULL"
        ).fetchone()[0]

        row = self.conn.execute(
            "SELECT parent_node, COUNT(*) FROM devices WHERE parent_node != '' "
            "GROUP BY parent_node ORDER BY 2 DESC LIMIT 1"
        ).fetchone()
        busiest_name, busiest = row if row else ('', 0)
        unparented = self.conn.execute(
            "SELECT COUNT(*) FROM devices WHERE parent_node = '' OR parent_node IS NULL"
        ).fetchone()[0]
        per_queue_unparented = math.ceil(unparented / max(queues or os.cpu_count() or 1, 1))

        per_queue = max(busiest, per_queue_unparented)
        if per_queue >= TC_U16_WARN_THRESHOLD:
            where = f"parent node '{busiest_name}'" if busiest >= per_queue_unparented else "one CPU queue"
            logger.warning(
                f"TC_U16_OVERFLOW RISK: ~{per_queue} devices on {where} "
                f"(threshold={TC_U16_WARN_THRESHOLD}). "
                "Increase queue count, or simplify or rebalance the hierarchy."
            )

        over_cap = 0 < MAPPED_CIRCUIT_WARN_THRESHOLD < total
        if over_cap and not self._over_circuit_cap:
            logger.warning(
                f"{total} circuits exceed {MAPPED_CIRCUIT_WARN_THRESHOLD}: LibreQoS without a "
                "license shapes only the first ones (MAPPED_CIRCUIT_LIMIT). ShapedDevices.csv "
                "lists manual devices first, then sources by priority."
            )
        self._over_circuit_cap = over_cap
        return total

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None
