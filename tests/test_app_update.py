"""Updating from GitHub: check, download, verify, install, roll back (a local git repo stands in for GitHub)."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app_update

GOOD_TEST = "import unittest\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n"


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=cwd,
                   check=True, capture_output=True, text=True)


def commit(repo, files, message):
    for rel, text in files.items():
        p = Path(repo) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-m", message)
    return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=repo, capture_output=True,
                          text=True).stdout.strip()


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class UpdateTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="lqsync-update-test-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.repo = self.root / "upstream"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        self.v1 = commit(self.repo, {"gui.py": "VERSION = 1\n", "helper.py": "X = 1\n",
                                     "templates/index.html": "<p>v1</p>", "tests/__init__.py": "",
                                     "tests/test_ok.py": GOOD_TEST}, "first")
        self.app = self.root / "app"
        self.app.mkdir()
        for rel in ("gui.py", "helper.py", "templates/index.html"):
            (self.app / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(self.repo / rel, self.app / rel)
        (self.app / app_update.MARKER).write_text(self.v1 + "\n")
        (self.app / "config.json").write_text('{"bras": []}')
        patcher = mock.patch.object(app_update, "REPO_URL", str(self.repo))
        patcher.start()
        self.addCleanup(patcher.stop)

    def download(self):
        dest = self.root / f"clone{len(list(self.root.iterdir()))}"
        return dest, app_update.download(dest)

    def test_check(self):
        self.assertEqual(app_update.check(self.app)["available"], False)
        v2 = commit(self.repo, {"helper.py": "X = 2\n"}, "second")
        c = app_update.check(self.app)
        self.assertEqual((c["installed"], c["latest"], c["available"]), (self.v1, v2, True))

    def test_download_changelog_verify_install(self):
        commit(self.repo, {"helper.py": "X = 2\n", "new_module.py": "Y = 1\n"}, "second change")
        commit(self.repo, {"templates/index.html": "<p>v3</p>"}, "third change")
        clone, version = self.download()
        changes = app_update.changelog(clone, self.v1)
        self.assertEqual([c.split(" ", 1)[1] for c in changes], ["third change", "second change"])
        ok, summary, _ = app_update.verify(clone)
        self.assertEqual((ok, summary), (True, "1 tests passed"))

        updated, backup = app_update.install(clone, self.app, version)
        self.assertIn("new_module.py", updated)
        self.assertEqual((self.app / "helper.py").read_text(), "X = 2\n")
        self.assertEqual((self.app / "templates/index.html").read_text(), "<p>v3</p>")
        self.assertEqual((backup / "helper.py").read_text(), "X = 1\n")
        self.assertEqual(app_update.installed_version(self.app), version)
        self.assertEqual(app_update.installed_version(backup), self.v1)
        self.assertEqual((self.app / "config.json").read_text(), '{"bras": []}')     # never touched
        self.assertFalse((self.app / "tests").exists())                             # only app code

        restored, back_to = app_update.rollback(self.app, backup.name)
        self.assertEqual(back_to, self.v1)
        self.assertEqual((self.app / "helper.py").read_text(), "X = 1\n")
        self.assertEqual((self.app / "templates/index.html").read_text(), "<p>v1</p>")
        self.assertEqual(app_update.installed_version(self.app), self.v1)

    def test_verify_blocks_broken_code(self):
        commit(self.repo, {"helper.py": "def broken(:\n"}, "syntax error")
        clone, _ = self.download()
        ok, summary, _ = app_update.verify(clone)
        self.assertFalse(ok)
        self.assertEqual(summary, "helper.py does not compile")

    def test_verify_blocks_failing_tests(self):
        commit(self.repo, {"tests/test_ok.py": GOOD_TEST.replace("assertTrue(True)", "assertTrue(False)")}, "bad")
        clone, _ = self.download()
        ok, summary, output = app_update.verify(clone)
        self.assertFalse(ok)
        self.assertIn("tests failed (failures=1)", summary)
        self.assertIn("AssertionError", output)

    def test_rollback_rejects_unknown_names(self):
        for name in ("", "../etc", ".update_backup_x", ".update_backup_123"):
            with self.assertRaises(app_update.UpdateError):
                app_update.rollback(self.app, name)

    def test_backups_are_pruned(self):
        for i in range(app_update.KEEP_BACKUPS + 2):
            (self.app / f"{app_update.BACKUP_PREFIX}{1000 + i}").mkdir()
        app_update._prune_backups(self.app)
        self.assertEqual([b["created"] for b in app_update.backups(self.app)],
                         list(range(1000 + app_update.KEEP_BACKUPS + 1, 1001, -1)))

    def test_gui_routes(self):
        import gui
        commit(self.repo, {"tests/test_ok.py": GOOD_TEST.replace("assertTrue(True)", "assertTrue(False)")}, "bad")
        client = gui.app.test_client()
        with client.session_transaction() as s:
            s["authed"] = True
        headers = {"X-Requested-With": "fetch"}
        with mock.patch.object(gui, "SCRIPT_DIR", self.app), \
                mock.patch.object(gui, "_RUNNING_VERSION", self.v1), \
                mock.patch.object(gui, "_restart_after_update", return_value=[]) as restart:
            v = client.get("/api/system/version").get_json()
            self.assertEqual((v["running"], v["installed"], v["restart_needed"]), (self.v1, self.v1, False))

            r = client.post("/api/system/update", json={}, headers=headers)
            self.assertEqual(r.status_code, 422)
            self.assertEqual(r.get_json()["stage"], "verify")
            self.assertEqual(app_update.installed_version(self.app), self.v1)       # nothing installed
            restart.assert_not_called()

            r = client.post("/api/system/update", json={"force": True}, headers=headers).get_json()
            self.assertTrue(r["ok"])
            self.assertFalse(r["checks_ok"])
            self.assertEqual(r["changes"][0].split(" ", 1)[1], "bad")
            restart.assert_called_once()
            v = client.get("/api/system/version").get_json()
            self.assertTrue(v["restart_needed"])                                    # still running v1
            self.assertEqual(len(v["backups"]), 1)

            r = client.post("/api/system/rollback", json={"backup": v["backups"][0]["name"]}, headers=headers)
            self.assertEqual(r.get_json()["version"], self.v1)

    def test_api_errors_are_json(self):
        import gui
        client = gui.app.test_client()
        r = client.get("/api/does-not-exist")
        self.assertEqual(r.status_code, 404)
        self.assertIn("restart the gui service", r.get_json()["error"])
        self.assertEqual(client.get("/nope").status_code, 404)        # pages still get the normal 404


if __name__ == "__main__":
    unittest.main()
