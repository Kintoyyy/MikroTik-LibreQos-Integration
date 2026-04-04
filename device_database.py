import csv
import logging
import os
import shutil
import sqlite3

from rate_resolver import RateResolver

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

# Higher number = higher priority. If the same IP already exists from a higher-priority
# source, the lower-priority source entry is skipped.
SOURCE_PRIORITY = {'pppoe': 4, 'hotspot': 3, 'dhcp': 2, 'address_list': 1}

TC_U16_WARN_THRESHOLD = 60_000

FIELDNAMES = [
    'Circuit ID', 'Circuit Name', 'Device ID', 'Device Name', 'Parent Node',
    'MAC', 'IPv4', 'IPv6', 'Download Min Mbps', 'Upload Min Mbps',
    'Download Max Mbps', 'Upload Max Mbps', 'Comment'
]


class DeviceDatabase:
    def __init__(self, db_path='devices.db', csv_path='ShapedDevices.csv',
                 network_json_path='network.json'):
        self.db_path          = db_path
        self.csv_path         = csv_path
        self.network_json_path = network_json_path
        self.conn             = None

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

        self.conn.commit()

    def upsert_device(self, code, parent_node, mac, ipv4, comment, source, router_name,
                      rx_max, tx_max, rx_min, tx_min, scan_time) -> bool:
        """
        Insert or update a device. Returns True if data changed.

        IPv4 conflict resolution: if the same IP already exists under a different
        code, the entry with the higher SOURCE_PRIORITY wins. Lower-priority
        source is skipped.
        """
        new_priority = SOURCE_PRIORITY.get(source, 0)

        if ipv4:
            conflict = self.conn.execute(
                "SELECT code, source FROM devices WHERE ipv4 = ? AND code != ?",
                (ipv4, code)
            ).fetchone()

            if conflict:
                conflict_code, conflict_source = conflict
                existing_priority = SOURCE_PRIORITY.get(conflict_source, 0)

                if new_priority > existing_priority:
                    self.conn.execute("DELETE FROM devices WHERE code = ?", (conflict_code,))
                    logger.info(
                        f"Replaced {conflict_code} ({conflict_source}) with "
                        f"{code} ({source}) for IP {ipv4}"
                    )
                else:
                    logger.debug(
                        f"Skipping {code} ({source}) — IP {ipv4} already owned by "
                        f"{conflict_code} ({conflict_source})"
                    )
                    return False

        row = self.conn.execute(
            "SELECT circuit_id, device_id, parent_node, mac, ipv4, comment, "
            "download_max_mbps, upload_max_mbps, download_min_mbps, upload_min_mbps, is_static "
            "FROM devices WHERE code = ?", (code,)
        ).fetchone()

        if row:
            (circuit_id, device_id, old_parent, old_mac, old_ipv4, old_comment,
             old_dlmax, old_ulmax, old_dlmin, old_ulmin, is_static) = row

            self.conn.execute(
                "UPDATE devices SET last_seen = ? WHERE code = ?", (scan_time, code)
            )

            if is_static:
                return False

            new_vals = (parent_node, mac, ipv4, comment, rx_max, tx_max, rx_min, tx_min)
            old_vals = (old_parent, old_mac, old_ipv4, old_comment,
                        old_dlmax, old_ulmax, old_dlmin, old_ulmin)
            if new_vals != old_vals:
                self.conn.execute("""
                    UPDATE devices
                    SET parent_node=?, mac=?, ipv4=?, comment=?, source=?, router=?,
                        download_max_mbps=?, upload_max_mbps=?, download_min_mbps=?,
                        upload_min_mbps=?, weight=?
                    WHERE code = ?
                """, (parent_node, mac, ipv4, comment, source, router_name,
                      rx_max, tx_max, rx_min, tx_min, rx_max + tx_max, code))
                logger.debug(f"Updated {code}")
                return True
            return False
        else:
            self.conn.execute("""
                INSERT INTO devices (code, circuit_id, device_id, parent_node, mac, ipv4, ipv6,
                    comment, source, router, download_max_mbps, upload_max_mbps,
                    download_min_mbps, upload_min_mbps, last_seen, is_static, weight)
                VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """, (code, RateResolver.generate_short_id(), RateResolver.generate_short_id(),
                  parent_node, mac,
                  ipv4 or None,
                  comment, source, router_name,
                  rx_max, tx_max, rx_min, tx_min, scan_time,
                  rx_max + tx_max))
            logger.info(f"New device: {code} (source={source}, IP={ipv4})")
            return True

    def remove_inactive(self, scan_time) -> bool:
        """Remove devices not seen in the current scan (excluding static entries)."""
        cur = self.conn.execute(
            "DELETE FROM devices WHERE last_seen < ? AND is_static = 0", (scan_time,)
        )
        count = cur.rowcount
        self.conn.commit()
        if count:
            logger.info(f"Removed {count} inactive device(s)")
        return count > 0

    def export_to_csv(self):
        """Export all devices from SQLite to ShapedDevices.csv."""
        rows = self.conn.execute("""
            SELECT circuit_id, code, device_id, code, parent_node, mac, ipv4, ipv6,
                   download_min_mbps, upload_min_mbps, download_max_mbps, upload_max_mbps, comment
            FROM devices
            ORDER BY source, code
        """).fetchall()
        with open(self.csv_path, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(FIELDNAMES)
            writer.writerows(rows)
        logger.info(f"Exported {len(rows)} devices to {self.csv_path}")

    def check_tc_u16_overflow(self):
        """
        Warn when total device count approaches the TC u16 classifier limit (~65535).
        See: LibreQoS Troubleshooting — TC_U16_OVERFLOW urgent code.
        """
        total = self.conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
        if total >= TC_U16_WARN_THRESHOLD:
            logger.warning(
                f"TC_U16_OVERFLOW RISK: {total} devices in DB "
                f"(threshold={TC_U16_WARN_THRESHOLD}). "
                "Consider reducing topology depth or increasing queue parallelism."
            )
        return total

    def backup_files(self):
        """Back up network.json and ShapedDevices.csv before any write."""
        for path in (self.network_json_path, self.csv_path):
            if os.path.exists(path):
                try:
                    shutil.copy2(path, path + '.bak')
                    logger.debug(f"Backed up {path} → {path}.bak")
                except Exception as e:
                    logger.warning(f"Could not back up {path}: {e}")

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None
