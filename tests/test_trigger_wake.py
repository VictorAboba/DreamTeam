"""Trigger wake-ups with a real swarm (docs/SPEC.md §8): file_changed() fires once per settled run of changes, after
the editing agent's turn ended, with the count and the editor; a member's repeating trigger wakes it at most once per
[agents] trigger_wake_seconds, and the fires in between reach it with its next wake-up or wait_for_events; the
settings and the database of an older version."""

from __future__ import annotations

import asyncio
import dataclasses
import re
import sqlite3
import time
import tomllib
from typing import Any

import pytest

from dreamteam.config import Config, render_template
from dreamteam.db import Database, loads
from dreamteam.models import USER, AgentStatus
from dreamteam.swarm import QUIET_KEY
from dreamteam.tools import ToolContext, call_tool
from test_runtime import all_text, call, kinds_for, last_text, make_swarm, run_until, shutdown

WAKE = 180.0   # [agents] trigger_wake_seconds by default


def waiting(*args):
    return call("wait")


class Clock:
    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def test_five_edits_in_one_turn_wake_the_trigger_owner_once(tmp_path):
    """An agent edits a watched file five times in one activation: the owner of a repeating file_changed() trigger
    gets exactly one notice, once the editor's turn has ended (the quiet window is a minute), saying how many
    changes there were and who made them."""
    who: dict[str, Any] = {}

    def policy(swarm, name, system, messages, tools):
        if name != who.get("editor"):
            return call("wait")
        n = who["edits"]
        if n < 5:
            who["edits"] = n + 1
            return call("edit_file", path="docs/AUDIT.md", old_text=f"v{n}", new_text=f"v{n}+v{n + 1}")
        who["ended"] = time.time()
        return call("wait")

    swarm, rec = make_swarm(tmp_path, policy, swarm={"start_agents": 2})
    await swarm.start()
    try:
        editor, owner = [m["name"] for m in swarm.members()]
        (swarm.workspace.root / "docs").mkdir()
        (swarm.workspace.root / "docs" / "AUDIT.md").write_text("v0", encoding="utf-8")
        trig = swarm.triggers.create(owner, "file_changed('docs/AUDIT.md')", once=False)
        who.update(editor=editor, edits=0)
        swarm.mail.send(USER, editor, "Edit the audit.")
        await run_until(lambda: "n_trigger_files" in kinds_for(swarm, owner))
        await asyncio.sleep(0.5)                       # more passes: no second notice
        rows = swarm.db.all("SELECT ts, text FROM notifications WHERE recipient = ? AND kind LIKE 'n_trigger%'",
                            (owner,))
        assert len(rows) == 1 and who["edits"] == 5 and rows[0]["ts"] >= who["ended"]
        assert rows[0]["text"].startswith(f"Trigger #{trig['id']} fired: file_changed('docs/AUDIT.md'). File "
                                          f"docs/AUDIT.md was changed (changes: 5, by {editor}); the last change was")
        assert swarm.triggers.get(trig["id"])["fire_count"] == 1
        await run_until(lambda: any(f"(changes: 5, by {editor})" in all_text(m) for m in rec.calls.get(owner, [])))
        assert not rec.violations, rec.violations
    finally:
        await shutdown(swarm)


@pytest.mark.parametrize("settle", [60.0, 0.0])
async def test_a_last_edit_with_wait_in_one_response_fires_once(tmp_path, settle):
    """The editor's last response is [edit_file, wait]: the pass that takes that edit's event may hold a probe made
    before the write landed and fire at once (the turn ends with it). One notice, and no second fire once the
    quiet window and the wake interval are over; with file_settle_seconds = 0 (and no wake limit) the notices
    count the five edits, none twice."""
    who: dict[str, Any] = {}

    def policy(swarm, name, system, messages, tools):
        if name != who.get("editor") or who["edits"] >= 5:
            return call("wait")
        n = who["edits"] = who["edits"] + 1
        edit = {"name": "edit_file", "arguments": {"path": "docs/AUDIT.md", "old_text": f"v{n - 1}",
                                                   "new_text": f"v{n - 1}+v{n}"}}
        return {"tool_calls": [edit] + ([{"name": "wait", "arguments": {}}] if n == 5 else [])}

    swarm, rec = make_swarm(tmp_path, policy, swarm={"start_agents": 2},
                            agents={"file_settle_seconds": settle, "trigger_wake_seconds": WAKE if settle else 0})
    clock = swarm.triggers.clock = Clock()
    swarm.triggers.tick_seconds = 0.05
    await swarm.start()
    try:
        editor, owner = [m["name"] for m in swarm.members()]
        (swarm.workspace.root / "docs").mkdir()
        (swarm.workspace.root / "docs" / "AUDIT.md").write_text("v0", encoding="utf-8")
        trig = swarm.triggers.create(owner, "file_changed('docs/AUDIT.md')", once=False)
        who.update(editor=editor, edits=0)
        swarm.mail.send(USER, editor, "Edit the audit.")
        await run_until(lambda: "n_trigger_files" in kinds_for(swarm, owner))
        await run_until(lambda: swarm.member(editor)["status"] != AgentStatus.WORKING)
        for seconds in (61.0, WAKE + 1):                  # the quiet window, then the end of the wake interval
            clock.advance(seconds)
            await asyncio.sleep(0.3)
        texts = [r["text"] for r in swarm.db.all(
            "SELECT text FROM notifications WHERE recipient = ? AND kind LIKE 'n_trigger%'", (owner,))]
        fires = swarm.triggers.get(trig["id"])["fire_count"]
        assert who["edits"] == 5 and len(texts) == fires, texts
        if settle:
            assert fires == 1 and f"(changes: 5, by {editor})" in texts[0], texts
        else:
            assert 1 <= fires <= 5 and all(f", by {editor})" in t for t in texts), texts
            assert sum(int(re.search(r"\(changes: (\d+)", t).group(1)) for t in texts) == 5, texts
        assert not rec.violations, rec.violations
    finally:
        await shutdown(swarm)


async def test_a_held_fire_comes_with_the_owners_next_wake_up(tmp_path):
    """A repeating trigger's second fire within [agents] trigger_wake_seconds wakes nobody: its notice comes, quietly,
    with the owner's next wake-up for another reason, in that same activation."""
    swarm, rec = make_swarm(tmp_path, waiting)
    swarm.triggers.tick_seconds = 0.05
    await swarm.start()
    try:
        owner = swarm.members()[0]["name"]
        flag = swarm.workspace.root / "flag"
        trig = swarm.triggers.create(owner, "file_exists('flag')", once=False)
        flag.write_text("x", encoding="utf-8")
        await run_until(lambda: rec.count(owner) == 1)    # the first fire wakes it
        await run_until(lambda: swarm.member(owner)["status"] == AgentStatus.IDLE)
        flag.unlink()
        await run_until(lambda: swarm.db.scalar("SELECT last_value FROM triggers WHERE id = ?", (trig["id"],)) == 0)
        flag.write_text("x", encoding="utf-8")
        await run_until(lambda: swarm.triggers.get(trig["id"])["held"] == 1)
        await asyncio.sleep(0.3)
        assert rec.count(owner) == 1 and not swarm.has_pending(owner)   # held: no wake-up of its own
        swarm.mail.send(USER, owner, "How is it going?")
        await run_until(lambda: rec.count(owner) == 2)
        wake = last_text(rec.calls[owner][1])
        assert "How is it going?" in wake
        assert f"Trigger #{trig['id']} fired again since it last told you at" in wake and "(fires: 1, " in wake
        row = swarm.db.one("SELECT data FROM notifications WHERE kind = 'n_trigger_held'")
        assert loads(row["data"])[QUIET_KEY] is True
        await asyncio.sleep(0.3)
        assert rec.count(owner) == 2 and swarm.triggers.get(trig["id"])["held"] == 0
        assert kinds_for(swarm, owner).count("n_trigger_held") == 1
        assert not rec.violations, rec.violations
    finally:
        await shutdown(swarm)


async def test_an_external_member_gets_the_held_fires_from_wait_for_events(tmp_path):
    """An external member's held fires come with its next wait_for_events result, and the end of the interval ends a
    long poll; a session setting of 0 releases them at the next pass."""
    swarm, _ = make_swarm(tmp_path, waiting)
    await swarm.start(run_agents=False)
    try:
        ext = "Claude Code"
        text, failed = await call_tool(ToolContext(swarm, USER), "join_swarm", {"role": "Reviewer", "name": ext})
        assert not failed, text
        ctx = ToolContext(swarm, ext)
        clock = swarm.triggers.clock = Clock()
        flag = swarm.workspace.root / "flag"
        trig = swarm.triggers.create(ext, "file_exists('flag')", once=False)

        def edge() -> None:   # no await in between: the engine's own loop cannot interleave
            flag.write_text("x", encoding="utf-8")
            swarm.triggers.evaluate_all()
            flag.unlink()
            swarm.triggers.evaluate_all()

        edge()                                            # told at once
        text, _ = await call_tool(ctx, "wait_for_events", {"timeout": 0})
        assert f"Trigger #{trig['id']} fired: file_exists('flag')" in text
        clock.advance(10)
        edge()                                            # held: nothing pending, the poll's own render tells it
        assert not swarm.has_pending(ext)
        text, _ = await call_tool(ctx, "wait_for_events", {"timeout": 0})
        assert "fired again since it last told you at" in text and "(fires: 1, " in text
        clock.advance(10)
        edge()                                            # held again: the interval's end wakes a long poll
        poll = asyncio.create_task(call_tool(ctx, "wait_for_events", {"timeout": 30}))
        await asyncio.sleep(0.2)
        assert not poll.done()
        clock.advance(WAKE)
        swarm.triggers.evaluate_all()
        text, _ = await asyncio.wait_for(poll, 5)
        assert "fired again since it last told you at" in text and "(fires: 1, " in text
        clock.advance(10)
        edge()                                            # held; the session's limit is switched off meanwhile
        assert swarm.triggers.get(trig["id"])["held"] == 1 and not swarm.has_pending(ext)
        agents = dataclasses.replace(swarm.config.agents, trigger_wake_seconds=0)
        swarm.reconfigure(dataclasses.replace(swarm.config, agents=agents))
        swarm.triggers.evaluate_all()
        assert swarm.has_pending(ext) and swarm.triggers.get(trig["id"])["held"] == 0
    finally:
        await shutdown(swarm)


def test_an_older_database_gets_the_wake_limit_columns(tmp_path):
    path = tmp_path / "swarm.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE triggers (id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT, condition TEXT,"
                     " note TEXT DEFAULT '', actions TEXT DEFAULT '[]', once INTEGER DEFAULT 1,"
                     " active INTEGER DEFAULT 1, created_at REAL, last_value INTEGER DEFAULT 0, last_fired REAL,"
                     " fire_count INTEGER DEFAULT 0, last_error TEXT)")
        conn.execute("INSERT INTO triggers(owner, condition, once, created_at)"
                     " VALUES ('Anna Smith', 'every(60)', 0, 1)")
    conn.close()
    db = Database(path)
    try:
        row = db.one("SELECT told_at, held, held_last, held_files FROM triggers")
        assert tuple(row) == (None, 0, None, None)
    finally:
        db.close()


def test_trigger_settings(tmp_path):
    agents = Config.from_dict(tmp_path, {}).agents
    keys = ("file_settle_seconds", "file_max_wait_seconds", "trigger_wake_seconds")
    assert tuple(getattr(agents, k) for k in keys) == (60.0, 300.0, 180.0)
    off = Config.from_dict(tmp_path, {"agents": dict.fromkeys(keys, 0)}).agents   # 0 has a meaning for each
    assert tuple(getattr(off, k) for k in keys) == (0, 0, 0)
    for key in keys:
        for value, expected in ((-1, ">= 0"), ("soon", "a number")):
            with pytest.raises(ValueError, match=rf"\[agents\] {key} must be {expected}"):
                Config.from_dict(tmp_path, {"agents": {key: value}})
    template = tomllib.loads(render_template("x"))["agents"]
    assert tuple(template[k] for k in keys) == (60, 300, 180)
