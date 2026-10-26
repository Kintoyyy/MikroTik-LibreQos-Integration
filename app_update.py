"""
Updating LQ-Sync from GitHub, safely:

  check()    latest commit on GitHub and what changed since the installed one;
  download() a shallow clone of the branch;
  verify()   the downloaded code compiles and its test suite passes, before
             anything is replaced;
  install()  copy the top-level *.py files and templates/ over the app,
             keeping a backup of every replaced file;
  rollback() put a backup's files back.

Never touched: config.json, settings.json, gui_auth.json, devices.db,
ShapedDevices.csv, network.json, lqos.conf and the systemd unit files.
gui.py runs these and restarts the services.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO_URL = "https://github.com/Kintoyyy/MikroTik-LibreQos-Integration"
BRANCH = "main"
MARKER = ".installed_version"          # the installed commit, written by install()
BACKUP_PREFIX = ".update_backup_"
KEEP_BACKUPS = 5
HISTORY_DEPTH = 50                     # commits fetched, for the changelog


class UpdateError(Exception):
    pass


def _git(*args, cwd=None, timeout=120):
    git = shutil.which("git") or "/usr/bin/git"
    try:
        r = subprocess.run([git, *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise UpdateError("git is not installed on this host (apt install git)")
    except subprocess.TimeoutExpired:
        raise UpdateError(f"git {args[0]} timed out after {timeout}s (no internet access?)")
    if r.returncode != 0:
        raise UpdateError(f"git {args[0]} failed: {(r.stderr or r.stdout).strip()[-400:]}")
    return r.stdout.strip()


def installed_version(app_dir):
    """The commit the app was last installed from, or "unknown"."""
    try:
        return (Path(app_dir) / MARKER).read_text().strip() or "unknown"
    except OSError:
        return "unknown"


def _github_compare(repo_url, base, head):
    """Commits from base to head via the GitHub API: [{sha, subject, date}] (best effort, None on failure)."""
    m = re.match(r"https://github\.com/([^/]+)/([^/.]+)", repo_url)
    if not m or base in ("", "unknown"):
        return None
    url = f"https://api.github.com/repos/{m.group(1)}/{m.group(2)}/compare/{base}...{head}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "lq-sync"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
    except Exception:
        return None
    return [{"sha": c["sha"][:7], "subject": c["commit"]["message"].splitlines()[0],
             "date": c["commit"]["author"]["date"]} for c in reversed(data.get("commits", []))]


def check(app_dir, repo_url=None, branch=None):
    """{installed, latest, available, commits} without downloading the code."""
    repo_url, branch = repo_url or REPO_URL, branch or BRANCH
    out = _git("ls-remote", repo_url, f"refs/heads/{branch}", timeout=30)
    if not out:
        raise UpdateError(f"branch {branch} not found on {repo_url}")
    latest = out.split()[0][:7]
    installed = installed_version(app_dir)
    available = not installed.startswith(latest) and not latest.startswith(installed)
    return {"installed": installed, "latest": latest, "available": available,
            "commits": _github_compare(repo_url, installed, latest) if available else []}


def download(dest, repo_url=None, branch=None):
    """Shallow-clone the branch into dest. Returns its short commit id."""
    repo_url, branch = repo_url or REPO_URL, branch or BRANCH
    _git("clone", "--depth", str(HISTORY_DEPTH), "--branch", branch, repo_url, str(dest), timeout=180)
    if not (Path(dest) / "gui.py").exists():
        raise UpdateError("the downloaded code has no gui.py; not an LQ-Sync checkout")
    return _git("rev-parse", "--short", "HEAD", cwd=dest)


def changelog(clone, installed):
    """Commit subjects from the installed commit up to the clone's HEAD (newest first)."""
    rng = "HEAD"
    if installed not in ("", "unknown"):
        try:
            _git("cat-file", "-e", f"{installed}^{{commit}}", cwd=clone)
            rng = f"{installed}..HEAD"
        except UpdateError:
            pass                      # older than the shallow history: show the latest ones
    log = _git("log", "--no-decorate", "--format=%h %s", "-n", "30", rng, cwd=clone)
    return [line for line in log.splitlines() if line]


def verify(clone, python=None, timeout=300):
    """
    Check the downloaded code before installing it: every top-level module
    compiles, and the test suite (tests/) passes with this host's Python and
    packages. Returns (ok, summary, output tail).
    """
    clone = Path(clone)
    for src in sorted(clone.glob("*.py")):
        try:
            compile(src.read_text(encoding="utf-8"), str(src), "exec")    # syntax only, writes nothing
        except (SyntaxError, ValueError, UnicodeDecodeError) as e:
            return False, f"{src.name} does not compile", str(e)[-1500:]
    if not (clone / "tests" / "__init__.py").exists():
        return True, "compiled (no tests in this version)", ""
    env = {k: v for k, v in os.environ.items() if not k.startswith("LQSYNC_")}
    try:
        r = subprocess.run([python or sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                           cwd=clone, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"tests did not finish within {timeout}s", ""
    output = (r.stderr + r.stdout)[-3000:]
    ran = re.search(r"Ran (\d+) tests?", output)
    count = ran.group(1) if ran else "?"
    if r.returncode != 0:
        failed = re.search(r"FAILED \(([^)]*)\)", output)
        return False, f"tests failed ({failed.group(1) if failed else 'see output'})", output
    return True, f"{count} tests passed", output[-600:]


def _app_files(root):
    root = Path(root)
    files = sorted(p.name for p in root.glob("*.py"))
    if (root / "templates").is_dir():
        files += sorted(str(p.relative_to(root)) for p in (root / "templates").rglob("*") if p.is_file())
    return files


def install(clone, app_dir, version):
    """
    Copy the clone's top-level *.py files and templates/ over app_dir, after
    backing up every file that is replaced. Returns (updated files, backup dir).
    """
    clone, app_dir = Path(clone), Path(app_dir)
    backup = app_dir / f"{BACKUP_PREFIX}{int(time.time())}"
    backup.mkdir(parents=True, exist_ok=False)
    (backup / MARKER).write_text(installed_version(app_dir) + "\n")
    updated = []
    for rel in _app_files(clone):
        src, dest = clone / rel, app_dir / rel
        if dest.exists():
            (backup / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest, backup / rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        updated.append(rel)
    (app_dir / MARKER).write_text(version + "\n")
    _prune_backups(app_dir)
    return updated, backup


def backups(app_dir):
    """[{name, created, version}] newest first."""
    out = []
    for d in Path(app_dir).glob(f"{BACKUP_PREFIX}*"):
        if d.is_dir():
            try:
                created = int(d.name[len(BACKUP_PREFIX):])
            except ValueError:
                continue
            out.append({"name": d.name, "created": created, "version": installed_version(d)})
    return sorted(out, key=lambda b: -b["created"])


def _prune_backups(app_dir, keep=KEEP_BACKUPS):
    for b in backups(app_dir)[keep:]:
        shutil.rmtree(Path(app_dir) / b["name"], ignore_errors=True)


def rollback(app_dir, name):
    """Put a backup's files back. Returns (restored files, the version restored)."""
    app_dir = Path(app_dir)
    if not re.fullmatch(re.escape(BACKUP_PREFIX) + r"\d+", name or ""):
        raise UpdateError("unknown backup")
    backup = app_dir / name
    if not backup.is_dir():
        raise UpdateError(f"backup {name} not found")
    restored = []
    for src in sorted(backup.rglob("*")):
        rel = src.relative_to(backup)
        if src.is_dir() or str(rel) == MARKER:
            continue
        dest = app_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        restored.append(str(rel))
    version = installed_version(backup)
    (app_dir / MARKER).write_text(version + "\n")
    return restored, version


def temp_dir():
    return Path(tempfile.mkdtemp(prefix="lqsync-update-"))
