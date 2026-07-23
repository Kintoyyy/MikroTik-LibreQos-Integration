import json
import os

_SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'settings.json')

_DEFAULTS = {
    "scanner": {
        "scan_interval": 600,
        "error_retry_interval": 30,
    },
    "wan_service": {
        "default_interval": 300,
        "error_retry_interval": 30,
        "rebalance_threshold": 1.10,
    },
    "rates": {
        "min_dl_rate_percentage": 0.5,
        "min_ul_rate_percentage": 0.5,
        "max_dl_rate_percentage": 1.0,
        "max_ul_rate_percentage": 1.0,
        "default_dl_bandwidth": 100,
        "default_ul_bandwidth": 100,
        "id_length": 8,
    },
    "database": {
        "tc_u16_warn_threshold": 60000,
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

# ── Rate resolver constants ───────────────────────────────────────────────────
MIN_DL_RATE_PERCENTAGE = float(_s["rates"]["min_dl_rate_percentage"])
MIN_UL_RATE_PERCENTAGE = float(_s["rates"]["min_ul_rate_percentage"])
MAX_DL_RATE_PERCENTAGE = float(_s["rates"]["max_dl_rate_percentage"])
MAX_UL_RATE_PERCENTAGE = float(_s["rates"]["max_ul_rate_percentage"])
DEFAULT_DL_BANDWIDTH   = int(_s["rates"]["default_dl_bandwidth"])
DEFAULT_UL_BANDWIDTH   = int(_s["rates"]["default_ul_bandwidth"])
ID_LENGTH              = int(_s["rates"]["id_length"])

# ── Scanner constants ─────────────────────────────────────────────────────────
SCAN_INTERVAL        = int(_s["scanner"]["scan_interval"])
ERROR_RETRY_INTERVAL = int(_s["scanner"]["error_retry_interval"])

# ── WAN service constants ─────────────────────────────────────────────────────
WAN_DEFAULT_INTERVAL     = int(_s["wan_service"]["default_interval"])
WAN_ERROR_RETRY_INTERVAL = int(_s["wan_service"]["error_retry_interval"])
WAN_REBALANCE_THRESHOLD  = float(_s["wan_service"]["rebalance_threshold"])

# ── Database constants ────────────────────────────────────────────────────────
TC_U16_WARN_THRESHOLD = int(_s["database"]["tc_u16_warn_threshold"])
SOURCE_PRIORITY       = dict(_s["database"]["source_priority"])

# ── GUI constants ─────────────────────────────────────────────────────────────
MANAGED_SERVICES     = list(_s["gui"]["managed_services"])
EXPECTED_MT_GROUP    = str(_s["gui"]["expected_mt_group"])
EXPECTED_MT_POLICY   = str(_s["gui"]["expected_mt_policy"])
EXPECTED_CORE_GROUP  = str(_s["gui"]["expected_core_group"])
EXPECTED_CORE_POLICY = str(_s["gui"]["expected_core_policy"])
