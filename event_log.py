"""
History of warnings and errors, kept in devices.db for the Events tab.

Two kinds of entry:

  - log: every WARNING and ERROR a service logs (updatecsv, wan_service,
    gui), through DBLogHandler. Repeats of the same message (numbers aside)
    within FOLD_SECONDS fold into one entry with a count;
  - condition: a problem with a start and an end — a router that does not
    answer, a failing or late job, a LibreQoS check, this host's snmpd.
    sync_conditions() opens an entry when one appears and marks it resolved
    when it is gone; one that comes back soon after reopens the same entry.

Every entry keeps the details needed to act on it: the full text and any
traceback, where it came from (service, logger, file:line, function, pid,
thread, host) or what it is about (router, job, interface…), the fix and a
docs link for known problems, and its last MAX_OCCURRENCES occurrences.
"""
import json
import logging
import re
import socket
import sqlite3
import threading
import time
import traceback

FOLD_SECONDS = 86400          # a repeat within this folds into the same log entry
REOPEN_SECONDS = 600          # a condition back within this reopens its resolved entry
MAX_OCCURRENCES = 50          # per entry
MAX_EVENTS = 20000
LEVELS = {"warn": "warning", "warning": "warning", "error": "error", "critical": "error"}

_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        key         TEXT NOT NULL,             -- what makes two occurrences the same problem
        kind        TEXT NOT NULL,             -- 'log' or 'condition'
        level       TEXT NOT NULL,             -- 'warning' or 'error'
        source      TEXT NOT NULL,             -- the service: updatecsv, wan_service, gui
        category    TEXT DEFAULT '',           -- log: the logger; condition: router, job, check, host
        title       TEXT NOT NULL,
        message     TEXT DEFAULT '',           -- the latest full text
        detail      TEXT DEFAULT '',           -- traceback, or the condition's explanation
        fix         TEXT DEFAULT '',
        link        TEXT DEFAULT '',
        context     TEXT DEFAULT '{}',         -- JSON: where it came from / what it is about
        first_at    REAL NOT NULL,
        last_at     REAL NOT NULL,
        count       INTEGER DEFAULT 1,
        resolved_at REAL DEFAULT 0             -- conditions: when it cleared (0: still active)
    )""",
    "CREATE INDEX IF NOT EXISTS events_key ON events (key, resolved_at)",
    "CREATE INDEX IF NOT EXISTS events_last ON events (last_at)",
    """CREATE TABLE IF NOT EXISTS event_occurrences (
        event_id INTEGER NOT NULL,
        at       REAL NOT NULL,
        message  TEXT DEFAULT ''
    )""",
    "CREATE INDEX IF NOT EXISTS event_occurrences_event ON event_occurrences (event_id, at)",
]
_HOST = socket.gethostname()


def ensure_tables(conn):
    for sql in _SCHEMA:
        conn.execute(sql)
    conn.commit()


def fold_key(text):
    """The part of a message that names the problem: numbers, addresses and quoted values left out."""
    text = re.sub(r"'[^']*'|\"[^\"]*\"", "'…'", text)
    text = re.sub(r"\b[0-9a-f]{2}(:[0-9a-f]{2}){5}\b", "MAC", text, flags=re.I)
    text = re.sub(r"\d+([.:]\d+)*", "#", text)
    return text[:200]


def _occurrence(conn, event_id, at, message):
    conn.execute("INSERT INTO event_occurrences (event_id, at, message) VALUES (?, ?, ?)",
                 (event_id, at, message[:4000]))
    conn.execute("""DELETE FROM event_occurrences WHERE event_id = ? AND rowid NOT IN (
                        SELECT rowid FROM event_occurrences WHERE event_id = ? ORDER BY at DESC LIMIT ?)""",
                 (event_id, event_id, MAX_OCCURRENCES))


def record_log(conn, source, level, logger_name, message, detail="", context=None, now=None):
    """One logged warning / error: folded into a recent entry with the same key, else a new entry."""
    now = now or time.time()
    level = LEVELS.get(level.lower(), "warning")
    title = message.strip().splitlines()[0][:300] if message.strip() else "(empty message)"
    key = f"log:{source}:{logger_name}:{level}:{fold_key(title)}"
    row = conn.execute("SELECT id FROM events WHERE key = ? AND last_at >= ? ORDER BY id DESC LIMIT 1",
                       (key, now - FOLD_SECONDS)).fetchone()
    ctx = json.dumps(context or {})
    if row:
        event_id = row[0]
        conn.execute("""UPDATE events SET last_at = ?, count = count + 1, title = ?, message = ?,
                        detail = CASE WHEN ? != '' THEN ? ELSE detail END, context = ? WHERE id = ?""",
                     (now, title, message[:8000], detail, detail[:16000], ctx, event_id))
    else:
        event_id = conn.execute(
            """INSERT INTO events (key, kind, level, source, category, title, message, detail, context,
                                   first_at, last_at) VALUES (?, 'log', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (key, level, source, logger_name, title, message[:8000], detail[:16000], ctx, now, now)).lastrowid
    _occurrence(conn, event_id, now, message)
    conn.commit()
    return event_id


def sync_conditions(conn, source, category, active, now=None):
    """
    The conditions of one (source, category) as they are now: a list of
    {key, level, title, message, detail, fix, link, context}. New ones are
    opened (or reopened when they cleared less than REOPEN_SECONDS ago),
    ongoing ones are updated — a changed message is kept as an occurrence —
    and open ones missing from the list are resolved.
    """
    now = now or time.time()
    keys = set()
    for c in active:
        key = f"cond:{source}:{category}:{c['key']}"
        keys.add(key)
        level = LEVELS.get(str(c.get("level", "warning")).lower(), "warning")
        message = str(c.get("message") or c["title"])
        fields = (level, c["title"][:300], message[:8000], str(c.get("detail") or "")[:16000],
                  c.get("fix") or "", c.get("link") or "", json.dumps(c.get("context") or {}))
        row = conn.execute("SELECT id, message, resolved_at FROM events WHERE key = ? ORDER BY id DESC LIMIT 1",
                           (key,)).fetchone()
        if row and row[2] == 0:                              # still going
            if row[1] != message:
                _occurrence(conn, row[0], now, message)
            conn.execute("""UPDATE events SET level = ?, title = ?, message = ?, detail = ?, fix = ?, link = ?,
                            context = ?, last_at = ? WHERE id = ?""", fields + (now, row[0]))
        elif row and now - row[2] < REOPEN_SECONDS:          # back soon after it cleared: the same problem
            conn.execute("""UPDATE events SET level = ?, title = ?, message = ?, detail = ?, fix = ?, link = ?,
                            context = ?, last_at = ?, resolved_at = 0, count = count + 1 WHERE id = ?""",
                         fields + (now, row[0]))
            _occurrence(conn, row[0], now, "again: " + message)
        else:
            event_id = conn.execute(
                """INSERT INTO events (key, kind, level, source, category, title, message, detail, fix, link,
                                       context, first_at, last_at) VALUES (?, 'condition', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (key, fields[0], source, category) + fields[1:] + (now, now)).lastrowid
            _occurrence(conn, event_id, now, message)
    for event_id, key in conn.execute(
            "SELECT id, key FROM events WHERE kind = 'condition' AND source = ? AND category = ? AND resolved_at = 0",
            (source, category)).fetchall():
        if key not in keys:
            conn.execute("UPDATE events SET resolved_at = ?, last_at = ? WHERE id = ?", (now, now, event_id))
            _occurrence(conn, event_id, now, "resolved")
    conn.commit()


def prune(conn, keep_days, now=None):
    """Forget entries older than keep_days (problems still open stay), and the oldest past MAX_EVENTS."""
    now = now or time.time()
    conn.execute("DELETE FROM events WHERE last_at < ? AND NOT (kind = 'condition' AND resolved_at = 0)",
                 (now - keep_days * 86400,))
    conn.execute("""DELETE FROM events WHERE id IN (SELECT id FROM events ORDER BY last_at DESC LIMIT -1 OFFSET ?)
                    AND NOT (kind = 'condition' AND resolved_at = 0)""", (MAX_EVENTS,))
    conn.execute("DELETE FROM event_occurrences WHERE event_id NOT IN (SELECT id FROM events)")
    conn.commit()


def _row(r):
    d = dict(r)
    try:
        d["context"] = json.loads(d.get("context") or "{}")
    except ValueError:
        d["context"] = {}
    d["state"] = "logged" if d["kind"] == "log" else ("resolved" if d["resolved_at"] else "active")
    return d


def query(conn, level="", source="", state="", kind="", since=0, q="", limit=100, offset=0):
    """(entries newest first, total matching) for the Events tab's filters."""
    where, args = ["last_at >= ?"], [since or 0]
    if level:
        where.append("level = ?")
        args.append(level)
    if source:
        where.append("source = ?")
        args.append(source)
    if kind:
        where.append("kind = ?")
        args.append(kind)
    if state == "active":
        where.append("kind = 'condition' AND resolved_at = 0")
    elif state == "resolved":
        where.append("kind = 'condition' AND resolved_at > 0")
    if q:
        where.append("(title LIKE ? OR message LIKE ? OR detail LIKE ? OR context LIKE ? OR category LIKE ?)")
        args += [f"%{q}%"] * 5
    sql = " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) FROM events WHERE {sql}", args).fetchone()[0]
    rows = conn.execute(f"""SELECT * FROM events WHERE {sql}
                            ORDER BY (kind = 'condition' AND resolved_at = 0) DESC, last_at DESC
                            LIMIT ? OFFSET ?""", args + [limit, offset]).fetchall()
    return [_row(r) for r in rows], total


def get(conn, event_id):
    """One entry with its occurrences (newest first), or None."""
    r = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if not r:
        return None
    d = _row(r)
    d["occurrences"] = [{"at": at, "message": m} for at, m in conn.execute(
        "SELECT at, message FROM event_occurrences WHERE event_id = ? ORDER BY at DESC", (event_id,))]
    return d


def summary(conn, now=None):
    """Open problems by level, and log entries in the last 24 hours by level."""
    now = now or time.time()
    out = {"active": {"error": 0, "warning": 0}, "day": {"error": 0, "warning": 0}}
    for level, n in conn.execute("SELECT level, COUNT(*) FROM events WHERE kind = 'condition' AND resolved_at = 0 "
                                 "GROUP BY level"):
        out["active"][level] = n
    for level, n in conn.execute("SELECT level, SUM(count) FROM events WHERE last_at >= ? GROUP BY level",
                                 (now - 86400,)):
        out["day"][level] = n or 0
    return out


class DBLogHandler(logging.Handler):
    """
    Logging handler that keeps WARNING and above in the events table.
    source is the service name, or a callable(record) → name. Writing the
    history never breaks the service: its own errors are swallowed.
    """
    def __init__(self, db_path, source, level=logging.WARNING):
        super().__init__(level)
        self.db_path, self.source = str(db_path), source
        self._conn, self._lock = None, threading.Lock()
        self._busy = threading.local()

    def emit(self, record):
        if getattr(self._busy, "on", False):         # a warning raised while recording one
            return
        self._busy.on = True
        try:
            message = record.getMessage()
            detail = ""
            if record.exc_info:
                detail = "".join(traceback.format_exception(*record.exc_info))
            elif record.stack_info:
                detail = record.stack_info
            context = {"service": self._source(record), "logger": record.name,
                       "file": f"{record.filename}:{record.lineno}", "function": record.funcName,
                       "pid": record.process, "thread": record.threadName, "host": _HOST}
            with self._lock:
                if self._conn is None:
                    self._conn = sqlite3.connect(self.db_path, timeout=5, check_same_thread=False)
                    ensure_tables(self._conn)
                record_log(self._conn, context["service"], record.levelname, record.name, message, detail,
                           context, record.created)
        except Exception:
            try:
                if self._conn is not None:
                    self._conn.close()
            except Exception:
                pass
            self._conn = None
        finally:
            self._busy.on = False

    def _source(self, record):
        return self.source(record) if callable(self.source) else self.source


def attach(db_path, source, logger=None):
    """Add a DBLogHandler to logger (the root logger by default) once; returns it."""
    logger = logger or logging.getLogger()
    for h in logger.handlers:
        if isinstance(h, DBLogHandler):
            return h
    handler = DBLogHandler(db_path, source)
    logger.addHandler(handler)
    return handler
