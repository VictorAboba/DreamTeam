"""The thread mode's foundation (docs/SPEC.md §22.1, §22.8 "Settings keys"): the settings keys, the forum tables
(db.FORUM_SCHEMA), the mode a session's database records (kv swarm_mode, db.session_mode) and the Swarm's mode."""

from __future__ import annotations

import dataclasses
import logging
import re
import sqlite3
import tomllib
from pathlib import Path
from typing import Any

import pytest

from dreamteam import db as dbmod
from dreamteam.config import AgentSettings, Config, SwarmSettings, render_template
from dreamteam.db import (FORUM_SCHEMA, MODE_KEY, SCHEMA, Database, columns_of, foreign_objects, known_columns,
                          known_objects, session_mode, unknown_columns)
from dreamteam.forum import Forum
from dreamteam.swarm import Swarm

FORUM_TABLES = {"posts", "post_comments", "votes", "subscriptions"}
WARNING = "the session's mode is {}, chosen when it was created; [swarm] mode is ignored"


def make_config(tmp_path: Path, **sections: dict[str, Any]) -> Config:
    raw: dict[str, Any] = {
        "swarm": {"name": "Test swarm", "language": "en", "start_agents": 1, "max_cost_usd": 0},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
    }
    for section, values in sections.items():
        raw.setdefault(section, {}).update(values)
    return Config.from_dict(tmp_path / "project", raw)


def db_path(tmp_path: Path) -> Path:
    return tmp_path / "project" / ".dreamteam" / "swarm.db"


def tables(source) -> set[str]:
    conn = source.conn if isinstance(source, Database) else source
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def mode_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "[swarm] mode is ignored" in r.getMessage()]


# ---- settings keys (§22.8) ------------------------------------------------------------------------------------------

def test_the_settings_keys_and_their_defaults(tmp_path):
    assert (SwarmSettings().mode, SwarmSettings().goal_rule, AgentSettings().digest_seconds) == ("board", "democracy",
                                                                                                  60.0)
    names = [f.name for f in dataclasses.fields(SwarmSettings)]
    assert names[names.index("clarify"):names.index("clarify") + 3] == ["clarify", "mode", "goal_rule"]
    agents = [f.name for f in dataclasses.fields(AgentSettings)]
    assert agents[agents.index("trigger_wake_seconds") + 1] == "digest_seconds"
    cfg = make_config(tmp_path, swarm={"mode": "thread", "goal_rule": "veto"}, agents={"digest_seconds": 10})
    assert (cfg.swarm.mode, cfg.swarm.goal_rule, cfg.agents.digest_seconds) == ("thread", "veto", 10)


@pytest.mark.parametrize("sections, message", [
    ({"swarm": {"mode": "forum"}}, "swarm.mode must be 'board' or 'thread', got 'forum'"),
    ({"swarm": {"mode": "Thread"}}, "swarm.mode must be 'board' or 'thread', got 'Thread'"),
    ({"swarm": {"goal_rule": "majority"}}, "swarm.goal_rule must be 'democracy' or 'veto', got 'majority'"),
    ({"swarm": {"goal_rule": "user"}}, "swarm.goal_rule must be 'democracy' or 'veto', got 'user'"),
    ({"swarm": {"mode": 1}}, "[swarm] mode must be a string, got 1"),
    ({"agents": {"digest_seconds": 5}}, "[agents] digest_seconds must be >= 10, got 5"),
    ({"agents": {"digest_seconds": "60"}}, "[agents] digest_seconds must be a number, got '60'"),
])
def test_the_settings_keys_are_checked(tmp_path, sections, message):
    with pytest.raises(ValueError, match=re.escape(message)):
        make_config(tmp_path, **sections)


def test_the_other_lower_bounds_are_unchanged(tmp_path):
    with pytest.raises(ValueError, match=re.escape("[swarm] max_agents must be >= 1, got 0")):
        make_config(tmp_path, swarm={"max_agents": 0})
    with pytest.raises(ValueError, match=re.escape("[agents] idle_nudge_seconds must be >= 0, got -1")):
        make_config(tmp_path, agents={"idle_nudge_seconds": -1})
    zero = make_config(tmp_path, agents={"idle_nudge_seconds": 0, "trigger_wake_seconds": 0})
    assert zero.agents.idle_nudge_seconds == 0
    from dreamteam.config import _MIN, _MIN_ONE
    assert _MIN["digest_seconds"] == 10 and "digest_seconds" not in _MIN_ONE
    assert {key for key, low in _MIN.items() if low == 1} == _MIN_ONE and "max_agents" in _MIN_ONE


def test_the_template_has_the_three_lines():
    text = render_template("X", "ru", "anthropic")
    raw = tomllib.loads(text)
    assert (raw["swarm"]["mode"], raw["swarm"]["goal_rule"], raw["agents"]["digest_seconds"]) == ("board",
                                                                                                    "democracy", 60)
    lines = text.splitlines()
    at = next(i for i, line in enumerate(lines) if line.startswith("clarify = false"))
    assert lines[at + 1].startswith('mode = "board"                # board — доска задач; thread — лента обсуждений')
    assert lines[at + 2].startswith('goal_rule = "democracy"       # в режиме треда: democracy — ')
    assert "в треде — вопросы вам в обсуждении #2" in lines[at]
    wake = next(i for i, line in enumerate(lines) if line.startswith("trigger_wake_seconds = 180"))
    assert lines[wake + 1].startswith("digest_seconds = 60           # в режиме треда: подписка «дайджестом» будит")
    assert not any("ё" in lines[i] for i in (at, at + 1, at + 2, wake + 1))
    Config.from_dict(Path("."), raw)  # the rendered settings load


# ---- the forum tables (§22.1) ---------------------------------------------------------------------------------------

def test_the_forum_tables_are_not_in_schema(tmp_path):
    assert not any(f"TABLE IF NOT EXISTS {name} " in SCHEMA for name in FORUM_TABLES)
    board = Database(tmp_path / "board.db")
    try:
        assert not tables(board) & FORUM_TABLES
    finally:
        board.close()


def test_a_forum_database_has_the_tables_and_the_reserved_ids(tmp_path):
    database = Database(tmp_path / "thread.db", forum=True)
    try:
        assert FORUM_TABLES <= tables(database)
        assert database.scalar("SELECT seq FROM sqlite_sequence WHERE name = 'posts'") == 2
        post = database.insert("INSERT INTO posts(author, kind, title) VALUES ('Anna Smith', 'post', 'first')")
        assert post == 3  # every ordinary post is #3 or later
        # the system posts take their explicit ids, which leaves the sequence where it is
        database.execute("INSERT INTO posts(id, author, kind, title) VALUES (1, NULL, 'goal', 'Goal reached?')")
        database.execute("INSERT INTO posts(id, author, kind, title) VALUES (2, NULL, 'plan', 'Plan')")
        assert database.insert("INSERT INTO posts(kind, title) VALUES ('post', 'next')") == 4
        database.ensure_forum()  # idempotent: the seed does not run again
        database.ensure_forum()
        assert database.scalar("SELECT seq FROM sqlite_sequence WHERE name = 'posts'") == 4
        assert database.scalar("SELECT count(*) FROM posts") == 4
        row = database.one("SELECT text, data, edited_at FROM posts WHERE id = 3")
        assert (row["text"], row["data"], row["edited_at"]) == ("", None, None)
    finally:
        database.close()


def test_ensure_forum_on_an_open_board_database(tmp_path):
    database = Database(tmp_path / "x.db")
    try:
        database.insert("INSERT INTO tasks(title, status) VALUES ('t', 'todo')")
        database.ensure_forum()
        assert FORUM_TABLES <= tables(database)
        assert database.insert("INSERT INTO posts(kind, title) VALUES ('post', 'p')") == 3
        assert database.insert("INSERT INTO tasks(title, status) VALUES ('u', 'todo')") == 2  # its own sequence
    finally:
        database.close()


def test_the_subscription_key_and_the_vote_key(tmp_path):
    database = Database(tmp_path / "x.db", forum=True)
    try:
        upsert = ("INSERT INTO subscriptions(member, kind, target, by_member, mode) VALUES (?, ?, ?, ?, ?) "
                  "ON CONFLICT (member, kind, target, by_member) DO UPDATE SET mode = excluded.mode")
        database.execute(upsert, ("Anna Smith", "feed", "", "", "wake"))
        database.execute(upsert, ("Anna Smith", "feed", "", "", "digest"))
        database.execute("INSERT INTO subscriptions(member, kind, mode, auto) VALUES ('Anna Smith', 'feed', 'wake', 1) "
                         "ON CONFLICT DO NOTHING")
        rows = database.all("SELECT kind, target, by_member, mode, auto FROM subscriptions")
        assert [tuple(r) for r in rows] == [("feed", "", "", "digest", 0)]
        database.execute("INSERT INTO votes(target, voter, value) VALUES ('p3', 'Anna Smith', 1)")
        with pytest.raises(sqlite3.IntegrityError):
            database.execute("INSERT INTO votes(target, voter, value) VALUES ('p3', 'Anna Smith', -1)")
    finally:
        database.close()


def test_known_columns_and_objects_include_the_forum(tmp_path):
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(FORUM_SCHEMA)
        forum_columns = columns_of(conn)
    finally:
        conn.close()
    assert {"posts.active_at", "post_comments.reply_to", "votes.value", "subscriptions.by_member"} <= forum_columns
    assert forum_columns <= known_columns()
    assert {("index", "posts_active"), ("index", "post_comments_post"),
            ("index", "subscriptions_match")} <= known_objects()
    path = tmp_path / "thread.db"
    Database(path, forum=True).close()
    assert unknown_columns(path) == [] and foreign_objects(path) == []
    board = tmp_path / "board.db"
    Database(board).close()
    assert unknown_columns(board) == [] and not columns_of(board) & forum_columns


# ---- db.session_mode (§22.1) ----------------------------------------------------------------------------------------

def test_session_mode_reads_every_kind_of_source(tmp_path):
    assert session_mode(tmp_path / "missing.db") == "board"
    assert not (tmp_path / "missing.db").exists()  # never created
    bare = tmp_path / "bare.db"
    sqlite3.connect(str(bare)).close()  # no kv table
    assert session_mode(bare) == "board"
    path = tmp_path / "swarm.db"
    database = Database(path)
    try:
        assert session_mode(database) == "board"  # no key
        database.set(MODE_KEY, "thread")
        assert session_mode(database) == "thread" and session_mode(database.conn) == "thread"
        assert session_mode(path) == "thread"  # an open WAL database is read through its files
    finally:
        database.close()
    before = sorted(p.name for p in tmp_path.iterdir())
    assert session_mode(path) == "thread" and session_mode(str(path)) == "thread"
    assert sorted(p.name for p in tmp_path.iterdir()) == before  # nothing written next to the file
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("UPDATE kv SET value = ? WHERE key = ?", ('"x"', MODE_KEY))
        conn.commit()
        assert session_mode(conn) == "x"  # a newer DreamTeam's mode, the caller's to refuse
        conn.execute("UPDATE kv SET value = ? WHERE key = ?", ("not json", MODE_KEY))
        conn.commit()
        assert session_mode(conn) == "board"
    finally:
        conn.close()


# ---- the Swarm's mode (§22.1) ---------------------------------------------------------------------------------------

def test_a_new_board_swarm(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    sw = Swarm(config=make_config(tmp_path))
    try:
        assert sw.db.get(MODE_KEY) == "board"
        assert (sw.mode, sw.is_thread, sw.forum, sw.config.swarm.mode) == ("board", False, None, "board")
        assert sw.goal_rule == "democracy"  # read, though a board session ignores it
        assert not tables(sw.db) & FORUM_TABLES  # a board file stays as 0.4.x knows it
    finally:
        sw.close()
    assert mode_warnings(caplog) == []


def test_a_new_thread_swarm(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    sw = Swarm(config=make_config(tmp_path, swarm={"mode": "thread", "goal_rule": "veto"}))
    try:
        assert sw.db.get(MODE_KEY) == "thread" and session_mode(sw.db) == "thread"
        assert (sw.mode, sw.is_thread, sw.config.swarm.mode, sw.goal_rule) == ("thread", True, "thread", "veto")
        assert isinstance(sw.forum, Forum) and sw.forum.swarm is sw
        assert FORUM_TABLES <= tables(sw.db)
    finally:
        sw.close()
    assert mode_warnings(caplog) == []


@pytest.mark.parametrize("table, sql", [
    ("tasks", "INSERT INTO tasks(title, status) VALUES ('old', 'todo')"),
    ("messages", "INSERT INTO messages(ts, sender, recipients, text) VALUES (1, 'user', '[]', 'hi')"),
    ("events", "INSERT INTO events(ts, type, actor, data) VALUES (1, 'goal.set', 'user', '{}')"),
])
def test_a_used_database_without_the_key_is_board(tmp_path, caplog, table, sql):
    caplog.set_level(logging.WARNING)
    database = Database(db_path(tmp_path))  # made before the thread mode existed, or by a board run
    database.execute(sql)
    database.close()
    sw = Swarm(config=make_config(tmp_path, swarm={"mode": "thread"}))
    try:
        assert sw.db.get(MODE_KEY) == "board"
        assert (sw.mode, sw.forum, sw.config.swarm.mode) == ("board", None, "board")
        assert not tables(sw.db) & FORUM_TABLES
    finally:
        sw.close()
    assert mode_warnings(caplog) == [WARNING.format("board")]


def test_an_empty_database_without_the_key_takes_the_config(tmp_path):
    Database(db_path(tmp_path)).close()  # e.g. only the schema: no task, message or event
    sw = Swarm(config=make_config(tmp_path, swarm={"mode": "thread"}))
    try:
        assert sw.mode == "thread" and sw.db.get(MODE_KEY) == "thread"
    finally:
        sw.close()


def test_the_stored_mode_wins_over_the_config(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    database = Database(db_path(tmp_path), forum=True)  # as SessionManager.create makes a thread session
    database.set(MODE_KEY, "thread")
    database.close()
    sw = Swarm(config=make_config(tmp_path))  # [swarm] mode = board (the default)
    try:
        assert (sw.mode, sw.is_thread, sw.config.swarm.mode) == ("thread", True, "thread")
        assert isinstance(sw.forum, Forum)
        assert mode_warnings(caplog) == [WARNING.format("thread")]
        # reconfigure pins it the same way: a settings update never switches a running swarm; one warning per swarm
        sw.reconfigure(make_config(tmp_path, swarm={"goal_rule": "veto"}, agents={"digest_seconds": 30}))
        assert (sw.mode, sw.config.swarm.mode) == ("thread", "thread")
        assert sw.goal_rule == "veto" and sw.config.agents.digest_seconds == 30  # read live
        assert len(mode_warnings(caplog)) == 1
        sw.reconfigure(make_config(tmp_path, swarm={"mode": "thread"}))
        assert sw.config.swarm.mode == "thread" and sw.goal_rule == "democracy"
    finally:
        sw.close()
    assert session_mode(db_path(tmp_path)) == "thread"


def test_a_board_swarm_ignores_a_thread_config_at_reconfigure(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    sw = Swarm(config=make_config(tmp_path))
    try:
        sw.reconfigure(make_config(tmp_path, swarm={"mode": "thread"}))
        assert (sw.mode, sw.config.swarm.mode, sw.forum) == ("board", "board", None)
        assert sw.db.get(MODE_KEY) == "board" and not tables(sw.db) & FORUM_TABLES
    finally:
        sw.close()
    assert mode_warnings(caplog) == [WARNING.format("board")]


def test_a_thread_swarm_ensures_its_tables_at_every_open(tmp_path):
    Swarm(config=make_config(tmp_path, swarm={"mode": "thread"})).close()
    conn = sqlite3.connect(str(db_path(tmp_path)))
    try:  # a damaged file: a forum table is gone
        conn.execute("DROP TABLE votes")
        conn.commit()
    finally:
        conn.close()
    sw = Swarm(config=make_config(tmp_path))
    try:
        assert sw.mode == "thread" and FORUM_TABLES <= tables(sw.db)
    finally:
        sw.close()


def test_an_unknown_stored_mode_is_refused(tmp_path):
    database = Database(db_path(tmp_path))
    database.set(MODE_KEY, "x")
    database.close()
    with pytest.raises(ValueError, match=re.escape("the session's mode 'x' is unknown: it was made by a newer "
                                                   "DreamTeam")):
        Swarm(config=make_config(tmp_path))
    db_path(tmp_path).rename(tmp_path / "moved.db")  # the database was closed (Windows keeps open files locked)
    assert session_mode(tmp_path / "moved.db") == "x"


def test_an_invalid_config_mode_is_never_stored(tmp_path):
    cfg = make_config(tmp_path)
    cfg = dataclasses.replace(cfg, swarm=dataclasses.replace(cfg.swarm, mode="forum"))  # past Config's own check
    with pytest.raises(ValueError, match=re.escape("swarm.mode must be 'board' or 'thread', got 'forum'")):
        Swarm(config=cfg)
    assert session_mode(db_path(tmp_path)) == "board"
    conn = sqlite3.connect(str(db_path(tmp_path)))
    try:
        assert conn.execute("SELECT count(*) FROM kv WHERE key = ?", (MODE_KEY,)).fetchone()[0] == 0
    finally:
        conn.close()


def test_the_mode_key_is_written_before_any_row(tmp_path):
    sw = Swarm(config=make_config(tmp_path))
    try:
        assert sw.db.scalar("SELECT count(*) FROM events") == 0  # nothing else was written at open
        assert dbmod.MODES == ("board", "thread")
    finally:
        sw.close()
