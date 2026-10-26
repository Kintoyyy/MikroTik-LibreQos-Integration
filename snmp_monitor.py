"""
MikroTik health, active PPP sessions and interface counters over SNMP v2c,
so the once-a-minute router poll does not use the RouterOS API.

  - health: sysName / sysDescr, HOST-RESOURCES CPU, memory and uptime,
    MIKROTIK-MIB temperature and voltage (RouterOS 6 scalars, or the
    RouterOS 7 gauge table) and version;
  - active PPP sessions: every session is a dynamic "<pppoe-…>" (or
    <l2tp-…>, <sstp-…>, …) interface, so sessions = ifNumber minus the
    router's own interfaces, counted from an interface-name walk that is
    cached for an hour;
  - interface counters: IF-MIB 64-bit octets, errors and discards.
"""
import re
import time

import snmp

SYS = {
    "descr":    "1.3.6.1.2.1.1.1.0",
    "uptime":   "1.3.6.1.2.1.1.3.0",                  # sysUpTime (fallback)
    "name":     "1.3.6.1.2.1.1.5.0",
    "if_count": "1.3.6.1.2.1.2.1.0",                  # ifNumber
    "hr_uptime": "1.3.6.1.2.1.25.1.1.0",              # hrSystemUptime
    "version":  "1.3.6.1.4.1.14988.1.1.4.4.0",        # MIKROTIK-MIB mtxrLicVersion
    "voltage":  "1.3.6.1.4.1.14988.1.1.3.8.0",        # mtxrHlVoltage, dV (RouterOS 6)
    "temp":     "1.3.6.1.4.1.14988.1.1.3.10.0",       # mtxrHlTemperature, dC (RouterOS 6)
    "cpu_temp": "1.3.6.1.4.1.14988.1.1.3.11.0",       # mtxrHlProcessorTemperature, dC
}
HR_PROCESSOR_LOAD = "1.3.6.1.2.1.25.3.3.1.2"
HR_STORAGE_DESCR = "1.3.6.1.2.1.25.2.3.1.3"
HR_STORAGE_UNITS, HR_STORAGE_SIZE, HR_STORAGE_USED = (
    "1.3.6.1.2.1.25.2.3.1.4", "1.3.6.1.2.1.25.2.3.1.5", "1.3.6.1.2.1.25.2.3.1.6")
GAUGE_NAME, GAUGE_VALUE, GAUGE_UNIT = (        # MIKROTIK-MIB mtxrGaugeTable (RouterOS 7)
    "1.3.6.1.4.1.14988.1.1.3.100.1.2", "1.3.6.1.4.1.14988.1.1.3.100.1.3", "1.3.6.1.4.1.14988.1.1.3.100.1.4")
GAUGE_CELSIUS, GAUGE_DECIVOLT = 1, 3
TEMP_GAUGES = ("temperature", "cpu-temperature", "board-temperature1", "board-temperature", "switch-temperature")
VOLT_GAUGES = ("voltage", "psu1-voltage", "psu-voltage", "psu2-voltage")
IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"
IF_TYPE, IF_OPER = "1.3.6.1.2.1.2.2.1.3", "1.3.6.1.2.1.2.2.1.8"
IF_COUNTERS = {                                    # field → (64-bit OID, 32-bit fallback or None)
    "rx_bytes":  ("1.3.6.1.2.1.31.1.1.1.6",  "1.3.6.1.2.1.2.2.1.10"),
    "tx_bytes":  ("1.3.6.1.2.1.31.1.1.1.10", "1.3.6.1.2.1.2.2.1.16"),
    "rx_errors": ("1.3.6.1.2.1.2.2.1.14", None),
    "tx_errors": ("1.3.6.1.2.1.2.2.1.20", None),
    "rx_drops":  ("1.3.6.1.2.1.2.2.1.13", None),
    "tx_drops":  ("1.3.6.1.2.1.2.2.1.19", None),
}
ETHERNET_CSMACD, OPER_UP = 6, 1
PHYSICAL = re.compile(r"^(ether|sfp|qsfp|combo)")
MAP_TTL = 3600            # seconds an interface-name walk is trusted


def settings(router):
    """The router's SNMP settings when it is polled over SNMP, else None."""
    s = router.get("snmp") or {}
    if not isinstance(s, dict) or not s.get("enabled"):
        return None
    try:
        port = int(s.get("port") or 161)
    except (TypeError, ValueError):
        port = 161
    return {"community": str(s.get("community") or "public"), "port": port}


class RouterCache:
    """What one router's polls keep: its interface-name → ifIndex map and memory row."""
    def __init__(self):
        self.ifindex = {}         # interface name → ifIndex
        self.static = None        # interfaces that are not PPP sessions (names not starting with "<")
        self.mapped_at = 0.0
        self.storage = None       # hrStorage row of main memory

    def stale(self):
        return not self.ifindex or time.time() - self.mapped_at > MAP_TTL


def format_uptime(ticks):
    """TimeTicks (1/100 s) as RouterOS shows uptime, e.g. 3w2d04:11:09."""
    if ticks is None:
        return ""
    s = int(ticks) // 100
    w, s = divmod(s, 7 * 86400)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return (f"{w}w" if w else "") + (f"{d}d" if d or w else "") + f"{h:02d}:{m:02d}:{s:02d}"


def _index(oid, base):
    return int(oid[len(base) + 1:].split(".")[0])


def _map_interfaces(client, cache):
    """Walk ifName: name → ifIndex, and how many interfaces are not PPP sessions."""
    names = {value: _index(oid, IF_NAME) for oid, value in client.walk(IF_NAME, 100) if value}
    cache.ifindex = names
    cache.static = sum(1 for n in names if not n.startswith("<"))
    cache.mapped_at = time.time()


def _memory(client, cache):
    if cache.storage is None:
        rows = client.walk(HR_STORAGE_DESCR)
        main = [_index(o, HR_STORAGE_DESCR) for o, v in rows if str(v).lower() in ("main memory", "physical memory")]
        cache.storage = main[0] if main else 65536          # RouterOS: 65536 = main memory
    i = cache.storage
    v = client.get([f"{HR_STORAGE_UNITS}.{i}", f"{HR_STORAGE_SIZE}.{i}", f"{HR_STORAGE_USED}.{i}"])
    size, used = v[f"{HR_STORAGE_SIZE}.{i}"], v[f"{HR_STORAGE_USED}.{i}"]
    return used / size * 100 if size and used is not None else None


def _gauges(client):
    """(temperature °C, voltage V) from the RouterOS 7 gauge table."""
    names = {_index(o, GAUGE_NAME): v for o, v in client.walk(GAUGE_NAME)}
    if not names:
        return None, None
    values = {_index(o, GAUGE_VALUE): v for o, v in client.walk(GAUGE_VALUE)}
    units = {_index(o, GAUGE_UNIT): v for o, v in client.walk(GAUGE_UNIT)}

    def pick(wanted, unit, scale):
        for want in wanted:
            for i, name in names.items():
                if name == want and units.get(i) == unit and values.get(i) is not None:
                    return values[i] / scale
        return None
    return pick(TEMP_GAUGES, GAUGE_CELSIUS, 1), pick(VOLT_GAUGES, GAUGE_DECIVOLT, 10)


def _graphed(client, cache, chosen, limit):
    """Interfaces to read: the chosen ones, else the running ethernet ports."""
    if chosen:
        return chosen[:limit]
    candidates = sorted((n for n in cache.ifindex if PHYSICAL.match(n)), key=lambda n: cache.ifindex[n])
    if not candidates:
        return []
    oids = [f"{base}.{cache.ifindex[n]}" for n in candidates for base in (IF_TYPE, IF_OPER)]
    v = client.get(oids)
    return [n for n in candidates
            if v[f"{IF_TYPE}.{cache.ifindex[n]}"] == ETHERNET_CSMACD
            and v[f"{IF_OPER}.{cache.ifindex[n]}"] == OPER_UP][:limit]


def _interfaces(client, cache, names):
    """{name: counters} of the given interfaces; a moved ifIndex re-walks the names once."""
    out, missing = {}, []
    for attempt in (1, 2):
        wanted = [n for n in names if n in cache.ifindex and n not in out]
        oids = []
        for n in wanted:
            i = cache.ifindex[n]
            oids += [f"{IF_NAME}.{i}", f"{IF_OPER}.{i}"]
            for hc, low in IF_COUNTERS.values():
                oids += [f"{hc}.{i}"] + ([f"{low}.{i}"] if low else [])
        v = client.get(oids) if oids else {}
        moved = False
        for n in wanted:
            i = cache.ifindex[n]
            if v.get(f"{IF_NAME}.{i}") != n:
                moved = True                      # ifIndex now belongs to another interface
                continue
            counters = {}
            for field, (hc, low) in IF_COUNTERS.items():
                value = v.get(f"{hc}.{i}")
                counters[field] = value if value is not None else (v.get(f"{low}.{i}") if low else None)
            counters["running"] = v.get(f"{IF_OPER}.{i}") == OPER_UP
            out[n] = counters
        missing = [n for n in names if n not in cache.ifindex]
        if attempt == 1 and (moved or missing):
            _map_interfaces(client, cache)
            continue
        break
    return out


def poll(host, port, community, kind, chosen_interfaces, cache, limit=8, timeout=2.0):
    """
    One SNMP poll of a router. Returns the same fields as the API poll:
    {ok, error, cpu, mem, temp, volt, uptime, identity, version, board,
    api_ms (poll latency), ppp and ifaces (BRAS only)}.
    """
    t0 = time.monotonic()
    info = {"ok": False, "error": "", "via": "snmp"}
    try:
        with snmp.Client(host, community, port, timeout=timeout) as client:
            v = client.get(list(SYS.values()))
            s = {k: v[oid] for k, oid in SYS.items()}
            if s["name"] is None and s["descr"] is None:
                raise snmp.SnmpError("no system information (is this a MikroTik with SNMP enabled?)")
            loads = [value for _, value in client.walk(HR_PROCESSOR_LOAD) if isinstance(value, int)]
            temp = s["temp"] / 10 if s["temp"] is not None else (s["cpu_temp"] / 10 if s["cpu_temp"] is not None else None)
            volt = s["voltage"] / 10 if s["voltage"] is not None else None
            if temp is None and volt is None:
                temp, volt = _gauges(client)
            descr = str(s["descr"] or "")
            info.update(
                ok=True, cpu=sum(loads) / len(loads) if loads else None, mem=_memory(client, cache),
                temp=temp, volt=volt, identity=s["name"] or "", version=s["version"] or "",
                board=re.sub(r"^RouterOS\s*", "", descr), uptime=format_uptime(s["hr_uptime"] or s["uptime"]),
            )
            if kind == "bras":
                if cache.stale() or (s["if_count"] is not None and s["if_count"] < (cache.static or 0)):
                    _map_interfaces(client, cache)
                if s["if_count"] is not None and cache.static is not None:
                    info["ppp"] = max(0, s["if_count"] - cache.static)
                info["ifaces"] = _interfaces(client, cache, _graphed(client, cache, chosen_interfaces, limit))
    except snmp.SnmpError as e:
        info["error"] = f"SNMP: {e}"
    except OSError as e:
        info["error"] = f"SNMP: {e}"
    info["api_ms"] = (time.monotonic() - t0) * 1000
    return info
