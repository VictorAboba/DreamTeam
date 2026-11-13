"""Schema helpers of dreamteam.db: what this version creates (known_columns), what a file holds (columns_of,
unknown_columns), the highest frozen tool-set revision a session database records (max_tools_revision), whether an
earlier version made it (made_by_earlier_version) and the read that writes nothing next to a file (reading_file)."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from dreamteam import db as dbmod
from dreamteam import tracing
from dreamteam.db import (ADDED_COLUMNS, Database, columns_of, foreign_objects, known_columns, made_by_earlier_version,
                          max_tools_revision, reading_file, unknown_columns)
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


def _files(folder: Path) -> dict[str, tuple[int, int]]:
    """Every file of `folder`: name -> (size, mtime): what a read must leave as it is."""
    return {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(folder.iterdir())}


def _history(database: Database, agent: str) -> None:
    database.execute("INSERT INTO history(agent, seq, msg) VALUES (?, 1, '{}')", (agent,))


def test_made_by_earlier_version_is_false_without_a_file_or_anything_frozen(tmp_path):
    missing = tmp_path / "sessions" / "x" / "swarm.db"
    assert made_by_earlier_version(missing) is False and made_by_earlier_version(str(missing)) is False
    assert not missing.exists() and not missing.parent.exists()  # never created
    assert made_by_earlier_version(tmp_path) is False  # a folder is no database file
    bare = tmp_path / "bare.db"  # neither a kv nor a history table
    conn = sqlite3.connect(str(bare))
    conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY)")
    conn.close()
    assert made_by_earlier_version(bare) is False
    kv_only = tmp_path / "kv.db"
    conn = sqlite3.connect(str(kv_only))
    conn.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO kv(key, value) VALUES ('goal', '{\"rev\": 1}')")
    conn.commit()
    conn.close()
    assert made_by_earlier_version(kv_only) is False
    database = _fresh(tmp_path / "swarm.db")  # only the user, external members or members never started
    try:
        database.execute("INSERT INTO agents(name, kind, status, created_at) VALUES ('ext', 'external', 'idle', 1)")
        database.set("goal", {"rev": 1})
        database.set("tools", {"rev": 1})  # no member name: not a tools:<member> record
        assert made_by_earlier_version(database) is False
    finally:
        database.close()
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"not a database at all, just text " * 40)
    with pytest.raises(sqlite3.DatabaseError):
        made_by_earlier_version(garbage)


@pytest.mark.parametrize("value, earlier", [
    ({"rev": 4, "language": "en", "tools": []}, True),
    ({"rev": 4}, True),  # an earlier revision, valid or not
    ({"rev": 1, "language": "ru", "tools": []}, True),
    ({"rev": frozen.OLDEST_OPEN_REVISION, "language": "en", "tools": []}, False),
    ({"rev": 99, "language": "en", "tools": []}, False),  # a newer DreamTeam's (refused elsewhere)
    ({"rev": True, "tools": []}, False),  # no integer revision: only a history makes it count
    ({"rev": "4", "tools": []}, False),
    ("not a record", False),
])
def test_made_by_earlier_version_by_a_record_without_a_history(tmp_path, value, earlier):
    database = _fresh(tmp_path / "swarm.db")
    try:
        database.set(frozen.kv_key("Anna"), frozen.current_record("en"))
        _history(database, "Anna")
        database.set(frozen.kv_key("Retired Bob"), value)  # no agents row: a member that left counts too
        assert made_by_earlier_version(database) is earlier
    finally:
        database.close()
    assert made_by_earlier_version(tmp_path / "swarm.db") is earlier


def test_made_by_earlier_version_by_a_history_without_a_usable_record(tmp_path):
    database = _fresh(tmp_path / "swarm.db")
    try:
        database.set(frozen.kv_key("Anna"), frozen.current_record("en"))
        _history(database, "Anna")
        assert made_by_earlier_version(database) is False  # a history with a valid record of this version
        _history(database, "Bob")
        assert made_by_earlier_version(database) is True  # no record: the baseline revision
        for broken in ({"rev": 5, "tools": "all"}, {"rev": 5, "tools": [{"name": "wait"}]}, {"rev": True, "tools": []},
                       {"rev": "5", "tools": []}, ["rev", 5], "text"):
            database.set(frozen.kv_key("Bob"), broken)
            assert not frozen.valid(broken) and made_by_earlier_version(database) is True, broken
        database.execute("UPDATE kv SET value = '{not json' WHERE key = ?", (frozen.kv_key("Bob"),))
        assert made_by_earlier_version(database) is True
        database.set(frozen.kv_key("Bob"), frozen.current_record("ru"))
        assert made_by_earlier_version(database) is False
        database.execute("DELETE FROM kv")  # a history alone, every record gone
        assert made_by_earlier_version(database) is True
    finally:
        database.close()


def test_made_by_earlier_version_reads_every_kind_of_source(tmp_path):
    path = tmp_path / "swarm.db"
    database = _fresh(path)
    try:
        database.set(frozen.kv_key("Anna"), frozen.record(4, "en", []))
        assert made_by_earlier_version(database) is True
    finally:
        database.close()
    conn = sqlite3.connect(str(path))
    try:
        assert made_by_earlier_version(conn) is True
        assert conn.execute("SELECT 1").fetchone() == (1,)  # a given connection stays open
    finally:
        conn.close()
    assert made_by_earlier_version(path) is True and made_by_earlier_version(str(path)) is True


def test_a_cleanly_closed_database_is_read_without_a_file_next_to_it(tmp_path):
    folder = tmp_path / "data"
    folder.mkdir()
    path = folder / "swarm.db"
    database = _fresh(path)
    _history(database, "Anna")
    database.close()
    before = _files(folder)
    assert list(before) == ["swarm.db"]
    assert made_by_earlier_version(path) is True
    assert "tasks.crew" in columns_of(path) and max_tools_revision(path) is None and foreign_objects(path) == []
    with reading_file(path) as (conn, immutable):
        assert immutable is True and conn.execute("SELECT agent FROM history").fetchall() == [("Anna",)]
    assert _files(folder) == before  # no -wal or -shm left behind, the file unchanged


def test_a_database_with_only_its_wal_is_read_from_a_private_copy(tmp_path):
    live = tmp_path / "live"
    live.mkdir()
    database = _fresh(live / "swarm.db")
    folder = tmp_path / "data"
    folder.mkdir()
    try:
        database.execute("PRAGMA wal_autocheckpoint=0")
        _history(database, "Anna")  # still in the WAL
        assert (live / "swarm.db-wal").stat().st_size > 0
        for name in ("swarm.db", "swarm.db-wal"):  # a copy that skipped the -shm (or a crash)
            shutil.copyfile(live / name, folder / name)
    finally:
        database.close()
    path = folder / "swarm.db"
    before = _files(folder)
    assert list(before) == ["swarm.db", "swarm.db-wal"]
    assert made_by_earlier_version(path) is True  # the history row of the WAL is read
    assert "tasks.crew" in columns_of(path)
    with reading_file(path) as (conn, immutable):
        assert immutable is False and conn.execute("SELECT agent FROM history").fetchall() == [("Anna",)]
    assert _files(folder) == before  # still its two files, unchanged


def test_a_database_with_both_its_files_is_read_through_them(tmp_path):
    path = tmp_path / "swarm.db"
    database = _fresh(path)
    try:
        database.execute("PRAGMA wal_autocheckpoint=0")
        _history(database, "Anna")
        assert {"swarm.db", "swarm.db-wal", "swarm.db-shm"} <= set(_files(tmp_path))
        with reading_file(path, timeout=5.0) as (conn, immutable):
            assert immutable is False and conn.execute("SELECT agent FROM history").fetchall() == [("Anna",)]
        assert made_by_earlier_version(path) is True
        database.set(frozen.kv_key("Anna"), frozen.current_record("en"))
        assert made_by_earlier_version(path) is False  # the record, also only in the WAL, is seen
    finally:
        database.close()


def test_trace_schema_is_part_of_the_known_columns():
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(tracing.SCHEMA)
        assert columns_of(conn) <= known_columns()
        assert {"traces.model", "spans.meta"} <= columns_of(conn)
    finally:
        conn.close()
