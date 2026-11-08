"""File locks with a real swarm (docs/SPEC.md §7 "Lock waiters", §3 scheduler): a member refused a file that waits
holding a task in progress is not nudged as stalled and is woken once the file is free; the scheduler tick settles an
expiry (also while paused) and tells a long holder once; the settings and the database of an older version."""

from __future__ import annotations

import asyncio
import sqlite3
import time
import tomllib

import pytest

from dreamteam import swarm as swarm_module
from dreamteam.config import Config, render_template
from dreamteam.db import Database
from dreamteam.models import USER, AgentStatus, ToolError
from test_runtime import all_text, call, kinds_for, make_swarm, run_until, shutdown


@pytest.fixture(autouse=True)
def ticking(monkeypatch):
    monkeypatch.setattr(swarm_module, "SCHEDULER_SECONDS", 0.05)


def waiting(*args):
    return call("wait")


async def test_a_refused_member_is_not_nudged_and_is_woken_when_the_file_is_free(tmp_path):
    """A member refused a file sleeps holding a task in progress: the refusal promised a notification, so no n_stalled
    meanwhile (idle_nudge_seconds is 0.2 s); the holder's unlock wakes it with n_lock_free, after which the usual
    nudge applies again."""
    swarm, rec = make_swarm(tmp_path, waiting, swarm={"start_agents": 2, "independent_review": False})
    await swarm.start()
    try:
        a, b = [m["name"] for m in swarm.members()]
        swarm.workspace.lock(a, "plan.md")
        with pytest.raises(ToolError, match="You will be notified when it is free"):
            await swarm.workspace.write(b, "plan.md", "mine")
        task = swarm.board.create(USER, "Plan", goal="g", assignee=b)
        swarm.board.claim(b, task.id)
        await run_until(lambda: swarm.member(b)["status"] == AgentStatus.IDLE and not swarm.has_pending(b)
                        and rec.count(b) >= 1)
        await asyncio.sleep(1.5)
        assert "n_stalled" not in kinds_for(swarm, b) and swarm.workspace.waits_for_lock(b)
        before = rec.count(b)
        swarm.workspace.unlock(a, "plan.md")
        await run_until(lambda: rec.count(b) > before)
        assert "Free again: plan.md (you were refused it earlier)." in all_text(rec.calls[b][before])
        await run_until(lambda: "n_stalled" in kinds_for(swarm, b))   # waiting for nothing now: nudged as usual
        assert kinds_for(swarm, b).count("n_lock_free") == 1 and not swarm.workspace.waits_for_lock(b)
        assert not rec.violations, rec.violations
    finally:
        await shutdown(swarm)


async def test_the_scheduler_settles_an_expiry_and_tells_a_long_holder_once(tmp_path):
    """The tick tells the holder once its lock was held for lock_wanted_minutes (here 0.6 s), and settles an expiry
    within a tick, also while the swarm is paused (the notification waits for the resume): one notice each."""
    swarm, rec = make_swarm(tmp_path, waiting, swarm={"start_agents": 2}, workspace={"lock_wanted_minutes": 0.01})
    await swarm.start(run_agents=False)
    try:
        a, b = [m["name"] for m in swarm.members()]
        swarm.workspace.lock(a, "x.md")
        with pytest.raises(ToolError):
            swarm.workspace.lock(b, "x.md")
        assert "n_lock_wanted" not in kinds_for(swarm, a)   # held for less than 0.6 s
        await run_until(lambda: "n_lock_wanted" in kinds_for(swarm, a))
        assert b in swarm.db.scalar("SELECT text FROM notifications WHERE kind = 'n_lock_wanted'")
        swarm.pause(USER, "paused by the user")
        swarm.db.execute("UPDATE file_locks SET expires_at = ?", (time.time() - 1,))
        await run_until(lambda: "n_lock_free" in kinds_for(swarm, b))
        await asyncio.sleep(0.3)
        assert kinds_for(swarm, b).count("n_lock_free") == 1 and kinds_for(swarm, a).count("n_lock_wanted") == 1
        assert swarm.paused and swarm.has_pending(b) and swarm.workspace.locks() == []
    finally:
        await shutdown(swarm)


NUDGE = "plain text reaches nobody"


async def test_a_refused_member_that_answers_with_text_gets_no_nudge(tmp_path):
    """The refusal promised a notification: a member holding a task in progress that answers it with plain text is not
    nudged in its own activation (no extra call, no `waiting` status, no discussion post); n_lock_free is the next
    thing it is asked about."""
    state: dict = {}

    def policy(swarm, name, system, messages, tools):
        if name != state.get("b"):
            return call("wait")
        last = all_text(messages[-1:])
        if "Free again" in last or NUDGE in last:
            return call("wait")
        if "You will be notified when it is free" in last:
            return {"text": "The file is locked, I will wait for it."}
        return call("write_file", path="plan.md", content="x")

    config = {"start_agents": 2, "independent_review": False}
    swarm, rec = make_swarm(tmp_path, policy, swarm=config)
    await swarm.start(run_agents=False)
    a, b = [m["name"] for m in swarm.members()]
    state["b"] = b
    swarm.workspace.lock(a, "plan.md")
    task = swarm.board.create(USER, "Plan", goal="g", assignee=b)
    swarm.board.claim(b, task.id)
    await shutdown(swarm)
    swarm, rec = make_swarm(tmp_path, policy, swarm=config)
    await swarm.start()
    try:
        swarm.notify(b, "n_ready", tasks=f"#{task.id} Plan")
        await run_until(lambda: swarm.workspace.waits_for_lock(b) and swarm.member(b)["status"] == AgentStatus.IDLE
                        and not swarm.has_pending(b))
        await asyncio.sleep(0.5)
        assert not any(NUDGE in all_text(c[-1:]) for c in rec.calls[b])
        assert swarm.board.get(task.id).status == "in_progress"
        before = rec.count(b)
        swarm.workspace.unlock(a, "plan.md")
        await run_until(lambda: rec.count(b) > before)
        assert "Free again: plan.md" in all_text(rec.calls[b][before])
    finally:
        await shutdown(swarm)


async def test_a_lock_freed_after_the_goal_is_done_wakes_nobody(tmp_path):
    """Once the goal is done a waiter's n_lock_free is quiet, like a job's end: neither the expiry nor the restart of
    the finished session makes an LLM call."""
    config = {"start_agents": 2, "independent_review": False}
    swarm, rec = make_swarm(tmp_path, waiting, swarm=config)
    await swarm.start()
    a, b = [m["name"] for m in swarm.members()]
    swarm.set_goal(USER, "G", "d")
    swarm.workspace.lock(a, "x.md", 0.02)      # 1.2 s
    swarm.workspace.lock(a, "y.md", 30)
    with pytest.raises(ToolError):
        await swarm.workspace.write(b, "x.md", "x")
    with pytest.raises(ToolError):
        await swarm.workspace.write(b, "y.md", "x")
    swarm.complete_goal(a, "done")
    try:
        await run_until(lambda: all(not swarm.has_pending(n) and swarm.member(n)["status"] == AgentStatus.IDLE
                                    for n in (a, b)))
        before = rec.count(b)
        await run_until(lambda: "n_lock_free" in kinds_for(swarm, b), timeout=10)
        await asyncio.sleep(0.5)
        assert rec.count(b) == before and swarm.workspace.lock_waits(b) == ["y.md"]
    finally:
        await shutdown(swarm)
    swarm, rec = make_swarm(tmp_path, waiting, swarm=config)
    swarm.db.execute("UPDATE file_locks SET expires_at = ?", (time.time() - 1,))   # it ran out while stopped
    await swarm.start()
    try:
        await run_until(lambda: not swarm.workspace.waits_for_lock(b))
        await asyncio.sleep(0.5)
        assert rec.count(b) == 0 and rec.count(a) == 0
    finally:
        await shutdown(swarm)


def test_lock_settings(tmp_path):
    cfg = Config.from_dict(tmp_path, {})
    assert (cfg.workspace.lock_ttl_minutes, cfg.workspace.lock_wanted_minutes) == (30, 5.0)
    ok = Config.from_dict(tmp_path, {"workspace": {"lock_ttl_minutes": 1, "lock_wanted_minutes": 0}})
    assert (ok.workspace.lock_ttl_minutes, ok.workspace.lock_wanted_minutes) == (1, 0)
    # a lock time of 0 would make every lock_file call without minutes fail
    for key, value, expected in (("lock_ttl_minutes", 0, ">= 1"), ("lock_wanted_minutes", -1, ">= 0"),
                                 ("lock_wanted_minutes", "soon", "a number")):
        with pytest.raises(ValueError, match=rf"\[workspace\] {key} must be {expected}"):
            Config.from_dict(tmp_path, {"workspace": {key: value}})
    workspace = tomllib.loads(render_template("x"))["workspace"]
    assert (workspace["lock_ttl_minutes"], workspace["lock_wanted_minutes"]) == (30, 5)


def test_an_older_database_gets_the_waiters_table_and_the_wanted_at_column(tmp_path):
    path = tmp_path / "swarm.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE file_locks (path TEXT PRIMARY KEY, owner TEXT, acquired_at REAL, expires_at REAL)")
        conn.execute("INSERT INTO file_locks VALUES ('a.md', 'Anna Smith', 1, 2)")
    conn.close()
    db = Database(path)
    try:
        assert db.one("SELECT wanted_at FROM file_locks WHERE path = 'a.md'")["wanted_at"] is None
        assert db.all("SELECT * FROM lock_waiters") == []
    finally:
        db.close()
