"""SQLite storage. One file per swarm (`.dreamteam/swarm.db`), WAL mode, used from the event loop thread."""

from __future__ import annotations

import contextlib
import functools
import json
import os
import re
import sqlite3
import threading
import urllib.parse
from pathlib import Path
from typing import Any, Iterable, Iterator

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
    last_value INTEGER DEFAULT 0, last_fired REAL, fire_count INTEGER DEFAULT 0, last_error TEXT,
    told_at REAL, held INTEGER DEFAULT 0, held_last REAL, held_files TEXT);
CREATE TABLE IF NOT EXISTS mcp_servers (
    name TEXT PRIMARY KEY, owner TEXT, scope TEXT, task_id INTEGER, description TEXT DEFAULT '',
    command TEXT, cwd TEXT, requirements TEXT DEFAULT '[]', origin TEXT, status TEXT, tools TEXT DEFAULT '[]',
    error TEXT, created_at REAL, updated_at REAL, digest TEXT, approved_digest TEXT);
CREATE TABLE IF NOT EXISTS file_locks (path TEXT PRIMARY KEY, owner TEXT, acquired_at REAL, expires_at REAL,
    wanted_at REAL);
CREATE TABLE IF NOT EXISTS lock_waiters (agent TEXT, path TEXT, since REAL, PRIMARY KEY (agent, path));
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, command TEXT NOT NULL, shell TEXT,
    origin TEXT NOT NULL, status TEXT NOT NULL, reason TEXT, stopped_by TEXT, exit_code INTEGER, log TEXT,
    pid INTEGER, pgid INTEGER, token TEXT, hub_pid INTEGER, processes TEXT DEFAULT '[]', tail TEXT DEFAULT '',
    started_at REAL, background_at REAL, ended_at REAL);
CREATE INDEX IF NOT EXISTS jobs_owner ON jobs(owner, status);
"""
# columns added to a table after its first release, (table, column, declaration): a file made by an older version
# gets them when it is opened
ADDED_COLUMNS = (("messages", "to_all", "INTEGER DEFAULT 0"), ("mcp_servers", "digest", "TEXT"),
                 ("mcp_servers", "approved_digest", "TEXT"), ("file_locks", "wanted_at", "REAL"),
                 ("triggers", "told_at", "REAL"), ("triggers", "held", "INTEGER DEFAULT 0"),
                 ("triggers", "held_last", "REAL"), ("triggers", "held_files", "TEXT"))


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


def sqlite_uri(path: str | os.PathLike, query: str) -> str:
    """The `file:` URI sqlite3.connect(..., uri=True) opens `path` by, with the parameters `query` (e.g.
    "mode=ro"). A UNC path (\\\\server\\share\\x.db) keeps its server in the path (file:////server/share/x.db: SQLite
    takes no URI authority), a mapped drive stays a drive letter (no resolve()), and '?', '#' and '%' in names are
    escaped."""
    text = os.path.abspath(os.fspath(path)).replace("\\", "/")
    if text.startswith("//"):
        prefix = "file://"
    elif text.startswith("/"):
        prefix = "file:"
    else:
        prefix = "file:/"
    # quoted as the bytes of the file system's encoding: a POSIX name that is not UTF-8 (surrogate escapes) works
    return prefix + urllib.parse.quote(os.fsencode(text), safe="/:") + "?" + query


# ---- schema of a database file (read-only): a file made by a newer version has tables or columns this one lacks
Source = str | os.PathLike | sqlite3.Connection | Database


@contextlib.contextmanager
def _reading(source: Source) -> Iterator[sqlite3.Connection]:
    """A connection to `source`: a Database's or a given connection (under the Database's lock), or a file opened
    read-only (never created, nothing written to it) and closed afterwards."""
    if isinstance(source, Database):
        with source._lock:
            yield source.conn
        return
    if isinstance(source, sqlite3.Connection):
        yield source
        return
    path = Path(source)
    if not path.is_file():
        raise FileNotFoundError(f"no database file at {path}")
    conn = sqlite3.connect(sqlite_uri(path, "mode=ro"), uri=True, timeout=5.0)
    try:
        yield conn
    finally:
        conn.close()


def _columns(conn: sqlite3.Connection) -> set[str]:
    tables = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'")]
    return {f"{table}.{row[0]}" for table in tables
            for row in conn.execute("SELECT name FROM pragma_table_info(?)", (table,))}


def columns_of(source: Source) -> set[str]:
    """Every "table.column" of a session database (SQLite's own tables left out). `source` is a file path (opened
    read-only; FileNotFoundError when it is missing), a sqlite3 connection or a Database. Raises sqlite3.Error for a
    file that is not a readable database."""
    with _reading(source) as conn:
        return _columns(conn)


@functools.lru_cache(maxsize=1)
def _known_columns() -> frozenset[str]:
    from .tracing import SCHEMA as TRACE_SCHEMA  # tracing imports this module

    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(SCHEMA)
        conn.executescript(TRACE_SCHEMA)
        return frozenset(_columns(conn) | {f"{table}.{column}" for table, column, _ in ADDED_COLUMNS})
    finally:
        conn.close()


def known_columns() -> frozenset[str]:
    """Every "table.column" this version creates in a session database: SCHEMA, the trace tables and
    ADDED_COLUMNS."""
    return _known_columns()


@functools.lru_cache(maxsize=1)
def known_objects() -> frozenset[tuple[str, str]]:
    """Every (type, name) of the indexes, triggers and views this version creates in a session database (SCHEMA
    and the trace tables); SQLite's own automatic indexes are not listed."""
    from .tracing import SCHEMA as TRACE_SCHEMA  # tracing imports this module

    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(SCHEMA)
        conn.executescript(TRACE_SCHEMA)
        return frozenset((str(t), str(n)) for t, n in conn.execute(
            "SELECT type, name FROM sqlite_master WHERE type IN ('index', 'trigger', 'view') AND name NOT LIKE "
            "'sqlite\\_%' ESCAPE '\\'"))
    finally:
        conn.close()


def foreign_objects(source: Source) -> list[str]:
    """The triggers, views and indexes of `source` (see columns_of) this version does not create, as
    "type name", sorted: a database another program changed, or one made by a newer DreamTeam."""
    with _reading(source) as conn:
        found = {(str(t), str(n)) for t, n in conn.execute(
            "SELECT type, name FROM sqlite_master WHERE type IN ('index', 'trigger', 'view') AND name NOT LIKE "
            "'sqlite\\_%' ESCAPE '\\'")}
    return sorted(f"{t} {n}" for t, n in found - known_objects())


def unknown_columns(source: Source) -> list[str]:
    """The "table.column" names of `source` (see columns_of) this version does not know, sorted: not empty for a
    database made by a newer DreamTeam."""
    return sorted(columns_of(source) - known_columns())


def max_tools_revision(source: Source) -> int | None:
    """The highest frozen tool-set revision stored in the database's kv (`tools:<member>` records {"rev", ...},
    tools/frozen.py), None when no member has one (or the file has no kv table). A revision above
    frozen.TOOLSET_REVISION was written by a newer DreamTeam. Records that are not JSON or carry no integer "rev"
    are ignored."""
    from .tools.frozen import KV_PREFIX  # the tools package imports this module

    best: int | None = None
    with _reading(source) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'kv'").fetchone():
            return None
        rows = conn.execute("SELECT value FROM kv WHERE substr(key, 1, ?) = ?", (len(KV_PREFIX), KV_PREFIX))
        for (value,) in rows:
            try:
                rec = json.loads(value) if isinstance(value, (str, bytes)) else None
            except ValueError:
                continue
            rev = rec.get("rev") if isinstance(rec, dict) else None
            if isinstance(rev, int) and not isinstance(rev, bool) and (best is None or rev > best):
                best = rev
    return best


def remap_sql(table: str, column: str, mapping: dict[str, str]) -> tuple[str, list[str]]:
    """One UPDATE that renames every `mapping` key in `column` at once, so a swap or a chain of names (a name both
    an old and a new one) stays correct. `table` and `column` are the caller's identifiers, never user input."""
    whens = " ".join("WHEN ? THEN ?" for _ in mapping)
    marks = ", ".join("?" for _ in mapping)
    params = [value for pair in mapping.items() for value in pair] + list(mapping)
    return f"UPDATE {table} SET {column} = CASE {column} {whens} END WHERE {column} IN ({marks})", params
