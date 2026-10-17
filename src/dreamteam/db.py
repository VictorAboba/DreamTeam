"""SQLite storage. One file per swarm (`.dreamteam/swarm.db`), WAL mode, used from the event loop thread."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, type TEXT, actor TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS events_type ON events(type);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, goal TEXT DEFAULT '', description TEXT DEFAULT '',
    status TEXT NOT NULL, assignee TEXT, reviewer TEXT, created_by TEXT, parent_id INTEGER,
    depends_on TEXT DEFAULT '[]', priority INTEGER DEFAULT 1, tags TEXT DEFAULT '[]', result TEXT,
    created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER, author TEXT, text TEXT, reply_to INTEGER, ts REAL);
CREATE INDEX IF NOT EXISTS comments_task ON comments(task_id, id);
CREATE TABLE IF NOT EXISTS agents (
    name TEXT PRIMARY KEY, kind TEXT, role TEXT, instructions TEXT DEFAULT '', model TEXT,
    language TEXT, status TEXT, hired_by TEXT, created_at REAL, last_active REAL,
    cost_usd REAL DEFAULT 0, tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0,
    notes TEXT DEFAULT '', system_prompt TEXT DEFAULT '', context_tokens INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS history (
    agent TEXT, seq INTEGER, msg TEXT, PRIMARY KEY (agent, seq));
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, sender TEXT, recipients TEXT, text TEXT, reply_to INTEGER,
    to_all INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS inbox (
    message_id INTEGER, recipient TEXT, read_at REAL, PRIMARY KEY (message_id, recipient));
CREATE INDEX IF NOT EXISTS inbox_recipient ON inbox(recipient, read_at);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, recipient TEXT, kind TEXT, text TEXT, data TEXT,
    delivered_at REAL);
CREATE INDEX IF NOT EXISTS notifications_recipient ON notifications(recipient, delivered_at);
CREATE TABLE IF NOT EXISTS triggers (
    id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT, condition TEXT, note TEXT DEFAULT '',
    actions TEXT DEFAULT '[]', once INTEGER DEFAULT 1, active INTEGER DEFAULT 1, created_at REAL,
    last_value INTEGER DEFAULT 0, last_fired REAL, fire_count INTEGER DEFAULT 0, last_error TEXT);
CREATE TABLE IF NOT EXISTS mcp_servers (
    name TEXT PRIMARY KEY, owner TEXT, scope TEXT, task_id INTEGER, description TEXT DEFAULT '',
    command TEXT, cwd TEXT, requirements TEXT DEFAULT '[]', origin TEXT, status TEXT, tools TEXT DEFAULT '[]',
    error TEXT, created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS file_locks (path TEXT PRIMARY KEY, owner TEXT, acquired_at REAL, expires_at REAL);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, command TEXT NOT NULL, shell TEXT,
    origin TEXT NOT NULL, status TEXT NOT NULL, reason TEXT, stopped_by TEXT, exit_code INTEGER, log TEXT,
    pid INTEGER, pgid INTEGER, token TEXT, hub_pid INTEGER, processes TEXT DEFAULT '[]', tail TEXT DEFAULT '',
    started_at REAL, background_at REAL, ended_at REAL);
CREATE INDEX IF NOT EXISTS jobs_owner ON jobs(owner, status);
"""
# columns added to a table after its first release, (table, column, declaration): a file made by an older version
# gets them when it is opened
ADDED_COLUMNS = (("messages", "to_all", "INTEGER DEFAULT 0"),)


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
        try:
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA foreign_keys=OFF")
            self.conn.executescript(SCHEMA)
            self._add_columns()
        except BaseException:  # e.g. "file is not a database": do not keep the file open until garbage collection
            self.conn.close()
            raise

    def _add_columns(self) -> None:
        for table, column, declaration in ADDED_COLUMNS:
            if column in {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}:
                continue
            try:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
            except sqlite3.OperationalError:  # another process opening the same file added it first
                if column not in {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}:
                    raise

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, tuple(params))

    def insert(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._lock:
            return int(self.conn.execute(sql, tuple(params)).lastrowid)

    def all(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, tuple(params)).fetchone()

    def scalar(self, sql: str, params: Iterable[Any] = ()) -> Any:
        row = self.one(sql, params)
        return None if row is None else row[0]

    def transaction(self):
        return _Transaction(self)

    # key/value helpers
    def get(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM kv WHERE key = ?", (key,))
        return default if row is None else json.loads(row["value"])

    def set(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, dumps(value)),
        )


class _Transaction:
    def __init__(self, db: Database):
        self.db = db

    def __enter__(self) -> Database:
        self.db._lock.acquire()
        self.db.conn.execute("BEGIN IMMEDIATE")
        return self.db

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.db.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.db._lock.release()


def dumps(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        # A lone surrogate (e.g. "\ud800" in LLM tool arguments) cannot be stored as UTF-8 by sqlite3.
        # Escaped JSON round-trips to the identical Python value, so replayed histories stay byte-identical.
        text = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    return text


_SURROGATE = re.compile("[\ud800-\udfff]")


def utf8_safe(text: str) -> str:
    """`text` with unpaired surrogates replaced by U+FFFD, for plain TEXT columns (sqlite3 stores UTF-8)."""
    return _SURROGATE.sub("�", text)


def loads(value: str | None, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    return json.loads(value)
