import os
import json
import sqlite3
import subprocess
import time
import threading
from pathlib import Path
from flask import Flask, render_template, request, jsonify, Response, stream_with_context

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Path resolution: prefer /opt/libreqos/src, fall back to script directory
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent.resolve()
OPT_DIR = Path("/opt/libreqos/src")

def find_file(name):
    """Return path to file, preferring the installed location."""
    opt_path = OPT_DIR / name
    local_path = SCRIPT_DIR / name
    if opt_path.exists():
        return opt_path
    return local_path

CONFIG_PATH = find_file("config.json")
DB_PATH     = find_file("devices.db")

YAML_FILES = {
    "libreqos":    find_file("libreqos.yaml"),
    "lqos":        find_file("lqos.conf"),
    "network":     find_file("50-cloud-init.yaml"),
    "config_json": find_file("config.json"),
    "network_json": find_file("network.json"),
    "updatecsv":   find_file("updatecsv.py"),
}

# ---------------------------------------------------------------------------
# SSE metrics broadcaster
# ---------------------------------------------------------------------------
_metric_listeners: list = []
_metric_lock = threading.Lock()

def _broadcast_loop():
    while True:
        if HAS_PSUTIL:
            cpu_per = psutil.cpu_percent(interval=1, percpu=True)
            cpu_avg = sum(cpu_per) / len(cpu_per)
            mem     = psutil.virtual_memory()
            payload = json.dumps({
                "cpu_per": cpu_per,
                "cpu_avg": round(cpu_avg, 1),
                "mem_used": mem.used,
                "mem_total": mem.total,
                "mem_percent": mem.percent,
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


@app.route("/api/metrics/stream")
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
def get_config():
    try:
        return jsonify({"ok": True, "path": str(CONFIG_PATH),
                        "content": json.loads(CONFIG_PATH.read_text())})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/config", methods=["POST"])
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
def save_yaml(name):
    path = YAML_FILES.get(name)
    if not path:
        return jsonify({"ok": False, "error": "unknown file"}), 404
    try:
        data    = request.get_json(force=True)
        content = data.get("content", "")
        apply   = data.get("apply_netplan", False)
        path.write_text(content)
        netplan_out = None
        if apply:
            try:
                subprocess.run(["chmod", "600", str(path)], check=True)
                result = subprocess.run(
                    ["netplan", "apply"],
                    capture_output=True, text=True, timeout=15
                )
                netplan_out = result.stdout.strip() or result.stderr.strip() or "applied"
            except Exception as ne:
                return jsonify({"ok": True, "netplan_error": str(ne)})
        return jsonify({"ok": True, "netplan_output": netplan_out})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/interfaces")
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


@app.route("/api/devices")
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
                   comment, source, router, last_seen, is_static, weight
            FROM devices ORDER BY last_seen DESC
        """)
        rows = [dict(r) for r in cur.fetchall()]
        con.close()
        return jsonify({"ok": True, "count": len(rows), "rows": rows})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/service/status")
def service_status():
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "updatecsv.service"],
            capture_output=True, text=True
        )
        active = result.stdout.strip()
        info = subprocess.run(
            ["systemctl", "show", "updatecsv.service",
             "--property=ActiveState,SubState,MainPID,ExecMainStartTimestamp"],
            capture_output=True, text=True
        )
        props = dict(line.split("=", 1) for line in info.stdout.strip().splitlines() if "=" in line)
        return jsonify({"ok": True, "active": active, **props})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/service/<action>", methods=["POST"])
def service_action(action):
    if action not in ("start", "stop", "restart"):
        return jsonify({"ok": False, "error": "invalid action"}), 400
    try:
        result = subprocess.run(
            ["systemctl", action, "updatecsv.service"],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            return jsonify({"ok": False, "error": result.stderr.strip()}), 500
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# Multi-service management
# ---------------------------------------------------------------------------
MANAGED_SERVICES = ["lqosd", "lqos_node_manager", "lqos_scheduler", "updatecsv", "gui"]

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
    info = subprocess.run(
        ["systemctl", "show", svc,
         "--property=ActiveState,SubState,MainPID,ExecMainStartTimestamp,LoadState"],
        capture_output=True, text=True
    )
    props = {}
    for line in info.stdout.strip().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            props[k] = v
    return {
        "name":      name,
        "active":    props.get("ActiveState", "unknown"),
        "sub":       props.get("SubState",    "unknown"),
        "pid":       props.get("MainPID",     "0"),
        "started":   props.get("ExecMainStartTimestamp", ""),
        "loaded":    props.get("LoadState",   "not-found"),
    }

@app.route("/api/services")
def get_services():
    try:
        result = [_get_service_info(n) for n in MANAGED_SERVICES]
        return jsonify({"ok": True, "services": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/services/<name>/<action>", methods=["POST"])
def manage_service(name, action):
    if name not in MANAGED_SERVICES:
        return jsonify({"ok": False, "error": "unknown service"}), 400
    if action not in ("start", "stop", "restart"):
        return jsonify({"ok": False, "error": "invalid action"}), 400
    try:
        result = subprocess.run(
            ["systemctl", action, _svc_name(name)],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            return jsonify({"ok": False, "error": result.stderr.strip()}), 500
        time.sleep(1)
        return jsonify({"ok": True, "service": _get_service_info(name)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/services/<name>/logs")
def service_logs(name):
    if name not in MANAGED_SERVICES:
        return jsonify({"ok": False, "error": "unknown service"}), 400
    try:
        result = subprocess.run(
            ["journalctl", "-u", _svc_name(name), "-n", "80",
             "--no-pager", "--output=short-iso"],
            capture_output=True, text=True
        )
        return jsonify({"ok": True, "logs": result.stdout})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/lqusers/status")
def lqusers_status():
    for p in LQUSERS_PATHS:
        if p.exists():
            return jsonify({"ok": True, "exists": True, "path": str(p)})
    return jsonify({"ok": True, "exists": False, "path": None})

@app.route("/api/lqusers/reset", methods=["POST"])
def lqusers_reset():
    try:
        removed = None
        for p in LQUSERS_PATHS:
            if p.exists():
                p.unlink()
                removed = str(p)
                break
        # restart lqosd after removal
        subprocess.run(["systemctl", "restart", "lqosd.service"],
                       capture_output=True, text=True)
        return jsonify({"ok": True, "removed": removed})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
