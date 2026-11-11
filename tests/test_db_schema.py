"""Schema helpers of dreamteam.db: what this version creates (known_columns), what a file holds (columns_of,
unknown_columns) and the highest frozen tool-set revision a session database records (max_tools_revision)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from dreamteam import db as dbmod
from dreamteam import tracing
from dreamteam.db import (ADDED_COLUMNS, Database, columns_of, known_columns, max_tools_revision, unknown_columns)
from dreamteam.models import CREW_MAX, Task
from dreamteam.tools import frozen
from dreamteam.tracing import Tracer


def _pragma_columns(path: Path) -> set[str]:
    conn = sqlite3.connect(str(path))
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
                  if not r[0].startswith("sqlite_")]
        return {f"{t}.{r[1]}" for t in tables for r in conn.execute(f'PRAGMA table_info("{t}")')}
    finally:
        conn.close()


def _fresh(path: Path) -> Database:
    database = Database(path)
    Tracer(database)
    return database


def _older(path: Path, dropped: set[tuple[str, str]]) -> None:
    """A database as an older version made it: SCHEMA without the trace tables and without the `dropped` columns."""
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(dbmod.SCHEMA)
        for table, column in sorted(dropped):
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    finally:
        conn.close()


def test_known_columns_are_what_a_fresh_database_and_tracer_create(tmp_path):
    path = tmp_path / "swarm.db"
    _fresh(path).close()
    known = known_columns()
    assert isinstance(known, frozenset) and known is known_columns()  # computed once
    assert known == _pragma_columns(path) == columns_of(path)
    assert {"kv.key", "events.data", "mcp_servers.approved_digest", "spans.usage", "history_archive.epoch",
            "jobs.tail"} <= known
    assert {f"{t}.{c}" for t, c, _ in ADDED_COLUMNS} <= known
    assert not any(name.startswith("sqlite_") for name in known)  # sqlite_sequence (AUTOINCREMENT) is SQLite's
    assert unknown_columns(path) == []


def test_an_older_database_is_a_strict_subset_and_gets_upgraded(tmp_path):
    path = tmp_path / "old.db"
    _older(path, {("mcp_servers", "digest"), ("mcp_servers", "approved_digest"), ("messages", "to_all"),
                  ("tasks", "crew")})
    conn = sqlite3.connect(str(path))
    try:  # a task an older version made
        conn.execute("INSERT INTO tasks(title, status, created_at, updated_at) VALUES ('old', 'todo', 1, 1)")
        conn.commit()
    finally:
        conn.close()
    old = columns_of(path)
    assert old < known_columns()
    assert known_columns() - old >= {"mcp_servers.digest", "mcp_servers.approved_digest", "messages.to_all",
                                     "tasks.crew", "traces.id", "spans.input"}
    assert unknown_columns(path) == []
    database = _fresh(path)  # opening it adds the columns and tables
    try:
        row = database.one("SELECT * FROM tasks WHERE title = 'old'")
        assert row["crew"] == 0  # the column's default: an ordinary task
        task = Task.from_row(row)
        assert task.crew == 0 and task.crew_working == []
    finally:
        database.close()
    assert columns_of(path) == known_columns()


def test_tasks_crew_is_stored_and_read_into_the_task(tmp_path):
    """`tasks.crew` (0 = an ordinary task, 2..CREW_MAX = a crew task) is read into `Task.crew`; `crew_working` is
    filled by the board, never stored; both are in `to_dict()`, and `short()` names a crew task's working set."""
    assert CREW_MAX == 8
    database = _fresh(tmp_path / "swarm.db")
    try:
        database.execute("INSERT INTO tasks(title, status, assignee, created_at, updated_at, crew)"
                         " VALUES ('big', 'in_progress', 'Anna', 1, 2, 3)")
        database.execute("INSERT INTO tasks(title, status, created_at, updated_at) VALUES ('plain', 'todo', 1, 1)")
        big, plain = (Task.from_row(database.one("SELECT * FROM tasks WHERE title = ?", (title,)))
                      for title in ("big", "plain"))
        legacy = Task.from_row(database.one("SELECT id, title, goal, description, status, assignee, reviewer,"
                                            " created_by, parent_id, depends_on, priority, tags, result, created_at,"
                                            " updated_at FROM tasks WHERE title = 'big'"))
    finally:
        database.close()
    assert big.crew == 3 and plain.crew == 0 and legacy.crew == 0  # a row without the column: an ordinary task
    assert big.crew_working == []
    assert big.to_dict()["crew"] == 3 and big.to_dict()["crew_working"] == []
    assert big.short() == f"#{big.id} [in_progress] big @Anna crew 0/3"
    big.crew_working = ["Anna", "Bob"]
    big.status, big.reviewer = "review", "Carl"
    assert big.short() == f"#{big.id} [review] big @Anna review:Carl crew 2/3"
    assert "crew" not in plain.short()
    plain.crew = 1  # below 2: not a crew task
    assert "crew" not in plain.short()
    assert "tasks.crew" in known_columns() and "tasks.crew_working" not in known_columns()


def test_a_database_of_a_newer_version_shows_its_unknown_columns(tmp_path):
    path = tmp_path / "new.db"
    database = _fresh(path)
    database.execute("ALTER TABLE tasks ADD COLUMN squad TEXT")
    database.execute("CREATE TABLE \"odd \"\"name\" (a TEXT, \"b c\" INTEGER)")
    assert unknown_columns(database) == ['odd "name.a', 'odd "name.b c', "tasks.squad"]
    database.close()
    assert unknown_columns(path) == ['odd "name.a', 'odd "name.b c', "tasks.squad"]
    assert columns_of(path) - known_columns() == {"tasks.squad", 'odd "name.a', 'odd "name.b c'}


def test_columns_of_reads_a_file_without_changing_it(tmp_path):
    path = tmp_path / "old.db"
    _older(path, {("messages", "to_all")})
    before = path.read_bytes()
    assert "messages.to_all" not in columns_of(path)
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["old.db"]  # no journal or wal file left behind
    missing = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError):
        columns_of(missing)
    assert not missing.exists()  # never created
    with pytest.raises(FileNotFoundError):
        columns_of(tmp_path)  # a folder is not a database file
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"not a database at all, just text " * 40)
    with pytest.raises(sqlite3.DatabaseError):
        columns_of(garbage)


def test_columns_of_sees_an_open_database_in_wal_mode(tmp_path):
    path = tmp_path / "swarm.db"
    database = _fresh(path)
    try:
        database.execute("ALTER TABLE agents ADD COLUMN extra TEXT")  # still in the WAL
        assert "agents.extra" in columns_of(path)
        conn = sqlite3.connect(str(path))
        try:
            assert "agents.extra" in columns_of(conn)  # a given connection stays open
            assert conn.execute("SELECT 1").fetchone() == (1,)
        finally:
            conn.close()
    finally:
        database.close()


def _tools_record(database: Database, member: str, rev) -> None:
    database.set(frozen.kv_key(member), {"rev": rev, "language": "en", "tools": []})


def test_max_tools_revision(tmp_path):
    path = tmp_path / "swarm.db"
    database = _fresh(path)
    try:
        assert max_tools_revision(database) is None  # no member started yet
        _tools_record(database, "alice", 1)
        _tools_record(database, "bob", frozen.TOOLSET_REVISION)
        database.set("goal", {"rev": 999})  # not a tool set
        database.set("tools", {"rev": 998})  # no member name: not a tools:<member> key
        database.set(frozen.kv_key("broken"), "not a record")
        database.set(frozen.kv_key("flag"), {"rev": True})
        database.set(frozen.kv_key("text"), {"rev": "99"})
        database.execute("INSERT INTO kv(key, value) VALUES(?, ?)", (frozen.kv_key("junk"), "{not json"))
        assert max_tools_revision(database) == frozen.TOOLSET_REVISION
        _tools_record(database, "carol", 99)
        assert max_tools_revision(database) == 99
    finally:
        database.close()
    assert max_tools_revision(path) == 99 and max_tools_revision(str(path)) == 99
    assert frozen.KV_PREFIX == "tools:" and frozen.kv_key("alice") == "tools:alice"


def test_max_tools_revision_of_a_database_without_kv(tmp_path):
    path = tmp_path / "bare.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY)")
    conn.close()
    assert max_tools_revision(path) is None
    assert columns_of(path) == {"events.id"} and unknown_columns(path) == []


def test_trace_schema_is_part_of_the_known_columns():
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(tracing.SCHEMA)
        assert columns_of(conn) <= known_columns()
        assert {"traces.model", "spans.meta"} <= columns_of(conn)
    finally:
        conn.close()
