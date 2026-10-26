"""
Hardware check against LibreQoS v2.0's system requirements:
https://libreqos.readthedocs.io/en/latest/docs/v2.0/requirements.html

gather() reads this host — os-release, /proc, sysfs, lspci, ethtool and
systemd-detect-virt — and evaluate() compares what it found with the
requirements. The result is sections of checks, each pass / warn / fail /
info, with what was found, what LibreQoS asks for, and what to do.
"""
import hashlib
import json
import math
import os
import platform
import random
import re
import shutil
import subprocess
import sys
import time
import zlib
from pathlib import Path

import cpu_planner

DOCS = "https://libreqos.readthedocs.io/en/latest/docs/v2.0/requirements.html"

# Single-thread score (cpubenchmark.net) → top-level node max (Gbps), customer plan max (Mbps)
SINGLE_THREAD = [(1000, 1, 100), (1500, 2, 500), (2000, 3, 1000), (3000, 4, 2000), (4000, 5, 3000)]
# Approximate aggregate capacity (Gbps) by CPU cores, for single-thread scores 1000 / 2000 / 3000 / 4000
AGGREGATE_SCORES = (1000, 2000, 3000, 4000)
AGGREGATE = {2: (1, 3, 5, 7), 4: (3, 5, 9, 13), 6: (4, 8, 14, 20), 8: (5, 10, 18, 27), 16: (10, 21, 36, 54),
             32: (21, 42, 72, 108), 64: (42, 84, 144, 216), 128: (84, 168, 288, None)}
# RAM (GB, using CAKE) → most subscribers
RAM_SUBSCRIBERS = [(8, 1000), (16, 2500), (32, 5000), (64, 10000), (128, 20000)]
# Fast sizing guide: (name, up to subscribers, up to Gbps, cores (min, max), RAM GB (min, max), NIC)
PROFILES = [
    ("Small", 1000, 1, (2, None), (8, None), "a supported 10G-class NIC"),
    ("Medium", 5000, 10, (6, 16), (32, 64), "a supported 10/25G NIC"),
    ("Large", 20000, 50, (16, 64), (64, 128), "a supported 25/50/100G NIC"),
    ("High-throughput", None, None, (16, None), (128, None), "validated NIC / XDP support, high single-thread score"),
]
MIN_DISK_GB = 50
# Officially supported shaping NICs: (words of the device name, controller, port speeds, note)
INTEL_NOTE = "Intel often locks SFP+ module compatibility: check the modules before buying."
SUPPORTED_NICS = [
    (("XXV710",), "Intel XXV710", "10 / 25 Gbps", INTEL_NOTE),
    (("XL710",), "Intel XL710", "10 / 40 Gbps", INTEL_NOTE),
    (("X710",), "Intel X710", "10 Gbps", INTEL_NOTE),
    (("X520",), "Intel X520", "10 Gbps", INTEL_NOTE),
    (("82599",), "Intel X520", "10 Gbps", INTEL_NOTE),
    (("ConnectX-4 Lx",), "Mellanox ConnectX-4 Lx", "10/25/40/50 Gbps", ""),
    (("ConnectX-6",), "Mellanox ConnectX-6", "10/25/100 Gbps", ""),
    (("ConnectX-7",), "Mellanox ConnectX-7", "200 Gbps", ""),
]
# Device names when lspci is missing: (vendor, device) PCI ids
PCI_NAMES = {
    ("0x8086", "0x10fb"): "Intel 82599ES 10-Gigabit SFI/SFP+ (X520)",
    ("0x8086", "0x1572"): "Intel Ethernet Controller X710 for 10GbE SFP+",
    ("0x8086", "0x15ff"): "Intel Ethernet Controller X710 for 10GBASE-T",
    ("0x8086", "0x1589"): "Intel Ethernet Controller X710/X557-AT 10GBASE-T",
    ("0x8086", "0x1583"): "Intel Ethernet Controller XL710 for 40GbE QSFP+",
    ("0x8086", "0x1584"): "Intel Ethernet Controller XL710 for 40GbE QSFP+",
    ("0x8086", "0x158b"): "Intel Ethernet Controller XXV710 for 25GbE SFP28",
    ("0x8086", "0x1592"): "Intel Ethernet Controller E810-C for QSFP",
    ("0x8086", "0x1593"): "Intel Ethernet Controller E810-C for SFP",
    ("0x15b3", "0x1015"): "Mellanox MT27710 Family [ConnectX-4 Lx]",
    ("0x15b3", "0x1017"): "Mellanox MT27800 Family [ConnectX-5]",
    ("0x15b3", "0x1019"): "Mellanox MT28800 Family [ConnectX-5 Ex]",
    ("0x15b3", "0x101b"): "Mellanox MT28908 Family [ConnectX-6]",
    ("0x15b3", "0x101d"): "Mellanox MT2892 Family [ConnectX-6 Dx]",
    ("0x15b3", "0x101f"): "Mellanox MT2894 Family [ConnectX-6 Lx]",
    ("0x15b3", "0x1021"): "Mellanox MT2910 Family [ConnectX-7]",
}
# Drivers with native (driver-mode) XDP; anything else falls back to slow generic XDP
NATIVE_XDP = {"i40e", "ice", "ixgbe", "ixgbevf", "igb", "igc", "mlx4_en", "mlx5_core", "bnxt_en", "qede", "nfp",
              "sfc", "ena", "virtio_net", "hv_netvsc", "atlantic", "thunder_nicvf", "octeontx2", "tun", "veth"}
# Desktop / 1G drivers: no good for shaping
WEAK_DRIVERS = {"r8169", "r8125", "e1000", "e1000e", "tg3", "atl1c", "alx", "8139too"}
VIRTUAL_PREFIXES = ("lo", "veth", "docker", "virbr", "vnet", "tap", "tun", "br-", "bond", "dummy", "wg")


def _read(path, default=""):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def _run(cmd, timeout=10):
    """(returncode, stdout) of a read-only command; (127, '') when it is missing."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""


# ── Gathering ───────────────────────────────────────────────────────────────

def _os_release(etc):
    values = {}
    for line in _read(Path(etc) / "os-release").splitlines():
        key, _, value = line.partition("=")
        values[key] = value.strip().strip('"')
    return {"id": values.get("ID", ""), "version": values.get("VERSION_ID", ""),
            "pretty": values.get("PRETTY_NAME", "") or "unknown"}


def _cpuinfo(proc):
    model, flags, logical, cores = "", "", 0, set()
    physical = core = None
    for line in _read(Path(proc) / "cpuinfo").splitlines() + [""]:
        key, _, value = (s.strip() for s in line.partition(":"))
        if key == "processor":
            logical += 1
        elif key == "model name" and not model:
            model = value
        elif key == "flags" and not flags:
            flags = value
        elif key == "physical id":
            physical = value
        elif key == "core id":
            core = value
        elif not line.strip():
            if core is not None:
                cores.add((physical, core))
            physical = core = None
    return {"model": model or platform.processor() or "unknown", "logical": logical or os.cpu_count() or 0,
            "physical": len(cores) or None, "hypervisor_flag": "hypervisor" in flags.split()}


def _nic(sysfs, run, name):
    base = Path(sysfs) / "class" / "net" / name
    nic = {"name": name, "exists": base.exists(), "driver": "", "pci": "", "model": "", "tx_queues": 0,
           "max_queues": None, "speed": None, "state": "", "bond": False, "bond_slave": False}
    if not nic["exists"]:
        return nic
    dev = base / "device"
    try:
        nic["driver"] = os.path.basename(os.readlink(dev / "driver"))
    except OSError:
        pass
    if dev.exists():
        nic["pci"] = os.path.basename(os.path.realpath(dev))
    vendor, device = _read(dev / "vendor"), _read(dev / "device")
    if nic["pci"]:
        code, out = run(["lspci", "-s", nic["pci"], "-mm"])
        fields = re.findall(r'"([^"]*)"', out) if code == 0 else []
        if len(fields) >= 3:
            nic["model"] = f"{fields[1]} {fields[2]}"
    nic["model"] = nic["model"] or PCI_NAMES.get((vendor, device), f"PCI {vendor}:{device}" if vendor else "")
    try:
        nic["tx_queues"] = sum(1 for e in os.listdir(base / "queues") if e.startswith("tx-"))
    except OSError:
        pass
    code, out = run(["ethtool", "-l", name])
    if code == 0:
        m = re.search(r"maximums:.*?Combined:\s*(\d+)", out, re.S)
        if m:
            nic["max_queues"] = int(m.group(1))
    try:
        speed = int(_read(base / "speed", "-1"))
        nic["speed"] = speed if speed > 0 else None
    except ValueError:
        pass
    nic["state"] = _read(base / "operstate")
    nic["bond"] = (base / "bonding").exists()
    nic["bond_slave"] = (base / "bonding_slave").exists() or (
        (base / "master").exists() and (Path(os.path.realpath(base / "master")) / "bonding").exists())
    return nic


def _addresses():
    """{interface: [IPv4 addresses]} of this host."""
    try:
        import psutil
        import socket
        return {name: [a.address for a in addrs if a.family == socket.AF_INET]
                for name, addrs in psutil.net_if_addrs().items()}
    except Exception:
        return {}


def _cpu_temperature():
    try:
        import psutil
        temps = psutil.sensors_temperatures()
    except Exception:
        return None
    readings = [t.current for key in ("coretemp", "k10temp", "zenpower", "cpu_thermal") for t in temps.get(key, [])
                if t.current]
    return max(readings) if readings else None


def gather(conf, sysfs="/sys", proc="/proc", etc="/etc", run=None):
    """What this host has, as plain data for evaluate()."""
    run = run or _run
    code, out = run(["systemd-detect-virt"])
    virt = out.strip() or ("none" if code in (0, 1) else "")
    cpu = _cpuinfo(proc)
    if not virt and cpu["hypervisor_flag"]:
        virt = "vm"
    shaping, stick = cpu_planner.shaping_interfaces(conf)
    all_ifaces = sorted(os.listdir(Path(sysfs) / "class" / "net")) if (Path(sysfs) / "class" / "net").is_dir() else []
    physical = [n for n in all_ifaces if (Path(sysfs) / "class" / "net" / n / "device").exists()]
    mem_kb = 0
    m = re.search(r"MemTotal:\s+(\d+)", _read(Path(proc) / "meminfo"))
    if m:
        mem_kb = int(m.group(1))
    try:
        disk = shutil.disk_usage("/")
        disk = {"total": disk.total, "free": disk.free}
    except OSError:
        disk = None
    cpu_dir = Path(sysfs) / "devices" / "system" / "cpu" / "smt"
    return {
        "os": _os_release(etc), "kernel": platform.release(), "virt": virt or "unknown",
        "cpu": {**cpu, "smt_active": _read(cpu_dir / "active") or None, "smt_control": _read(cpu_dir / "control") or None,
                "temp_c": _cpu_temperature()},
        "queues": cpu_planner.detect_queues(conf, sysfs),
        "mem_bytes": mem_kb * 1024, "disk": disk,
        "shaping": [_nic(sysfs, run, n) for n in shaping], "stick": stick,
        "physical_ifaces": physical, "addresses": _addresses(),
        "exclude_efficiency_cores": bool(conf.get("exclude_efficiency_cores", True)),
    }


# ── Single-thread score estimate ────────────────────────────────────────────
# A ~3 s test of C-level work — compression, string sorting, 2048-bit modular
# exponentiation (as in RSA), float math, hashing — so the Python version
# hardly matters. It runs in its own process pinned to one idle performance
# core, best of BENCH_ROUNDS. The geometric mean of the rates × BENCH_SCALE
# approximates PassMark's single-thread rating (cpubenchmark.net), the
# number LibreQoS's tables use. Calibrated on an Intel Core Ultra 5 135U:
# PassMark single-thread ≈ 3,350 (an average over many machines); its runs
# here give a geometric mean of 100–150 (a laptop's boost budget), typically
# ~120 on CPython 3.12. Expect about ±20%; a rating typed in by hand wins.
BENCH_SCALE = 3350 / 120
BENCH_ROUNDS = 3
BENCH_SLICE = 0.07         # seconds per timing slice; three slices per workload per round
BENCH_STEADY = 0.15        # rounds further apart than this: the clock moved, the estimate is rough


def _bench_workloads():
    rng = random.Random(42)
    words = [''.join(rng.choice('abcdefghijklmnopqrstuvwxyz') for _ in range(rng.randint(3, 12))) for _ in range(4000)]
    text = ' '.join(rng.choice(words) for _ in range(40000)).encode()
    strings = [''.join(rng.choice('abcdefghijklmnopqrstuvwxyz0123456789') for _ in range(16)) for _ in range(60000)]
    floats = [rng.random() * 100 for _ in range(200000)]
    blob = rng.randbytes(1 << 20) if hasattr(rng, "randbytes") else bytes(rng.getrandbits(8) for _ in range(1 << 20))
    base, exp, mod = rng.getrandbits(2048) | 1, rng.getrandbits(2048), rng.getrandbits(2048) | (1 << 2047) | 1
    return {
        "compression": lambda: zlib.compress(text, 6),
        "sorting": lambda: sorted(strings),
        "integer": lambda: pow(base, exp, mod),
        "floating": lambda: math.fsum(map(math.sin, floats)),
        "hashing": lambda: hashlib.md5(blob).digest(),          # no CPU has MD5 instructions: plain integer work
    }


def _rate(fn, slice_seconds):
    best = 0.0
    for _ in range(3):
        n, t0 = 0, time.perf_counter()
        while True:
            fn()
            n += 1
            dt = time.perf_counter() - t0
            if dt >= slice_seconds:
                break
        best = max(best, n / dt)
    return best


def run_bench(cpu, rounds=BENCH_ROUNDS, slice_seconds=BENCH_SLICE):
    """The test itself, in this process: pinned to cpu, best geometric mean of `rounds`."""
    try:
        os.sched_setaffinity(0, {cpu})
    except (AttributeError, OSError):
        pass
    work = _bench_workloads()
    for fn in work.values():                      # warm up: caches, and the core's boost clock
        fn()
    best, rates, rounds_seen = 0.0, {}, []
    for _ in range(rounds):
        r = {name: _rate(fn, slice_seconds) for name, fn in work.items()}
        geo = math.exp(sum(math.log(v) for v in r.values()) / len(r))
        rounds_seen.append(geo)
        if geo > best:
            best, rates = geo, r
    return {"geomean": best, "rates": rates, "rounds": rounds_seen}


def _idlest_cpu(exclude):
    """(cpu, load %) of the least busy CPU this process may use, E-cores left out when possible."""
    try:
        allowed = sorted(os.sched_getaffinity(0))
    except AttributeError:
        allowed = list(range(os.cpu_count() or 1))
    candidates = [c for c in allowed if c not in exclude] or allowed
    try:
        import psutil
        loads = psutil.cpu_percent(interval=0.5, percpu=True)
    except Exception:
        loads = []
    cpu = min(candidates, key=lambda c: loads[c] if c < len(loads) else 0.0)
    return cpu, (loads[cpu] if cpu < len(loads) else None)


def estimate_single_thread(sysfs="/sys", rounds=BENCH_ROUNDS, slice_seconds=BENCH_SLICE, timeout=60):
    """
    Estimate this CPU's single-thread score: {score, cpu, load, geomean, rates, seconds, python}.
    Runs the test in a child process, so the GUI's threads neither slow it down nor are slowed by it.
    """
    ecores = set(cpu_planner._cpulist(_read(Path(sysfs) / "devices" / "cpu_atom" / "cpus")))
    cpu, load = _idlest_cpu(ecores)
    started = time.monotonic()
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "--bench", str(cpu), str(rounds), str(slice_seconds)],
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"the CPU test failed: {(r.stderr or r.stdout).strip()[-300:]}")
    result = json.loads(r.stdout)
    rounds_seen = result.get("rounds") or [result["geomean"]]
    # How much the core's speed moved between rounds: boost budgets, thermal limits, a busy core
    spread = (max(rounds_seen) - min(rounds_seen)) / max(rounds_seen) if max(rounds_seen) else 0.0
    return {"score": int(round(result["geomean"] * BENCH_SCALE, -1)), "cpu": cpu, "load": load,
            "geomean": round(result["geomean"], 2), "rates": {k: round(v, 2) for k, v in result["rates"].items()},
            "spread": round(spread, 3), "steady": spread <= BENCH_STEADY,
            "seconds": round(time.monotonic() - started, 1), "python": platform.python_version(), "at": time.time()}


# ── Evaluating ──────────────────────────────────────────────────────────────

def _check(status, title, found, needs="", fix="", detail=""):
    return {"status": status, "title": title, "found": found, "needs": needs, "fix": fix, "detail": detail}


def single_thread_limits(score):
    """(top-level node max Gbps, customer plan max Mbps) for a single-thread score, from LibreQoS's table."""
    row = None
    for s, node, plan in SINGLE_THREAD:
        if score >= s:
            row = (node, plan)
    return row


def aggregate_capacity(cores, score):
    """Approximate total Gbps for cores at score, interpolated in LibreQoS's table; None outside it."""
    if not cores or not score or score < AGGREGATE_SCORES[0]:
        return None
    sizes = sorted(AGGREGATE)
    cores = min(max(cores, sizes[0]), sizes[-1])

    def at(n):                                    # interpolate across scores for a core count in the table
        row = AGGREGATE[n]
        s = min(score, AGGREGATE_SCORES[-1])
        for i in range(len(AGGREGATE_SCORES) - 1):
            lo, hi = AGGREGATE_SCORES[i], AGGREGATE_SCORES[i + 1]
            if lo <= s <= hi and row[i] is not None and row[i + 1] is not None:
                return row[i] + (row[i + 1] - row[i]) * (s - lo) / (hi - lo)
        known = [v for v in row if v is not None]
        return known[-1]
    lower = max(n for n in sizes if n <= cores)
    upper = min(n for n in sizes if n >= cores)
    if lower == upper:
        return at(lower)
    return at(lower) + (at(upper) - at(lower)) * (cores - lower) / (upper - lower)


def ram_subscribers(ram_gb):
    """Most subscribers for this much RAM, interpolated in LibreQoS's table (0 below 8 GB's share)."""
    if ram_gb <= 0:
        return 0
    prev_gb, prev_n = 0, 0
    for gb, n in RAM_SUBSCRIBERS:
        if ram_gb <= gb:
            return int(prev_n + (n - prev_n) * (ram_gb - prev_gb) / (gb - prev_gb))
        prev_gb, prev_n = gb, n
    return int(RAM_SUBSCRIBERS[-1][1] * ram_gb / RAM_SUBSCRIBERS[-1][0])


def supported_nic(model):
    """(controller, speeds, note) when the device name is on LibreQoS's supported list, else None."""
    for words, controller, speeds, note in SUPPORTED_NICS:
        if any(w.lower() in (model or "").lower() for w in words):
            return controller, speeds, note
    return None


def profile_for(subscribers, link_gbps):
    for p in PROFILES[:-1]:
        if subscribers <= p[1] and (link_gbps or 0) <= p[2]:
            return p
    return PROFILES[-1]


def _fmt_gb(n):
    return f"{n / 1024 ** 3:.0f} GB" if n >= 1024 ** 3 else f"{n / 1024 ** 2:.0f} MB"


def evaluate(facts, subscribers=0, max_plan_mbps=0, link_mbps=0, score=None, score_source="manual", score_rough=False):
    """{sections: [{name, checks}], counts, profile} for the GUI."""
    q = facts["queues"]
    cpu = facts["cpu"]
    cores = cpu["physical"] or cpu["logical"]
    shaping_cores = q.get("shaping_cpus") or cores      # LibreQoS opens one queue per (logical) CPU it shapes on
    # Capacity and sizing count real cores: with Hyper-Threading on, two queues share one core
    capacity_cores = max(1, min(shaping_cores, cores - (q.get("e_cores") or 0) if cpu["physical"] else shaping_cores))
    ram = facts["mem_bytes"]
    link_gbps = (link_mbps or 0) / 1000
    sections = []

    # System
    os_info, virt = facts["os"], facts["virt"]
    checks = [
        _check("pass" if (os_info["id"], os_info["version"]) == ("ubuntu", "24.04") else "warn", "Operating system",
               os_info["pretty"], "Ubuntu Server 24.04",
               "" if (os_info["id"], os_info["version"]) == ("ubuntu", "24.04")
               else "LibreQoS v2.0 is supported on Ubuntu Server 24.04 only; others may work but are not supported."),
        _check("info", "Kernel", facts["kernel"]),
    ]
    if virt in ("none", ""):
        checks.append(_check("pass", "Bare metal or VM", "bare metal", "bare metal or a VM"))
    else:
        checks.append(_check(
            "warn", "Bare metal or VM", f"virtual machine ({virt})", "bare metal for best performance",
            "Expect about 30% less throughput than bare metal. Above 10 Gbps pass the NIC through to the VM (native "
            "XDP, not generic). Give the shaping interfaces at least 2 queues — on Proxmox enable Multiqueue equal to "
            "the VM's vCPU count.", "A VM needs about 2 vCPU / 8 GB RAM / 50 GB disk for 1 Gbps, 8 vCPU / 32 GB / 50 GB "
            "for 10 Gbps (vCPU as fast as one Xeon E-2456 core, Hyper-Threading off)."))
    sections.append({"name": "System", "icon": "bi-pc-display", "checks": checks})

    # CPU
    checks = [_check("info", "CPU model", cpu["model"])]
    checks.append(_check("pass" if cores >= 2 else "fail", "CPU cores",
                         f"{cores} core(s), {cpu['logical']} thread(s)", "at least 2 cores",
                         "" if cores >= 2 else "LibreQoS needs at least 2 cores: one queue per core."))
    smt = cpu.get("smt_active")
    if smt == "1":
        checks.append(_check("warn", "Hyper-Threading (SMT)", "on", "off",
                             "Turn Hyper-Threading / SMT off in the BIOS / UEFI: it can interfere with XDP processing."))
    elif smt == "0" or cpu.get("smt_control") in ("off", "forceoff", "notsupported"):
        checks.append(_check("pass", "Hyper-Threading (SMT)", "off" if smt == "0" else cpu.get("smt_control"), "off"))
    elif cpu["physical"] and cpu["logical"] > cpu["physical"]:
        checks.append(_check("warn", "Hyper-Threading (SMT)", f"likely on ({cpu['logical']} threads on {cpu['physical']} cores)",
                             "off", "Turn Hyper-Threading / SMT off in the BIOS / UEFI: it can interfere with XDP processing."))
    else:
        checks.append(_check("info", "Hyper-Threading (SMT)", "unknown", "off"))
    if q.get("e_cores"):
        excluded = facts.get("exclude_efficiency_cores", True)
        checks.append(_check("pass" if excluded else "warn", "Efficiency cores",
                             f"{q['e_cores']} E-core(s), {'excluded' if excluded else 'used'} by LibreQoS",
                             "excluded from shaping", "" if excluded else
                             "Set exclude_efficiency_cores = true in lqos.conf: a queue on a slow E-core limits its subscribers."))
    checks.append(_check("info", "Shaping queues", f"{q.get('queues')} (one per core LibreQoS shapes on)",
                         "", "", "; ".join(q.get("warnings") or [])))
    estimated = score_source == "estimated"
    shown = (f"≈{score:,} (estimated{', rough' if score_rough else ''})" if estimated
             else f"{score:,}" if score else "")
    how = ""
    if estimated:
        how = ("Estimated by a short CPU test on this host. The CPU's speed changed during it (boost or thermal limits, "
               "or a busy core), so this is rough." if score_rough else
               "Estimated by a short CPU test on this host (about ±20%).") + \
              " For an exact value, type the rating from cpubenchmark.net."
    if score:
        limits = single_thread_limits(score)
        if limits is None:
            checks.append(_check("fail", "Single-thread score", shown, "1000 or more",
                                 "Below LibreQoS's table: expect under 1 Gbps per top-level node and under 100 Mbps plans.",
                                 how))
        else:
            node_gbps, plan_mbps = limits
            status = "warn" if max_plan_mbps and max_plan_mbps > plan_mbps else "pass"
            checks.append(_check(
                status, "Single-thread score", f"{shown}: up to {node_gbps} Gbps per top-level node (one core), "
                f"plans up to {plan_mbps} Mbps", f"plans ≤ {plan_mbps} Mbps",
                f"Your largest plan is {max_plan_mbps:.0f} Mbps, above what one core of this CPU shapes safely. "
                f"A faster CPU (single-thread) or smaller top plans." if status == "warn" else "",
                " ".join(t for t in (how, f"With Auto CPU, set Settings → Topology → CPU capacity to "
                                          f"{node_gbps * 1000} Mbps so a node that would overload one core is split.") if t)))
    else:
        checks.append(_check("info", "Single-thread score", "not known", "",
                             "Run the CPU test (Detect), or type the CPU's single-thread rating from cpubenchmark.net, "
                             "to check plan and node limits and total capacity."))
    temp = cpu.get("temp_c")
    if temp is not None:
        checks.append(_check("warn" if temp >= 85 else "pass", "CPU temperature", f"{temp:.0f} °C", "under 85 °C",
                             "Check cooling: small boxes (e.g. MS-01) run up to 20 °C cooler with new thermal paste."
                             if temp >= 85 else ""))
    sections.append({"name": "CPU", "icon": "bi-cpu", "checks": checks})

    # Memory and disk
    ram_gb = ram / 1024 ** 3
    max_subs = ram_subscribers(ram_gb)
    if not ram:
        status = "info"
    elif subscribers > max_subs:
        status = "fail"
    elif ram_gb < 8 or subscribers > max_subs * 0.8:
        status = "warn"
    else:
        status = "pass"
    checks = [_check(status, "RAM for the subscriber count",
                     f"{_fmt_gb(ram)} for {subscribers:,} subscribers (enough for about {max_subs:,})",
                     "8 GB → 1,000 · 16 GB → 2,500 · 32 GB → 5,000 · 64 GB → 10,000 · 128 GB → 20,000 (CAKE)",
                     "" if status in ("pass", "info") else
                     f"Add RAM: {subscribers:,} subscribers need about "
                     f"{next((gb for gb, n in RAM_SUBSCRIBERS if n >= subscribers), 256)} GB.")]
    disk = facts.get("disk")
    if disk:
        total_gb = disk["total"] / 1024 ** 3
        checks.append(_check("pass" if total_gb >= MIN_DISK_GB else "warn", "Disk",
                             f"{total_gb:.0f} GB, {disk['free'] / 1024 ** 3:.0f} GB free", f"{MIN_DISK_GB} GB or more",
                             "" if total_gb >= MIN_DISK_GB else "Use a disk of 50 GB or more."))
        if disk["total"] and disk["free"] / disk["total"] < 0.1:
            checks.append(_check("warn", "Free disk space", f"{disk['free'] / disk['total'] * 100:.0f}% free",
                                 "10% or more", "Free space: old journal logs, apt caches, old kernels."))
    sections.append({"name": "Memory & disk", "icon": "bi-memory", "checks": checks})

    # Network
    checks = []
    nics = facts["shaping"]
    if not nics:
        checks.append(_check("fail", "Shaping interfaces", "none in lqos.conf", "two shaping interfaces on a dedicated NIC",
                             "Set the bridge (to_internet / to_network) on the LibreQoS tab."))
    for nic in nics:
        label = f"{nic['name']}"
        if not nic["exists"]:
            checks.append(_check("fail", f"{label}: interface", "not found on this host", "present",
                                 "lqos.conf names an interface this host does not have."))
            continue
        sup = supported_nic(nic["model"])
        if sup:
            checks.append(_check("pass", f"{label}: NIC", f"{nic['model']} ({nic['driver'] or 'driver ?'})",
                                 "a NIC on LibreQoS's supported list", "",
                                 f"Supported: {sup[0]}, {sup[1]}." + (f" {sup[2]}" if sup[2] else "")))
        elif nic["driver"] in WEAK_DRIVERS:
            checks.append(_check("fail", f"{label}: NIC", f"{nic['model'] or 'unknown'} ({nic['driver']})",
                                 "a NIC on LibreQoS's supported list",
                                 "A desktop / 1G-class NIC: use a supported Intel X520 / X710 / XXV710 / XL710 or "
                                 "Mellanox ConnectX-4 Lx / 6 / 7 for shaping."))
        else:
            checks.append(_check("warn", f"{label}: NIC", f"{nic['model'] or 'unknown model'} ({nic['driver'] or 'driver ?'})",
                                 "a NIC on LibreQoS's supported list",
                                 "Not on LibreQoS's supported list: it may work if it has as many TX/RX queues as cores "
                                 "and native XDP. Supported: Intel X520, X710, XXV710, XL710; Mellanox ConnectX-4 Lx, "
                                 "ConnectX-6, ConnectX-7."))
        if nic["driver"]:
            native = nic["driver"] in NATIVE_XDP
            checks.append(_check("pass" if native else "warn", f"{label}: XDP", "native (driver mode)" if native
                                 else f"generic only ({nic['driver']})", "native XDP for 10 Gbps and above",
                                 "" if native else "Generic XDP is much slower: use a NIC whose driver supports XDP."))
        queues = nic["max_queues"] or nic["tx_queues"]
        need = shaping_cores
        checks.append(_check("pass" if queues >= need else "warn", f"{label}: queues",
                             f"{queues} TX/RX queue(s)" + (f" (max {nic['max_queues']})" if nic["max_queues"] else ""),
                             f"at least {need} (one per shaping core)",
                             "" if queues >= need else f"LibreQoS can use only {queues} of {need} cores. Raise the queue "
                             f"count (ethtool -L {nic['name']} combined {need}), or use a NIC with more queues."))
        if nic["speed"]:
            checks.append(_check("info", f"{label}: link", f"{nic['speed'] / 1000:g} Gbps, {nic['state'] or '?'}"))
        if nic["state"] and nic["state"] not in ("up", "unknown"):
            checks.append(_check("warn", f"{label}: state", nic["state"], "up", "The interface is not up: check the cable / optic."))
        if nic["bond"] or nic["bond_slave"]:
            checks.append(_check("fail", f"{label}: bonding", "bonded", "not bonded",
                                 "Bonded interfaces are not supported (an XDP limitation): shape on plain interfaces."))
    if len(nics) == 2 and all(n["pci"] for n in nics):
        same_card = nics[0]["pci"].rsplit(".", 1)[0] == nics[1]["pci"].rsplit(".", 1)[0]
        checks.append(_check("pass" if same_card else "info", "Dedicated shaping NIC",
                             "both ports on one card" if same_card else "the two ports are on different cards / chips",
                             "a dedicated NIC with two shaping ports"))
    elif facts.get("stick"):
        checks.append(_check("info", "Shaping on a stick", "one interface for both directions", "",
                             "On a stick LibreQoS halves its queues: each core shapes one direction."))
    shaping_names = {n["name"] for n in nics}
    mgmt = [n for n, ips in (facts.get("addresses") or {}).items()
            if ips and n not in shaping_names and not n.startswith(VIRTUAL_PREFIXES)]
    checks.append(_check("pass" if mgmt else "warn", "Management interface",
                         ", ".join(f"{n} ({', '.join(facts['addresses'][n])})" for n in mgmt) or "none found",
                         "a separate interface with an address, usually the onboard port",
                         "" if mgmt else "Manage the box over its own interface, not the shaping ones."))
    sections.append({"name": "Network", "icon": "bi-ethernet", "checks": checks})

    # Capacity and sizing
    checks = []
    prof = profile_for(subscribers, link_gbps)
    name, _, _, (cmin, cmax), (rmin, rmax), nic_text = prof
    ok_cores = capacity_cores >= cmin
    ok_ram = ram_gb >= rmin * 0.95
    checks.append(_check("pass" if ok_cores and ok_ram else "warn", "Sizing profile",
                         f"{name}: {subscribers:,} subscribers" + (f", {link_gbps:g} Gbps link" if link_gbps else "")
                         + f" → this host has {capacity_cores} shaping core(s), {ram_gb:.0f} GB RAM",
                         f"{cmin}{'–' + str(cmax) if cmax else '+'} strong cores, {rmin}{'–' + str(rmax) if rmax else '+'} "
                         f"GB RAM, {nic_text}",
                         "" if ok_cores and ok_ram else
                         " ".join(t for t in (f"More cores (at least {cmin})." if not ok_cores else "",
                                              f"More RAM (at least {rmin} GB)." if not ok_ram else "") if t)))
    if score:
        cap = aggregate_capacity(capacity_cores, score)
        if cap is None:
            checks.append(_check("warn", "Total capacity", "below LibreQoS's table (score under 1000)", ""))
        else:
            status = "pass" if not link_gbps or cap >= link_gbps else "warn"
            checks.append(_check(status, "Total capacity", f"about {cap:.0f} Gbps on {capacity_cores} core(s) at score {shown}",
                                 f"≥ the {link_gbps:g} Gbps link in lqos.conf" if link_gbps else "",
                                 "" if status == "pass" else "The link is faster than this CPU can shape: a CPU with more "
                                 "cores or a higher single-thread score.",
                                 "Approximate, from LibreQoS's table of cores × single-thread score."))
    sections.append({"name": "Capacity & sizing", "icon": "bi-speedometer2", "checks": checks})

    counts = {s: 0 for s in ("pass", "warn", "fail", "info")}
    for sec in sections:
        for c in sec["checks"]:
            counts[c["status"]] += 1
    return {"sections": sections, "counts": counts, "profile": name, "docs": DOCS}


if __name__ == "__main__" and len(sys.argv) == 5 and sys.argv[1] == "--bench":
    # estimate_single_thread() runs the test here, in a process of its own
    print(json.dumps(run_bench(int(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4]))))
