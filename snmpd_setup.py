"""
Set up this host's snmpd (net-snmp) for the Overview, in one click.

  1. install snmpd with apt-get when it is missing;
  2. give LQ-Sync its own read-only community, answered from 127.0.0.1
     only, in a drop-in file (/etc/snmp/snmpd.conf.d/lqsync.conf). Its own
     community, because Ubuntu's snmpd.conf already answers "public" with
     the system group only, and the first matching community line wins;
  3. make sure snmpd.conf reads the drop-in directory (Ubuntu's does; an
     includeDir line is appended, after a backup, when it does not);
  4. enable and restart snmpd, and check that it answers.

The rest of snmpd.conf is left alone, so other SNMP monitoring keeps working.
Root work goes through the callables passed in: run(cmd, timeout) →
(returncode, output), read(path) → text or None, write(path, text),
which(name) → path or None, and probe(community) → a host_snmp sample.
"""
import secrets
import time
from pathlib import Path

PATHS = {
    "main": Path("/etc/snmp/snmpd.conf"),
    "dropin_dir": Path("/etc/snmp/snmpd.conf.d"),
    "dropin": Path("/etc/snmp/snmpd.conf.d/lqsync.conf"),
}
APT_ENV = ["env", "DEBIAN_FRONTEND=noninteractive"]
PROBE_TRIES = 12                      # half a second apart, while snmpd starts
WANTED = {"cpu_avg": "CPU", "cpu_per": "per-core load", "mem_used": "memory", "load": "load average",
          "uptime_seconds": "uptime", "net": "interfaces"}


def new_community():
    return "lqsync-" + secrets.token_hex(6)


def dropin_text(community):
    return ("# Written by LQ-Sync (Settings → Host Monitoring): read-only SNMP for this host's\n"
            "# Overview, answered to this host only. Remove this file to undo.\n"
            f"rocommunity {community} 127.0.0.1\n")


def includes_dir(main_text, dropin_dir):
    """Whether snmpd.conf already reads dropin_dir (includeDir, any spacing, not commented out)."""
    for line in (main_text or "").splitlines():
        words = line.split()
        if len(words) >= 2 and words[0] == "includeDir" and words[1].rstrip("/") == str(dropin_dir).rstrip("/"):
            return True
    return False


def configure(community, run, read, write, which, probe, log, paths=None):
    """Do the four steps; log(text) reports progress. Returns {ok, error, community, missing}."""
    paths = paths or PATHS

    def step(text):
        log(text)

    def must(cmd, what, timeout=60):
        code, out = run(cmd, timeout)
        if code != 0:
            raise RuntimeError(f"{what} failed: {(out or '').strip()[-400:] or f'exit {code}'}")
        return out

    try:
        if which("snmpd"):
            step("snmpd is installed.")
        else:
            apt = which("apt-get")
            if not apt:
                raise RuntimeError("snmpd is not installed and apt-get was not found: install net-snmp's snmpd by hand")
            step("Installing snmpd (apt-get install snmpd)…")
            code, out = run(APT_ENV + [apt, "install", "-y", "-q", "snmpd"], 600)
            if code != 0:                     # stale package lists: refresh them once
                step("apt-get install failed; refreshing the package lists (apt-get update)…")
                must(APT_ENV + [apt, "update", "-q"], "apt-get update", 600)
                must(APT_ENV + [apt, "install", "-y", "-q", "snmpd"], "apt-get install snmpd", 600)
            step("snmpd installed.")

        step(f"Writing {paths['dropin']} (community {community}, from 127.0.0.1 only).")
        must(["mkdir", "-p", str(paths["dropin_dir"])], f"creating {paths['dropin_dir']}")
        write(paths["dropin"], dropin_text(community))

        main = read(paths["main"])
        if main is None:
            step(f"{paths['main']} is missing: writing a minimal one.")
            write(paths["main"], f"agentaddress udp:127.0.0.1:161\nincludeDir {paths['dropin_dir']}\n")
        elif includes_dir(main, paths["dropin_dir"]):
            step(f"{paths['main']} already reads {paths['dropin_dir']}.")
        else:
            backup = Path(str(paths["main"]) + ".lqsync-bak")
            step(f"Adding includeDir {paths['dropin_dir']} to {paths['main']} (backup: {backup}).")
            if read(backup) is None:
                write(backup, main)
            write(paths["main"], main.rstrip("\n") + f"\n\n# Added by LQ-Sync: read the drop-in files (LQ-Sync's is "
                                 f"{paths['dropin'].name})\nincludeDir {paths['dropin_dir']}\n")

        step("Enabling and restarting snmpd…")
        must(["systemctl", "enable", "snmpd"], "systemctl enable snmpd", 30)
        must(["systemctl", "restart", "snmpd"], "systemctl restart snmpd", 60)

        sample = {"ok": False, "error": "no answer"}
        for _ in range(PROBE_TRIES):
            sample = probe(community)
            if sample.get("ok"):
                break
            time.sleep(0.5)
        if not sample.get("ok"):
            raise RuntimeError(f"snmpd does not answer on 127.0.0.1:161 ({sample.get('error') or 'no answer'}). "
                               f"Check the agentaddress line of {paths['main']}: it must include 127.0.0.1 or udp:161.")
        missing = [label for field, label in WANTED.items()
                   if field not in sample and not (field == "cpu_avg" and sample.get("cpu_counters"))]
        step("snmpd answers" + (f", but not for: {', '.join(missing)}." if missing else " with everything the Overview uses."))
        return {"ok": True, "error": "", "community": community, "missing": missing}
    except Exception as e:
        log(f"Stopped: {e}")
        return {"ok": False, "error": str(e), "community": community, "missing": []}
