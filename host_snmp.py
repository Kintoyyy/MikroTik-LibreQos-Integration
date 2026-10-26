"""
This host's CPU, memory, load, uptime and interface traffic over SNMP v2c
from its own snmpd (net-snmp), for the Overview.

  - CPU: UCD-SNMP-MIB raw tick counters (on Linux the same disjoint fields
    as /proc/stat: user, nice, system, idle, iowait, irq, softirq, steal),
    so busy % counts the softirq time LibreQoS's shaping runs in. Per core:
    HOST-RESOURCES hrProcessorLoad, which snmpd averages over a minute;
  - memory: UCD memTotalReal / memAvailReal / memBuffer / memCached (kB);
  - load average: UCD laLoadInt; uptime: hrSystemUptime;
  - interfaces: IF-MIB ifName and 64-bit octet counters.

snmpd refreshes some tables only every few seconds, so a rate is taken
between the last two readings that changed, and kept until the next change.
Whatever the agent does not expose (Ubuntu's default view shows only the
system group) is missing from the sample, and the caller fills it in.
"""
import time

import snmp

UCD = "1.3.6.1.4.1.2021"
CPU_RAW = {"user": f"{UCD}.11.50.0", "nice": f"{UCD}.11.51.0", "system": f"{UCD}.11.52.0",
           "idle": f"{UCD}.11.53.0", "wait": f"{UCD}.11.54.0", "interrupt": f"{UCD}.11.56.0",
           "softirq": f"{UCD}.11.61.0", "steal": f"{UCD}.11.64.0"}
CPU_IDLE = ("idle", "wait")              # iowait counts as idle, as in psutil
MEM = {"total": f"{UCD}.4.5.0", "free": f"{UCD}.4.6.0", "buffers": f"{UCD}.4.14.0", "cached": f"{UCD}.4.15.0"}
LOAD = [f"{UCD}.10.1.5.{i}" for i in (1, 2, 3)]          # laLoadInt: load average × 100
SYS = {"name": "1.3.6.1.2.1.1.5.0", "descr": "1.3.6.1.2.1.1.1.0", "uptime": "1.3.6.1.2.1.25.1.1.0"}
HR_PROCESSOR_LOAD = "1.3.6.1.2.1.25.3.3.1.2"
IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"
IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
IF_HC_IN, IF_HC_OUT = "1.3.6.1.2.1.31.1.1.1.6", "1.3.6.1.2.1.31.1.1.1.10"
WRAP32 = 2 ** 32
MAP_TTL = 60                             # seconds an interface-name walk is trusted
STALE = 30                               # a rate older than this is dropped


def settings(raw):
    """settings.json host_snmp → {enabled, address, port, community}."""
    s = raw if isinstance(raw, dict) else {}
    try:
        port = int(s.get("port") or 161)
    except (TypeError, ValueError):
        port = 161
    return {"enabled": bool(s.get("enabled", False)), "address": str(s.get("address") or "127.0.0.1"),
            "port": port, "community": str(s.get("community") or "public")}


def _delta(now, before, wrap=None):
    if now is None or before is None:
        return None
    d = now - before
    return d % wrap if wrap and d < 0 else d


class HostSnmp:
    """Samples one host's snmpd; keeps the previous counters for the next rate."""
    def __init__(self, host="127.0.0.1", port=161, community="public", timeout=1.0):
        self.target = (host, int(port), community)
        self.timeout = timeout
        self.cpu_prev = None             # last CPU tick counters that differed from the ones before
        self.cpu_avg = None
        self.ifindex, self.mapped_at = {}, 0.0
        self.if_prev = {}                # name → (rx, tx, time) of the last changed reading
        self.if_rate = {}                # name → (rx_mbps, tx_mbps, time)

    def _cpu(self, raw):
        if any(raw[k] is None for k in ("user", "system", "idle")):
            return None
        if self.cpu_prev and raw != self.cpu_prev:
            d = {k: _delta(raw[k], self.cpu_prev[k], WRAP32) or 0 for k in CPU_RAW if raw[k] is not None}
            total = sum(d.values())
            if total > 0:
                self.cpu_avg = max(0.0, min(100.0, (1 - sum(d.get(k, 0) for k in CPU_IDLE) / total) * 100))
        if raw != self.cpu_prev:
            self.cpu_prev = raw
        return self.cpu_avg

    def _map_interfaces(self, client):
        names = {oid.rsplit(".", 1)[1]: v for oid, v in client.walk(IF_NAME, 50) if v}
        if not names:                    # some agents only fill ifDescr
            names = {oid.rsplit(".", 1)[1]: v for oid, v in client.walk(IF_DESCR, 50) if v}
        self.ifindex = {str(v): int(i) for i, v in names.items()}
        self.mapped_at = time.time()

    def _interfaces(self, client, exclude):
        if not self.ifindex or time.time() - self.mapped_at > MAP_TTL:
            self._map_interfaces(client)
        wanted = [n for n in sorted(self.ifindex) if n not in exclude]
        oids = [f"{base}.{self.ifindex[n]}" for n in wanted for base in (IF_HC_IN, IF_HC_OUT)]
        v = client.get(oids) if oids else {}
        now, out = time.time(), {}
        for n in wanted:
            i = self.ifindex[n]
            rx, tx = v.get(f"{IF_HC_IN}.{i}"), v.get(f"{IF_HC_OUT}.{i}")
            if rx is None or tx is None:
                continue
            prev = self.if_prev.get(n)
            if prev is None or (rx, tx) != prev[:2]:
                if prev and now > prev[2] and rx >= prev[0] and tx >= prev[1]:
                    dt = now - prev[2]
                    self.if_rate[n] = ((rx - prev[0]) * 8 / dt / 1e6, (tx - prev[1]) * 8 / dt / 1e6, now)
                self.if_prev[n] = (rx, tx, now)
            rate = self.if_rate.get(n)
            if rate and now - rate[2] < STALE:
                out[n] = {"rx_mbps": round(rate[0], 2), "tx_mbps": round(rate[1], 2)}
        return out

    def sample(self, exclude=()):
        """
        {ok, error, fields, …}: fields lists what the agent answered, among
        cpu_avg, cpu_per, mem_*, load, uptime_seconds, hostname and net.
        """
        host, port, community = self.target
        out = {"ok": False, "error": "", "fields": []}
        try:
            with snmp.Client(host, community, port, timeout=self.timeout, retries=0) as client:
                v = client.get(list(SYS.values()) + list(CPU_RAW.values()) + list(MEM.values()) + LOAD)
                if all(v[o] is None for o in SYS.values()):
                    raise snmp.SnmpError("the agent answers but shows no system information")
                out["ok"] = True
                if v[SYS["name"]]:
                    out["hostname"] = str(v[SYS["name"]])
                if v[SYS["uptime"]] is not None:
                    out["uptime_seconds"] = int(v[SYS["uptime"]]) // 100
                raw = {k: v[o] for k, o in CPU_RAW.items()}
                out["cpu_counters"] = all(raw[k] is not None for k in ("user", "system", "idle"))
                cpu = self._cpu(raw)
                if cpu is not None:
                    out["cpu_avg"] = round(cpu, 1)
                mem = {k: v[o] for k, o in MEM.items()}
                if mem["total"] and mem["free"] is not None:
                    total = mem["total"] * 1024
                    used = (mem["total"] - mem["free"] - (mem["buffers"] or 0) - (mem["cached"] or 0)) * 1024
                    out.update(mem_total=total, mem_used=max(0, used), mem_percent=round(max(0, used) / total * 100, 1))
                if all(v[o] is not None for o in LOAD):
                    out["load"] = [v[o] / 100 for o in LOAD]
                per = [int(x) for _, x in client.walk(HR_PROCESSOR_LOAD) if isinstance(x, int)]
                if per:
                    out["cpu_per"] = per
                    out.setdefault("cpu_avg", round(sum(per) / len(per), 1))
                net = self._interfaces(client, set(exclude))
                if net or self.ifindex:
                    out["net"] = net
        except (snmp.SnmpError, OSError) as e:
            out["error"] = str(e)
        out["fields"] = [k for k in ("cpu_avg", "cpu_per", "mem_used", "load", "uptime_seconds", "hostname", "net")
                         if k in out]
        return out
