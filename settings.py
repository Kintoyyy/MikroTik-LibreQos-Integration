import json
import os
import re
import shutil
import tempfile

# Where this app keeps its state (settings.json, config.json, devices.db).
# LQSYNC_HOME overrides it, e.g. for the preview server and tests; installed
# services leave it unset and use the script directory (/opt/lq-sync).
APP_HOME = os.environ.get('LQSYNC_HOME') or os.path.dirname(os.path.abspath(__file__))
_SETTINGS_PATH = os.path.join(APP_HOME, 'settings.json')

# LibreQoS's config file; LQSYNC_LQOS_CONF overrides it the same way.
LQOS_CONF_PATH = os.environ.get('LQSYNC_LQOS_CONF') or '/etc/lqos.conf'

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
        # auto_cpu: download Mbps one CPU core (one top-level node) can shape.
        # 0 = lqos.conf [queues] generated_pn_download_mbps, else 3000.
        "cpu_capacity_mbps": 0,
        # auto_cpu: split a site/router across CPUs when it carries more than
        # this many times one core's fair share. 0 = split only above capacity.
        "auto_cpu_split_ratio": 1.0,
        # auto_cpu: most subtrees moved between CPUs per cycle. 0 = auto (1% of items, 1-32).
        "auto_cpu_move_budget": 0,
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
    "webhook": {
        # PPP webhook (routers' on-up / on-down scripts): seconds to collect a
        # burst of events before acting on them. Keep at least 1: the session
        # must be gone from /ppp active when an on-down event is looked up.
        "debounce_seconds": 2,
        # Least seconds between webhook-triggered syncs (each may run LibreQoS).
        "min_interval_seconds": 10,
        # More users than this on one router in one batch: rescan the whole router instead.
        "full_scan_over": 50,
        # Days of received events kept for the GUI.
        "keep_days": 7,
    },
    "events": {
        # Warning & error history (Events tab): days an entry is kept after it
        # was last seen. Problems still open are always kept.
        "keep_days": 30,
    },
    "host_snmp": {
        # The Overview's host CPU, memory, load, uptime and interface traffic
        # from this host's own snmpd (net-snmp); psutil fills in whatever it
        # does not answer, or everything while it is unreachable.
        "enabled": True,
        "address": "127.0.0.1",
        "port": 161,
        "community": "public",
    },
    "burst": {
        # PowerBoost-style burst for all traffic: an idle subscriber may send
        # `seconds` worth of its plan at full speed before LibreQoS holds it to
        # the plan. Paused while the shaping link is busy.
        "enabled": False,
        "seconds": 4,
        "max_mb": 256,
        "pause_above_pct": 80,
        "resume_below_pct": 60,
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
        with open(LQOS_CONF_PATH) as f:
            for line in f:
                m = re.match(r"""\s*lqos_directory\s*=\s*["']([^"']+)["']""", line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return DEFAULT_LIBREQOS_DIR


def burst_settings():
    """
    The [burst] settings, re-read from settings.json on every call so they
    apply without a restart. Seconds are clamped to 1-30, and the resume
    threshold is kept below the pause threshold.
    """
    b = _load()["burst"]
    pause = min(max(float(b.get("pause_above_pct", 80)), 10.0), 100.0)
    return {
        "enabled":          bool(b.get("enabled", False)),
        "seconds":          min(max(float(b.get("seconds", 4)), 1.0), 30.0),
        "max_bytes":        int(max(float(b.get("max_mb", 256)), 1.0) * 1024 * 1024),
        "pause_above_pct":  pause,
        "resume_below_pct": min(max(float(b.get("resume_below_pct", 60)), 0.0), pause - 5),
    }


def topology_settings():
    """
    The [topology] settings used by the auto_cpu planner, re-read from
    settings.json on every call (like burst_settings) so a GUI preview and
    the next sync cycle see edits without a restart. Includes the
    database tc_u16_warn_threshold the planner checks per CPU.
    """
    s = _load()
    t = s["topology"]
    return {
        "multiplier":           float(t.get("parent_bandwidth_multiplier", 1.1)),
        "skew_warn_ratio":      float(t.get("skew_warn_ratio", 2.0)),
        "sticky":               bool(t.get("sticky_cpu_assignment", True)),
        "rebalance_ratio":      max(float(t.get("cpu_rebalance_ratio", 1.5)), 1.0),
        "virtual_site_nodes":   bool(t.get("virtual_site_nodes", False)),
        "cpu_capacity_mbps":    max(int(t.get("cpu_capacity_mbps", 0) or 0), 0),
        "split_ratio":          max(float(t.get("auto_cpu_split_ratio", 1.0)), 0.0),
        "move_budget":          max(int(t.get("auto_cpu_move_budget", 0) or 0), 0),
        "tc_u16_warn":          int(s["database"].get("tc_u16_warn_threshold", 60000)),
    }


def webhook_settings():
    """The [webhook] settings, re-read from settings.json on every call (no restart needed)."""
    w = _load()["webhook"]
    return {
        "debounce":     min(max(float(w.get("debounce_seconds", 2)), 0.5), 60.0),
        "min_interval": min(max(float(w.get("min_interval_seconds", 10)), 0.0), 3600.0),
        "full_scan_over": max(int(w.get("full_scan_over", 50)), 1),
        "keep_seconds": max(float(w.get("keep_days", 7)), 0.1) * 86400,
    }


def events_settings():
    """The [events] settings, re-read from settings.json on every call."""
    e = _load()["events"]
    try:
        keep = float(e.get("keep_days", 30))
    except (TypeError, ValueError):
        keep = 30.0
    return {"keep_days": min(max(keep, 1.0), 3650.0)}


def host_snmp_settings():
    """The [host_snmp] settings, re-read from settings.json on every call (no restart needed)."""
    return dict(_load()["host_snmp"])


def libreqos_version():
    """
    Installed LibreQoS version as (major, minor), or None when unknown:
    the top-level 'version' in /etc/lqos.conf, else the VERSION_STRING file
    LibreQoS ships in its directory.
    """
    sources = ((LQOS_CONF_PATH, r"""\s*version\s*=\s*["']([^"']+)"""),
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
