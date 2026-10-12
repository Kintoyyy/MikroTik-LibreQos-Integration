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
import hmac
import contextlib
from pathlib import Path
from flask import Flask, render_template, request, jsonify, Response, stream_with_context, session
from wan_manager import WANManager
from rate_resolver import RateResolver
from settings import (
    MANAGED_SERVICES as _SETTINGS_MANAGED_SERVICES,
    EXPECTED_MT_GROUP as _SETTINGS_MT_GROUP,
    EXPECTED_MT_POLICY as _SETTINGS_MT_POLICY,
    EXPECTED_CORE_GROUP as _SETTINGS_CORE_GROUP,
    EXPECTED_CORE_POLICY as _SETTINGS_CORE_POLICY,
    LOGIN_MAX_ATTEMPTS, LOGIN_LOCKOUT_SECONDS, WAN_MANUAL_ACTIVE_CUTOFF, SCAN_INTERVAL,
    atomic_write_text, libreqos_dir,
)
from device_database import ensure_jobs_table, prune_jobs, timed_job

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
# SameSite=Strict: the session cookie is never sent on cross-site requests (CSRF).
app.config.update(SESSION_COOKIE_SAMESITE="Strict", SESSION_COOKIE_HTTPONLY=True)

# ---------------------------------------------------------------------------
# Paths. The app and its state live in its own directory (SCRIPT_DIR,
# /opt/lq-sync when installed); only ShapedDevices.csv and network.json are
# written into LibreQoS's directory. System files fall back to the script
# directory so local/dev runs without /etc/netplan or /etc/lqos.conf still
# find something to display.
# ---------------------------------------------------------------------------
SCRIPT_DIR   = Path(__file__).parent.resolve()
LIBREQOS_DIR = Path(libreqos_dir())
NETPLAN_DIR  = Path("/etc/netplan")

def find_file(name, base):
    """Return base/name if it exists, else the copy in the script directory."""
    path = base / name
    return path if path.exists() else SCRIPT_DIR / name

CONFIG_PATH   = SCRIPT_DIR / "config.json"
SETTINGS_PATH = SCRIPT_DIR / "settings.json"
DB_PATH       = SCRIPT_DIR / "devices.db"
AUTH_PATH     = SCRIPT_DIR / "gui_auth.json"

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
    _save_auth(data)
    return data

def _save_auth(data: dict):
    # 0600: the secret_key signs session cookies; anyone who can read it can log in.
    atomic_write_text(AUTH_PATH, json.dumps(data, indent=2), mode=0o600)

_auth = _load_auth()
app.secret_key = _auth["secret_key"]
try:
    os.chmod(AUTH_PATH, 0o600)  # tighten files created by older versions
except OSError:
    pass

def _verify_password(password: str) -> bool:
    return hmac.compare_digest(_hash_password(password, _auth["salt"]), _auth["password_hash"])

# Failed logins per client IP: {ip: (failure_count, locked_until)}
_login_failures: dict = {}
_login_lock = threading.Lock()

@app.before_request
def _csrf_guard():
    """
    Reject state-changing API calls that lack the X-Requested-With header.
    The frontend adds it to every fetch; a cross-site form or image cannot
    set custom headers, and a cross-site fetch with one needs CORS approval
    this app never grants.
    """
    if (request.path.startswith("/api/")
            and request.method not in ("GET", "HEAD", "OPTIONS")
            and request.headers.get("X-Requested-With") != "fetch"):
        return jsonify({"ok": False, "error": "Missing X-Requested-With header"}), 403

def require_auth(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("authed"):
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper

LQOS_CONF_PATH = find_file("lqos.conf", base=Path("/etc"))

def _find_lqos_conf():
    return LQOS_CONF_PATH

YAML_FILES = {
    "libreqos":      find_file("libreqos.yaml", base=NETPLAN_DIR),
    "lqos":          LQOS_CONF_PATH,
    "network_json":  LIBREQOS_DIR / "network.json",
    "config_json":   CONFIG_PATH,
    "settings_json": SETTINGS_PATH,
    "shaped_devices": LIBREQOS_DIR / "ShapedDevices.csv",
    "updatecsv":     SCRIPT_DIR / "updatecsv.py",
    "rate_resolver": SCRIPT_DIR / "rate_resolver.py",
    "wan_manager":   SCRIPT_DIR / "wan_manager.py",
    "network":       find_file("50-cloud-init.yaml", base=NETPLAN_DIR),
}

EXPECTED_MT_GROUP    = _SETTINGS_MT_GROUP
EXPECTED_MT_POLICY   = _SETTINGS_MT_POLICY
EXPECTED_MT_POLICY_SET = {p.strip() for p in EXPECTED_MT_POLICY.split(",") if p.strip()}

EXPECTED_CORE_GROUP  = _SETTINGS_CORE_GROUP
EXPECTED_CORE_POLICY = _SETTINGS_CORE_POLICY
EXPECTED_CORE_POLICY_SET = {p.strip() for p in EXPECTED_CORE_POLICY.split(",") if p.strip()}

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

def _as_root(cmd):
    """
    Run cmd as root: unchanged when this process is root (the service runs as
    root), else prefixed with sudo. Binaries are resolved without relying on
    PATH, which older unit files set to a literal "$PATH".
    """
    if os.geteuid() == 0:
        return cmd
    sudo = _which("sudo")
    if not sudo:
        raise RuntimeError("needs root, and sudo was not found")
    return [sudo] + list(cmd)

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
_live: dict = {}              # latest 1-second host CPU %, for the status strip
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
        binary = _which(cmd[0])
        if not binary:
            continue
        try:
            proc = subprocess.run([binary] + cmd[1:], capture_output=True, text=True, timeout=3)
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


def _router_from_key(key: str) -> dict:
    """Resolve a router from a 'type:index' key (e.g. 'bras:0' or 'core:0')."""
    cfg = _load_config()
    try:
        rtype, idx_str = key.split(":", 1)
        idx = int(idx_str)
    except (ValueError, AttributeError):
        raise ValueError(f"Invalid router key: {key!r}")
    if rtype == "bras":
        pool = cfg.get("bras") or []
    elif rtype == "core":
        pool = cfg.get("cores") or []
    else:
        raise ValueError(f"Unknown router type: {rtype!r}")
    if idx < 0 or idx >= len(pool):
        raise ValueError(f"Router key {key!r} out of range")
    router = dict(pool[idx])
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

@contextlib.contextmanager
def _connect_for_wan(router: dict):
    """Adapter for WANManager: yields an API object (or None) and closes the connection."""
    pool = api = None
    try:
        pool = _connect_router_api(router)
        api = pool.get_api()
    except Exception:
        api = None
    try:
        yield api
    finally:
        if pool is not None:
            try:
                pool.disconnect()
            except Exception:
                pass


def _wan_enabled(cfg: dict) -> bool:
    """WAN assignment is optional and off unless explicitly enabled."""
    return bool((cfg.get("wan_assignment") or {}).get("enabled", False))

_wan_manager = WANManager(_connect_for_wan)

def _broadcast_loop():
    while True:
        if HAS_PSUTIL:
            cpu_per  = psutil.cpu_percent(interval=1, percpu=True)
            cpu_avg  = sum(cpu_per) / len(cpu_per)
            _live["cpu"] = cpu_avg
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
                    if len(q) < 30:   # a stalled client must not grow memory forever
                        q.append(data)
                except Exception:
                    dead.append(q)
            for q in dead:
                _metric_listeners.remove(q)

        time.sleep(1)

threading.Thread(target=_broadcast_loop, daemon=True).start()

# ---------------------------------------------------------------------------
# Monitoring. Once a minute, sample this host, this app's services, every
# job's CPU / duration / errors, and each router's health. The latest values
# feed the Pollers page; the history goes into RRD files for the graphs.
# ---------------------------------------------------------------------------
RRD_DIR      = SCRIPT_DIR / "rrd"
RRDTOOL      = _which("rrdtool")
MONITOR_STEP = 60                 # seconds between samples (the RRD step)
APP_SCRIPTS  = {"updatecsv": "updatecsv.py", "wan_service": "wan_service.py", "gui": "gui.py"}
# Job kind → CPU-by-job graph series
JOB_SERIES   = {"poller": "cpu_poller", "config": "cpu_build", "build": "cpu_build",
                "apply": "cpu_apply", "wan": "cpu_wan", "stats": "cpu_stats", "router": "cpu_stats"}
_STARTED_AT  = time.time()

# 1-minute rows for 5 h, 5-minute for 50 h, 30-minute for 15 days, 6-hour for 375 days
_RRAS = [f"RRA:{cf}:0.5:{steps}:{rows}"
         for cf in ("AVERAGE", "MAX")
         for steps, rows in ((1, 300), (5, 600), (30, 720), (360, 1500))]

RRD_SCHEMAS = {
    "host":    ["cpu", "mem", "disk", "load1"],
    "app":     ["cpu_poller", "cpu_build", "cpu_apply", "cpu_wan", "cpu_stats", "cpu_other",
                "mem_updatecsv", "mem_wan_service", "mem_gui"],
    "polling": ["scan_s", "build_s", "apply_s", "wan_s", "errors"],
    "devices": ["pppoe", "hotspot", "dhcp", "address_list", "static"],
    "router":  ["cpu", "mem", "temp", "volt", "api_ms"],   # one file per router
}

# Latest samples, shared with the request threads
_mon = {"host": {}, "app": {}, "routers": {}, "job_cpu_now": {}}
_mon_lock = threading.Lock()


def _rrd_path(name):
    return RRD_DIR / f"{name}.rrd"


def _rrd_update(name, schema, values):
    """Append one sample to RRD file `name` (created on first use). None is stored as unknown."""
    if not RRDTOOL:
        return
    path = _rrd_path(name)
    if not path.exists():
        RRD_DIR.mkdir(parents=True, exist_ok=True)
        ds = [f"DS:{ds}:GAUGE:{MONITOR_STEP * 2}:U:U" for ds in RRD_SCHEMAS[schema]]
        subprocess.run([RRDTOOL, "create", str(path), "--step", str(MONITOR_STEP),
                        "--start", "now-120"] + ds + _RRAS,
                       capture_output=True, text=True, timeout=10, check=True)
    sample = ":".join("U" if v is None else f"{v:.4f}" for v in values)
    r = subprocess.run([RRDTOOL, "update", str(path), f"N:{sample}"],
                       capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        raise RuntimeError(f"rrdtool update {path.name}: {r.stderr.strip()}")


def _app_processes():
    """{service: psutil.Process} for this app's running services."""
    found = {"gui": psutil.Process(os.getpid())}
    for proc in psutil.process_iter(["pid", "cmdline"]):
        cmdline = proc.info.get("cmdline") or []
        for service, script in APP_SCRIPTS.items():
            if service in found:
                continue
            for arg in cmdline:
                if arg == str(SCRIPT_DIR / script):
                    found[service] = proc
                elif arg == script:
                    try:
                        if Path(proc.cwd()) == SCRIPT_DIR:
                            found[service] = proc
                    except (psutil.Error, OSError):
                        pass
    return found


def _sample_stats(conn, prev):
    """One monitoring sample: host, app services, jobs, devices. Updates _mon and the RRDs."""
    now = time.time()

    vm, du = psutil.virtual_memory(), psutil.disk_usage("/")
    host = {
        "cpu":  psutil.cpu_percent(interval=None),
        "mem":  vm.percent, "mem_used": vm.total - vm.available, "mem_total": vm.total,
        "disk": du.percent, "disk_used": du.used, "disk_total": du.total,
        "load": list(os.getloadavg()),
    }

    services, seen = {}, set()
    for service, proc in _app_processes().items():
        try:
            with proc.oneshot():
                t = proc.cpu_times()
                cpu_s, rss = t.user + t.system, proc.memory_info().rss
                threads, created = proc.num_threads(), proc.create_time()
        except psutil.Error:
            continue
        key = (service, proc.pid)
        seen.add(key)
        last = prev["proc"].get(key)
        prev["proc"][key] = (cpu_s, now)
        services[service] = {
            "pid": proc.pid, "threads": threads, "uptime_s": now - created,
            "mem_mb": rss / 1048576,
            "cpu": (cpu_s - last[0]) / (now - last[1]) * 100 if last else None,
        }
    prev["proc"] = {k: v for k, v in prev["proc"].items() if k in seen}

    # Per-job CPU (% of one core) over the last sample window, from cpu_total deltas
    series = dict.fromkeys(set(JOB_SERIES.values()), 0.0)
    job_cpu_now, error_delta, have_prev = {}, 0, bool(prev["jobs"])
    took = {"poller": 0.0, "build": None, "apply": None, "wan": 0.0}
    for name, kind, cpu_total, errors, job_took in conn.execute(
            "SELECT name, kind, cpu_total, errors, took FROM jobs"):
        last = prev["jobs"].get(name)
        if last:
            pct = max(0.0, (cpu_total - last[0]) / (now - last[2]) * 100)
            job_cpu_now[name] = pct
            if kind in JOB_SERIES:
                series[JOB_SERIES[kind]] += pct
            error_delta += max(0, errors - last[1])
        prev["jobs"][name] = (cpu_total, errors, now)
        if kind in ("poller", "wan"):
            took[kind] += job_took
        elif kind in ("build", "apply"):
            took[kind] = job_took

    service_cpu = [s["cpu"] for s in services.values()]
    if have_prev and service_cpu and None not in service_cpu:
        in_jobs = sum(v for k, v in series.items() if k != "cpu_apply")   # apply runs in a child process
        series["cpu_other"] = max(0.0, sum(service_cpu) - in_jobs)
    else:
        series["cpu_other"] = None

    try:
        by_source = dict(conn.execute(
            "SELECT source, COUNT(*) FROM devices WHERE is_static = 0 GROUP BY source").fetchall())
        static = conn.execute("SELECT COUNT(*) FROM devices WHERE is_static = 1").fetchone()[0]
        devices = [by_source.get(s, 0) for s in ("pppoe", "hotspot", "dhcp", "address_list")] + [static]
    except sqlite3.Error:
        devices = [None] * 5   # the scanner has not created the table yet

    with _mon_lock:
        _mon["host"] = host
        _mon["app"] = {"services": services, "at": now}
        _mon["job_cpu_now"] = job_cpu_now

    mem = lambda svc: services[svc]["mem_mb"] if svc in services else None
    cpu = lambda ds: series[ds] if have_prev else None
    _rrd_update("host", "host", [host["cpu"], host["mem"], host["disk"], host["load"][0]])
    _rrd_update("app", "app", [cpu("cpu_poller"), cpu("cpu_build"), cpu("cpu_apply"), cpu("cpu_wan"),
                               cpu("cpu_stats"), series["cpu_other"],
                               mem("updatecsv"), mem("wan_service"), mem("gui")])
    _rrd_update("polling", "polling", [took["poller"], took["build"], took["apply"], took["wan"],
                                       error_delta if have_prev else None])
    _rrd_update("devices", "devices", devices)


def _health_values(rows):
    """(temperature °C, voltage V) from /system/health: one row of fields (RouterOS 6) or name/value rows (7)."""
    values = {}
    for row in rows:
        if "name" in row and "value" in row:
            values[row["name"]] = row["value"]
        else:
            values.update(row)

    def first(*keys):
        for k in keys:
            try:
                return float(values[k])
            except (KeyError, TypeError, ValueError):
                pass
        return None

    return (first("temperature", "cpu-temperature", "board-temperature1", "board-temperature"),
            first("voltage", "psu-voltage", "psu1-voltage"))


def _router_targets(cfg):
    """[(kind, router)] polled for health: every BRAS, plus core routers while WAN is on."""
    targets = [("bras", r) for r in cfg.get("bras") or []]
    if _wan_enabled(cfg):
        targets += [("core", r) for r in cfg.get("cores") or []]
    return [(kind, r) for kind, r in targets if r.get("address")]


def _router_key(kind, name):
    return f"router-{kind}-" + re.sub(r"[^A-Za-z0-9_.-]+", "_", name or "router")


def _poll_router(kind, router):
    """Read CPU, memory, temperature and voltage from one router."""
    info = {"name": router.get("name") or router.get("address", ""), "kind": kind,
            "address": router.get("address", ""), "ok": False, "error": "", "at": time.time()}
    t0, pool = time.monotonic(), None
    try:
        pool = _connect_router_api(router)
        pool.socket_timeout = 5           # a dead router must not stall the poll loop
        api = pool.get_api()
        res = (api.get_resource("/system/resource").get() or [{}])[0]
        ident = (api.get_resource("/system/identity").get() or [{}])[0]
        try:
            health = api.get_resource("/system/health").get()
        except Exception:
            health = []                   # not every board reports health
        total = float(res.get("total-memory", 0) or 0)
        free = float(res.get("free-memory", 0) or 0)
        temp, volt = _health_values(health)
        info.update(
            ok=True, cpu=float(res.get("cpu-load", 0) or 0),
            mem=(total - free) / total * 100 if total else None,
            temp=temp, volt=volt, uptime=res.get("uptime", ""), identity=ident.get("name", ""),
            version=res.get("version", ""), board=res.get("board-name", ""),
            api_ms=(time.monotonic() - t0) * 1000,
        )
    except Exception as e:
        info["error"] = str(e) or type(e).__name__
    finally:
        if pool is not None:
            try:
                pool.disconnect()
            except Exception:
                pass
    return info


def _poll_routers(conn):
    """Poll every configured router once, record each poll as a job, update the router RRDs."""
    targets = _router_targets(_load_config())
    latest, job_names = {}, []
    for kind, router in targets:
        name = router.get("name") or router.get("address", "")
        job_names.append(f"router {name}")
        with timed_job(conn, f"router {name}", "router", "gui", MONITOR_STEP) as job:
            info = _poll_router(kind, router)
            job["error"] = info["error"]
        key = _router_key(kind, name)
        latest[key] = info
        try:
            _rrd_update(key, "router", [info.get(k) for k in RRD_SCHEMAS["router"]])
        except Exception as e:
            app.logger.warning(f"router RRD {key}: {e}")
    prune_jobs(conn, "gui", "router", job_names)
    with _mon_lock:
        _mon["routers"] = latest


def _monitor_loop(work, name):
    """Run work(conn) every MONITOR_STEP seconds on this thread's own DB connection."""
    conn = None
    while True:
        started = time.time()
        try:
            if conn is None:
                conn = sqlite3.connect(str(DB_PATH), timeout=30)
                ensure_jobs_table(conn)
            work(conn)
        except Exception as e:
            app.logger.warning(f"{name}: {e}")
        time.sleep(max(1.0, MONITOR_STEP - (time.time() - started)))


# Previous cumulative CPU / error counters, for per-minute deltas
_stats_prev = {"proc": {}, "jobs": {}}


def _stats_job(conn):
    with timed_job(conn, "host & app stats", "stats", "gui", MONITOR_STEP):
        _sample_stats(conn, _stats_prev)


def start_monitoring():
    if not HAS_PSUTIL:
        return
    threading.Thread(target=_monitor_loop, args=(_stats_job, "stats"), daemon=True).start()
    threading.Thread(target=_monitor_loop, args=(_poll_routers, "router polls"), daemon=True).start()


# ── Graphs (rendered by rrdtool, styled to match the GUI) ──────────────────
GRAPH_RANGES = {"3h": "3 hours", "30h": "30 hours", "10d": "10 days", "360d": "360 days"}
_GRAPH_STYLE = [
    "--width", "620", "--height", "160", "--border", "0", "--slope-mode",
    "--disable-rrdtool-tag", "--units-exponent", "0", "--lower-limit", "0",
    "--color", "BACK#0d1117", "--color", "CANVAS#0d1117",
    "--color", "SHADEA#0d1117", "--color", "SHADEB#0d1117",
    "--color", "GRID#262c36", "--color", "MGRID#3a414c", "--color", "FONT#c9d1d9",
    "--color", "AXIS#8b949e", "--color", "FRAME#30363d", "--color", "ARROW#8b949e",
    "--grid-dash", "1:2",
    "--font", "DEFAULT:7:DejaVu Sans Mono", "--font", "TITLE:8:DejaVu Sans Mono",
]
_graph_cache: dict = {}


def _series(rrd, ds, var, label, color, draw="LINE1", unit="", stack=False):
    """DEF/VDEF/draw/GPRINT arguments for one series with an avg / max / now legend."""
    path = str(_rrd_path(rrd)).replace(":", "\\:")
    fmt = "%6.2lf" + unit.replace("%", "%%")
    label = label.replace(":", "\\:")
    return [
        f"DEF:{var}={path}:{ds}:AVERAGE",
        f"DEF:{var}x={path}:{ds}:MAX",
        f"VDEF:{var}a={var},AVERAGE",
        f"VDEF:{var}m={var}x,MAXIMUM",
        f"VDEF:{var}l={var},LAST",
        f"{draw}:{var}#{color}:{label:<20}" + (":STACK" if stack else ""),
        f"GPRINT:{var}a:avg {fmt}",
        f"GPRINT:{var}m:max {fmt}",
        f"GPRINT:{var}l:now {fmt}\\l",
    ]


def _graph_defs(cfg):
    """{graph id: {title, vlabel, rrd, args}} for every graph the current config has."""
    defs = {}

    def graph(gid, title, vlabel, rrd, series):
        args = []
        for i, (ds, label, color, draw, unit, stack) in enumerate(series):
            args += _series(rrd, ds, f"v{i}", label, color, draw, unit, stack)
        defs[gid] = {"title": title, "vlabel": vlabel, "rrd": rrd, "args": args}

    graph("app-cpu", "LQ-Sync CPU by job", "% of one core", "app", [
        ("cpu_poller", "router scans",       "3b82f6", "AREA", "%", False),
        ("cpu_build",  "build topology",     "8b5cf6", "AREA", "%", True),
        ("cpu_apply",  "LibreQoS apply",     "22c55e", "AREA", "%", True),
        ("cpu_wan",    "WAN sync",           "14b8a6", "AREA", "%", True),
        ("cpu_stats",  "stats & router polls", "f59e0b", "AREA", "%", True),
        ("cpu_other",  "UI / API & other",   "ef4444", "AREA", "%", True),
    ])
    graph("app-mem", "Memory by service", "MB", "app", [
        ("mem_updatecsv",   "updatecsv",   "3b82f6", "LINE1", " MB", False),
        ("mem_wan_service", "wan_service", "22c55e", "LINE1", " MB", False),
        ("mem_gui",         "gui",         "f59e0b", "LINE1", " MB", False),
    ])
    graph("host", "This host", "%", "host", [
        ("cpu",   "CPU %",        "3b82f6", "LINE1", "%", False),
        ("mem",   "memory %",     "22c55e", "LINE1", "%", False),
        ("disk",  "disk %",       "f59e0b", "LINE1", "%", False),
        ("load1", "load (1 min)", "8b5cf6", "LINE1", "",  False),
    ])
    graph("polling", "Polling time and errors", "s / errors", "polling", [
        ("scan_s",  "router scans",   "3b82f6", "LINE1", " s", False),
        ("build_s", "build topology", "8b5cf6", "LINE1", " s", False),
        ("apply_s", "LibreQoS apply", "22c55e", "LINE1", " s", False),
        ("wan_s",   "WAN sync",       "14b8a6", "LINE1", " s", False),
        ("errors",  "job errors",     "ef4444", "AREA",  "",   False),
    ])
    graph("devices", "Shaped devices by source", "devices", "devices", [
        ("pppoe",        "PPPoE",        "3b82f6", "AREA", "", False),
        ("hotspot",      "Hotspot",      "22c55e", "AREA", "", True),
        ("dhcp",         "DHCP / IPoE",  "f59e0b", "AREA", "", True),
        ("address_list", "Address list", "8b5cf6", "AREA", "", True),
        ("static",       "manual",       "8b949e", "AREA", "", True),
    ])
    for kind, router in _router_targets(cfg):
        name = router.get("name") or router.get("address", "")
        key = _router_key(kind, name)
        graph(key, f"{'Router' if kind == 'bras' else 'Core'} {name}", "% / C / V / ms", key, [
            ("cpu",    "CPU %",         "3b82f6", "LINE1", "%",   False),
            ("mem",    "memory %",      "22c55e", "LINE1", "%",   False),
            ("temp",   "temperature C", "ef4444", "LINE1", " C",  False),
            ("volt",   "voltage V",     "f59e0b", "LINE1", " V",  False),
            ("api_ms", "API latency",   "8b5cf6", "LINE1", " ms", False),
        ])
    return defs


@app.route("/api/graphs")
@require_auth
def graphs_list():
    defs = _graph_defs(_load_config())
    return jsonify({
        "ok": True,
        "rrdtool": bool(RRDTOOL),
        "ranges": GRAPH_RANGES,
        "graphs": [{"id": gid, "title": d["title"], "ready": _rrd_path(d["rrd"]).exists()}
                   for gid, d in defs.items()],
    })


@app.route("/api/graphs/<gid>.png")
@require_auth
def graph_png(gid):
    rng = request.args.get("range", "3h")
    if rng not in GRAPH_RANGES:
        return jsonify({"ok": False, "error": "unknown range"}), 400
    if not RRDTOOL:
        return jsonify({"ok": False, "error": "rrdtool is not installed (apt install rrdtool)"}), 503
    d = _graph_defs(_load_config()).get(gid)
    if not d or not _rrd_path(d["rrd"]).exists():
        return jsonify({"ok": False, "error": "no data yet"}), 404

    ttl = 30 if rng in ("3h", "30h") else 300
    cached = _graph_cache.get((gid, rng))
    if cached and time.time() - cached[0] < ttl:
        png = cached[1]
    else:
        r = subprocess.run(
            [RRDTOOL, "graph", "-", "--imgformat", "PNG", "--start", f"end-{rng}", "--end", "now",
             "--title", f"{d['title']} - last {GRAPH_RANGES[rng]}", "--vertical-label", d["vlabel"]]
            + _GRAPH_STYLE + d["args"],
            capture_output=True, timeout=20,
        )
        if r.returncode != 0:
            return jsonify({"ok": False, "error": r.stderr.decode(errors="replace").strip()}), 500
        png = r.stdout
        _graph_cache[(gid, rng)] = (time.time(), png)
    return Response(png, mimetype="image/png", headers={"Cache-Control": "no-store"})


_lqos_state = {"at": 0, "value": {}}


def _libreqos_services():
    """{lqosd, lqos_scheduler: active|inactive|…}, cached for 15 s."""
    if time.time() - _lqos_state["at"] > 15:
        try:
            r = _systemctl("is-active", "lqosd.service", "lqos_scheduler.service",
                           capture_output=True, text=True, timeout=5)
            states = r.stdout.split()
        except Exception:
            states = []
        _lqos_state["value"] = dict(zip(("lqosd", "lqos_scheduler"), states + ["unknown"] * 2))
        _lqos_state["at"] = time.time()
    return _lqos_state["value"]


@app.route("/api/monitor")
@require_auth
def monitor_status():
    """Everything the status strip and the Pollers page show."""
    cfg = _load_config()
    now = time.time()
    with _mon_lock:
        host = dict(_mon["host"])
        app_stats = dict(_mon["app"])
        cached_routers = dict(_mon["routers"])
        cpu_now = dict(_mon["job_cpu_now"])

    if HAS_PSUTIL:
        vm, du = psutil.virtual_memory(), psutil.disk_usage("/")
        host.update(mem=vm.percent, mem_used=vm.total - vm.available, mem_total=vm.total,
                    disk=du.percent, disk_used=du.used, disk_total=du.total,
                    load=list(os.getloadavg()), cores=psutil.cpu_count(logical=True),
                    uptime_s=_uptime_seconds())
        if "cpu" in _live:
            host["cpu"] = _live["cpu"]
    host["hostname"] = socket.gethostname()

    routers = []
    for kind, router in _router_targets(cfg):
        name = router.get("name") or router.get("address", "")
        info = cached_routers.get(_router_key(kind, name)) or {
            "name": name, "kind": kind, "address": router.get("address", ""),
            "ok": None, "error": "", "at": 0}
        routers.append(info)

    wan_on = _wan_enabled(cfg)
    jobs = []
    try:
        con = _db_con()
        try:
            ensure_jobs_table(con)
            rows = con.execute("SELECT * FROM jobs ORDER BY service, kind, name").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        rows = []
    for r in rows:
        if r["service"] == "wan_service" and not wan_on:
            continue
        job = dict(r)
        every = job["every"] or 0
        if every and now - job["last_run"] > every * 2.5 + 60:
            job["status"] = "late"     # the service is stopped or stuck
        job["cpu_now"] = cpu_now.get(job["name"])
        jobs.append(job)

    return jsonify({
        "ok": True,
        "now": now,
        "rrdtool": bool(RRDTOOL),
        "app_started": _STARTED_AT,
        "host": host,
        "app": app_stats,
        "routers": routers,
        "jobs": jobs,
        "libreqos": _libreqos_services(),
        "wan_enabled": wan_on,
    })

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
    ip  = request.remote_addr or "?"
    now = time.time()
    with _login_lock:
        count, locked_until = _login_failures.get(ip, (0, 0))
        if locked_until > now:
            wait = int(locked_until - now) + 1
            return jsonify({"ok": False, "error": f"Too many attempts — try again in {wait}s"}), 429

    data = request.get_json(silent=True) or {}
    if _verify_password(str(data.get("password", ""))):
        with _login_lock:
            _login_failures.pop(ip, None)
        session.clear()
        session["authed"] = True
        return jsonify({"ok": True})

    with _login_lock:
        count += 1
        if count >= LOGIN_MAX_ATTEMPTS:
            _login_failures[ip] = (0, now + LOGIN_LOCKOUT_SECONDS)
        else:
            _login_failures[ip] = (count, 0)
    return jsonify({"ok": False, "error": "Invalid password"}), 401

@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})

@app.route("/api/auth/password", methods=["POST"])
@require_auth
def change_password():
    global _auth
    data = request.get_json(silent=True) or {}
    current  = str(data.get("current", ""))
    new_pass = str(data.get("new", ""))
    if not _verify_password(current):
        return jsonify({"ok": False, "error": "Current password is incorrect"}), 400
    if len(new_pass) < 6:
        return jsonify({"ok": False, "error": "Password must be at least 6 characters"}), 400
    salt = secrets.token_hex(16)
    _auth["salt"]          = salt
    _auth["password_hash"] = _hash_password(new_pass, salt)
    _save_auth(_auth)
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


def _config_error(content):
    """Return why content is not a usable config.json, or None."""
    if not isinstance(content, dict):
        return "config must be a JSON object"
    bras = content.get("bras", [])
    if not isinstance(bras, list):
        return "'bras' must be a list"
    for i, r in enumerate(bras):
        if not isinstance(r, dict) or not str(r.get("name", "")).strip() or not str(r.get("address", "")).strip():
            return f"BRAS #{i + 1} needs a name and an address"
    if not isinstance(content.get("cores", []), list):
        return "'cores' must be a list"
    return None


@app.route("/api/config", methods=["POST"])
@require_auth
def save_config():
    try:
        data = request.get_json(silent=True) or {}
        content = data.get("content")
        err = _config_error(content)
        if err:
            return jsonify({"ok": False, "error": err}), 400
        atomic_write_text(CONFIG_PATH, json.dumps(content, indent=4))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/settings", methods=["GET"])
@require_auth
def get_settings():
    try:
        content = json.loads(SETTINGS_PATH.read_text()) if SETTINGS_PATH.exists() else {}
        return jsonify({"ok": True, "content": content})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/settings", methods=["POST"])
@require_auth
def save_settings():
    try:
        data = request.get_json(silent=True) or {}
        content = data.get("content")
        if not isinstance(content, dict):
            return jsonify({"ok": False, "error": "settings must be a JSON object"}), 400
        atomic_write_text(SETTINGS_PATH, json.dumps(content, indent=4))
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
        data    = request.get_json(silent=True) or {}
        content = data.get("content", "")
        apply   = data.get("apply_netplan", False)

        if str(path).endswith(".json"):
            try:
                parsed = json.loads(content)
            except ValueError as je:
                return jsonify({"ok": False, "error": f"Invalid JSON: {je}"}), 400
            if name == "config_json":
                err = _config_error(parsed)
                if err:
                    return jsonify({"ok": False, "error": err}), 400

        if Path(str(path)).parent == NETPLAN_DIR:
            # Netplan files are root-owned and must not be world-readable.
            if os.geteuid() == 0:
                atomic_write_text(path, content, mode=0o600)
            else:
                with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as tmp:
                    tmp.write(content)
                    tmp_path = tmp.name
                try:
                    for cmd in (["/bin/mv", tmp_path, str(path)], ["/bin/chmod", "600", str(path)]):
                        r = subprocess.run(_as_root(cmd), capture_output=True, text=True, timeout=10)
                        if r.returncode != 0:
                            return jsonify({"ok": False, "error": r.stderr.strip() or f"{cmd[0]} failed"}), 500
                except Exception as e:
                    return jsonify({"ok": False, "error": str(e)}), 500
        else:
            atomic_write_text(path, content)

        netplan_out = None
        if apply:
            try:
                result = subprocess.run(
                    _as_root([_which("netplan") or "/usr/sbin/netplan", "apply"]),
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
    # timeout: the scanner and wan_service write to the same DB
    con = sqlite3.connect(str(DB_PATH), timeout=30)
    con.row_factory = sqlite3.Row
    return con


def _device_rates(d: dict):
    """
    Validate GUI-entered max rates and derive min rates the same way the
    scanner does. Returns (dl_max, ul_max, dl_min, ul_min) or raises ValueError.
    """
    try:
        dl = int(float(d.get("download_max_mbps", 100)))
        ul = int(float(d.get("upload_max_mbps", 100)))
    except (TypeError, ValueError):
        raise ValueError("Download and upload rates must be numbers")
    floor = RateResolver.MAX_RATE_FLOOR
    if dl < floor or ul < floor:
        raise ValueError(f"Download and upload max must be at least {floor} Mbps")
    dl_min, ul_min = RateResolver.calculate_min_rates(dl, ul)
    return dl, ul, dl_min, ul_min


@app.route("/api/devices")
@require_auth
def get_devices():
    try:
        if not DB_PATH.exists():
            return jsonify({"ok": False, "error": "devices.db not found"}), 404
        con = _db_con()
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
        enabled = _wan_enabled(cfg)

        stats = {"enabled": enabled, "wans": []}
        if not enabled:
            return jsonify({"ok": True, **stats})

        if not DB_PATH.exists():
            return jsonify({"ok": True, **stats})

        con = _db_con()
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
                al = wan.get("address_list") or f"WAN{i}"
                limits[(core["name"], al)] = {
                    "dl_limit": wan.get("download_limit", 0),
                    "ul_limit": wan.get("upload_limit", 0),
                    "wan_label": wan.get("name", al),
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
    Sync core WAN address-lists via WANManager — the single source of truth
    for each router's address-list state.

    This used to be a second, independent implementation (raw per-IP,
    non-collapsed, no persistent cache) running alongside WANManager's own
    subnet-collapsed sync in wan_service.py. The two disagreed on address
    representation (e.g. "10.0.0.5" vs "10.0.0.5/32") and each treated the
    other's entries as untracked drift, so stale/duplicate entries could
    build up on the router without either side ever reliably cleaning them
    up. Delegating here keeps tracking (and cleanup) consistent regardless
    of whether a sync was triggered by the daemon or a manual GUI action.
    """
    cores = cfg.get("cores", []) or []
    return _wan_manager.sync_wan_address_lists(con, cores)


@app.route("/api/wan/rebalance", methods=["POST"])
@require_auth
def wan_rebalance():
    """Rebalance active devices across WANs with delta DB updates (no table wipe)."""
    try:
        cfg = _load_config()
        wan_cfg = cfg.get("wan_assignment", {})
        if not _wan_enabled(cfg):
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
        active_cutoff = time.time() - WAN_MANUAL_ACTIVE_CUTOFF

        con = _db_con()
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


@app.route("/api/wan/purge", methods=["POST"])
@require_auth
def wan_purge():
    """Purge all libreqos-managed WAN address-list entries from every core router, then rebalance."""
    cfg = _load_config()
    cores = cfg.get("cores", []) or []
    wan_cfg = cfg.get("wan_assignment", {})

    # ── Step 1: delete all libreqos-managed entries from each core ──────────
    # Delegated to WANManager so the purge goes through the same cache the
    # daemon and the sync above rely on — a raw, uncached delete here would
    # leave WANManager's in-memory cache pointing at now-deleted router IDs,
    # which could make later syncs think nothing needs to be re-added/removed.
    purge_results = _wan_manager.purge_wan_entries(cores)
    purge_errors = [f"{r['core']}: {r['error']}" for r in purge_results if r.get("error")]

    # ── Step 2: clear WAN assignments in DB ──────────────────────────────────
    try:
        con = _db_con()
        try:
            con.execute("UPDATE devices SET core_name='', wan_name=''")
            con.commit()
        finally:
            con.close()
    except Exception as ex:
        purge_errors.append(f"DB clear: {ex}")

    if purge_errors:
        return jsonify({
            "ok": False,
            "error": "Purge completed with errors",
            "purged": purge_results,
            "errors": purge_errors,
        }), 502

    # With WAN assignment off, purge only cleans up — nothing is reassigned.
    if not _wan_enabled(cfg):
        return jsonify({"ok": True, "purged": purge_results, "assigned": 0, "synced": None})

    # ── Step 3: rebalance ────────────────────────────────────────────────────
    try:
        excluded = []
        if not wan_cfg.get("include_hotspot", False):
            excluded.append("hotspot")
        if not wan_cfg.get("include_dhcp", False):
            excluded.append("dhcp")

        excl_sql = ""
        if excluded:
            placeholders = ",".join("?" * len(excluded))
            excl_sql = f"(source NOT IN ({placeholders}) OR source IS NULL) AND "

        active_cutoff = time.time() - WAN_MANUAL_ACTIVE_CUTOFF
        con = _db_con()
        try:
            active_codes = [r[0] for r in con.execute(
                "SELECT code FROM devices WHERE " + excl_sql + "(is_static = 1 OR last_seen >= ?)",
                excluded + [active_cutoff],
            ).fetchall()]

            _wan_manager.assign_wan_nodes(con, cores, wan_cfg)
            sync_summary = _sync_core_wan_address_lists(con, cfg)
        finally:
            con.close()

        return jsonify({
            "ok": True,
            "purged": purge_results,
            "assigned": len(active_codes),
            "synced": sync_summary,
        })
    except Exception as ex:
        return jsonify({
            "ok": False,
            "error": f"Purge succeeded but rebalance failed: {ex}",
            "purged": purge_results,
        }), 500


@app.route("/api/devices", methods=["POST"])
@require_auth
def add_device():
    try:
        d = request.get_json(silent=True) or {}
        try:
            dl, ul, dl_min, ul_min = _device_rates(d)
        except ValueError as ve:
            return jsonify({"ok": False, "error": str(ve)}), 400
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
        d = request.get_json(silent=True) or {}
        try:
            dl, ul, dl_min, ul_min = _device_rates(d)
        except ValueError as ve:
            return jsonify({"ok": False, "error": str(ve)}), 400
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
# App update (pull from GitHub, reinstall in place)
# ---------------------------------------------------------------------------
GITHUB_REPO_URL = "https://github.com/Kintoyyy/MikroTik-LibreQos-Integration"

# "Update App" replaces every top-level *.py file plus templates/ with the
# GitHub copy, so modules added later are picked up too. Anything holding
# live state or credentials — config.json, settings.json, gui_auth.json,
# devices.db, ShapedDevices.csv, network.json, lqos.conf, and the systemd
# unit files — is never touched, so an update can't silently roll back
# routers, WAN assignments, or the login password.
# Services are try-restarted: stopped or disabled ones (e.g. wan_service with
# WAN assignment off) stay that way. gui.service restarts itself, separately, last.
UPDATE_SERVICES = ["updatecsv.service", "wan_service.service"]


def _installed_version_path():
    return SCRIPT_DIR / ".installed_version"


def _current_version():
    marker = _installed_version_path()
    if marker.exists():
        try:
            return marker.read_text().strip() or "unknown"
        except Exception:
            pass
    return "unknown"


@app.route("/api/system/version")
@require_auth
def system_version():
    return jsonify({"ok": True, "version": _current_version(), "repo": GITHUB_REPO_URL})


@app.route("/api/system/update", methods=["POST"])
@require_auth
def update_app():
    """
    Pull the latest code from GitHub and reinstall it in place.

    Only the top-level *.py files plus templates/ are replaced. A copy
    of every file about to be overwritten is stashed in a timestamped backup
    folder next to gui.py first, so a bad update can be rolled back by hand.
    """
    git = _which("git")
    if not git:
        return jsonify({"ok": False, "error": "git is not installed on this host"}), 500

    tmp_dir = tempfile.mkdtemp(prefix="libreqos-update-")
    try:
        clone = subprocess.run(
            [git, "clone", "--depth", "1", GITHUB_REPO_URL, tmp_dir],
            capture_output=True, text=True, timeout=120,
        )
        if clone.returncode != 0:
            return jsonify({"ok": False, "error": f"git clone failed: {clone.stderr.strip()}"}), 500

        src_root = Path(tmp_dir)
        if not (src_root / "gui.py").exists():
            return jsonify({"ok": False, "error": "Downloaded repo is missing gui.py — aborting"}), 500

        backup_dir = SCRIPT_DIR / f".update_backup_{int(time.time())}"
        backup_dir.mkdir(parents=True, exist_ok=True)

        updated = []
        for src in sorted(src_root.glob("*.py")):
            name = src.name
            dest = SCRIPT_DIR / name
            if dest.exists():
                shutil.copy2(dest, backup_dir / name)
            shutil.copy2(src, dest)
            updated.append(name)

        templates_src = src_root / "templates"
        if templates_src.exists():
            templates_dest = SCRIPT_DIR / "templates"
            if templates_dest.exists():
                shutil.copytree(templates_dest, backup_dir / "templates", dirs_exist_ok=True)
            shutil.copytree(templates_src, templates_dest, dirs_exist_ok=True)
            updated.append("templates/")

        rev = subprocess.run(
            [git, "-C", tmp_dir, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        new_version = rev.stdout.strip() if rev.returncode == 0 else ""
        if new_version:
            _installed_version_path().write_text(new_version + "\n")

        restart_errors = []
        for svc in UPDATE_SERVICES:
            try:
                result = _systemctl("try-restart", svc, capture_output=True, text=True)
                if result.returncode != 0:
                    restart_errors.append(f"{svc}: {result.stderr.strip()}")
            except Exception as ex:
                restart_errors.append(f"{svc}: {ex}")

        # gui.service restarts itself — deferred a couple seconds on a
        # background thread so this response reaches the browser first,
        # instead of dying mid-request the way an inline restart would.
        def _restart_gui_later():
            time.sleep(2)
            try:
                _systemctl("restart", "gui.service", capture_output=True, text=True)
            except Exception:
                pass

        threading.Thread(target=_restart_gui_later, daemon=True).start()

        return jsonify({
            "ok": True,
            "version": new_version or "unknown",
            "updated_files": updated,
            "backup": str(backup_dir),
            "restart_errors": restart_errors,
            "message": "Updated — GUI service is restarting now, reload in a few seconds.",
        })
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "git operation timed out"}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Multi-service management
# ---------------------------------------------------------------------------
MANAGED_SERVICES = _SETTINGS_MANAGED_SERVICES

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
            "--property=ActiveState,SubState,MainPID,ExecMainStartTimestamp,LoadState,UnitFileState",
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
        "enabled":   props.get("UnitFileState", ""),
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
    # enable/disable also start/stop the unit and keep it that way across reboots
    commands = {
        "start":   ["start"],
        "stop":    ["stop"],
        "restart": ["restart"],
        "enable":  ["enable", "--now"],
        "disable": ["disable", "--now"],
    }
    if action not in commands:
        return jsonify({"ok": False, "error": "invalid action"}), 400
    try:
        result = _systemctl(*commands[action], _svc_name(name), capture_output=True, text=True)
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
            con = _db_con()
            con.execute("DELETE FROM devices")
            con.commit()
            con.close()
            results["devices_db"] = "cleared"
        else:
            results["devices_db"] = "not found"
    except Exception as e:
        errors.append(f"devices.db: {e}")

    # Clear ShapedDevices.csv (write header only)
    try:
        header = "Circuit ID,Circuit Name,Device ID,Device Name,Parent Node,MAC,IPv4,IPv6,Download Min Mbps,Upload Min Mbps,Download Max Mbps,Upload Max Mbps,Comment\n"
        atomic_write_text(YAML_FILES["shaped_devices"], header)
        results["ShapedDevices.csv"] = "cleared"
    except Exception as e:
        errors.append(f"ShapedDevices.csv: {e}")

    # Clear network.json (write empty object)
    try:
        atomic_write_text(YAML_FILES["network_json"], "{}\n")
        results["network.json"] = "cleared"
    except Exception as e:
        errors.append(f"network.json: {e}")

    return jsonify({"ok": not errors, "results": results, "errors": errors})


@app.route("/api/troubleshoot/bras")
@require_auth
def troubleshoot_routers():
    try:
        cfg = _load_config()
        bras_list = cfg.get("bras") or []
        cores_list = cfg.get("cores") or []
        bras = [
            {
                "key": f"bras:{idx}",
                "type": "bras",
                "index": idx,
                "name": r.get("name") or f"BRAS {idx + 1}",
                "address": r.get("address", ""),
                "port": int(r.get("port", 8728) or 8728),
            }
            for idx, r in enumerate(bras_list)
        ]
        cores = [
            {
                "key": f"core:{idx}",
                "type": "core",
                "index": idx,
                "name": r.get("name") or f"Core {idx + 1}",
                "address": r.get("address", ""),
                "port": int(r.get("port", 8728) or 8728),
            }
            for idx, r in enumerate(cores_list)
        ]
        return jsonify({"ok": True, "bras": bras, "cores": cores})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/troubleshoot/mt/ping", methods=["POST"])
@require_auth
def troubleshoot_mt_ping():
    try:
        data = request.get_json(silent=True) or {}
        key = data.get("router_key") or f"bras:{data.get('router_index', 0)}"
        router = _router_from_key(key)
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
        key = data.get("router_key") or f"bras:{data.get('router_index', 0)}"
        router = _router_from_key(key)
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
    """
    Compact CPU/RAM for every router, from the monitor's once-a-minute poll.
    Browsers never connect to the routers themselves.
    """
    cfg = _load_config()
    with _mon_lock:
        cached = dict(_mon["routers"])
    out = {"bras": [], "core": []}
    for kind, router in _router_targets(cfg):
        name = router.get("name") or router.get("address", "")
        info = cached.get(_router_key(kind, name)) or {}
        out[kind].append({
            "index": len(out[kind]),
            "router": name,
            "identity": info.get("identity", ""),
            "cpu_percent": info.get("cpu"),
            "ram_percent": round(info["mem"], 1) if info.get("mem") is not None else None,
            "uptime": info.get("uptime"),
            "ok": bool(info.get("ok")),
            "error": info.get("error") or ("" if info else "waiting for first poll"),
        })
    return jsonify({
        "ok": True,
        "count": len(out["bras"]) + len(out["core"]),
        "bras": out["bras"],
        "cores": out["core"],
    })


@app.route("/api/troubleshoot/mt/permissions", methods=["POST"])
@require_auth
def troubleshoot_mt_permissions():
    pool = None
    try:
        data = request.get_json(silent=True) or {}
        key = data.get("router_key") or f"bras:{data.get('router_index', 0)}"
        is_core = key.startswith("core:")
        router = _router_from_key(key)
        pool = _connect_router_api(router)
        api = pool.get_api()

        expected_group  = EXPECTED_CORE_GROUP  if is_core else EXPECTED_MT_GROUP
        expected_policy = EXPECTED_CORE_POLICY if is_core else EXPECTED_MT_POLICY
        expected_set    = EXPECTED_CORE_POLICY_SET if is_core else EXPECTED_MT_POLICY_SET

        configured_user = router.get("username", "")

        group_rows = api.get_resource("/user/group").get()
        user_rows = api.get_resource("/user").get()

        group = next((g for g in group_rows if g.get("name") == expected_group), None)
        user = next((u for u in user_rows if u.get("name") == configured_user), None)

        found_policy = (group or {}).get("policy", "")
        found_policy_set = {p.strip() for p in found_policy.split(",") if p.strip()}
        missing = sorted(expected_set - found_policy_set)
        # Extra deny items (starting with !) are acceptable — they only add more restrictions
        extra = sorted(
            i for i in (found_policy_set - expected_set)
            if not i.startswith("!")
        )

        group_ok = bool(group) and not missing and not extra
        user_group = (user or {}).get("group", "")
        user_disabled = _to_bool((user or {}).get("disabled", False))
        user_ok = bool(user) and user_group == expected_group and not user_disabled

        return jsonify({
            "ok": True,
            "permissions_ok": group_ok and user_ok,
            "group_ok": group_ok,
            "user_ok": user_ok,
            "group_found": bool(group),
            "user_found": bool(user),
            "user_group": user_group,
            "user_disabled": user_disabled,
            "expected_group": expected_group,
            "expected_user": configured_user,
            "expected_policy": expected_policy,
            "found_policy": found_policy,
            "missing_policy_items": missing,
            "extra_policy_items": sorted(found_policy_set - expected_set),
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
        updates = (request.get_json(silent=True) or {}).get("updates", {})
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
        atomic_write_text(path, text)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    start_monitoring()
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
