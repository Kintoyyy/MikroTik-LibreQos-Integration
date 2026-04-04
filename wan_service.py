"""
wan_service.py — standalone WAN assignment and address-list sync daemon.

Runs independently from the device scanner (updatecsv.py) so its cadence
can be tuned separately. Reads config.json on every cycle, so interval and
enabled state changes take effect without a restart.

Typical usage:
    python wan_service.py

config.json knobs (under wan_assignment):
    enabled   (bool, default true)  — master on/off switch
    interval  (int,  default 300)   — seconds between sync cycles
"""

import json
import logging
import os
import time

from device_database import DeviceDatabase
from wan_manager import WANManager
from router_scanner import RouterScanner

CONFIG_JSON          = 'config.json'
DB_FILE              = 'devices.db'
SHAPED_DEVICES_CSV   = 'ShapedDevices.csv'
NETWORK_JSON         = 'network.json'
DEFAULT_INTERVAL     = 300
ERROR_RETRY_INTERVAL = 30

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def _read_wan_config():
    """
    Read config.json and return (cores, wan_sources, interval).
    Returns safe defaults on any read/parse error.
    """
    try:
        with open(CONFIG_JSON, 'r') as f:
            config = json.load(f)

        cores   = config.get('cores', [])
        wan_cfg = config.get('wan_assignment', {})

        wan_sources = {
            'enabled':         wan_cfg.get('enabled',         True),
            'include_hotspot': wan_cfg.get('include_hotspot', False),
            'include_dhcp':    wan_cfg.get('include_dhcp',    False),
        }
        interval = int(wan_cfg.get('interval', DEFAULT_INTERVAL))
        return cores, wan_sources, interval

    except FileNotFoundError:
        logger.error(f"Config file not found: {CONFIG_JSON}")
        return [], {'enabled': False}, DEFAULT_INTERVAL
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in config: {e}")
        return [], {'enabled': False}, DEFAULT_INTERVAL
    except Exception as e:
        logger.error(f"Error reading config: {e}")
        return [], {'enabled': False}, DEFAULT_INTERVAL


def main():
    logger.info("Starting WAN service")

    db = DeviceDatabase(DB_FILE, SHAPED_DEVICES_CSV, NETWORK_JSON)
    db.open()

    wan_mgr = WANManager(RouterScanner.connect)

    while True:
        cores, wan_sources, interval = _read_wan_config()

        try:
            if not wan_sources.get('enabled', True):
                logger.info("WAN assignment disabled in config — sleeping.")
            elif not cores:
                logger.info("No cores configured — sleeping.")
            else:
                wan_totals = wan_mgr.assign_wan_nodes(db.conn, cores, wan_sources)
                wan_mgr.check_wan_capacity(wan_totals, cores)
                wan_mgr.sync_wan_address_lists(db.conn, cores)
                logger.info(f"WAN cycle complete. Next in {interval}s.")

        except Exception as e:
            logger.error(f"Error in WAN cycle: {e}")
            time.sleep(ERROR_RETRY_INTERVAL)
            continue

        time.sleep(interval)


if __name__ == "__main__":
    main()
