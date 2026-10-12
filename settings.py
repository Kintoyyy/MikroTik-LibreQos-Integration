import json
import os
import re
import shutil
import tempfile

_SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'settings.json')

_DEFAULTS = {
    "scanner": {
        "scan_interval": 600,
        "error_retry_interval": 30,
        "connect_retries": 3,
        "connect_retry_delay": 5,
        # Command run after ShapedDevices.csv / network.json change.
        # "auto": LibreQoS.py --updateonly, run with the interpreter the
        # installed LibreQoS version uses. Empty string: run nothing and let
        # lqos_scheduler apply the files on its own refresh.
        "libreqos_update_command": "auto",
        "libreqos_update_timeout": 300,
        # Where LibreQoS reads ShapedDevices.csv / network.json.
        # Empty: lqos_directory from /etc/lqos.conf, else /opt/libreqos/src.
        "libreqos_dir": "",
    },
    "wan_service": {
        "default_interval": 300,
        "error_retry_interval": 30,
        # Move devices only when the busiest WAN's utilization is this many
        # times the least busy one's. Absolute oversubscription is normal for
        # an ISP and must not trigger moves (a move changes a subscriber's
        # exit WAN, which breaks their NAT sessions).
        "rebalance_imbalance_ratio": 1.5,
        "full_reconcile_interval": 1800,
        # Manual GUI rebalance treats dynamic devices not seen for this long as offline.
        "manual_rebalance_active_cutoff": 1200,
    },
    "rates": {
        # LibreQoS recommends min 1/1 Mbps and max about 1.15x the plan rate.
        # A min percentage of 0 gives the 1 Mbps floor.
        "min_dl_rate_percentage": 0.0,
        "min_ul_rate_percentage": 0.0,
        "max_dl_rate_percentage": 1.15,
        "max_ul_rate_percentage": 1.15,
        "default_dl_bandwidth": 100,
        "default_ul_bandwidth": 100,
        "id_length": 8,
    },
    "topology": {
        # Parent node bandwidth = sum of its devices' max rates x this.
        "parent_bandwidth_multiplier": 1.1,
        # Warn when the busiest parent/CPU node carries this many times the lightest one's load.
        "skew_warn_ratio": 2.0,
        # cpu strategy: keep devices on their current CPU node (stable queues);
        # only new devices are placed, and devices are moved only when the
        # busiest CPU node's load is over cpu_rebalance_ratio x the lightest.
        "sticky_cpu_assignment": True,
        "cpu_rebalance_ratio": 1.5,
        # Add a stable "id" to every network.json node.
        "node_ids": True,
        # ap_site/full: mark site nodes virtual (logical grouping only, not shaped).
        "virtual_site_nodes": False,
    },
    "database": {
        "tc_u16_warn_threshold": 60000,
        # LibreQoS without a license admits only this many circuits to shaping.
        "mapped_circuit_warn_threshold": 1000,
        "source_priority": {
            "pppoe": 4,
            "hotspot": 3,
            "dhcp": 2,
            "address_list": 1,
        },
    },
    "gui": {
        "managed_services": ["lqosd", "lqos_scheduler", "updatecsv", "wan_service", "gui"],
        "expected_mt_group": "API_READ",
        "expected_mt_policy": (
            "read,sensitive,api,!policy,!local,!telnet,!ssh,!ftp,!reboot,"
            "!write,!test,!winbox,!password,!web,!sniff,!romon"
        ),
        "expected_core_group": "API_WRITE",
        "expected_core_policy": (
            "read,sensitive,api,!policy,!local,!telnet,!ssh,!ftp,!reboot,"
            "write,!test,!winbox,!password,!web,!sniff,!romon"
        ),
        "login_max_attempts": 5,
        "login_lockout_seconds": 60,
    },
}


def _load():
    try:
        with open(_SETTINGS_PATH) as f:
            data = json.load(f)
        result = {}
        for section, defaults in _DEFAULTS.items():
            merged = dict(defaults)
            merged.update(data.get(section, {}))
            # Re-merge nested dicts (e.g. source_priority)
            for k, v in defaults.items():
                if isinstance(v, dict) and isinstance(data.get(section, {}).get(k), dict):
                    merged[k] = {**v, **data[section][k]}
            result[section] = merged
        return result
    except (FileNotFoundError, json.JSONDecodeError):
        return {k: dict(v) for k, v in _DEFAULTS.items()}


_s = _load()


def atomic_write_text(path, text, mode=None):
    """
    Write text to path atomically: write a temp file in the same directory,
    then os.replace() it over the target. Readers never see a half-written
    file. mode (e.g. 0o600) is applied before the file becomes visible.

    Lives here because every service already imports settings.py.
    """
    path = os.path.realpath(os.fspath(path))   # replace a symlink's target, not the link
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix='.' + os.path.basename(path) + '.')
    try:
        with os.fdopen(fd, 'w', newline='') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if mode is None and os.path.exists(path):
            mode = os.stat(path).st_mode & 0o777
        os.chmod(tmp, mode if mode is not None else 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_if_changed(path, text):
    """
    Atomically replace path with text only when its content differs.
    The previous version is kept as path + '.bak'. Returns True if written.
    """
    path = os.fspath(path)
    try:
        with open(path, 'r', newline='') as f:
            if f.read() == text:
                return False
        shutil.copy2(path, path + '.bak')
    except FileNotFoundError:
        pass
    atomic_write_text(path, text)
    return True


DEFAULT_LIBREQOS_DIR = '/opt/libreqos/src'


def libreqos_dir():
    """
    Directory LibreQoS reads ShapedDevices.csv and network.json from:
    scanner.libreqos_dir if set, else lqos_directory from /etc/lqos.conf,
    else /opt/libreqos/src. This app lives in its own directory and only
    writes those two files into LibreQoS's tree.
    """
    configured = str(_s["scanner"].get("libreqos_dir") or "").strip()
    if configured:
        return configured
    try:
        with open('/etc/lqos.conf') as f:
            for line in f:
                m = re.match(r"""\s*lqos_directory\s*=\s*["']([^"']+)["']""", line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return DEFAULT_LIBREQOS_DIR


def libreqos_version():
    """
    Installed LibreQoS version as (major, minor), or None when unknown:
    the top-level 'version' in /etc/lqos.conf, else the VERSION_STRING file
    LibreQoS ships in its directory.
    """
    sources = (('/etc/lqos.conf', r"""\s*version\s*=\s*["']([^"']+)"""),
               (os.path.join(libreqos_dir(), 'VERSION_STRING'), r'\s*v?(\S+)'))
    for path, pattern in sources:
        try:
            with open(path) as f:
                for line in f:
                    if line.lstrip().startswith('['):
                        break                      # only top-level keys count
                    m = re.match(pattern, line)
                    v = m and re.match(r'(\d+)\.(\d+)', m.group(1))
                    if v:
                        return int(v.group(1)), int(v.group(2))
        except OSError:
            pass
    return None

# ── Rate resolver constants ───────────────────────────────────────────────────
MIN_DL_RATE_PERCENTAGE = float(_s["rates"]["min_dl_rate_percentage"])
MIN_UL_RATE_PERCENTAGE = float(_s["rates"]["min_ul_rate_percentage"])
MAX_DL_RATE_PERCENTAGE = float(_s["rates"]["max_dl_rate_percentage"])
MAX_UL_RATE_PERCENTAGE = float(_s["rates"]["max_ul_rate_percentage"])
DEFAULT_DL_BANDWIDTH   = int(_s["rates"]["default_dl_bandwidth"])
DEFAULT_UL_BANDWIDTH   = int(_s["rates"]["default_ul_bandwidth"])
ID_LENGTH              = int(_s["rates"]["id_length"])

# ── Scanner constants ─────────────────────────────────────────────────────────
SCAN_INTERVAL           = int(_s["scanner"]["scan_interval"])
ERROR_RETRY_INTERVAL    = int(_s["scanner"]["error_retry_interval"])
CONNECT_RETRIES         = max(1, int(_s["scanner"]["connect_retries"]))
CONNECT_RETRY_DELAY     = float(_s["scanner"]["connect_retry_delay"])
LIBREQOS_UPDATE_COMMAND = str(_s["scanner"]["libreqos_update_command"] or "")
LIBREQOS_UPDATE_TIMEOUT = int(_s["scanner"]["libreqos_update_timeout"])

# ── WAN service constants ─────────────────────────────────────────────────────
WAN_DEFAULT_INTERVAL     = int(_s["wan_service"]["default_interval"])
WAN_ERROR_RETRY_INTERVAL = int(_s["wan_service"]["error_retry_interval"])
WAN_REBALANCE_IMBALANCE  = float(_s["wan_service"]["rebalance_imbalance_ratio"])
WAN_FULL_RECONCILE_INTERVAL = int(_s["wan_service"]["full_reconcile_interval"])
WAN_MANUAL_ACTIVE_CUTOFF = int(_s["wan_service"]["manual_rebalance_active_cutoff"])

# ── Topology constants ────────────────────────────────────────────────────────
PARENT_BANDWIDTH_MULTIPLIER = float(_s["topology"]["parent_bandwidth_multiplier"])
SKEW_WARN_RATIO             = float(_s["topology"]["skew_warn_ratio"])
STICKY_CPU_ASSIGNMENT       = bool(_s["topology"]["sticky_cpu_assignment"])
CPU_REBALANCE_RATIO         = float(_s["topology"]["cpu_rebalance_ratio"])
NODE_IDS                    = bool(_s["topology"]["node_ids"])
VIRTUAL_SITE_NODES          = bool(_s["topology"]["virtual_site_nodes"])

# ── Database constants ────────────────────────────────────────────────────────
TC_U16_WARN_THRESHOLD         = int(_s["database"]["tc_u16_warn_threshold"])
MAPPED_CIRCUIT_WARN_THRESHOLD = int(_s["database"]["mapped_circuit_warn_threshold"])
SOURCE_PRIORITY               = dict(_s["database"]["source_priority"])

# ── GUI constants ─────────────────────────────────────────────────────────────
MANAGED_SERVICES     = list(_s["gui"]["managed_services"])
EXPECTED_MT_GROUP    = str(_s["gui"]["expected_mt_group"])
EXPECTED_MT_POLICY   = str(_s["gui"]["expected_mt_policy"])
EXPECTED_CORE_GROUP  = str(_s["gui"]["expected_core_group"])
EXPECTED_CORE_POLICY = str(_s["gui"]["expected_core_policy"])
LOGIN_MAX_ATTEMPTS   = max(1, int(_s["gui"]["login_max_attempts"]))
LOGIN_LOCKOUT_SECONDS = int(_s["gui"]["login_lockout_seconds"])
