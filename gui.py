import os
import json
import random
import shutil
import sqlite3
import string
import subprocess
import tempfile
import time
import threading
import hashlib
import secrets
import functools
import socket
import re
from pathlib import Path
from flask import Flask, render_template, request, jsonify, Response, stream_with_context, session
from wan_manager import WANManager

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

try:
    import routeros_api
    HAS_ROUTEROS_API = True
except ImportError:
    HAS_ROUTEROS_API = False

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Path resolution: prefer /opt/libreqos/src, fall back to script directory
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent.resolve()
OPT_DIR = Path("/opt/libreqos/src")
NETPLAN_DIR = Path("/etc/netplan")

def find_file(name):
    """Return path to file, preferring the installed location."""
    opt_path = OPT_DIR / name
    local_path = SCRIPT_DIR / name
    if opt_path.exists():
        return opt_path
    return local_path

CONFIG_PATH = OPT_DIR / "config.json"
DB_PATH     = find_file("devices.db")
AUTH_PATH   = find_file("gui_auth.json")

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
_DEFAULT_PASSWORD = "admin"

def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), 260_000
    ).hex()

def _load_auth() -> dict:
    if AUTH_PATH.exists():
        try:
            return json.loads(AUTH_PATH.read_text())
        except Exception:
            pass
    # First-run: create default auth file
    salt       = secrets.token_hex(16)
    secret_key = secrets.token_hex(32)
    data = {
        "password_hash": _hash_password(_DEFAULT_PASSWORD, salt),
        "salt":          salt,
        "secret_key":    secret_key,
    }
    AUTH_PATH.write_text(json.dumps(data, indent=2))
    return data

_auth = _load_auth()
app.secret_key = _auth["secret_key"]

def _verify_password(password: str) -> bool:
    return _hash_password(password, _auth["salt"]) == _auth["password_hash"]

def require_auth(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("authed"):
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper

LQOS_CONF_PATH = Path("/etc/lqos.conf")

def _find_lqos_conf():
    return LQOS_CONF_PATH

YAML_FILES = {
    "libreqos":      NETPLAN_DIR / "libreqos.yaml",
    "lqos":          LQOS_CONF_PATH,
    "network_json":  OPT_DIR / "network.json",
    "config_json":   OPT_DIR / "config.json",
    "shaped_devices": OPT_DIR / "ShapedDevices.csv",
    "updatecsv":     OPT_DIR / "updatecsv.py",
    "network":       NETPLAN_DIR / "50-cloud-init.yaml",
}

EXPECTED_MT_GROUP = "API_READ"
EXPECTED_MT_POLICY = (
    "read,sensitive,api,!policy,!local,!telnet,!ssh,!ftp,!reboot,!write,!test,"
    "!winbox,!password,!web,!sniff,!romon"
)
EXPECTED_MT_POLICY_SET = {p.strip() for p in EXPECTED_MT_POLICY.split(",") if p.strip()}

SYSTEMCTL  = "/bin/systemctl"
JOURNALCTL = "/bin/journalctl"

def _which(cmd):
    """Return full path of a binary, checking PATH then common locations."""
    found = shutil.which(cmd)
    if found:
        return found
    for d in ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]:
        p = os.path.join(d, cmd)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None

def _systemctl(*args, **kwargs):
    """Run /bin/systemctl directly."""
    try:
        return subprocess.run([SYSTEMCTL] + list(args), **kwargs)
    except (FileNotFoundError, OSError) as e:
        raise RuntimeError(f"systemctl not available: {e}")

# ---------------------------------------------------------------------------
# SSE metrics broadcaster
# ---------------------------------------------------------------------------
_metric_listeners: list = []
_metric_lock = threading.Lock()
_prev_net: dict = {}          # {iface: (bytes_recv, bytes_sent, ts)}
_BRIDGE_EXCLUDE = {"lo"}      # interfaces to skip from the bridge graph
_lqos_conf_cache = {"mtime": None, "to_internet": ""}
_system_profile_cache = None


def _get_bridge_iface_from_lqos() -> str:
    """Read to_internet from lqos.conf, with mtime-based caching."""
    try:
        st = LQOS_CONF_PATH.stat()
    except Exception:
        return ""

    mtime = st.st_mtime
    if _lqos_conf_cache["mtime"] == mtime:
        return _lqos_conf_cache["to_internet"]

    to_internet = ""
    try:
        for raw in LQOS_CONF_PATH.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            line = line.split("#", 1)[0].strip()
            m = re.match(r'^to_internet\s*=\s*"([^"]+)"\s*$', line)
            if m:
                to_internet = m.group(1).strip()
                break
    except Exception:
        to_internet = ""

    _lqos_conf_cache["mtime"] = mtime
    _lqos_conf_cache["to_internet"] = to_internet
    return to_internet


def _detect_ram_type() -> str:
    """Best-effort detection of RAM DDR generation."""
    for cmd in (["dmidecode", "-t", "memory"], ["lshw", "-class", "memory"]):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            text = (proc.stdout or "") + "\n" + (proc.stderr or "")
            m = re.search(r"\bDDR\d\b", text, re.IGNORECASE)
            if m:
                return m.group(0).upper()
        except Exception:
            continue
    return "Unknown"


def _get_system_profile() -> dict:
    """Collect host hardware/platform details once, then reuse."""
    global _system_profile_cache
    if _system_profile_cache is not None:
        return _system_profile_cache

    cpu_model = "Unknown"
    try:
        for raw in Path("/proc/cpuinfo").read_text().splitlines():
            if raw.lower().startswith("model name"):
                cpu_model = raw.split(":", 1)[1].strip()
                break
    except Exception:
        pass

    cpu_clock_ghz = 0.0
    try:
        if HAS_PSUTIL:
            f = psutil.cpu_freq()
            if f and f.current:
                cpu_clock_ghz = round(float(f.current) / 1000.0, 2)
    except Exception:
        pass

    if cpu_clock_ghz <= 0:
        try:
            for raw in Path("/proc/cpuinfo").read_text().splitlines():
                if raw.lower().startswith("cpu mhz"):
                    mhz = float(raw.split(":", 1)[1].strip())
                    cpu_clock_ghz = round(mhz / 1000.0, 2)
                    break
        except Exception:
            pass

    os_pretty_name = "Linux"
    try:
        os_release = Path("/etc/os-release").read_text().splitlines()
        for raw in os_release:
            if raw.startswith("PRETTY_NAME="):
                os_pretty_name = raw.split("=", 1)[1].strip().strip('"')
                break
    except Exception:
        pass

    kernel_release = "Unknown"
    arch = "Unknown"
    try:
        un = os.uname()
        kernel_release = un.release
        arch = un.machine
    except Exception:
        pass

    logical_cores = 0
    physical_cores = 0
    mem_total = 0
    try:
        if HAS_PSUTIL:
            logical_cores = int(psutil.cpu_count(logical=True) or 0)
            physical_cores = int(psutil.cpu_count(logical=False) or 0)
            mem_total = int(psutil.virtual_memory().total)
    except Exception:
        pass

    _system_profile_cache = {
        "hostname": socket.gethostname(),
        "os": os_pretty_name,
        "kernel": kernel_release,
        "arch": arch,
        "cpu_model": cpu_model,
        "cpu_clock_ghz": cpu_clock_ghz,
        "cpu_cores_physical": physical_cores,
        "cpu_cores_logical": logical_cores,
        "ram_type": _detect_ram_type(),
        "ram_total": mem_total,
    }
    return _system_profile_cache


def _uptime_seconds() -> int:
    """Best-effort system uptime in seconds."""
    try:
        if HAS_PSUTIL:
            return max(0, int(time.time() - psutil.boot_time()))
    except Exception:
        pass
    try:
        return max(0, int(float(Path("/proc/uptime").read_text().split()[0])))
    except Exception:
        return 0

def _net_throughput():
    """Return {iface: {rx_mbps, tx_mbps}} using delta from previous sample."""
    global _prev_net
    if not HAS_PSUTIL:
        return {}
    counters = psutil.net_io_counters(pernic=True)
    now      = time.time()
    result   = {}
    for iface, c in counters.items():
        if iface in _BRIDGE_EXCLUDE:
            continue
        prev = _prev_net.get(iface)
        if prev:
            dt = max(now - prev[2], 0.001)
            rx = max((c.bytes_recv - prev[0]) * 8 / 1_000_000 / dt, 0)
            tx = max((c.bytes_sent - prev[1]) * 8 / 1_000_000 / dt, 0)
            result[iface] = {"rx_mbps": round(rx, 2), "tx_mbps": round(tx, 2)}
        _prev_net[iface] = (c.bytes_recv, c.bytes_sent, now)
    return result


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {}


def _router_from_config(index: int = 0) -> dict:
    cfg = _load_config()
    routers = cfg.get("bras") or []
    if not routers:
        raise ValueError("No BRAS found in config.json")
    if index < 0 or index >= len(routers):
        raise ValueError("Router index out of range")
    router = dict(routers[index])
    router["port"] = int(router.get("port", 8728) or 8728)
    return router


def _to_bool(value) -> bool:
    return str(value).strip().lower() in {"true", "yes", "1", "on"}


def _connect_router_api(router: dict):
    if not HAS_ROUTEROS_API:
        raise RuntimeError("routeros-api module is not installed")
    return routeros_api.RouterOsApiPool(
        router.get("address", ""),
        username=router.get("username", ""),
        password=router.get("password", ""),
        port=int(router.get("port", 8728) or 8728),
        plaintext_login=True,
    )

def _connect_for_wan(router: dict):
    """Adapter for WANManager: returns API object (not pool)."""
    return _connect_router_api(router).get_api()

_wan_manager = WANManager(_connect_for_wan)

def _broadcast_loop():
    while True:
        if HAS_PSUTIL:
            cpu_per  = psutil.cpu_percent(interval=1, percpu=True)
            cpu_avg  = sum(cpu_per) / len(cpu_per)
            mem      = psutil.virtual_memory()
            net      = _net_throughput()
            bridge_iface = _get_bridge_iface_from_lqos()
            sys_profile = _get_system_profile()
            payload  = json.dumps({
                "cpu_per":    cpu_per,
                "cpu_avg":    round(cpu_avg, 1),
                "mem_used":   mem.used,
                "mem_total":  mem.total,
                "mem_percent": mem.percent,
                "net":        net,
                "bridge_iface": bridge_iface,
                "system":     sys_profile,
                "uptime_seconds": _uptime_seconds(),
            })
        else:
            payload = json.dumps({"error": "psutil not installed"})

        data = f"data: {payload}\n\n"
        with _metric_lock:
            dead = []
            for q in _metric_listeners:
                try:
                    q.append(data)
                except Exception:
                    dead.append(q)
            for q in dead:
                _metric_listeners.remove(q)

        time.sleep(1)

threading.Thread(target=_broadcast_loop, daemon=True).start()

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# Auth routes (public)
# ---------------------------------------------------------------------------

@app.route("/api/auth/check")
def auth_check():
    return jsonify({"ok": True, "authed": bool(session.get("authed"))})

@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(force=True)
    if _verify_password(data.get("password", "")):
        session["authed"] = True
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Invalid password"}), 401

@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})

@app.route("/api/auth/password", methods=["POST"])
@require_auth
def change_password():
    global _auth
    data = request.get_json(force=True)
    current  = data.get("current", "")
    new_pass = data.get("new", "")
    if not _verify_password(current):
        return jsonify({"ok": False, "error": "Current password is incorrect"}), 400
    if len(new_pass) < 6:
        return jsonify({"ok": False, "error": "Password must be at least 6 characters"}), 400
    salt = secrets.token_hex(16)
    _auth["salt"]          = salt
    _auth["password_hash"] = _hash_password(new_pass, salt)
    AUTH_PATH.write_text(json.dumps(_auth, indent=2))
    return jsonify({"ok": True})


@app.route("/api/metrics/stream")
@require_auth
def metrics_stream():
    queue = []
    with _metric_lock:
        _metric_listeners.append(queue)

    def generate():
        try:
            while True:
                if queue:
                    yield queue.pop(0)
                else:
                    time.sleep(0.05)
        finally:
            with _metric_lock:
                if queue in _metric_listeners:
                    _metric_listeners.remove(queue)

    return Response(stream_with_context(generate()),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/config", methods=["GET"])
@require_auth
def get_config():
    try:
        return jsonify({"ok": True, "path": str(CONFIG_PATH),
                        "content": json.loads(CONFIG_PATH.read_text())})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/config", methods=["POST"])
@require_auth
def save_config():
    try:
        data = request.get_json(force=True)
        content = data.get("content")
        # validate JSON
        json.dumps(content)
        CONFIG_PATH.write_text(json.dumps(content, indent=4))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/yaml/<name>", methods=["GET"])
@require_auth
def get_yaml(name):
    path = YAML_FILES.get(name)
    if not path:
        return jsonify({"ok": False, "error": "unknown file"}), 404
    try:
        content = path.read_text() if path.exists() else ""
        return jsonify({"ok": True, "path": str(path), "content": content})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/yaml/<name>", methods=["POST"])
@require_auth
def save_yaml(name):
    path = YAML_FILES.get(name)
    if not path:
        return jsonify({"ok": False, "error": "unknown file"}), 404
    try:
        data    = request.get_json(force=True)
        content = data.get("content", "")
        apply   = data.get("apply_netplan", False)

        # Files under /etc/netplan are root-owned; use sudo to write them.
        # Other files (under /opt/libreqos) are writable directly by www-data.
        needs_sudo = Path(str(path)).parent == Path("/etc/netplan")

        if needs_sudo:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as tmp:
                tmp.write(content)
                tmp_path = tmp.name
            try:
                mv = subprocess.run(
                    ["sudo", "/bin/mv", tmp_path, str(path)],
                    capture_output=True, text=True, timeout=10
                )
                if mv.returncode != 0:
                    return jsonify({"ok": False, "error": mv.stderr.strip() or "mv failed"}), 500
                # sudoers allows this exact command only for libreqos.yaml
                if str(path) == "/etc/netplan/libreqos.yaml":
                    subprocess.run(
                        ["sudo", "/bin/chmod", "600", str(path)],
                        capture_output=True, text=True, timeout=10
                    )
            except Exception as e:
                return jsonify({"ok": False, "error": str(e)}), 500
        else:
            path.write_text(content)

        netplan_out = None
        if apply:
            try:
                result = subprocess.run(
                    ["sudo", "/usr/sbin/netplan", "apply"],
                    capture_output=True, text=True, timeout=15
                )
                if result.returncode != 0:
                    err_output = result.stderr.strip() or result.stdout.strip() or ""
                    return jsonify({"ok": True, "netplan_error": err_output or "netplan apply failed"})
                netplan_out = result.stdout.strip() or result.stderr.strip() or "applied"
            except Exception as ne:
                return jsonify({"ok": True, "netplan_error": str(ne)})
        return jsonify({"ok": True, "netplan_output": netplan_out})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/interfaces")
@require_auth
def get_interfaces():
    try:
        if not HAS_PSUTIL:
            return jsonify({"ok": False, "error": "psutil not installed"}), 500
        stats  = psutil.net_if_stats()
        addrs  = psutil.net_if_addrs()
        result = []
        for name, st in stats.items():
            ips = [a.address for a in addrs.get(name, [])
                   if a.family.name in ("AF_INET", "AF_INET6") and not a.address.startswith("fe80")]
            result.append({
                "name":    name,
                "is_up":   st.isup,
                "speed":   st.speed,   # Mbps, 0 if unknown
                "mtu":     st.mtu,
                "duplex":  st.duplex.name if hasattr(st.duplex, "name") else str(st.duplex),
                "addresses": ips,
            })
        result.sort(key=lambda x: (not x["is_up"], x["name"]))
        return jsonify({"ok": True, "interfaces": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _random_id(length=8):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def _db_con():
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    return con


@app.route("/api/devices")
@require_auth
def get_devices():
    try:
        if not DB_PATH.exists():
            return jsonify({"ok": False, "error": "devices.db not found"}), 404
        con = sqlite3.connect(str(DB_PATH))
        con.row_factory = sqlite3.Row
        cur = con.execute("""
            SELECT code, circuit_id, device_id, parent_node, mac, ipv4, ipv6,
                   download_min_mbps, upload_min_mbps,
                   download_max_mbps, upload_max_mbps,
                   comment, source, router, last_seen, is_static, weight,
                   core_name, wan_name
            FROM devices ORDER BY last_seen DESC
        """)
        rows = [dict(r) for r in cur.fetchall()]
        con.close()
        return jsonify({"ok": True, "count": len(rows), "rows": rows})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/wan/stats")
@require_auth
def wan_stats():
    """Return per-WAN device counts and bandwidth totals, plus wan_assignment config."""
    try:
        cfg = _load_config()
        wan_cfg = cfg.get("wan_assignment", {})
        enabled = wan_cfg.get("enabled", True)

        stats = {"enabled": enabled, "wans": []}
        if not enabled:
            return jsonify({"ok": True, **stats})

        if not DB_PATH.exists():
            return jsonify({"ok": True, **stats})

        con = sqlite3.connect(str(DB_PATH))
        con.row_factory = sqlite3.Row
        rows = con.execute("""
            SELECT core_name, wan_name,
                   COUNT(*) AS device_count,
                   COALESCE(SUM(download_max_mbps), 0) AS total_dl,
                   COALESCE(SUM(upload_max_mbps),   0) AS total_ul
            FROM devices
            WHERE core_name != '' AND core_name IS NOT NULL
              AND wan_name  != '' AND wan_name  IS NOT NULL
            GROUP BY core_name, wan_name
            ORDER BY core_name, wan_name
        """).fetchall()
        con.close()

        # Attach configured limits from cores
        limits = {}
        for core in cfg.get("cores", []):
            for i, wan in enumerate(core.get("wans", []), start=1):
                limits[(core["name"], f"WAN{i}")] = {
                    "dl_limit": wan.get("download_limit", 0),
                    "ul_limit": wan.get("upload_limit", 0),
                    "wan_label": wan.get("name", f"WAN{i}"),
                }

        for r in rows:
            key = (r["core_name"], r["wan_name"])
            lim = limits.get(key, {"dl_limit": 0, "ul_limit": 0, "wan_label": r["wan_name"]})
            stats["wans"].append({
                "core_name":    r["core_name"],
                "wan_name":     r["wan_name"],
                "wan_label":    lim["wan_label"],
                "device_count": r["device_count"],
                "total_dl":     r["total_dl"],
                "total_ul":     r["total_ul"],
                "dl_limit":     lim["dl_limit"],
                "ul_limit":     lim["ul_limit"],
            })

        return jsonify({"ok": True, **stats})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _sync_core_wan_address_lists(con, cfg: dict) -> dict:
    """
    Sync core WAN address-lists by delta only (add/remove differences).
    Returns a summary with per-core changes and errors.
    """
    cores = cfg.get("cores", []) or []
    summary = {
        "ok": True,
        "cores": [],
        "total_added": 0,
        "total_removed": 0,
        "errors": [],
    }

    for core in cores:
        core_name = core.get("name", "")
        core_result = {"core": core_name, "added": 0, "removed": 0, "error": ""}
        wans = core.get("wans", []) or []
        if not wans:
            summary["cores"].append(core_result)
            continue

        pool = None
        try:
            pool = _connect_router_api(core)
            api = pool.get_api()
            resource = api.get_resource("/ip/firewall/address-list")

            target = set()
            for i, _wan in enumerate(wans, start=1):
                wan_name = f"WAN{i}"
                rows = con.execute(
                    "SELECT ipv4 FROM devices "
                    "WHERE core_name=? AND wan_name=? AND ipv4 IS NOT NULL AND ipv4 != ''",
                    (core_name, wan_name),
                ).fetchall()
                for (ip,) in rows:
                    target.add((wan_name, ip))

            current = {}
            for i, _wan in enumerate(wans, start=1):
                wan_name = f"WAN{i}"
                entries = resource.get(list=wan_name)
                for e in entries:
                    ip = e.get("address")
                    eid = e.get(".id")
                    if ip and eid:
                        current[(wan_name, ip)] = eid

            to_add = target - set(current.keys())
            to_remove = set(current.keys()) - target

            for wan_name, ip in to_remove:
                try:
                    resource.remove(id=current[(wan_name, ip)])
                    core_result["removed"] += 1
                except Exception:
                    # Keep going; one bad row should not block the rest.
                    pass

            for wan_name, ip in to_add:
                try:
                    resource.add(list=wan_name, address=ip, comment="libreqos-managed")
                    core_result["added"] += 1
                except Exception as ex:
                    if "already have such entry" not in str(ex):
                        raise

            summary["total_added"] += core_result["added"]
            summary["total_removed"] += core_result["removed"]

        except Exception as ex:
            core_result["error"] = str(ex)
            summary["ok"] = False
            summary["errors"].append(f"{core_name}: {ex}")
        finally:
            if pool is not None:
                try:
                    pool.disconnect()
                except Exception:
                    pass

        summary["cores"].append(core_result)

    return summary


@app.route("/api/wan/rebalance", methods=["POST"])
@require_auth
def wan_rebalance():
    """Rebalance active devices across WANs with delta DB updates (no table wipe)."""
    try:
        cfg = _load_config()
        wan_cfg = cfg.get("wan_assignment", {})
        if not wan_cfg.get("enabled", True):
            return jsonify({"ok": False, "error": "WAN assignment is disabled"}), 400

        cores = cfg.get("cores", [])
        if not any(core.get("wans") for core in cores):
            return jsonify({"ok": False, "error": "No WANs configured in cores"}), 400

        excluded = []
        if not wan_cfg.get("include_hotspot", False):
            excluded.append("hotspot")
        if not wan_cfg.get("include_dhcp", False):
            excluded.append("dhcp")

        excl_sql = ""
        if excluded:
            placeholders = ",".join("?" * len(excluded))
            excl_sql = f"(source NOT IN ({placeholders}) OR source IS NULL) AND "

        # Consider stale dynamic rows as offline for manual rebalance cleanup.
        active_cutoff = time.time() - 1200

        con = sqlite3.connect(str(DB_PATH))
        try:
            active_codes = [r[0] for r in con.execute(
                "SELECT code FROM devices WHERE " + excl_sql + "(is_static = 1 OR last_seen >= ?)",
                excluded + [active_cutoff],
            ).fetchall()]

            stale_codes = [r[0] for r in con.execute(
                "SELECT code FROM devices "
                "WHERE " + excl_sql
                + "is_static = 0 AND last_seen < ? "
                "AND COALESCE(core_name, '') != '' AND COALESCE(wan_name, '') != ''",
                excluded + [active_cutoff],
            ).fetchall()]

            if stale_codes:
                con.executemany(
                    "UPDATE devices SET core_name='', wan_name='' WHERE code=?",
                    [(code,) for code in stale_codes],
                )

            # Clear all active assignments so WANManager treats everything as new
            if active_codes:
                con.executemany(
                    "UPDATE devices SET core_name='', wan_name='' WHERE code=?",
                    [(code,) for code in active_codes],
                )

            _wan_manager.assign_wan_nodes(con, cores, wan_cfg)
            sync_summary = _sync_core_wan_address_lists(con, cfg)

            # Rebalance is only fully successful when router API sync succeeds.
            if not sync_summary.get("ok", False):
                return jsonify({
                    "ok": False,
                    "error": "Rebalance applied to DB, but MikroTik API sync failed",
                    "assigned": len(active_codes),
                    "updated": len(active_codes),
                    "offline_cleared": len(stale_codes),
                    "synced": sync_summary,
                }), 502
        finally:
            con.close()

        return jsonify({
            "ok": True,
            "assigned": len(active_codes),
            "updated": len(active_codes),
            "offline_cleared": len(stale_codes),
            "synced": sync_summary,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/devices", methods=["POST"])
@require_auth
def add_device():
    try:
        d = request.get_json(force=True)
        dl = int(d.get("download_max_mbps", 100))
        ul = int(d.get("upload_max_mbps", 100))
        dl_min = max(1, int(dl * 0.5))
        ul_min = max(1, int(ul * 0.5))
        code = d.get("code", "").strip()
        if not code:
            return jsonify({"ok": False, "error": "Code is required"}), 400
        ipv4 = d.get("ipv4", "").strip() or None
        ipv6 = d.get("ipv6", "").strip() or None
        con = _db_con()
        # generate unique IDs
        while True:
            cid = _random_id()
            did = _random_id()
            row = con.execute("SELECT 1 FROM devices WHERE circuit_id=? OR device_id=?", (cid, did)).fetchone()
            if not row:
                break
        con.execute("""
            INSERT INTO devices
              (code, circuit_id, device_id, parent_node, mac, ipv4, ipv6,
               download_min_mbps, upload_min_mbps, download_max_mbps, upload_max_mbps,
               comment, source, router, last_seen, is_static, weight,
               core_name, wan_name)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)
        """, (
            code, cid, did,
            d.get("parent_node", ""),
            d.get("mac", "").strip().upper(),
            ipv4, ipv6,
            dl_min, ul_min, dl, ul,
            d.get("comment", ""),
            d.get("source", "address_list"),
            d.get("router", ""),
            time.time(),
            dl + ul,
            d.get("core_name", ""),
            d.get("wan_name", ""),
        ))
        con.commit()
        con.close()
        return jsonify({"ok": True})
    except sqlite3.IntegrityError as e:
        return jsonify({"ok": False, "error": "Duplicate code or IP: " + str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/devices/<path:code>", methods=["PUT"])
@require_auth
def update_device(code):
    try:
        d = request.get_json(force=True)
        dl = int(d.get("download_max_mbps", 100))
        ul = int(d.get("upload_max_mbps", 100))
        dl_min = max(1, int(dl * 0.5))
        ul_min = max(1, int(ul * 0.5))
        ipv4 = d.get("ipv4", "").strip() or None
        ipv6 = d.get("ipv6", "").strip() or None
        con = _db_con()
        con.execute("""
            UPDATE devices SET
              parent_node=?, mac=?, ipv4=?, ipv6=?,
              download_min_mbps=?, upload_min_mbps=?,
              download_max_mbps=?, upload_max_mbps=?,
              comment=?, source=?, router=?, weight=?,
              core_name=?, wan_name=?
            WHERE code=?
        """, (
            d.get("parent_node", ""),
            d.get("mac", "").strip().upper(),
            ipv4, ipv6,
            dl_min, ul_min, dl, ul,
            d.get("comment", ""),
            d.get("source", "address_list"),
            d.get("router", ""),
            dl + ul,
            d.get("core_name", ""),
            d.get("wan_name", ""),
            code,
        ))
        con.commit()
        con.close()
        return jsonify({"ok": True})
    except sqlite3.IntegrityError as e:
        return jsonify({"ok": False, "error": "Duplicate IP: " + str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/devices/<path:code>", methods=["DELETE"])
@require_auth
def delete_device(code):
    try:
        con = _db_con()
        con.execute("DELETE FROM devices WHERE code=?", (code,))
        con.commit()
        con.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/service/status")
@require_auth
def service_status():
    try:
        result = _systemctl("is-active", "updatecsv.service", capture_output=True, text=True)
        active = result.stdout.strip()
        info = _systemctl(
            "show", "updatecsv.service",
            "--property=ActiveState,SubState,MainPID,ExecMainStartTimestamp",
            capture_output=True, text=True
        )
        props = dict(line.split("=", 1) for line in info.stdout.strip().splitlines() if "=" in line)
        return jsonify({"ok": True, "active": active, **props})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/service/<action>", methods=["POST"])
@require_auth
def service_action(action):
    if action not in ("start", "stop", "restart"):
        return jsonify({"ok": False, "error": "invalid action"}), 400
    try:
        result = _systemctl(action, "updatecsv.service", capture_output=True, text=True)
        if result.returncode != 0:
            return jsonify({"ok": False, "error": result.stderr.strip()}), 500
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/system/restart", methods=["POST"])
@require_auth
def restart_ubuntu_host():
    """Schedule host reboot through systemd."""
    try:
        result = _systemctl("reboot", capture_output=True, text=True)
        if result.returncode != 0:
            return jsonify({"ok": False, "error": result.stderr.strip() or "reboot failed"}), 500
        return jsonify({"ok": True, "message": "Ubuntu restart requested"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# Multi-service management
# ---------------------------------------------------------------------------
MANAGED_SERVICES = ["lqosd", "lqos_scheduler", "updatecsv", "wan_service", "gui"]

LQUSERS_PATHS = [
    Path("/etc/lqos/lqusers.toml"),
    Path("/opt/libreqos/lqusers.toml"),
    Path("/opt/libreqos/src/lqusers.toml"),
]

def _svc_name(name):
    """Ensure .service suffix."""
    return name if name.endswith(".service") else name + ".service"

def _get_service_info(name):
    svc = _svc_name(name)
    try:
        info = _systemctl(
            "show", svc,
            "--property=ActiveState,SubState,MainPID,ExecMainStartTimestamp,LoadState",
            capture_output=True, text=True
        )
        props = {}
        for line in info.stdout.strip().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                props[k] = v
    except RuntimeError:
        props = {}
    return {
        "name":      name,
        "active":    props.get("ActiveState", "unknown"),
        "sub":       props.get("SubState",    "unknown"),
        "pid":       props.get("MainPID",     "0"),
        "started":   props.get("ExecMainStartTimestamp", ""),
        "loaded":    props.get("LoadState",   "not-found"),
    }

@app.route("/api/services")
@require_auth
def get_services():
    result = []
    for n in MANAGED_SERVICES:
        try:
            result.append(_get_service_info(n))
        except Exception:
            result.append({"name": n, "active": "unknown", "sub": "unknown",
                           "pid": "0", "started": "", "loaded": "not-found"})
    return jsonify({"ok": True, "services": result})

@app.route("/api/services/<name>/<action>", methods=["POST"])
@require_auth
def manage_service(name, action):
    if name not in MANAGED_SERVICES:
        return jsonify({"ok": False, "error": "unknown service"}), 400
    if action not in ("start", "stop", "restart"):
        return jsonify({"ok": False, "error": "invalid action"}), 400
    try:
        result = _systemctl(action, _svc_name(name), capture_output=True, text=True)
        if result.returncode != 0:
            return jsonify({"ok": False, "error": result.stderr.strip()}), 500
        time.sleep(1)
        return jsonify({"ok": True, "service": _get_service_info(name)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/services/<name>/logs")
@require_auth
def service_logs(name):
    if name not in MANAGED_SERVICES:
        return jsonify({"ok": False, "error": "unknown service"}), 400
    try:
        result = subprocess.run(
            [JOURNALCTL, "-u", _svc_name(name), "-n", "80",
             "--no-pager", "--output=short-iso"],
            capture_output=True, text=True
        )
        return jsonify({"ok": True, "logs": result.stdout})
    except (FileNotFoundError, OSError) as e:
        return jsonify({"ok": False, "error": f"journalctl not available: {e}"}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/lqusers/status")
@require_auth
def lqusers_status():
    for p in LQUSERS_PATHS:
        if p.exists():
            return jsonify({"ok": True, "exists": True, "path": str(p)})
    return jsonify({"ok": True, "exists": False, "path": None})

@app.route("/api/lqusers/reset", methods=["POST"])
@require_auth
def lqusers_reset():
    try:
        removed = None
        for p in LQUSERS_PATHS:
            if p.exists():
                p.unlink()
                removed = str(p)
                break
        # restart lqosd after removal
        try:
            _systemctl("restart", "lqosd.service", capture_output=True, text=True)
        except RuntimeError:
            pass
        return jsonify({"ok": True, "removed": removed})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/troubleshoot/flush", methods=["POST"])
@require_auth
def flush_data():
    results = {}
    errors = []

    # Clear devices.db
    try:
        if DB_PATH.exists():
            con = sqlite3.connect(str(DB_PATH))
            con.execute("DELETE FROM devices")
            con.commit()
            con.close()
            results["devices_db"] = "cleared"
        else:
            results["devices_db"] = "not found"
    except Exception as e:
        errors.append(f"devices.db: {e}")

    # Clear ShapedDevices.csv (write header only)
    shaped_path = OPT_DIR / "ShapedDevices.csv"
    try:
        header = "Circuit ID,Circuit Name,Device ID,Device Name,Parent Node,MAC,IPv4,IPv6,Download Min Mbps,Upload Min Mbps,Download Max Mbps,Upload Max Mbps,Comment\n"
        shaped_path.write_text(header)
        results["ShapedDevices.csv"] = "cleared"
    except Exception as e:
        errors.append(f"ShapedDevices.csv: {e}")

    # Clear network.json (write empty object)
    network_path = OPT_DIR / "network.json"
    try:
        network_path.write_text("{}\n")
        results["network.json"] = "cleared"
    except Exception as e:
        errors.append(f"network.json: {e}")

    return jsonify({"ok": not errors, "results": results, "errors": errors})


@app.route("/api/troubleshoot/bras")
@require_auth
def troubleshoot_routers():
    try:
        cfg = _load_config()
        routers = cfg.get("bras") or []
        result = []
        for idx, router in enumerate(routers):
            result.append({
                "index": idx,
                "name": router.get("name") or f"Router {idx + 1}",
                "address": router.get("address", ""),
                "port": int(router.get("port", 8728) or 8728),
            })
        return jsonify({"ok": True, "bras": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/troubleshoot/mt/ping", methods=["POST"])
@require_auth
def troubleshoot_mt_ping():
    try:
        data = request.get_json(silent=True) or {}
        idx = int(data.get("router_index", 0))
        router = _router_from_config(idx)
        host = (data.get("host") or router.get("address") or "").strip()
        if not host:
            return jsonify({"ok": False, "error": "Router address is empty"}), 400
        ping_bin = _which("ping")
        if ping_bin:
            result = subprocess.run(
                [ping_bin, "-c", "3", "-W", "2", host],
                capture_output=True,
                text=True,
                timeout=15,
            )
            output = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
            success = result.returncode == 0
            method = "ping"
        else:
            # Minimal Ubuntu images may not include iputils-ping; fallback to TCP connect.
            port = int(data.get("port") or router.get("port", 8728) or 8728)
            start = time.time()
            try:
                with socket.create_connection((host, port), timeout=3):
                    pass
                latency_ms = int((time.time() - start) * 1000)
                output = f"tcp connect to {host}:{port} succeeded in {latency_ms}ms"
                success = True
            except Exception as ce:
                output = f"tcp connect to {host}:{port} failed: {ce}"
                success = False
            method = "tcp_connect"
        return jsonify({
            "ok": True,
            "success": success,
            "host": host,
            "method": method,
            "output": output.strip(),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/troubleshoot/mt/connect", methods=["POST"])
@require_auth
def troubleshoot_mt_connect():
    pool = None
    try:
        data = request.get_json(silent=True) or {}
        idx = int(data.get("router_index", 0))
        router = _router_from_config(idx)
        pool = _connect_router_api(router)
        api = pool.get_api()
        identity_rows = api.get_resource("/system/identity").get()
        identity = identity_rows[0].get("name", "unknown") if identity_rows else "unknown"
        return jsonify({
            "ok": True,
            "connected": True,
            "router": router.get("name") or router.get("address"),
            "identity": identity,
        })
    except Exception as e:
        return jsonify({"ok": False, "connected": False, "error": str(e)}), 500
    finally:
        if pool is not None:
            try:
                pool.disconnect()
            except Exception:
                pass


@app.route("/api/dashboard/mikrotik/resource")
@require_auth
def dashboard_mikrotik_resource():
    """Return compact CPU/RAM percentages for all routers in config.json."""
    try:
        cfg = _load_config()
        routers = cfg.get("bras") or []
        result = []

        for idx, router in enumerate(routers):
            pool = None
            try:
                router_copy = dict(router)
                router_copy["port"] = int(router_copy.get("port", 8728) or 8728)
                pool = _connect_router_api(router_copy)
                api = pool.get_api()

                resource_rows = api.get_resource("/system/resource").get()
                identity_rows = api.get_resource("/system/identity").get()
                resource = resource_rows[0] if resource_rows else {}
                identity = identity_rows[0] if identity_rows else {}

                total_mem = int(resource.get("total-memory", 0) or 0)
                free_mem = int(resource.get("free-memory", 0) or 0)
                used_mem = max(total_mem - free_mem, 0)
                ram_percent = round((used_mem / total_mem * 100.0), 1) if total_mem > 0 else 0.0

                result.append({
                    "index": idx,
                    "router": router_copy.get("name") or router_copy.get("address") or f"Router {idx + 1}",
                    "identity": identity.get("name", ""),
                    "cpu_percent": float(resource.get("cpu-load", 0) or 0),
                    "ram_percent": ram_percent,
                    "uptime": str(resource.get("uptime", "") or ""),
                    "ok": True,
                })
            except Exception as re:
                result.append({
                    "index": idx,
                    "router": router.get("name") or router.get("address") or f"Router {idx + 1}",
                    "cpu_percent": None,
                    "ram_percent": None,
                    "uptime": None,
                    "ok": False,
                    "error": str(re),
                })
            finally:
                if pool is not None:
                    try:
                        pool.disconnect()
                    except Exception:
                        pass

        return jsonify({"ok": True, "count": len(result), "bras": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/troubleshoot/mt/permissions", methods=["POST"])
@require_auth
def troubleshoot_mt_permissions():
    pool = None
    try:
        data = request.get_json(silent=True) or {}
        idx = int(data.get("router_index", 0))
        router = _router_from_config(idx)
        pool = _connect_router_api(router)
        api = pool.get_api()

        # Use the configured username for this router
        configured_user = router.get("username", "")

        group_rows = api.get_resource("/user/group").get()
        user_rows = api.get_resource("/user").get()

        group = next((g for g in group_rows if g.get("name") == EXPECTED_MT_GROUP), None)
        user = next((u for u in user_rows if u.get("name") == configured_user), None)

        found_policy = (group or {}).get("policy", "")
        found_policy_set = {p.strip() for p in found_policy.split(",") if p.strip()}
        missing = sorted(EXPECTED_MT_POLICY_SET - found_policy_set)
        # Extra deny items (starting with !) are acceptable — they only add more restrictions
        extra = sorted(
            i for i in (found_policy_set - EXPECTED_MT_POLICY_SET)
            if not i.startswith("!")
        )

        group_ok = bool(group) and not missing and not extra
        user_group = (user or {}).get("group", "")
        user_disabled = _to_bool((user or {}).get("disabled", False))
        user_ok = bool(user) and user_group == EXPECTED_MT_GROUP and not user_disabled

        return jsonify({
            "ok": True,
            "permissions_ok": group_ok and user_ok,
            "group_ok": group_ok,
            "user_ok": user_ok,
            "group_found": bool(group),
            "user_found": bool(user),
            "user_group": user_group,
            "user_disabled": user_disabled,
            "expected_group": EXPECTED_MT_GROUP,
            "expected_user": configured_user,
            "expected_policy": EXPECTED_MT_POLICY,
            "found_policy": found_policy,
            "missing_policy_items": missing,
            "extra_policy_items": sorted(found_policy_set - EXPECTED_MT_POLICY_SET),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        if pool is not None:
            try:
                pool.disconnect()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# lqos.conf structured API
# ---------------------------------------------------------------------------

def _parse_toml_simple(text: str) -> dict:
    """Minimal TOML parser: handles string/bool/int/float/array values."""
    result: dict = {}
    section = result
    section_key = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('[') and not line.startswith('[['):
            section_key = line[1:line.index(']')]
            section = {}
            result[section_key] = section
            continue
        if '=' in line:
            k, _, v = line.partition('=')
            k = k.strip(); v = v.strip()
            # strip inline comment
            if '#' in v and not v.startswith('"'):
                v = v[:v.index('#')].strip()
            if v == 'true':
                section[k] = True
            elif v == 'false':
                section[k] = False
            elif v.startswith('"') and v.endswith('"'):
                section[k] = v[1:-1]
            elif v.startswith('['):
                # collect array (may span lines — grab from raw text)
                section[k] = _extract_array(text, section_key, k)
            else:
                try:
                    section[k] = int(v) if '.' not in v else float(v)
                except ValueError:
                    section[k] = v
    return result

def _extract_array(text: str, section: str | None, key: str) -> list:
    """Extract an array value from TOML text for a given section+key."""
    import re
    # locate the key= line, then collect items until ]
    if section:
        sec_m = re.search(rf'^\[{re.escape(section)}\]', text, re.MULTILINE)
        search_text = text[sec_m.end():] if sec_m else text
    else:
        search_text = text
    m = re.search(rf'^{re.escape(key)}\s*=\s*(\[.*?\])', search_text, re.MULTILINE | re.DOTALL)
    if not m:
        return []
    raw = m.group(1)
    items = re.findall(r'"([^"]*)"', raw)
    return items

def _toml_update(text: str, section: str | None, key: str, value) -> str:
    """Return TOML text with the given key updated to value."""
    import re

    def _update_key_in_scope(scope_text: str) -> str:
        """Replace a key assignment inside a section/top-level text block."""
        lines = scope_text.splitlines(keepends=True)
        key_re = re.compile(rf'^(\s*)({re.escape(key)})(\s*=\s*)(.*?)(\r?\n?)$')

        for i, line in enumerate(lines):
            m = key_re.match(line)
            if not m:
                continue

            indent, _, eq_ws, rhs, line_end = m.groups()
            if not line_end:
                line_end = "\n"

            new_line = f"{indent}{key}{eq_ws}{v_str}{line_end}"
            rhs_stripped = rhs.strip()

            # If this assignment is a multiline array, remove the full old block.
            if rhs_stripped.startswith("[") and "]" not in rhs_stripped:
                j = i + 1
                while j < len(lines):
                    if "]" in lines[j]:
                        j += 1
                        break
                    j += 1
                lines[i:j] = [new_line]
            else:
                lines[i] = new_line

            return "".join(lines)

        return scope_text

    if isinstance(value, bool):
        v_str = 'true' if value else 'false'
    elif isinstance(value, str):
        v_str = f'"{value}"'
    elif isinstance(value, list):
        items = ', '.join(f'"{i}"' for i in value)
        v_str = f'[{items}]'
    else:
        v_str = str(value)

    if section:
        sec_m = re.search(rf'^\[{re.escape(section)}\]', text, re.MULTILINE)
        if not sec_m:
            return text
        before = text[:sec_m.end()]
        after  = text[sec_m.end():]
        next_sec = re.search(r'^\[', after, re.MULTILINE)
        body_end = next_sec.start() if next_sec else len(after)
        body = after[:body_end]
        rest = after[body_end:]
        body = _update_key_in_scope(body)
        return before + body + rest
    else:
        first_sec = re.search(r'^\[', text, re.MULTILINE)
        top_end = first_sec.start() if first_sec else len(text)
        top = text[:top_end]
        top = _update_key_in_scope(top)
        return top + text[top_end:]

@app.route("/api/lqos", methods=["GET"])
@require_auth
def get_lqos_conf():
    path = _find_lqos_conf()
    if not path.exists():
        return jsonify({"ok": True, "exists": False, "path": str(path), "data": {}})
    try:
        raw  = path.read_text()
        data = _parse_toml_simple(raw)
        return jsonify({"ok": True, "exists": True, "path": str(path), "data": data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/lqos", methods=["POST"])
@require_auth
def save_lqos_conf():
    path = _find_lqos_conf()
    try:
        updates = request.get_json(force=True).get("updates", {})
        # updates = { "section|key": value, ...} where section="" means top-level
        if not path.exists():
            return jsonify({"ok": False, "error": "lqos.conf not found"}), 404
        text = path.read_text()
        for field_key, value in updates.items():
            if '|' in field_key:
                sec, k = field_key.split('|', 1)
            else:
                sec, k = None, field_key
            text = _toml_update(text, sec or None, k, value)
        path.write_text(text)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
