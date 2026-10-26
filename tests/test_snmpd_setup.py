"""One-click snmpd setup: install, LQ-Sync's own community in a drop-in, includeDir, restart, check, save."""
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import snmpd_setup

UBUNTU_CONF = """\
sysLocation    Sitting on the Dock of the Bay
agentaddress  127.0.0.1,[::1]
view   systemonly  included   .1.3.6.1.2.1.1
view   systemonly  included   .1.3.6.1.2.1.25.1
rocommunity  public default -V systemonly
rouser authPrivUser authpriv -V systemonly
includeDir /etc/snmp/snmpd.conf.d
"""


class Host:
    """A fake root: files under a temp dir, commands recorded, snmpd present or not."""
    def __init__(self, root, installed=True, apt_fails_once=False, answers=True):
        self.root, self.installed, self.apt_fails_once, self.answers = root, installed, apt_fails_once, answers
        self.commands = []
        self.paths = {"main": root / "snmpd.conf", "dropin_dir": root / "snmpd.conf.d",
                      "dropin": root / "snmpd.conf.d" / "lqsync.conf"}

    def run(self, cmd, timeout):
        self.commands.append(cmd)
        if "install" in cmd and "snmpd" in cmd:
            if self.apt_fails_once:
                self.apt_fails_once = False
                return 100, "E: Unable to locate package snmpd"
            self.installed = True
        if cmd[0] == "mkdir":
            os.makedirs(cmd[-1], exist_ok=True)
        return 0, ""

    def read(self, path):
        return Path(path).read_text() if Path(path).exists() else None

    def write(self, path, text):
        Path(path).write_text(text)

    def which(self, name):
        return {"snmpd": "/usr/sbin/snmpd" if self.installed else None, "apt-get": "/usr/bin/apt-get"}.get(name)

    def probe(self, community):
        if not self.answers:
            return {"ok": False, "error": "no answer from 127.0.0.1:161 (timeout)"}
        return {"ok": True, "cpu_counters": True, "cpu_per": [1], "mem_used": 1, "load": [0, 0, 0],
                "uptime_seconds": 5, "net": {}}

    def configure(self, community="lqsync-abc"):
        log = []
        result = snmpd_setup.configure(community, self.run, self.read, self.write, self.which, self.probe,
                                       log.append, self.paths)
        return result, log


class ConfigureTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="lqsync-snmpd-"))
        self.addCleanup(shutil.rmtree, self.dir)

    def test_fresh_ubuntu(self):
        host = Host(self.dir, installed=False)
        host.write(host.paths["main"], UBUNTU_CONF.replace("/etc/snmp/snmpd.conf.d", str(self.dir / "snmpd.conf.d")))
        result, log = host.configure()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["missing"], [])
        self.assertIn(["env", "DEBIAN_FRONTEND=noninteractive", "/usr/bin/apt-get", "install", "-y", "-q", "snmpd"],
                      host.commands)
        self.assertIn("rocommunity lqsync-abc 127.0.0.1\n", host.read(host.paths["dropin"]))
        self.assertNotIn("Added by LQ-Sync", host.read(host.paths["main"]))         # it already reads the drop-ins
        self.assertEqual(host.commands[-2:], [["systemctl", "enable", "snmpd"], ["systemctl", "restart", "snmpd"]])

    def test_include_dir_added_once_with_backup(self):
        host = Host(self.dir)
        old = "agentAddress udp:127.0.0.1:161\nrocommunity public default -V systemonly\n"
        host.write(host.paths["main"], old)
        self.assertTrue(host.configure()[0]["ok"])
        main = host.read(host.paths["main"])
        self.assertTrue(main.endswith(f"includeDir {self.dir / 'snmpd.conf.d'}\n"))
        self.assertEqual(host.read(str(host.paths["main"]) + ".lqsync-bak"), old)
        self.assertTrue(host.configure()[0]["ok"])                                  # again: nothing doubled
        self.assertEqual(host.read(host.paths["main"]), main)
        self.assertEqual(host.read(str(host.paths["main"]) + ".lqsync-bak"), old)

    def test_apt_retries_after_update(self):
        host = Host(self.dir, installed=False, apt_fails_once=True)
        host.write(host.paths["main"], UBUNTU_CONF)
        self.assertTrue(host.configure()[0]["ok"])
        self.assertIn(["env", "DEBIAN_FRONTEND=noninteractive", "/usr/bin/apt-get", "update", "-q"], host.commands)

    def test_no_answer_is_reported(self):
        host = Host(self.dir, answers=False)
        host.write(host.paths["main"], UBUNTU_CONF)
        with mock.patch.object(snmpd_setup.time, "sleep"):
            result, log = host.configure()
        self.assertFalse(result["ok"])
        self.assertIn("agentaddress", result["error"])
        self.assertTrue(log[-1].startswith("Stopped:"))

    def test_includes_dir(self):
        self.assertTrue(snmpd_setup.includes_dir("includeDir  /etc/snmp/snmpd.conf.d/\n", "/etc/snmp/snmpd.conf.d"))
        self.assertFalse(snmpd_setup.includes_dir("# includeDir /etc/snmp/snmpd.conf.d\n", "/etc/snmp/snmpd.conf.d"))
        self.assertTrue(snmpd_setup.new_community().startswith("lqsync-"))


class RouteTest(unittest.TestCase):
    def test_setup_runs_and_saves_the_community(self):
        import gui
        d = Path(tempfile.mkdtemp(prefix="lqsync-snmpd-gui-"))
        self.addCleanup(shutil.rmtree, d)
        host = Host(d)
        host.write(host.paths["main"], UBUNTU_CONF)
        saved = gui.SETTINGS_PATH.read_text() if gui.SETTINGS_PATH.exists() else None
        self.addCleanup(lambda: gui.SETTINGS_PATH.write_text(saved) if saved is not None else gui.SETTINGS_PATH.unlink())
        with mock.patch.multiple(gui, SNMPD_PATHS=host.paths, _snmpd_run=host.run, _snmpd_read=host.read,
                                 _snmpd_write=host.write, _snmpd_which=host.which, _snmpd_probe=host.probe):
            c = gui.app.test_client()
            with c.session_transaction() as s:
                s["authed"] = True
            c.post("/api/host-snmp/setup", headers={"X-Requested-With": "fetch"})
            for _ in range(50):
                job = c.get("/api/host-snmp/setup").get_json()
                if not job["running"]:
                    break
                time.sleep(0.1)
        self.assertTrue(job["result"]["ok"], job)
        community = job["result"]["community"]
        self.assertTrue(community.startswith("lqsync-"))
        self.assertEqual(json.loads(gui.SETTINGS_PATH.read_text())["host_snmp"],
                         {"enabled": True, "address": "127.0.0.1", "port": 161, "community": community})
        self.assertIn(f"rocommunity {community} 127.0.0.1", host.read(host.paths["dropin"]))


if __name__ == "__main__":
    unittest.main()
