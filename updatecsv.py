import ipaddress
import json
import logging
import os
import shlex
import subprocess
import time

from device_database import DeviceDatabase, timed_job, prune_jobs
from node_assigner import NodeAssigner, STRATEGY_CPU, STRATEGY_FLAT, ALL_STRATEGIES
from router_scanner import RouterScanner
from settings import (
    SCAN_INTERVAL, ERROR_RETRY_INTERVAL, LIBREQOS_UPDATE_COMMAND, LIBREQOS_UPDATE_TIMEOUT,
    libreqos_dir,
)

try:
    import tomllib  # Python 3.11+
except ImportError:
    tomllib = None

# ── Constants ─────────────────────────────────────────────────────────────────

# App state lives in this app's own directory (the service's working directory).
CONFIG_JSON = 'config.json'
DB_FILE     = 'devices.db'
LQOS_CONF   = '/etc/lqos.conf'
# ShapedDevices.csv and network.json stay in LibreQoS's directory, where LibreQoS reads them.
LIBREQOS_DIR       = libreqos_dir()
SHAPED_DEVICES_CSV = os.path.join(LIBREQOS_DIR, 'ShapedDevices.csv')
NETWORK_JSON       = os.path.join(LIBREQOS_DIR, 'network.json')

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# ── Config ────────────────────────────────────────────────────────────────────

class ConfigError(Exception):
    """config.json cannot be used. The cycle must be skipped: treating an
    unreadable config as "no routers" would remove every device."""


def read_config_json():
    """
    Read config.json and return (routers, strategy, queues, promote_to_root).
    Raises ConfigError if the config cannot be used.

    Strategy resolution order:
      1. Explicit 'strategy' key in config.json
      2. Legacy 'no_parent: true'  → STRATEGY_FLAT
      3. Legacy 'queues: false'    → no network.json (queues=None)
      4. Default                   → STRATEGY_CPU with auto cpu_count
    """
    try:
        with open(CONFIG_JSON, 'r') as f:
            config = json.load(f)

        if not isinstance(config, dict):
            raise ValueError("top level must be a JSON object")

        routers         = config.get('bras', [])
        promote_to_root = config.get('promote_to_root', False)

        if not isinstance(routers, list):
            raise ValueError("'bras' must be a list")
        for i, router in enumerate(routers):
            if not isinstance(router, dict) or not router.get('name') or not router.get('address'):
                raise ValueError(f"bras[{i}] needs a 'name' and an 'address'")
            router['port']     = int(router.get('port', 8728) or 8728)
            router['username'] = router.get('username', '')
            router['password'] = router.get('password', '')

        if 'strategy' in config:
            strategy = config['strategy']
            if strategy not in ALL_STRATEGIES:
                logger.warning(f"Unknown strategy '{strategy}', falling back to '{STRATEGY_CPU}'")
                strategy = STRATEGY_CPU
        elif config.get('no_parent', False):
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

        logger.info(
            f"Read {len(routers)} routers from {CONFIG_JSON} (strategy={strategy})"
        )
        return routers, strategy, queues, promote_to_root

    except FileNotFoundError:
        raise ConfigError(f"{CONFIG_JSON} not found")
    except json.JSONDecodeError as e:
        raise ConfigError(f"invalid JSON in {CONFIG_JSON}: {e}")
    except Exception as e:
        raise ConfigError(f"unusable {CONFIG_JSON}: {e}")


def run_libreqos_update() -> str:
    """
    Run the configured LibreQoS apply command. With an empty command nothing
    runs: lqos_scheduler applies the new files on its next refresh.
    Returns '' on success, else the error.
    """
    if not LIBREQOS_UPDATE_COMMAND.strip():
        logger.info("Files updated — lqos_scheduler will apply them on its next refresh.")
        return ''
    try:
        result = subprocess.run(
            shlex.split(LIBREQOS_UPDATE_COMMAND),
            # LibreQoS.py opens network.json / ShapedDevices.csv relative to its
            # working directory, so run it from LibreQoS's directory, not ours.
            cwd=LIBREQOS_DIR,
            capture_output=True, text=True, check=True, timeout=LIBREQOS_UPDATE_TIMEOUT
        )
        logger.info("LibreQoS updated: " + result.stdout.strip())
        return ''
    except subprocess.CalledProcessError as e:
        error = (e.stderr or e.stdout or '').strip()[-400:] or f"exit code {e.returncode}"
    except subprocess.TimeoutExpired:
        error = f"timed out after {LIBREQOS_UPDATE_TIMEOUT}s"
    except Exception as e:
        error = str(e)
    logger.error(f"LibreQoS update failed: {error}")
    return error


def check_lqos_conf(conn, strategy):
    """
    Warn about lqos.conf settings that silently undo this integration's output:
      - [topology] compile_mode = "flat" ignores the Parent Node hierarchy.
      - Device IPs outside [ip_ranges] allow_subnets, or inside ignore_subnets,
        are not shaped.
    Skipped when lqos.conf is missing or Python has no tomllib (< 3.11).
    """
    if tomllib is None:
        return
    try:
        with open(LQOS_CONF, 'rb') as f:
            conf = tomllib.load(f)
    except FileNotFoundError:
        return
    except Exception as e:
        logger.warning(f"Could not parse {LQOS_CONF}: {e}")
        return

    compile_mode = conf.get('topology', {}).get('compile_mode')
    if compile_mode == 'flat' and strategy != STRATEGY_FLAT:
        logger.warning(
            f'{LQOS_CONF}: [topology] compile_mode = "flat" — LibreQoS ignores the Parent '
            f"Node hierarchy written for strategy '{strategy}'. Set compile_mode = \"full\" "
            "or use strategy 'flat'."
        )

    def _nets(values):
        nets = []
        for v in values or []:
            try:
                nets.append(ipaddress.ip_network(v, strict=False))
            except ValueError:
                pass
        return nets

    ranges = conf.get('ip_ranges', {})
    allow, ignore = _nets(ranges.get('allow_subnets')), _nets(ranges.get('ignore_subnets'))
    if not allow and not ignore:
        return

    unshaped = []
    for code, ip in conn.execute("SELECT code, ipv4 FROM devices WHERE ipv4 IS NOT NULL"):
        try:
            net = ipaddress.ip_network(ip, strict=False)
        except ValueError:
            continue
        inside = lambda nets: any(net.version == n.version and net.subnet_of(n) for n in nets)
        if (allow and not inside(allow)) or inside(ignore):
            unshaped.append(f"{code} ({ip})")
    if unshaped:
        sample = ', '.join(unshaped[:5]) + (' …' if len(unshaped) > 5 else '')
        logger.warning(
            f"{len(unshaped)} device(s) are outside lqos.conf allow_subnets or inside "
            f"ignore_subnets and will not be shaped: {sample}"
        )


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    logger.info("Starting MikroTik-LibreQoS integration")

    db = DeviceDatabase(DB_FILE, SHAPED_DEVICES_CSV)
    db.open()

    scanner  = RouterScanner(db)
    assigner = NodeAssigner(NETWORK_JSON)

    # Set when a LibreQoS update fails, so it is retried even if the files
    # do not change again.
    update_pending = False

    while True:
        try:
            try:
                with timed_job(db.conn, 'read config.json', 'config', 'updatecsv', SCAN_INTERVAL):
                    routers, strategy, queues, promote_to_root = read_config_json()
            except ConfigError as e:
                logger.error(
                    f"Skipping cycle — {e}; devices left unchanged. "
                    f"Retrying in {ERROR_RETRY_INTERVAL}s."
                )
                time.sleep(ERROR_RETRY_INTERVAL)
                continue

            scan_time  = time.time()
            scanned_ok = []

            for router in routers:
                logger.info(f"Processing router: {router['name']} ({router['address']})")
                with timed_job(db.conn, f"scan {router['name']}", 'poller', 'updatecsv',
                               SCAN_INTERVAL) as job:
                    ok, _ = scanner.scan_router(router, scan_time)
                    job['error'] = scanner.last_error
                if ok:
                    scanned_ok.append(router['name'])
            prune_jobs(db.conn, 'updatecsv', 'poller', [f"scan {r['name']}" for r in routers])

            # Always rebuild both files; they are only rewritten when their
            # content changes, which also picks up edits made in the GUI.
            with timed_job(db.conn, 'build topology', 'build', 'updatecsv', SCAN_INTERVAL):
                db.remove_inactive(scan_time, scanned_ok, [r['name'] for r in routers])
                network_changed = assigner.assign(db.conn, strategy, routers, queues, promote_to_root)
                csv_changed     = db.export_to_csv()
                db.check_limits(queues)

            if network_changed or csv_changed or update_pending:
                check_lqos_conf(db.conn, strategy)
                # every=0: runs only when the files change
                with timed_job(db.conn, 'libreqos apply', 'apply', 'updatecsv', 0) as job:
                    job['error'] = run_libreqos_update()
                update_pending = bool(job['error'])
            else:
                logger.info("No changes detected.")

            logger.info(f"Scan complete. Next in {SCAN_INTERVAL}s.")
            time.sleep(SCAN_INTERVAL)

        except Exception as e:
            logger.error(f"Error in main loop: {e}")
            time.sleep(ERROR_RETRY_INTERVAL)


if __name__ == "__main__":
    main()
