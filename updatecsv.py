import json
import logging
import os
import subprocess
import time

from device_database import DeviceDatabase
from node_assigner import NodeAssigner, STRATEGY_CPU, ALL_STRATEGIES
from router_scanner import RouterScanner
from wan_manager import WANManager

# ── Constants ─────────────────────────────────────────────────────────────────

CONFIG_JSON          = 'config.json'
SHAPED_DEVICES_CSV   = 'ShapedDevices.csv'
NETWORK_JSON         = 'network.json'
DB_FILE              = 'devices.db'

SCAN_INTERVAL        = 600
ERROR_RETRY_INTERVAL = 30

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# ── Config ────────────────────────────────────────────────────────────────────

def read_config_json():
    """
    Read config.json and return
    (routers, strategy, queues, promote_to_root, cores, wan_sources).

    Strategy resolution order:
      1. Explicit 'strategy' key in config.json
      2. Legacy 'no_parent: true'  → STRATEGY_FLAT
      3. Legacy 'queues: false'    → no network.json (queues=None)
      4. Default                   → STRATEGY_CPU with auto cpu_count
    """
    try:
        with open(CONFIG_JSON, 'r') as f:
            config = json.load(f)

        routers         = config.get('bras', [])
        cores           = config.get('cores', [])
        promote_to_root = config.get('promote_to_root', False)

        if 'strategy' in config:
            strategy = config['strategy']
            if strategy not in ALL_STRATEGIES:
                logger.warning(f"Unknown strategy '{strategy}', falling back to '{STRATEGY_CPU}'")
                strategy = STRATEGY_CPU
        elif config.get('no_parent', False):
            from node_assigner import STRATEGY_FLAT
            strategy = STRATEGY_FLAT
        else:
            strategy = STRATEGY_CPU

        queues_raw = config.get('queues', True)
        if queues_raw is False:
            queues = None
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
            'enabled':         wan_cfg.get('enabled',         True),
            'include_hotspot': wan_cfg.get('include_hotspot', False),
            'include_dhcp':    wan_cfg.get('include_dhcp',    False),
        }

        logger.info(
            f"Read {len(routers)} routers, {len(cores)} cores from {CONFIG_JSON} "
            f"(strategy={strategy})"
        )
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


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    logger.info("Starting MikroTik-LibreQoS integration")

    db      = DeviceDatabase(DB_FILE, SHAPED_DEVICES_CSV, NETWORK_JSON)
    db.open()

    scanner  = RouterScanner(db)
    assigner = NodeAssigner(NETWORK_JSON)
    wan_mgr  = WANManager(RouterScanner.connect)

    while True:
        try:
            routers, strategy, queues, promote_to_root, cores, wan_sources = read_config_json()

            scan_time   = time.time()
            any_changes = False

            for router in routers:
                logger.info(f"Processing router: {router['name']} ({router['address']})")
                if scanner.scan_router(router, scan_time):
                    any_changes = True

            if db.remove_inactive(scan_time):
                any_changes = True

            if any_changes:
                db.backup_files()
                db.check_tc_u16_overflow()

                assigner.assign(db.conn, strategy, routers, queues, promote_to_root)

                if cores and wan_sources.get('enabled', True):
                    wan_totals = wan_mgr.assign_wan_nodes(db.conn, cores, wan_sources)
                    wan_mgr.check_wan_capacity(wan_totals, cores)
                    wan_mgr.sync_wan_address_lists(db.conn, cores)

                db.export_to_csv()

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
