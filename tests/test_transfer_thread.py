"""Export and import of thread sessions (docs/SPEC.md §22.9 "Export and import (§20)"): the forum tables travel
inside swarm.db, the manifest names each session's mode and the tool-set revisions of both modes, a session keeps
the mode of its database (never switched by an import, never taken from the home's default), and a board session
this version makes stays importable by 0.4.x. ScriptedLLM, tmp_path homes, no network."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from dreamteam import transfer
from dreamteam.db import ADDED_COLUMNS, FORUM_SCHEMA, SCHEMA, max_tools_revision, session_mode
from dreamteam.tools import frozen
from dreamteam.tools.frozen import TOOLSET_REVISIONS
from dreamteam.tracing import SCHEMA as TRACE_SCHEMA
from dreamteam.transfer import MANIFEST, export_home, plan_import, render_export_report, render_import_plan
from test_transfer import (_db, _event, _fill_db, _home_named, _import, _item, _manager, _manifest, _raw, _raw_b,
                           _session, _set_db_sha, _statuses)

FORUM_TABLES = ("posts", "post_comments", "votes", "subscriptions", "read_marks")
DECISION = {"rule": "veto", "for": ["alice", "bob"], "against": [], "counted": 2, "wrap_up": False}
MIXED = "its settings named another mode than its database; the database's ({mode}) is kept"


def _thread_overrides(rule: str = "veto") -> dict[str, Any]:
    return {"swarm": {"mode": "thread", "goal_rule": rule}}


def _raw_thread_default() -> dict[str, Any]:
    raw = _raw_b()
    raw["swarm"] = {**raw["swarm"], "mode": "thread", "goal_rule": "democracy"}
    return raw


def _db_path(home: Path, sid: str) -> Path:
    return home / "sessions" / sid / ".dreamteam" / "swarm.db"


def _fill_forum(home: Path, sid: str) -> None:
    """A thread session that worked: two members with their thread tool sets, the system posts #1 and #2, a post
    with comments (one a reply), votes on posts and on a comment, subscriptions and a goal decided by the vote."""
    db = _db(home, sid)
    try:
        for name, created in (("alice", 1.0), ("bob", 1.5)):
            db.execute("INSERT INTO agents(name, kind, role, language, status, created_at) "
                       "VALUES (?, 'llm', 'Coder', 'en', 'idle', ?)", (name, created))
            frozen.write(db, name, frozen.current_record("en", "thread"))
        db.execute("INSERT INTO history(agent, seq, msg) VALUES ('alice', 1, ?)", (json.dumps({"text": "hi"}),))
        db.execute("INSERT INTO events(ts, type, actor, data) VALUES (1.0, 'session.started', 'user', '{}')")
        db.execute("INSERT INTO events(ts, type, actor, data) VALUES (2.0, 'post.created', 'alice', '{}')")
        db.execute("INSERT INTO posts(id, author, kind, title, text, data, created_at, active_at) VALUES "
                   "(1, NULL, 'goal', 'Goal', 'Ship the parser', NULL, 1.0, 6.0), "
                   "(2, NULL, 'plan', 'Plan', 'Who does what', '{\"clarify\": false, \"asker\": null}', 1.0, 1.0)")
        db.execute("INSERT INTO posts(author, kind, title, text, created_at, edited_at, active_at) "
                   "VALUES ('alice', 'post', 'Parser draft', 'In src/parser.py', 2.0, 2.5, 4.0)")
        db.execute("INSERT INTO post_comments(post_id, author, text, reply_to, ts) VALUES (3, 'bob', 'Looks good', "
                   "NULL, 3.0), (3, 'alice', 'Thanks, @bob', 1, 4.0)")
        db.execute("INSERT INTO votes(target, voter, value, ts) VALUES ('p1', 'alice', 1, 5.0), ('p1', 'bob', 1, 6.0), "
                   "('c1', 'alice', 1, 3.5), ('p3', 'bob', -1, 3.2)")
        db.execute("INSERT INTO subscriptions(member, kind, target, by_member, mode, auto, created_at) VALUES "
                   "('alice', 'feed', '', '', 'digest', 1, 1.0), ('alice', 'post', '3', '', 'wake', 0, 2.0), "
                   "('bob', 'file', 'src/*.py', '', 'wake', 0, 2.1), ('bob', 'feed', '', 'alice', 'wake', 0, 2.2)")
        db.execute("INSERT INTO read_marks(member, target, version, ts) VALUES ('alice', 'p3', '0123456789abcdef', "
                   "2.5), ('bob', 'p3', NULL, 2.6), ('bob', 'c2', NULL, 4.1)")
        db.set("goal", {"title": "Ship the parser", "description": "", "status": "done", "summary": "Decided by vote",
                        "completed_by": None, "decision": DECISION, "created_at": 1.0, "updated_at": 6.0})
    finally:
        db.close()


def _forum_state(path: Path) -> dict[str, list[tuple]]:
    """The rows of the forum tables, kv (the mode, the goal, the tool records) and the posts' sequence."""
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?immutable=1", uri=True)
    try:
        state = {table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() for table in FORUM_TABLES}
        state["kv"] = conn.execute("SELECT key, value FROM kv ORDER BY key").fetchall()
        state["sequence"] = conn.execute("SELECT name, seq FROM sqlite_sequence WHERE name LIKE 'post%' "
                                         "ORDER BY name").fetchall()
        return state
    finally:
        conn.close()


def _known_to_041() -> tuple[set[str], set[tuple[str, str]]]:
    """The columns and the indexes 0.4.x knows (its db.known_columns / known_objects): SCHEMA, the trace tables and
    ADDED_COLUMNS, which this version keeps as they were; the forum tables are not in them."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(SCHEMA)
        conn.executescript(TRACE_SCHEMA)
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE "
                                             "'sqlite\\_%' ESCAPE '\\'")]
        columns = {f"{t}.{r[1]}" for t in tables for r in conn.execute(f"PRAGMA table_info({t})")}
        objects = {(str(a), str(b)) for a, b in conn.execute(
            "SELECT type, name FROM sqlite_master WHERE type IN ('index', 'trigger', 'view') AND name NOT LIKE "
            "'sqlite\\_%' ESCAPE '\\'")}
        return columns | {f"{t}.{c}" for t, c, _ in ADDED_COLUMNS}, objects
    finally:
        conn.close()


def _schema_of(path: Path) -> tuple[set[str], set[tuple[str, str]]]:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?immutable=1", uri=True)
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE "
                                             "'sqlite\\_%' ESCAPE '\\'")]
        columns = {f"{t}.{r[1]}" for t in tables for r in conn.execute(f"PRAGMA table_info({t})")}
        objects = {(str(a), str(b)) for a, b in conn.execute(
            "SELECT type, name FROM sqlite_master WHERE type IN ('index', 'trigger', 'view') AND name NOT LIKE "
            "'sqlite\\_%' ESCAPE '\\'")}
        return columns, objects
    finally:
        conn.close()


def _edit_db(path: Path, *sql: str) -> None:
    conn = sqlite3.connect(path)
    try:
        for statement in sql:
            conn.executescript(statement)
        conn.commit()
    finally:
        conn.close()


def _session_json(folder: Path, sid: str) -> dict[str, Any]:
    return json.loads((folder / "sessions" / sid / "session.json").read_text(encoding="utf-8"))


def _write_session_json(folder: Path, sid: str, meta: dict[str, Any]) -> None:
    (folder / "sessions" / sid / "session.json").write_text(json.dumps(meta), encoding="utf-8")


async def _two_sessions(tmp_path: Path) -> tuple[Path, str, str]:
    """Home A (board default) with a thread session that worked and a board session that worked."""
    home = _home_named(tmp_path, "a", _raw())
    manager = _manager(home)
    thread = await _session(manager, "Forum", overrides=_thread_overrides())
    board = await _session(manager, "Board")
    await manager.shutdown()
    _fill_forum(home, thread)
    _fill_db(home, board)
    return home, thread, board


# ---- a thread session travels intact --------------------------------------------------------------------------
async def test_a_thread_session_is_exported_and_imported_with_its_forum_and_stays_thread(tmp_path):
    home_a, thread, board = await _two_sessions(tmp_path)
    source = _forum_state(_db_path(home_a, thread))
    assert len(source["posts"]) == 3 and len(source["votes"]) == 4 and len(source["subscriptions"]) == 4
    assert len(source["read_marks"]) == 3   # the read marks travel inside swarm.db (docs/SPEC.md §22.11)
    dest = tmp_path / "usb"
    report = export_home(home_a, dest, include=["sessions"])
    assert report.ok
    manifest = _manifest(dest)
    assert manifest["schema"]["toolset_revisions"] == {"board": 5, "thread": 6} == TOOLSET_REVISIONS
    assert manifest["schema"]["toolset_revision"] == 6
    assert {f"{t}.id" for t in ("posts", "post_comments", "subscriptions")} <= set(manifest["schema"]["db_columns"])
    entries = {e["id"]: e for e in manifest["sessions"]}
    assert (entries[thread]["swarm_mode"], entries[board]["swarm_mode"]) == ("thread", "board")
    assert entries[thread]["max_tools_rev"] == 6
    assert f"            thread mode: {thread}" in render_export_report(report).splitlines()
    assert _forum_state(dest / "sessions" / thread / ".dreamteam" / "swarm.db") == source

    home_b = _home_named(tmp_path, "b", _raw_b())   # its default is board
    manager = _manager(home_b)
    plan = plan_import(manager, dest)
    text = render_import_plan(plan)
    assert f"  sessions  {thread} (thread): add" in text and f"  sessions  {board}: add" in text
    manager, plan, result = await _import(home_b, dest)
    assert result.ok and _statuses(result) == {("sessions", thread): "added", ("sessions", board): "added"}
    assert _forum_state(_db_path(home_b, thread)) == source
    assert manager.stored_mode(thread) == "thread" and manager.stored_mode(board) == "board"
    assert manager.meta(thread)["overrides"]["swarm"] == {"mode": "thread", "goal_rule": "veto"}
    assert "swarm" not in manager.meta(board)["overrides"]
    assert manager.config_for(thread).swarm.mode == "thread" and manager.config_for(thread).swarm.goal_rule == "veto"
    summary = manager.summary(thread)
    assert (summary["mode"], summary["goal_rule"], summary["posts"]) == ("thread", "veto", 1)
    assert summary["goal"]["status"] == "done" and summary["goal"]["decision"] == DECISION
    assert manager.summary(board)["mode"] == "board"
    # the same export again: nothing to change
    assert {i.name: i.action for i in plan_import(manager, dest).items} == {thread: "unchanged", board: "unchanged"}

    # more work on A: the rewrite brings it, the session stays thread
    _edit_db(_db_path(home_a, thread), "INSERT INTO post_comments(post_id, author, text, reply_to, ts) "
                                       "VALUES (1, 'bob', 'Done from my side', NULL, 7.0)")
    _event(home_a, thread, 7.0, "post.commented")
    newer = _forum_state(_db_path(home_a, thread))
    export_home(home_a, tmp_path / "usb2", include=["sessions"])
    item = _item(plan_import(manager, tmp_path / "usb2").items, "sessions", thread)
    assert item.action == "rewrite" and item.reason.startswith("the export has newer work in it")
    manager, plan, result = await _import(home_b, tmp_path / "usb2")
    assert _statuses(result)[("sessions", thread)] == "rewritten"
    assert _forum_state(_db_path(home_b, thread)) == newer and len(newer["post_comments"]) == 3
    assert manager.stored_mode(thread) == "thread"
    assert manager.meta(thread)["overrides"]["swarm"] == {"mode": "thread", "goal_rule": "veto"}

    swarm = await manager.start(thread)   # this version opens it as the thread session it is
    try:
        assert swarm.mode == "thread" and swarm.goal_rule == "veto"
    finally:
        await manager.shutdown()


async def test_a_never_started_thread_session_carries_its_forum_tables_and_mode(tmp_path):
    home_a = _home_named(tmp_path, "a", _raw())
    manager = _manager(home_a)
    sid = await _session(manager, "Fresh", overrides=_thread_overrides("democracy"))
    await manager.shutdown()
    export_home(home_a, tmp_path / "usb", include=["sessions"])
    assert _manifest(tmp_path / "usb")["sessions"][0]["swarm_mode"] == "thread"
    home_b = _home_named(tmp_path, "b", _raw_b())
    manager, plan, result = await _import(home_b, tmp_path / "usb")
    assert result.ok and session_mode(_db_path(home_b, sid)) == "thread"
    assert set(FORUM_TABLES) <= {c.split(".")[0] for c in _schema_of(_db_path(home_b, sid))[0]}
    assert manager.meta(sid)["overrides"]["swarm"] == {"mode": "thread", "goal_rule": "democracy"}
    await manager.shutdown()


# ---- board sessions and 0.4.x ---------------------------------------------------------------------------------
async def test_a_041_archive_imports_as_board_sessions_into_a_home_whose_default_is_thread(tmp_path):
    home_a = _home_named(tmp_path, "a", _raw())
    manager_a = _manager(home_a)
    ran = await _session(manager_a, "Ran")
    fresh = await _session(manager_a, "Fresh")   # never started: no database
    await manager_a.shutdown()
    _fill_db(home_a, ran)
    dest = tmp_path / "usb"
    export_home(home_a, dest, include=["sessions"])
    # as 0.4.1 wrote it: tool records without "mode", no kv swarm_mode, a manifest without the modes
    copied = dest / "sessions" / ran / ".dreamteam" / "swarm.db"
    record = {k: v for k, v in frozen.current_record("en").items() if k != "mode"}
    conn = sqlite3.connect(copied)
    try:
        conn.execute("UPDATE kv SET value = ? WHERE key = 'tools:alice'", (json.dumps(record),))
        conn.execute("DELETE FROM kv WHERE key = 'swarm_mode'")
        conn.commit()
    finally:
        conn.close()
    _set_db_sha(dest, ran)
    manifest = _manifest(dest)
    known, _objects = _known_to_041()
    manifest.update(dreamteam_version="0.4.1", schema={"db_columns": sorted(known), "toolset_revision": 5})
    for entry in manifest["sessions"]:
        del entry["swarm_mode"]
    (dest / MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")

    home_b = _home_named(tmp_path, "b", _raw_thread_default())
    manager, plan, result = await _import(home_b, dest)
    assert result.ok and _statuses(result) == {("sessions", ran): "added", ("sessions", fresh): "added"}
    assert "(thread)" not in render_import_plan(plan)
    for sid in (ran, fresh):
        assert manager.stored_mode(sid) == "board", sid
        assert "swarm" not in manager.meta(sid)["overrides"], sid
        assert manager.config_for(sid).swarm.mode == "board", sid
        assert manager.summary(sid)["mode"] == "board", sid
    swarm = await manager.start(fresh)   # its first start fixes it as board, whatever the home's default
    try:
        assert swarm.mode == "board" and session_mode(swarm.db) == "board"
    finally:
        await manager.shutdown()


async def test_a_board_session_of_this_version_stays_as_041_knows_it(tmp_path):
    """No swarm.mode in session.json, no forum table, tool records of revision 5, columns and indexes 0.4.x knows:
    0.4.x imports its export (its db.unknown_columns, foreign_objects and revision checks pass)."""
    home = _home_named(tmp_path, "a", {**_raw(), "mcp": {}})
    manager = _manager(home)
    sid = await _session(manager, "Board")
    assert "swarm" not in manager.meta(sid)["overrides"]
    swarm = await manager.start(sid)   # a run of this version: kv swarm_mode and the members' records
    for _ in range(200):   # (until its first agent has frozen its tool set)
        if max_tools_revision(swarm.db) is not None:
            break
        await asyncio.sleep(0.02)
    assert session_mode(swarm.db) == "board" and max_tools_revision(swarm.db) == 5
    await manager.shutdown()
    dest = tmp_path / "usb"
    export_home(home, dest, include=["sessions"])
    session = _session_json(dest, sid)
    assert "swarm" not in session["overrides"] and "agents" not in session["overrides"]
    copied = dest / "sessions" / sid / ".dreamteam" / "swarm.db"
    columns, objects = _schema_of(copied)
    known, known_objects = _known_to_041()
    assert columns <= known and objects <= known_objects
    assert not {c.split(".")[0] for c in columns} & set(FORUM_TABLES)
    assert max_tools_revision(copied) == TOOLSET_REVISIONS["board"] == 5
    assert _manifest(dest)["sessions"][0]["swarm_mode"] == "board"


# ---- the mode is never switched -------------------------------------------------------------------------------
async def test_a_same_uid_target_of_the_other_mode_is_refused(tmp_path):
    home_a, thread, board = await _two_sessions(tmp_path)
    dest = tmp_path / "usb"
    export_home(home_a, dest, include=["sessions"])
    home_b = _home_named(tmp_path, "b", _raw_b())
    manager, _plan, result = await _import(home_b, dest)
    assert result.ok
    # by hand on B: the thread copy turned board, the board copy turned thread
    _edit_db(_db_path(home_b, thread), "DELETE FROM kv WHERE key = 'swarm_mode'")
    meta = json.loads((home_b / "sessions" / thread / "session.json").read_text(encoding="utf-8"))
    meta["overrides"].pop("swarm")
    (home_b / "sessions" / thread / "session.json").write_text(json.dumps(meta), encoding="utf-8")
    _edit_db(_db_path(home_b, board), FORUM_SCHEMA,
             "INSERT OR REPLACE INTO kv(key, value) VALUES ('swarm_mode', '\"thread\"')")
    manager = _manager(home_b)
    assert manager.stored_mode(thread) == "board" and manager.stored_mode(board) == "thread"
    before = {sid: _forum_state(_db_path(home_b, sid)) for sid in (thread, board)}
    plan = plan_import(manager, dest)
    reasons = {i.name: (i.action, i.reason) for i in plan.items}
    tail = ": the mode is chosen when a session is created; delete one copy or import it as a new session"
    assert reasons[thread] == ("refuse", f"its mode differs here (board/thread){tail}")
    assert reasons[board] == ("refuse", f"its mode differs here (thread/board){tail}")
    manager, plan, result = await _import(home_b, dest)
    assert _statuses(result) == {("sessions", thread): "refused", ("sessions", board): "refused"}
    assert {sid: _forum_state(_db_path(home_b, sid)) for sid in (thread, board)} == before
    await manager.shutdown()


async def test_settings_naming_another_mode_than_the_database_get_the_databases(tmp_path):
    home_a, thread, board = await _two_sessions(tmp_path)
    manager_a = _manager(home_a)
    plain = await _session(manager_a, "Plain")
    await manager_a.shutdown()
    _fill_db(home_a, plain)
    dest = tmp_path / "usb"
    export_home(home_a, dest, include=["sessions"])
    for sid, swarm in ((thread, {"goal_rule": "veto"}), (board, {"mode": "thread", "max_agents": 2}),
                       (plain, {"mode": "board"})):
        meta = _session_json(dest, sid)
        meta["overrides"]["swarm"] = swarm
        _write_session_json(dest, sid, meta)
    home_b = _home_named(tmp_path, "b", _raw_thread_default())
    manager = _manager(home_b)
    plan = plan_import(manager, dest)
    warnings = render_import_plan(plan)
    assert f"session {thread}: {MIXED.format(mode='thread')}" in warnings
    assert f"session {board}: {MIXED.format(mode='board')}" in warnings
    assert f"session {plain}: its settings" not in warnings   # "board" on a board database is only dropped
    manager, plan, result = await _import(home_b, dest)
    assert result.ok
    assert manager.meta(thread)["overrides"]["swarm"] == {"goal_rule": "veto", "mode": "thread"}
    assert manager.meta(board)["overrides"]["swarm"] == {"max_agents": 2}
    assert "swarm" not in manager.meta(plain)["overrides"]
    assert [manager.stored_mode(s) for s in (thread, board, plain)] == ["thread", "board", "board"]
    # what the import wrote is what the plan compares: the same export again changes nothing
    assert {i.action for i in plan_import(manager, dest).items} == {"unchanged"}
    await manager.shutdown()


# ---- what this version does not know -------------------------------------------------------------------------
@pytest.mark.parametrize("change, reason", [
    ("UPDATE kv SET value = json_set(value, '$.mode', 'forest') WHERE key = 'tools:alice'",
     "its agents use a tool set of a mode this DreamTeam does not know: update DreamTeam here"),
    ("UPDATE kv SET value = json_set(value, '$.rev', 7) WHERE key = 'tools:alice'",
     "its agents use the tool set 7, which this DreamTeam does not know (it knows up to 6): update DreamTeam here"),
    ("UPDATE kv SET value = '\"forest\"' WHERE key = 'swarm_mode'",
     "its mode 'forest' is unknown: it was made by a newer DreamTeam: update DreamTeam here"),
])
async def test_a_tool_set_or_a_mode_this_version_does_not_know_is_refused(tmp_path, change, reason):
    home_a, thread, board = await _two_sessions(tmp_path)
    dest = tmp_path / "usb"
    export_home(home_a, dest, include=["sessions"])
    _edit_db(dest / "sessions" / thread / ".dreamteam" / "swarm.db", change)
    _set_db_sha(dest, thread)
    home_b = _home_named(tmp_path, "b", _raw_b())
    manager, plan, result = await _import(home_b, dest)
    assert _item(plan.items, "sessions", thread).reason == reason
    assert _statuses(result) == {("sessions", thread): "refused", ("sessions", board): "added"}
    assert not (home_b / "sessions" / thread).exists()
    await manager.shutdown()


def test_the_record_modes_reader_ignores_what_is_not_a_record(tmp_path):
    path = tmp_path / "x.db"
    _edit_db(path, "CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT)",
             "INSERT INTO kv VALUES ('tools:a', '{\"rev\": 5}'), ('tools:b', 'not json'), "
             "('tools:c', '{\"rev\": 6, \"mode\": \"thread\"}'), ('other', '{\"mode\": \"x\"}'), "
             "('tools:d', '[1, 2]'), ('tools:e', '{\"mode\": [\"x\"]}')")
    assert transfer._record_modes(path) == {"thread", "['x']"}
    _edit_db(tmp_path / "empty.db", "CREATE TABLE t (a)")
    assert transfer._record_modes(tmp_path / "empty.db") == set()
