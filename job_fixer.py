"""
Fixes for failing background jobs (the Pollers table's Fix buttons and auto-fix).

This module only decides: what a job's error means, which fix applies and
whether it is safe to run unattended. gui.py carries the fixes out. Pure
functions, so the rules are unit-tested without a server.

    diagnose(job)          → {"id", "label", "cause", "auto"} or None
    shift_rrd_dump(xml)    → an `rrdtool dump` with its last update moved back
    router_verdict(probe)  → what a router reachability probe found
"""
import re
import time

# rrdtool refuses a sample older than the file's last one: the clock went back.
RRD_CLOCK_RE = re.compile(
    r"rrdtool update (?P<file>\S+?\.rrd): .*illegal attempt to update using time (?P<now>\d+) "
    r"when last update time is (?P<last>\d+)", re.S)
# Any other rrdtool failure on one file (wrong data source count, corrupt or truncated file …)
RRD_OTHER_RE = re.compile(r"rrdtool update (?P<file>\S+?\.rrd):")

DISK_FULL_RE = re.compile(r"No space left on device|database or disk is full|\bENOSPC\b", re.I)
DB_LOCKED_RE = re.compile(r"database is locked|database table is locked", re.I)
ROUTER_AUTH_RE = re.compile(
    r"invalid user name or password|cannot log in|login failure|not logged in|"
    r"authentication failed|wrong user", re.I)
ROUTER_PERM_RE = re.compile(r"not enough permissions|permission denied \(policy\)|no such command", re.I)
ROUTER_CONN_RE = re.compile(
    r"connection failed|Errno (101|110|111|113|104|-2|-3|-5)|timed out|No route to host|"
    r"Connection refused|Network is unreachable|Connection reset|Name or service not known|"
    r"Temporary failure in name resolution|getaddrinfo", re.I)
LQOSD_RE = re.compile(r"lqosd|/run/lqos|bus|ConnectionRefused|Connection refused|timed out", re.I)

# Job kinds that talk to a MikroTik
ROUTER_KINDS = ("poller", "webhook", "router", "wan")


def _fix(fix_id, label, cause, auto=False):
    return {"id": fix_id, "label": label, "cause": cause, "auto": auto}


def fmt_span(seconds):
    seconds = abs(seconds)
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.0f} days"


def diagnose(job):
    """
    The fix for a job that is failing ("error") or has stopped running
    ("late"); None while it is fine. job is a row of the jobs table plus its
    shown status. "auto" fixes are safe to run unattended: they never touch
    the router, config.json or subscribers' traffic beyond a LibreQoS reload.
    """
    status, kind = job.get("status"), job.get("kind", "")
    service, error = job.get("service", ""), job.get("last_error") or ""
    if status == "late":
        return _fix("restart_service", f"Restart {service}",
                    f"The {service} service has not run '{job.get('name')}' for over two intervals: "
                    "it is stopped, crashed or stuck.", auto=True)
    if status != "error":
        return None

    if DISK_FULL_RE.search(error):
        return _fix("free_disk", "Free disk space",
                    "The disk is full, so nothing can be written (database, graphs, LibreQoS's files).")
    m = RRD_CLOCK_RE.search(error)
    if m:
        skew = int(m.group("last")) - int(m.group("now"))
        return _fix("rrd_clock", "Repair graph files",
                    f"The system clock went back by {fmt_span(skew)} (NTP correction or RTC reset): the graph "
                    "files hold samples from the 'future', so rrdtool refuses every new one.", auto=True)
    m = RRD_OTHER_RE.search(error)
    if m:
        return _fix("rrd_rebuild", "Rebuild graph file",
                    f"rrdtool cannot write {m.group('file')} (it is damaged or from an older layout). "
                    "It is moved aside and started over.", auto=True)
    if DB_LOCKED_RE.search(error):
        return _fix("retry", "Retry now", "Another service held the database for too long; this is "
                    "usually momentary and clears on the next run.")

    if kind in ROUTER_KINDS:
        if ROUTER_AUTH_RE.search(error):
            return _fix("router", "Test router login",
                        "The router refused the API user name or password.")
        if ROUTER_PERM_RE.search(error):
            return _fix("router", "Test router access",
                        "The API user may log in but lacks a permission this job needs.")
        if ROUTER_CONN_RE.search(error):
            return _fix("router", "Diagnose router",
                        "This host cannot open the router's API port. A probe (DNS, route, ping, API port, "
                        "login) tells where it stops.")
        if kind == "wan":
            return _fix("restart_service", "Restart wan_service", "The WAN sync failed: " + error[:160])
        return _fix("retry", "Retry now", "The router answered but the job failed while reading it.")

    if kind == "apply":
        lqosd = bool(LQOSD_RE.search(error))
        return _fix("apply", "Restart LibreQoS & re-apply" if lqosd else "Re-apply now",
                    "LibreQoS did not take the new files" + (" — lqosd looks down or hung." if lqosd else "."),
                    auto=lqosd)
    if kind == "config":
        return _fix("config", "Restore last good config.json",
                    "config.json cannot be used, so every sync cycle is skipped until it is fixed.")
    if kind == "build":
        return _fix("retry", "Rebuild now", "Building network.json / ShapedDevices.csv failed.")
    if kind == "burst":
        return _fix("retry", "Retry now", "Changing the HTB classes failed (often LibreQoS rebuilt its "
                    "queues mid-pass).")
    return _fix("retry", "Retry now", "The last run failed.")


# ── RRD clock repair ─────────────────────────────────────────────────────────

def shift_rrd_dump(xml, now=None, margin=1):
    """
    Move the last update of an `rrdtool dump` back to just before now, so
    rrdtool accepts new samples again after the clock went back. The data
    keeps its order and moves back with it (whole steps, so consolidation
    stays aligned). Returns (xml, seconds moved); 0 when no move was needed.
    """
    now = int(now if now is not None else time.time())
    step_m = re.search(r"<step>\s*(\d+)\s*</step>", xml)
    last_m = re.search(r"<lastupdate>\s*(\d+)\s*</lastupdate>", xml)
    if not step_m or not last_m:
        raise ValueError("not an rrdtool dump (no <step> / <lastupdate>)")
    step, last = int(step_m.group(1)), int(last_m.group(1))
    if last < now:
        return xml, 0
    steps = (last - now) // step + margin
    shift = steps * step
    new = xml[:last_m.start()] + f"<lastupdate>{last - shift}</lastupdate>" + xml[last_m.end():]
    return new, shift


# ── Router probe ─────────────────────────────────────────────────────────────

def router_verdict(p):
    """
    What a router probe found. p: address, port, resolved, ip, src (this
    host's address towards it), ping (True/False/None = not run), tcp
    ("ok" | "refused" | "unreachable" | "no_route" | "timeout" | "error"),
    tcp_error, login ("ok" | "auth" | "error" | None), login_error,
    health_ok (the GUI's own once-a-minute poll), health_via.
    Returns {"ok", "cause", "advice", "commands"}: ok when the API works now.
    """
    addr, port = p.get("address", ""), p.get("port", 8728)
    src = p.get("src") or "<this host's IP>"
    if not p.get("resolved"):
        return {"ok": False, "cause": f"'{addr}' does not resolve: {p.get('resolve_error', '')}",
                "advice": "Use the router's IP address, or fix this host's DNS (/etc/resolv.conf).",
                "commands": []}
    tcp, ping = p.get("tcp"), p.get("ping")
    if tcp == "ok":
        if p.get("login") == "ok":
            return {"ok": True, "cause": f"The router answers on {addr}:{port} and the login works now "
                    f"({p.get('identity') or 'identity read'}); the failure was temporary.",
                    "advice": "", "commands": []}
        if p.get("login") == "auth":
            return {"ok": False, "cause": f"{addr}:{port} answers but refuses the login: {p.get('login_error', '')}",
                    "advice": "Check the user name and password on the Routers tab, and that the user exists "
                              "on the router with the 'api' policy.",
                    "commands": ["/user print where name=<api user>"]}
        return {"ok": False, "cause": f"{addr}:{port} accepts connections but the API session failed: "
                f"{p.get('login_error', '')}",
                "advice": "Port " + str(port) + " may not be the RouterOS API (api-ssl is 8729 and needs TLS), "
                          "or the router is overloaded.",
                "commands": ["/ip service print"]}

    reachable = ping is True or p.get("health_ok") is True
    allow = [f"/ip service set api disabled=no port={port}",
             f"/ip firewall filter add chain=input protocol=tcp dst-port={port} src-address={src} "
             f"action=accept place-before=0 comment=\"LQ-Sync API\""]
    if tcp in ("unreachable", "refused") and reachable:
        how = "rejects" if tcp == "unreachable" else "refuses"
        seen = "answers ping" if ping else f"answers the GUI's {(p.get('health_via') or 'snmp').upper()} poll"
        return {"ok": False,
                "cause": f"{addr} {seen} but {how} TCP {port}: the API service is disabled, limited by "
                         f"'Available From', or a firewall rule rejects this host ({src}).",
                "advice": "On the router: enable the API service, allow this host in its 'Available From' list, "
                          "and accept the port in the input chain before any drop/reject rule.",
                "commands": allow}
    if tcp == "timeout" and reachable:
        return {"ok": False, "cause": f"{addr} answers ping but TCP {port} times out: a firewall drops it.",
                "advice": "Accept the API port from this host in the router's input chain.",
                "commands": allow}
    if tcp == "no_route":
        return {"ok": False, "cause": f"This host has no route to {p.get('ip', addr)} ({p.get('tcp_error', '')}).",
                "advice": "Add a route or fix this host's management IP / gateway (Files tab: netplan).",
                "commands": []}
    if ping is False or tcp in ("unreachable", "timeout"):
        where = f" (route: {p['route']})" if p.get("route") else ""
        return {"ok": False, "cause": f"{addr} does not answer at all{where}: it is down, rebooting, or not on "
                f"this network (no ARP reply / no path).",
                "advice": "Check that the router is powered and reachable from this host, its address on the "
                          "Routers tab, and the cabling / VLAN between them.",
                "commands": []}
    return {"ok": False, "cause": f"Connecting to {addr}:{port} failed: {p.get('tcp_error') or tcp}",
            "advice": "Check the address and port on the Routers tab.", "commands": allow}
