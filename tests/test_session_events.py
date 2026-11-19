"""A run's start and end in the session's journal (session.started / session.stopped), the waiters of a session
(SessionManager.watch), `is_final` and `resume_pending` (docs/SPEC.md §9, §17.2, §21.6, §21.7)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from dreamteam import settings as st
from dreamteam import uitext
from dreamteam.db import reading_file
from dreamteam.eventtext import event_text
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.models import USER
from dreamteam.sessions import (MOVE_JOURNAL, SESSION_EVENTS, STOP_REASONS, TRASH_PREFIX, SessionError, SessionManager,
                                read_stats)
from dreamteam.swarm import Swarm
from dreamteam.tui import render

NO_GIT = {"workspace": {"git": False}}


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    st.write_raw(home, {
        "swarm": {"name": "Home", "language": "en", "start_agents": 1, "max_agents": 3, "max_cost_usd": 0},
        "agents": {"stagger_seconds": 0, "idle_nudge_seconds": 60},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
    })
    return home


def _manager(home: Path) -> SessionManager:
    def factory(profile, agent):
        return ScriptedLLM(lambda system, messages, tools: "Nothing to do right now.", profile)

    return SessionManager(home, llm_factory=factory)


def journal(manager: SessionManager, sid: str) -> list[dict[str, Any]]:
    """The session's events in id order: through its connection while it is loaded, else from its file."""
    swarm = manager.swarms.get(sid)
    if swarm is not None:
        try:
            rows = swarm.db.all("SELECT id, ts, type, data FROM events ORDER BY id")
            return [{"id": r["id"], "ts": r["ts"], "type": r["type"], "data": json.loads(r["data"])} for r in rows]
        except sqlite3.ProgrammingError:   # closed meanwhile: read the file
            pass
    path = manager.dir_of(sid) / ".dreamteam" / "swarm.db"
    if not path.exists():
        return []
    with reading_file(path) as (conn, _immutable):
        return [{"id": i, "ts": ts, "type": t, "data": json.loads(d)}
                for i, ts, t, d in conn.execute("SELECT id, ts, type, data FROM events ORDER BY id")]


def runs(manager: SessionManager, sid: str) -> list[tuple[str, Any]]:
    """The session.* events: ("started", None) and ("stopped", data)."""
    return [(e["type"].split(".")[1], e["data"] or None) for e in journal(manager, sid) if e["type"] in SESSION_EVENTS]


# ---- what is journaled ----------------------------------------------------------------------------------------
async def test_a_start_journals_session_started_before_the_runs_own_events(tmp_path):
    manager = _manager(_home(tmp_path))
    try:
        sid = (await manager.create("Calc", "Build a calculator", overrides=NO_GIT))["id"]
        types = [e["type"] for e in journal(manager, sid)]
        started = types.index("session.started")
        assert types.count("session.started") == 1
        assert started < types.index("agent.hired") and started < types.index("goal.set")
        assert "session.stopped" not in types
        assert runs(manager, sid) == [("started", None)]
    finally:
        await manager.shutdown()


async def test_a_stop_journals_its_reason_as_the_last_event(tmp_path):
    manager = _manager(_home(tmp_path))
    try:
        sid = (await manager.create("Calc", "Build a calculator", overrides=NO_GIT))["id"]
        await manager.stop(sid)
        events = journal(manager, sid)
        assert events[-1]["type"] == "session.stopped" and events[-1]["data"] == {"reason": "user"}
        await manager.stop(sid)   # not running: nothing more
        await manager.load(sid)   # loaded for viewing, then stopped: never ran here, nothing either
        await manager.stop(sid)
        assert runs(manager, sid) == [("started", None), ("stopped", {"reason": "user"})]
        await manager.start(sid)
        await manager.stop(sid, reason="budget")   # (stop_when_done passes "done" or "budget")
        assert runs(manager, sid)[-2:] == [("started", None), ("stopped", {"reason": "budget"})]
        for reason in ("tired", "shutdown", "restart", "failed"):   # the last three are the manager's own
            with pytest.raises(ValueError):
                await manager.stop(sid, reason=reason)
        await manager.start(sid)
        before = len(journal(manager, sid))
        db = manager.dir_of(sid) / ".dreamteam" / "swarm.db"
        assert db.exists()
        await manager.delete(sid)   # the journal goes away with the session
        assert not db.exists() and before > 0
    finally:
        await manager.shutdown()


async def test_a_hub_shutdown_journals_shutdown_and_keeps_the_mark(tmp_path):
    home = _home(tmp_path)
    manager = _manager(home)
    sid = (await manager.create("Calc", "Build a calculator", overrides=NO_GIT))["id"]
    idle = (await manager.create("Idle", "Wait", overrides=NO_GIT, start=False))["id"]
    await manager.load(idle)   # loaded, not running: nothing to journal
    await manager.shutdown()
    assert runs(manager, sid) == [("started", None), ("stopped", {"reason": "shutdown"})]
    assert runs(manager, idle) == []
    assert manager.meta(sid)["autostart"] is True and not manager.swarms


async def test_a_workspace_move_of_a_running_session_is_a_restart(tmp_path):
    manager = _manager(_home(tmp_path))
    try:
        sid = (await manager.create("Mover", "Move", overrides=NO_GIT))["id"]
        result = await manager.set_workspace(sid, str(tmp_path / "elsewhere" / "project"))
        assert result["mode"] == "moved" and result["restarted"] is True
        assert runs(manager, sid) == [("started", None), ("stopped", {"reason": "restart"}), ("started", None)]
        stopped = await manager.create("Stopped", "Move", overrides=NO_GIT, start=False)
        await manager.set_workspace(stopped["id"], str(tmp_path / "other"))   # not running: no stop, no start
        assert runs(manager, stopped["id"]) == []
    finally:
        await manager.shutdown()


async def test_a_failed_start_journals_failed_with_its_error_after_the_runs_events(tmp_path, monkeypatch):
    manager = _manager(_home(tmp_path))
    real_start = Swarm.start

    async def failing_start(self):
        await real_start(self)   # the members are hired, then something fails
        raise RuntimeError("the MCP server would not start")

    try:
        sid = (await manager.create("Calc", "Build a calculator", overrides=NO_GIT, start=False))["id"]
        monkeypatch.setattr(Swarm, "start", failing_start)
        with manager.watch(sid) as changed:
            with pytest.raises(RuntimeError):
                await manager.start(sid)
            assert changed.is_set()
        types = [e["type"] for e in journal(manager, sid)]
        assert types[0] == "session.started" and "agent.hired" in types
        assert types[-1] == "session.stopped" and types.count("session.stopped") == 1
        assert runs(manager, sid)[-1] == ("stopped", {"reason": "failed",
                                                      "error": "RuntimeError: the MCP server would not start"})
        assert sid not in manager.running and sid not in manager.swarms and manager.is_final(sid)
        monkeypatch.setattr(Swarm, "start", real_start)
        await manager.start(sid)   # works again: a new run
        assert runs(manager, sid)[-1] == ("started", None)
    finally:
        await manager.shutdown()


@pytest.mark.parametrize("path", ["/home/alice/proj/f", "C:\\Users\\alice\\proj\\f"])
async def test_a_failed_starts_journaled_error_names_no_absolute_path(tmp_path, monkeypatch, caplog, path):
    """The journal goes to v1 clients (event data) and into exports: DreamTeam's own error text names a path of the
    hub's computer by its last component there; the log keeps it whole."""
    manager = _manager(_home(tmp_path))

    async def failing_start(self):
        raise OSError(2, "No such file or directory", path)

    try:
        sid = (await manager.create("Calc", "Build a calculator", overrides=NO_GIT, start=False))["id"]
        monkeypatch.setattr(Swarm, "start", failing_start)
        with caplog.at_level(logging.ERROR, logger="dreamteam.sessions"), pytest.raises(FileNotFoundError):
            await manager.start(sid)
        error = runs(manager, sid)[-1][1]["error"]
        assert error == "FileNotFoundError: [Errno 2] No such file or directory: 'f'"
        assert "alice" not in json.dumps(journal(manager, sid))
        assert path.replace("\\", "\\\\") in caplog.text or path in caplog.text
    finally:
        await manager.shutdown()


async def test_a_cancelled_start_journals_the_hubs_stop(tmp_path, monkeypatch):
    """Ctrl+C while the hub's resume() waits on a long start: the run that began ends with the hub's stop, so the next
    hub start does not journal a second session.started with no end in between."""
    manager = _manager(_home(tmp_path))
    began = asyncio.Event()

    async def hanging_start(self):
        began.set()
        await asyncio.Event().wait()

    sid = (await manager.create("Calc", "Build a calculator", overrides=NO_GIT, start=False))["id"]
    monkeypatch.setattr(Swarm, "start", hanging_start)
    task = asyncio.ensure_future(manager.start(sid))
    await asyncio.wait_for(began.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await manager.shutdown()
    assert runs(manager, sid) == [("started", None), ("stopped", {"reason": "shutdown"})]
    assert sid not in manager.running and sid not in manager.swarms


@pytest.mark.parametrize("refusal", ["missing_folder", "move_journal"])
async def test_a_start_refused_before_the_swarm_starts_journals_nothing_but_wakes_the_waiters(tmp_path, monkeypatch,
                                                                                             refusal):
    manager = _manager(_home(tmp_path))
    woken_at_the_refusal: list[bool] = []
    real_check = manager._check_workspace_ready
    try:
        sid = (await manager.create("Calc", "Build a calculator", overrides=NO_GIT))["id"]
        await manager.stop(sid)
        before = journal(manager, sid)
        if refusal == "missing_folder":
            meta = manager.meta(sid)
            meta["workspace"] = {"path": str(tmp_path / "gone"), "adopted": True}
            manager._write_meta(sid, meta)
        else:
            (manager.dir_of(sid) / ".dreamteam" / MOVE_JOURNAL).write_text("{}", encoding="utf-8")
        with manager.watch(sid) as changed:
            def check(sid_: str, meta: dict) -> None:   # (the start holds the session's lock here)
                woken_at_the_refusal.append(changed.is_set())
                real_check(sid_, meta)

            monkeypatch.setattr(manager, "_check_workspace_ready", check)
            with pytest.raises(SessionError) as exc:
                await manager.start(sid)
            assert exc.value.status == 409
            # nothing woke the waiter while the start held the lock; the lock's release did: the start is over
            assert woken_at_the_refusal == [False] and changed.is_set()
        assert journal(manager, sid) == before and manager.is_final(sid)
        assert not (tmp_path / "gone").exists()
    finally:
        await manager.shutdown()


async def test_last_activity_leaves_out_the_session_events(tmp_path):
    """A hub shutdown journals session.stopped into every running session: it must not reorder the sessions list
    (running first, then the newest activity) or change which session `-c` continues."""
    manager = _manager(_home(tmp_path))
    try:
        old = (await manager.create("Old", "First", overrides=NO_GIT))["id"]
        await manager.stop(old)
        new = (await manager.create("New", "Second", overrides=NO_GIT))["id"]
        await manager.stop(new)
        for sid in (old, new):
            events = journal(manager, sid)
            assert events[-1]["type"] == "session.stopped"
            work = [e for e in events if e["type"] not in SESSION_EVENTS]
            stats = read_stats(manager.dir_of(sid) / ".dreamteam" / "swarm.db")
            assert stats["last_activity"] == work[-1]["ts"] == manager.summary(sid)["last_activity"]
        assert [s["id"] for s in manager.list()] == [new, old]
        # the old session's run events become the newest of the home (as a hub's start and shutdown would make them):
        # the order stays, and the loaded connection reads the same
        swarm = await manager.load(old)
        work_ts = manager.summary(old)["last_activity"]
        await asyncio.sleep(0.02)
        swarm.bus.emit("session.started", None)
        swarm.bus.emit("session.stopped", None, reason="shutdown")
        assert journal(manager, old)[-1]["ts"] > journal(manager, new)[-1]["ts"]
        assert manager.summary(old)["last_activity"] == work_ts
        assert [s["id"] for s in manager.list()] == [new, old]
    finally:
        await manager.shutdown()


# ---- waiters, final -------------------------------------------------------------------------------------------
async def test_watch_is_woken_by_an_event_a_change_and_the_end_of_an_operation(tmp_path):
    manager = _manager(_home(tmp_path))
    try:
        sid = (await manager.create("Calc", "Build a calculator", overrides=NO_GIT))["id"]
        other = (await manager.create("Other", "Something else", overrides=NO_GIT, start=False))["id"]
        swarm = manager.swarms[sid]
        with manager.watch(sid) as changed, manager.watch(sid) as second, manager.watch(other) as elsewhere:
            changed.clear()
            second.clear()
            swarm.bus.publish("trace.started", USER)   # live-only: no id, not a change of the journal
            assert not changed.is_set()
            waiting = asyncio.ensure_future(asyncio.wait_for(changed.wait(), 1))
            await asyncio.sleep(0)
            swarm.board.create(USER, "Parse", goal="parse the input")   # task.created
            await waiting
            assert second.is_set() and not elsewhere.is_set()

            changed.clear()
            manager.update(sid, title="Renamed")   # _changed(sid, "updated")
            assert changed.is_set() and not elsewhere.is_set()

            changed.clear()
            async with manager._lock(sid):   # an operation that held the lock ends: it may make the session final
                assert not changed.is_set()
            assert changed.is_set()

            changed.clear()
            await manager.stop(sid)
            assert changed.is_set() and not elsewhere.is_set()

            # the same waiter outlives the unload: the next run's Swarm has a new bus, and still wakes it
            await manager.start(sid)
            changed.clear()
            manager.swarms[sid].board.create(USER, "Print", goal="print the result")
            assert changed.is_set()
        assert manager._wakers == {}
    finally:
        await manager.shutdown()


async def test_a_poke_from_another_thread_reaches_the_waiting_loop(tmp_path):
    manager = _manager(_home(tmp_path))
    sid = (await manager.create("Calc", "Build a calculator", overrides=NO_GIT, start=False))["id"]
    with manager.watch(sid) as changed:
        await asyncio.to_thread(manager._poke, sid)
        await asyncio.wait_for(changed.wait(), 1)
    await manager.shutdown()


async def test_final_never_comes_before_session_stopped(tmp_path):
    """A client reads `final` first, then the journal: once final, the run's session.stopped is already there (the
    session leaves `running` before it journals it, but its lock is held until the stop is over)."""
    manager = _manager(_home(tmp_path))
    try:
        sid = (await manager.create("Calc", "Build a calculator", overrides=NO_GIT))["id"]
        assert not manager.is_final(sid)
        seen: list[tuple[bool, str]] = []

        async def client() -> None:
            with manager.watch(sid) as changed:
                while True:
                    changed.clear()
                    final = manager.is_final(sid)
                    last = journal(manager, sid)[-1]["type"]
                    seen.append((final, last))
                    if final:
                        return
                    await asyncio.wait_for(changed.wait(), 5)

        watcher = asyncio.ensure_future(client())
        await asyncio.sleep(0)
        await manager.stop(sid)
        await asyncio.wait_for(watcher, 5)
        assert seen[-1] == (True, "session.stopped")
        assert all(not final for final, _last in seen[:-1]) and len(seen) >= 2
    finally:
        await manager.shutdown()


async def test_is_final_while_running_starting_resume_pending_or_locked(tmp_path):
    manager = _manager(_home(tmp_path))
    try:
        sid = (await manager.create("Calc", "Build a calculator", overrides=NO_GIT, start=False))["id"]
        assert manager.is_final(sid)   # never started: nothing will start it by itself
        for holder in (manager.starting, manager.resume_pending):
            holder.add(sid)
            assert not manager.is_final(sid)
            holder.discard(sid)
        async with manager._lock(sid):
            assert not manager.is_final(sid)
        assert manager.is_final(sid)
        await manager.start(sid)
        assert not manager.is_final(sid)
        await manager.stop(sid)
        assert manager.is_final(sid)
    finally:
        await manager.shutdown()


# ---- resume_pending -------------------------------------------------------------------------------------------
async def test_resume_pending_is_filled_by_the_plan_and_emptied_one_by_one(tmp_path, caplog):
    home = _home(tmp_path)
    manager = _manager(home)
    first = (await manager.create("First", "One", overrides=NO_GIT))["id"]
    second = (await manager.create("Second", "Two", overrides=NO_GIT))["id"]
    third = (await manager.create("Third", "Three", overrides=NO_GIT))["id"]
    gone = (await manager.create("Gone", "Four", overrides=NO_GIT))["id"]
    unmarked = (await manager.create("Unmarked", "Five", overrides=NO_GIT, start=False))["id"]
    await manager.shutdown()   # every running session stays marked for resume()

    again = _manager(home)
    try:
        assert again.resume_pending == set() and again.is_final(first)
        assert sorted(again.plan_resume()) == sorted([first, second, third, gone])
        assert again.resume_pending == {first, second, third, gone}
        assert not again.is_final(first) and again.is_final(unmarked)
        await again.stop(second)   # the user stops one before its turn: final at once, never resumed
        assert second not in again.resume_pending and again.is_final(second)
        await again.delete(gone)
        assert gone not in again.resume_pending
        pending_at_start: dict[str, set[str]] = {}
        real_start = again._start_locked

        async def start_locked(sid, **kwargs):
            pending_at_start[sid] = set(again.resume_pending)
            return await real_start(sid, **kwargs)

        again._start_locked = start_locked
        assert await again.resume() == [first, third]
        assert pending_at_start == {first: {first, third}, third: {third}}
        assert again.resume_pending == set() and not again.is_final(first)
        assert again.meta(second)["autostart"] is False
    finally:
        await again.shutdown()


async def test_resume_without_a_plan_and_a_cancelled_plan(tmp_path):
    home = _home(tmp_path)
    manager = _manager(home)
    sid = (await manager.create("Calc", "Build a calculator", overrides=NO_GIT))["id"]
    await manager.shutdown()

    again = _manager(home)
    try:
        assert again.plan_resume() == [sid] and again.resume_pending == {sid}
        with again.watch(sid) as changed:
            again.cancel_resume()   # the launcher did not get its port
            assert changed.is_set()
        assert again.resume_pending == set() and again.is_final(sid) and again.meta(sid)["autostart"] is True
        again.cancel_resume()   # (nothing planned: a no-op)
        assert await again.resume() == [sid]   # chooses now
        assert again.resume_pending == set() and sid in again.running
    finally:
        await again.shutdown()


async def test_a_cancelled_resume_leaves_nothing_pending(tmp_path):
    home = _home(tmp_path)
    manager = _manager(home)
    ids = [(await manager.create(f"S{i}", "Work", overrides=NO_GIT))["id"] for i in range(2)]
    await manager.shutdown()

    again = _manager(home)
    gate = asyncio.Event()

    async def slow_start(sid, **kwargs):
        await gate.wait()

    again._start_locked = slow_start
    again.plan_resume()
    task = asyncio.ensure_future(again.resume())
    await asyncio.sleep(0.05)
    assert again.resume_pending == set(ids)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert again.resume_pending == set() and all(again.is_final(sid) for sid in ids)
    await again.shutdown()


async def test_a_stop_holding_the_lock_when_resume_reaches_the_session_is_not_undone(tmp_path, monkeypatch):
    """resume() decides again under the session's lock: a user's stop that held it while resume() waited has taken
    the session out of `resume_pending` and cleared its mark, and the stopped (final) session stays stopped."""
    home = _home(tmp_path)
    manager = _manager(home)
    sid = (await manager.create("Calc", "Build a calculator", overrides=NO_GIT))["id"]
    await manager.shutdown()

    again = _manager(home)
    try:
        assert again.plan_resume() == [sid]
        await again.start(sid)   # started by hand before resume() gets to it...
        gate = asyncio.Event()
        real_stop = Swarm.stop

        async def slow_stop(self):
            await gate.wait()
            await real_stop(self)

        monkeypatch.setattr(Swarm, "stop", slow_stop)
        stopping = asyncio.ensure_future(again.stop(sid))   # ...and stopped: the stop holds the lock
        await asyncio.sleep(0.01)
        resuming = asyncio.ensure_future(again.resume())
        await asyncio.sleep(0.01)
        assert again._lock(sid).locked() and not resuming.done() and sid in again.resume_pending
        gate.set()
        await stopping
        monkeypatch.setattr(Swarm, "stop", real_stop)
        assert await resuming == []
        assert sid not in again.running and again.is_final(sid) and again.meta(sid)["autostart"] is False
        assert runs(again, sid)[-1] == ("stopped", {"reason": "user"})
    finally:
        await again.shutdown()


async def test_the_plan_is_quiet_and_resume_tells_why_at_each_turn(tmp_path, caplog):
    """plan_resume() runs before the launcher's port opens: it reads the marks alone and logs nothing, so a launcher
    that loses the port, then the CLI's own resume(), say each reason once."""
    home = _home(tmp_path)
    manager = _manager(home)
    done = (await manager.create("Done", "Finish it", overrides=NO_GIT))["id"]
    swarm = manager.swarms[done]
    swarm.db.execute("DELETE FROM tasks")
    swarm.complete_goal(USER, "all done")
    assert manager.finished(done)
    await manager.shutdown()   # keeps its mark

    again = _manager(home)
    try:
        with caplog.at_level(logging.DEBUG, logger="dreamteam.sessions"):
            caplog.clear()
            assert again.plan_resume() == [done]   # chosen by its mark: about to be tried, not final yet
            assert caplog.records == [] and not again.is_final(done)
            again.cancel_resume()   # the launcher did not get its port...
            assert await again.resume() == []   # ...and the CLI resumes without it
        said = [r.getMessage() for r in caplog.records if "not resumed" in r.getMessage()]
        assert said == [f"session {done} is finished (goal achieved, no open task): not resumed"]
        assert again.is_final(done) and again.meta(done)["autostart"] is True and not again.resume_pending
    finally:
        await again.shutdown()


async def test_a_delete_that_cannot_rename_the_folder_leaves_a_stopped_session_with_its_end(tmp_path, monkeypatch):
    """The delete stops the session as the user would: when its folder cannot be renamed away (files in use, 409)
    the session stays, with its run's end journaled and no resume mark."""
    manager = _manager(_home(tmp_path))
    real_rename, real_sleep = os.rename, asyncio.sleep

    def busy(src, dst, *args, **kwargs):
        if TRASH_PREFIX in os.fspath(dst):
            raise PermissionError(13, "The process cannot access the file because it is being used", os.fspath(src))
        return real_rename(src, dst, *args, **kwargs)

    async def no_pause(delay, *args, **kwargs):   # (the delete's retries)
        return await real_sleep(0 if delay == 0.2 else delay, *args, **kwargs)

    try:
        sid = (await manager.create("Calc", "Build a calculator", overrides=NO_GIT))["id"]
        monkeypatch.setattr(os, "rename", busy)
        monkeypatch.setattr(asyncio, "sleep", no_pause)
        with pytest.raises(SessionError) as exc:
            await manager.delete(sid)
        monkeypatch.undo()
        assert exc.value.status == 409 and manager.exists(sid)
        assert runs(manager, sid) == [("started", None), ("stopped", {"reason": "user"})]
        assert manager.is_final(sid) and manager.meta(sid)["autostart"] is False
        await manager.delete(sid)   # the files are free again
        assert not manager.exists(sid)
    finally:
        await manager.shutdown()


# ---- the words ------------------------------------------------------------------------------------------------
def _ev(type_: str, **data: Any) -> dict:
    return {"id": 7, "ts": 1_760_000_000.0, "type": type_, "actor": None, "data": data}


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_every_stop_reason_has_its_words(lang):
    texts = uitext.TEXTS[lang]
    assert {f"ev_session_stopped_{reason}" for reason in STOP_REASONS} <= set(texts)
    lines = {reason: event_text(_ev("session.stopped", reason=reason), lang) for reason in STOP_REASONS}
    assert len(set(lines.values())) == len(STOP_REASONS)
    assert event_text(_ev("session.started"), lang) == "▶ " + texts["ev_session_started"]
    assert lines["budget"] == "· " + texts["ev_session_stopped_budget"]
    assert lines["failed"] == "⚠ " + texts["ev_session_stopped_failed"]
    # no reason, or one this version has no words for
    assert event_text(_ev("session.stopped"), lang) == "· " + texts["ev_session_stopped"]
    assert event_text(_ev("session.stopped", reason="later"), lang) == f"· {texts['ev_session_stopped']} (later)"


def test_a_failed_starts_error_is_quoted_without_absolute_paths():
    error = "SessionError: The workspace folder of session 001-x does not exist: /home/alice/projects/calc"
    line = event_text(_ev("session.stopped", reason="failed", error=error + "\nmore"), "en")
    assert line == ("⚠ The session did not start: SessionError: The workspace folder of session 001-x does not exist: "
                    "calc more")
    assert "/home" not in line and "alice" not in line


def test_the_terminal_shows_them_in_verbose_only():
    for event in (_ev("session.started"), _ev("session.stopped", reason="user"),
                  _ev("session.stopped", reason="failed", error="RuntimeError: boom")):
        assert render.event(render.RenderContext(lang="en", verbosity="normal"), event) is None
        shown = render.event(render.RenderContext(lang="en", verbosity="verbose"), event)
        assert shown is not None
    assert {"session.started", "session.stopped"} <= set(render.EVENT_TYPES)
