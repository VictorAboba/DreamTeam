"""The user's wrap-up mode (docs/SPEC.md §3 "budget / control", §7.1 "Wrap-up mode") and quiet broadcasts (§5).

Wrap-up mode: the swarm flag, its event and the agents' notices, the wake-up line, what ends it, the jobs rules
(background refused, a command at its timeout killed instead of adopted, leftovers killed, earlier jobs untouched,
external members alike), swarm_control / swarm_status / wait_for_events, the board UI (Node), the launcher card, the
TUI and console commands. Quiet broadcasts: a member's "all" does not wake an agent asleep while it waits for its own
job; that agent sees the message at its next wake-up."""

from __future__ import annotations

import asyncio
import os
import re
import time
from types import SimpleNamespace

import pytest

from dreamteam import prompts
from dreamteam.db import loads
from dreamteam.hub_mcp import INSTRUCTIONS
from dreamteam.models import USER, AgentKind, ToolError
from dreamteam.sessions import read_stats
from dreamteam.swarm import QUIET_KEY
from dreamteam.tools import ToolContext, call_tool
from dreamteam.tui import commands, render
from dreamteam.tui.commands import execute
from test_job_tools import (ANNA, BORIS, EXT, agent_ctx, apid, fast, kids, leave, ok, py_cmd,  # noqa: F401
                            team, until)
from test_procs import pid_alive, wait_until
from test_runtime import call, make_swarm, shutdown
from test_tui_commands import FakeApp, HubApp
from test_ui import needs_node, run_js

CARL = "Carl Young"
RU = "Анна Смирнова"


def kinds(swarm, name: str) -> list[str]:
    return [r["kind"] for r in swarm.db.all("SELECT kind FROM notifications WHERE recipient = ? ORDER BY id", (name,))]


def quiet(swarm, name: str, kind: str) -> list[bool]:
    rows = swarm.db.all("SELECT data FROM notifications WHERE recipient = ? AND kind = ? ORDER BY id", (name, kind))
    return [bool(loads(r["data"], {}).get(QUIET_KEY)) for r in rows]


def wrap_events(swarm) -> list[tuple[str, bool]]:
    return [(e["actor"], e["data"]["on"]) for e in swarm.bus.recent(types=("swarm.wrap_up",))]


async def user(swarm, tool: str, args: dict | None = None) -> tuple[str, bool]:
    return await call_tool(ToolContext(swarm, USER), tool, args or {})


def gone(kids, index: int = -1, timeout: float = 5.0) -> bool:
    """The child `index` is not running: dead, or killed before it wrote its pid."""
    path = kids.files[index]
    if not (path.exists() and path.read_text().strip()):
        return True
    pid = int(path.read_text())
    return wait_until(lambda: not pid_alive(pid), timeout)


# --- the swarm flag ----------------------------------------------------------------------------------------------

async def test_wrap_up_flag_event_notices_and_wake_line(tmp_path):
    swarm, _ = await team(tmp_path)
    await swarm.hire(USER, "Developer", name=RU, language="ru")
    await swarm.hire(USER, "Assistant", name=EXT, kind=AgentKind.EXTERNAL)
    try:
        swarm.set_goal(USER, "Ship it")
        assert not swarm.wrapping_up and swarm.snapshot()["swarm"]["wrap_up"] is False
        for name in (ANNA, BORIS, RU):
            swarm.render_wake(name)  # drain the hiring and kickoff news
        assert swarm.wrap_up(USER) is True
        assert swarm.wrap_up(USER) is False  # once: no second notice, no second event
        assert swarm.wrapping_up and swarm.snapshot()["swarm"]["wrap_up"] is True and not swarm.paused
        assert wrap_events(swarm) == [(USER, True)]
        # every AI agent is told once, waking it; an external member's refusals say it (no notice)
        for name in (ANNA, BORIS, RU):
            assert kinds(swarm, name).count("n_wrap_up") == 1 and quiet(swarm, name, "n_wrap_up") == [False]
            assert swarm.has_pending(name)
        assert "n_wrap_up" not in kinds(swarm, EXT)
        wake = swarm.render_wake(ANNA)
        lines = wake.split("\n")
        assert lines[1] == ("Wrap-up mode (the user): start no new computations or background jobs; finish your tasks "
                            "with what you have.")
        assert "• The user asked the team to wrap up: start no new computations or background jobs, not even one " \
               "more quick pass." in wake and "`task_complete` with the result so far and what is left" in wake
        ru = swarm.render_wake(RU).split("\n")
        assert ru[1] == prompts.TEXTS["ru"]["wrap_up_line"] and ru[1].startswith("Режим завершения (пользователь)")
        assert any(line.startswith("• Пользователь попросил команду закругляться") for line in ru)
        # the line repeats at every wake-up while the mode is on, never in the news of a running activation
        assert swarm.render_wake(ANNA).split("\n")[1] == prompts.t("en", "wrap_up_line")
        swarm.notify(ANNA, "n_restart")
        assert prompts.t("en", "wrap_up_line") not in swarm.render_wake(ANNA, incoming=True)
        # off: a quiet notice for the next wake-up, no line any more
        assert swarm.end_wrap_up(USER) is True and swarm.end_wrap_up(USER) is False
        assert wrap_events(swarm) == [(USER, True), (USER, False)]
        assert quiet(swarm, BORIS, "n_wrap_up_end") == [True] and not swarm.has_pending(ANNA)
        wake = swarm.render_wake(ANNA)
        assert "Wrap-up mode (the user)" not in wake and "• Wrap-up mode is off: background jobs are allowed again" in wake
        # a new goal ends it, and so does a reopened one
        swarm.wrap_up(USER)
        swarm.set_goal(USER, "Ship it again")
        assert not swarm.wrapping_up and wrap_events(swarm)[-1] == (USER, False)
        swarm.complete_goal(ANNA, "Done")
        swarm.wrap_up(USER)
        swarm.reopen_goal(USER, "one more thing")
        assert not swarm.wrapping_up and wrap_events(swarm)[-1] == (USER, False)
        assert kinds(swarm, ANNA).count("n_wrap_up") == 3
        swarm.wrap_up(USER)
    finally:
        await shutdown(swarm)
    # kept in the session database: a restart, the launcher's card
    again, _ = make_swarm(tmp_path, lambda *a: call("wait"))
    try:
        assert again.wrapping_up
    finally:
        again.close()
    stats = read_stats(tmp_path / "project" / ".dreamteam" / "swarm.db")
    assert stats["wrap_up"] is True


async def test_all_done_while_wrapping_up_asks_for_the_goal_not_new_tasks(tmp_path, fast):
    swarm, rec = await team(tmp_path, run_agents=True, swarm={"independent_review": False})
    try:
        await until(lambda: rec.count(ANNA) and rec.count(BORIS) and not swarm.has_pending(ANNA)
                    and not swarm.has_pending(BORIS) and swarm.member(ANNA)["status"] == "idle"
                    and swarm.member(BORIS)["status"] == "idle")
        swarm.set_goal(USER, "Ship it")
        swarm.wrap_up(USER)
        planning = swarm.board.list()[0]
        swarm.board.claim(ANNA, planning.id)
        swarm.board.complete(ANNA, planning.id, "ok")
        await until(lambda: "n_all_done_wrap_up" in kinds(swarm, ANNA) + kinds(swarm, BORIS))
        assert "n_all_done" not in kinds(swarm, ANNA) + kinds(swarm, BORIS)
        text = prompts.t("en", "n_all_done_wrap_up")
        assert "Don't create new tasks" in text and "`goal_complete`" in text
    finally:
        await shutdown(swarm)


# --- jobs --------------------------------------------------------------------------------------------------------

async def test_jobs_in_wrap_up_mode(tmp_path, fast, kids):
    """While wrapping up no command of a member becomes a new job: background=true is refused before anything starts,
    a command still running at its timeout is killed then (an AI agent waits up to its timeout, not foreground_max),
    processes a command leaves running die at its end; jobs started before keep running; an external member alike."""
    swarm, _ = await team(tmp_path)
    await swarm.hire(USER, "Assistant", name=EXT, kind=AgentKind.EXTERNAL)
    swarm.config.shell.foreground_max = 1
    anna, ext = agent_ctx(swarm, ANNA), ToolContext(swarm, EXT)

    async def srun(actor, command, timeout=10, background=False):
        return await swarm.jobs.run(actor, command, timeout=timeout, env=dict(os.environ), max_output=4000, background=background)

    try:
        server = await ok(anna, "shell", {"command": py_cmd(kids.script, "60", kids.pidfile()), "background": True})
        assert server.startswith("STARTED in the background as job j1")
        server_pid = await apid(kids)
        swarm.wrap_up(USER)
        # background=true: refused before anything starts
        for ctx in (anna, ext):
            text, is_error = await call_tool(ctx, "shell", {"command": py_cmd(kids.script, "30", kids.pidfile()),
                                                            "background": True})
            assert is_error and "The user asked the team to wrap up, so no new background jobs are started." in text
            assert "run it without background" in text and "task_complete" in text
            assert not kids.files[-1].exists()
        # a command still running at its timeout: killed then, not adopted, and the AI agent waits its own timeout
        started = time.monotonic()
        out = await srun(ANNA, py_cmd(kids.script, "30", kids.pidfile()), timeout=3)
        assert out.state == "timeout" and out.note == "wrap_up" and out.job is None
        assert out.wait == pytest.approx(3) and time.monotonic() - started >= 2.9 and gone(kids)
        text = out.render(has_jobs_tool=True, cfg=swarm.config.shell)
        assert text.startswith("TIMEOUT after 3s (process tree killed)")
        assert text.endswith("[It was not moved to the background: the user asked the team to wrap up, so no new "
                             "background jobs are started. Finish your task with the results you have and say what "
                             "is left.]")
        # a command longer than foreground_max but within its timeout simply finishes
        out = await srun(ANNA, py_cmd(kids.script, "2", kids.pidfile()), timeout=10)
        assert out.state == "exited" and out.code == 0 and "child end" in out.output
        # processes a command leaves running: killed at its end instead of becoming a detached job
        out = await srun(ANNA, leave(kids.script, "30", kids.pidfile()), timeout=10)
        assert out.state == "leftovers_killed" and out.note == "wrap_up_left" and out.code == 0 and gone(kids)
        assert out.render(has_jobs_tool=True, cfg=swarm.config.shell).endswith(
            "[They were not kept as a background job: the user asked the team to wrap up, so no new background jobs "
            "are started.]")
        # an external member: the same rule (its MCP cap stays)
        out = await srun(EXT, py_cmd(kids.script, "30", kids.pidfile()), timeout=2)
        assert out.state == "timeout" and out.note == "wrap_up" and gone(kids)
        # the job started before keeps running; only one job ever started
        assert [j.id for j in swarm.jobs.running_of()] == [1] and pid_alive(server_pid)
        assert swarm.db.scalar("SELECT COUNT(*) FROM jobs") == 1
        assert not swarm.jobs.wrapping_up(USER) and swarm.jobs.wrapping_up(EXT)
        # switched on while a call waits: past foreground_max it is not adopted, it gets its own timeout
        swarm.end_wrap_up(USER)
        call_task = asyncio.ensure_future(srun(ANNA, py_cmd(kids.script, "30", kids.pidfile()), timeout=3))
        await asyncio.sleep(0.3)
        swarm.wrap_up(USER)
        out = await call_task
        assert out.state == "timeout" and out.note == "wrap_up" and out.wait == pytest.approx(3) and gone(kids)
        # switched off while a call waits (e.g. by a new goal): it is adopted at foreground_max, not blocked to its
        # timeout (regression: the wait was fixed to the whole timeout when the call started)
        call_task = asyncio.ensure_future(srun(ANNA, py_cmd(kids.script, "30", kids.pidfile()), timeout=8))
        await asyncio.sleep(0.3)
        swarm.end_wrap_up(USER)
        started = time.monotonic()
        out = await call_task
        assert out.state == "background" and out.wait == 1 and time.monotonic() - started < 3
        await swarm.jobs.stop(out.job.id, USER, force=True)
        swarm.wrap_up(USER)
        # off again: back to the usual rules (adopted at foreground_max)
        swarm.end_wrap_up(USER)
        out = await srun(ANNA, py_cmd(kids.script, "30", kids.pidfile()), timeout=10)
        assert out.state == "background" and out.wait == 1 and out.job.origin == "timeout"
        for job in swarm.jobs.running_of():
            await swarm.jobs.stop(job.id, USER, force=True)
    finally:
        await shutdown(swarm)


# --- the user's tools --------------------------------------------------------------------------------------------

async def test_swarm_control_swarm_status_and_progress(tmp_path):
    swarm, _ = await team(tmp_path)
    await swarm.hire(USER, "Assistant", name=EXT, kind=AgentKind.EXTERNAL)
    ext = ToolContext(swarm, EXT)
    try:
        await ok(ext, "wait_for_events", {"timeout": 0, "progress": True})  # its progress starts now
        text, is_error = await user(swarm, "swarm_control", {"wrap_up": True})
        assert not is_error and text.startswith("Done: wrap-up mode on: every agent was told") and swarm.wrapping_up
        assert (await user(swarm, "swarm_control", {"wrap_up": "true"}))[0] == "Done: wrap-up mode was already on."
        status = (await user(swarm, "swarm_status"))[0]
        assert status.split("\n")[0].endswith("— running; WRAP-UP MODE: the agents finish their tasks with what "
                                              "they have, no new computations or background jobs")
        assert "WRAP-UP MODE" in await ok(ext, "swarm_status")
        news = await ok(ext, "wait_for_events", {"timeout": 0, "progress": True})
        assert re.search(r"• \d\d:\d\d wrap-up mode on \(user\): finish your tasks with what you have", news)
        # with a pause in one call; then off
        text, _ = await user(swarm, "swarm_control", {"action": "pause", "wrap_up": False})
        assert text == "Done: paused; wrap-up mode off." and swarm.paused and not swarm.wrapping_up
        assert (await user(swarm, "swarm_control", {"wrap_up": False}))[0] == "Done: wrap-up mode was not on."
        assert "WRAP-UP" not in (await user(swarm, "swarm_status"))[0]
        text, is_error = await user(swarm, "swarm_control", {})
        assert is_error and "pass action (pause/resume), wrap_up, a limit" in text
        text, is_error = await user(swarm, "swarm_control", {"wrap_up": "maybe"})
        assert is_error and "'wrap_up' must be true or false" in text
        # reopen and wrap up in one call: the reopen ends the mode, the switch comes after it
        swarm.set_goal(USER, "Ship it")
        swarm.complete_goal(ANNA, "Done")
        text, _ = await user(swarm, "swarm_control", {"reopen_goal_comment": "polish", "wrap_up": True})
        assert text.startswith("Done: goal reopened; wrap-up mode on") and swarm.wrapping_up
        # members cannot switch it (a tool of the user)
        assert (await call_tool(ext, "swarm_control", {"wrap_up": False}))[1] and swarm.wrapping_up
    finally:
        await shutdown(swarm)
    assert "switch wrap-up mode" in INSTRUCTIONS and "In the user's wrap-up mode no new background job starts" in \
        INSTRUCTIONS


# --- quiet broadcasts ----------------------------------------------------------------------------------------------

async def running_team(tmp_path, **overrides):
    swarm, rec = await team(tmp_path, run_agents=True, **overrides)
    await swarm.hire(USER, "Developer", name=CARL)
    await until(lambda: all(rec.count(n) and not swarm.has_pending(n) and swarm.member(n)["status"] == "idle"
                            for n in (ANNA, BORIS, CARL)))
    return swarm, rec


def woken(rec, name: str, before: int) -> bool:
    return rec.count(name) > before


async def test_a_broadcast_does_not_wake_an_agent_waiting_for_its_job(tmp_path, fast, kids):
    swarm, rec = await running_team(tmp_path)
    try:
        out = await swarm.jobs.run(ANNA, py_cmd(kids.script, "3", kids.pidfile()), timeout=0.3, env=dict(os.environ),
                                   max_output=4000)
        assert out.state == "background" and swarm.waits_for_job(ANNA)
        assert not swarm.waits_for_job(CARL)
        anna, carl = rec.count(ANNA), rec.count(CARL)
        sent = swarm.mail.send(BORIS, "all", "Status: the parser is merged.")
        assert sent["recipients"] == [ANNA, CARL]  # the sender's result is unchanged
        await until(lambda: woken(rec, CARL, carl))  # idle and not busy: woken as always
        await asyncio.sleep(0.5)
        assert not woken(rec, ANNA, anna) and swarm.has_pending(ANNA)  # unread, for its next wake-up
        await until(lambda: woken(rec, ANNA, anna), 15)  # the job's end wakes it: one wake-up shows both
        wake = rec.calls[ANNA][anna][-1]["content"]
        assert f"Message #{sent['id']} from {BORIS} to you, {CARL}:\nStatus: the parser is merged." in wake
        assert "• Your background job j1 has ended (exit code 0)" in wake
        assert rec.count(ANNA) == anna + 1  # no extra wake-up for the broadcast
    finally:
        await shutdown(swarm)


async def test_personal_and_user_messages_wake_an_agent_waiting_for_its_job(tmp_path, fast, kids):
    swarm, rec = await running_team(tmp_path)
    try:
        out = await swarm.jobs.run(ANNA, py_cmd(kids.script, "60", kids.pidfile()), timeout=0.3, env=dict(os.environ),
                                   max_output=4000)
        assert out.state == "background"
        for sender, to in ((BORIS, [ANNA, CARL]), (USER, "all"), (BORIS, ["all", ANNA])):
            await until(lambda: swarm.waits_for_job(ANNA) and not swarm.has_pending(ANNA))
            before = rec.count(ANNA)
            swarm.mail.send(sender, to, f"from {sender}")
            await until(lambda: woken(rec, ANNA, before)), (sender, to)
        # a member's "all" to her while she is awake (working or not busy) wakes as always: only the job wait is quiet
        await swarm.jobs.stop(out.job.id, USER, force=True)
        await until(lambda: not swarm.jobs.busy(ANNA) and not swarm.has_pending(ANNA)
                    and swarm.member(ANNA)["status"] == "idle")
        before = rec.count(ANNA)
        swarm.mail.send(BORIS, "all", "after the job")
        await until(lambda: woken(rec, ANNA, before))
    finally:
        await shutdown(swarm)


async def test_a_deferred_broadcast_is_shown_when_the_wait_ends_without_a_wake(tmp_path, fast, kids):
    """A server (background=true) keeps its owner busy only while it is young and the owner holds a task in progress;
    once that busy window ends, the agent is no longer waiting for its job's end, so the scheduler wakes it for the
    broadcast that reached it meanwhile (no nudge loop: it is woken once)."""
    swarm, rec = await running_team(tmp_path, swarm={"independent_review": False})
    swarm.config.shell.max_timeout = 2
    try:
        task = swarm.board.create(USER, "Build", goal="g")
        swarm.board.claim(ANNA, task.id)
        await until(lambda: not swarm.has_pending(ANNA) and swarm.member(ANNA)["status"] == "idle")
        out = await swarm.jobs.run(ANNA, py_cmd(kids.script, "60", kids.pidfile()), timeout=2, env=dict(os.environ),
                                   max_output=4000, background=True)
        assert out.state == "started" and swarm.waits_for_job(ANNA)
        before = rec.count(ANNA)
        sent = swarm.mail.send(BORIS, "all", "Heads-up: the API changed.")
        await asyncio.sleep(0.3)
        assert not woken(rec, ANNA, before) and swarm.has_pending(ANNA)
        await until(lambda: woken(rec, ANNA, before), 10)
        assert not swarm.jobs.busy(ANNA) and swarm.jobs.running_of(ANNA)  # the busy window ended, the job runs
        assert f"Message #{sent['id']} from {BORIS}" in rec.calls[ANNA][before][-1]["content"]
        await asyncio.sleep(0.5)
        assert rec.count(ANNA) == before + 1 and not swarm._deferred
        await swarm.jobs.stop(out.job.id, USER, force=True)
    finally:
        await shutdown(swarm)


# --- surfaces: TUI, console, board, launcher card ------------------------------------------------------------------

async def test_tui_wrapup_command_and_event():
    app = HubApp(attached=True)
    await execute(app, "/wrapup")
    await execute(app, "/wrapup off")
    await execute(app, "/wrapup ON")
    await execute(app, "/wrapup later")
    assert app.acts == [("swarm_control", {"wrap_up": True}), ("swarm_control", {"wrap_up": False}),
                        ("swarm_control", {"wrap_up": True})]
    assert "Usage: /wrapup [off]" in app.text and "swarm_control ok" not in app.text  # the event prints the change

    async def already(sid, tool, args):
        return True, "Done: wrap-up mode was already on."
    app.client.act = already
    await execute(app, "/wrapup")
    assert "Done: wrap-up mode was already on." in app.text

    async def refused(sid, tool, args):
        return False, "The session is not running."
    app.client.act = refused
    await execute(app, "/wrapup off")
    assert app.out[-1].lstrip().startswith(app.ctx.sym["warn"]) and "The session is not running." in app.out[-1]
    unattached = FakeApp()
    await execute(unattached, "/wrapup")
    assert "No current session" in unattached.text
    cmd = commands.find("wrapup")
    assert cmd.session and cmd.usage("en") == "/wrapup [off]" and cmd.usage("ru") == "/wrapup [off]"
    assert "wrap-up mode" in cmd.description("en") and "режим завершения" in cmd.description("ru")
    # the event: a warning line at every verbosity (like a pause), and the switch back off
    for verbosity in render.VERBOSITY:
        ctx = render.RenderContext(lang="en", verbosity=verbosity)
        on = render.to_plain(render.event(ctx, {"type": "swarm.wrap_up", "actor": "user", "data": {"on": True}}), 200)
        assert "wrap-up mode — the agents finish their tasks with what they have" in on
    ru = render.RenderContext(lang="ru")
    off = render.to_plain(render.event(ru, {"type": "swarm.wrap_up", "actor": "user", "data": {"on": False}}), 200)
    assert "режим завершения выключен" in off


def test_tui_sessions_show_wrap_up():
    """The /sessions table and the session picker show the mode, as the launcher's card does (a pause wins)."""
    from dreamteam.tui.dialogs import session_status

    running = {"id": "001-x", "title": "X", "status": "running", "paused": False}
    for lang, word in (("en", "wrapping up"), ("ru", "завершается")):
        ctx = render.RenderContext(lang=lang)
        assert render._session_status(ctx, {**running, "wrap_up": True})[0] == word
        assert session_status(lang, {**running, "wrap_up": True}) == word
        assert word not in render.to_plain(render.sessions_table(ctx, [running]), 200)
        assert word in render.to_plain(render.sessions_table(ctx, [{**running, "wrap_up": True}]), 200)
    ctx = render.RenderContext(lang="en")
    assert render._session_status(ctx, {**running, "wrap_up": True, "paused": True})[0] == "paused"
    assert render._session_status(ctx, {**running, "wrap_up": True, "status": "stopped"})[0] == "stopped"
    assert session_status("en", {**running, "wrap_up": False}) == "running"
    assert session_status("en", {**running, "wrap_up": True, "paused": True}) == "paused"
    assert session_status("en", {**running, "wrap_up": True, "status": "stopped"}) == "stopped"


def test_launcher_mcp_session_line_shows_wrap_up():
    """The sessions list of the launcher's MCP tools: a running session in wrap-up mode, and a pause winning."""
    from dreamteam.launcher_mcp import session_line

    base = {"id": "001-x", "title": "X", "status": "running", "tasks": {"done": 1, "total": 3},
            "agents": {"total": 2, "working": 1}, "spent_usd": 0.5, "max_cost_usd": 5}
    assert session_line({**base, "wrap_up": True}).startswith("001-x [running, wrapping up] X | tasks 1/3 done")
    assert session_line({**base, "wrap_up": True, "paused": True, "pause_reason": "budget"}).startswith(
        "001-x [paused (budget)] X")
    assert session_line({**base, "wrap_up": True, "status": "stopped"}).startswith("001-x [stopped] X")
    assert session_line(base).startswith("001-x [running] X")


def test_read_stats_defaults_without_a_readable_database(tmp_path, monkeypatch):
    """A missing database, one that cannot be opened and a file that is no database give the same defaults: not
    paused, no wrap-up, nothing counted (the launcher's cards and lists rely on them)."""
    import sqlite3

    import dreamteam.sessions as sessions_module

    defaults = {
        "goal": None, "tasks": {"total": 0, "done": 0, "open": 0, "by_status": {}},
        "agents": {"total": 0, "working": 0, "llm": 0, "external": 0}, "spent_usd": 0.0,
        "limits": {}, "paused": False, "pause_reason": "", "wrap_up": False, "last_activity": None,
        "jobs": {"running": 0},
    }
    assert read_stats(tmp_path / "nope.db") == defaults
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"this is not a sqlite database" * 10)
    assert read_stats(garbage) == defaults

    def refuse(*args, **kwargs):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(sessions_module.sqlite3, "connect", refuse)
    assert read_stats(garbage) == defaults


def test_console_wrapup_command():
    from dreamteam.console import HELP, WRAP_UP_OFF, WRAP_UP_ON, Console

    console = Console("http://127.0.0.1:9/s/001-x", "en")
    acts: list[tuple[str, dict]] = []
    console.hub = SimpleNamespace(act=lambda tool, **args: acts.append((tool, args)) or "Done.")
    assert console._dispatch("/wrapup") == "Done."
    console._dispatch("/wrapup off")
    assert "/wrapup [off]" in console._dispatch("/wrapup soon")
    # the TUI's words too (regression: the console took only on/off)
    console._dispatch("/wrapup вкл")
    console._dispatch("/wrapup Выкл")
    assert acts == [("swarm_control", {"wrap_up": True}), ("swarm_control", {"wrap_up": False}),
                    ("swarm_control", {"wrap_up": True}), ("swarm_control", {"wrap_up": False})]
    assert (WRAP_UP_ON, WRAP_UP_OFF) == (commands.WRAP_UP_ON, commands.WRAP_UP_OFF)
    assert all("/wrapup [off]" in HELP[lang] for lang in ("en", "ru"))


@needs_node
def test_board_wrap_up_button_banner_and_feed():
    out = run_js(r"""
      app.S.lang = "en";
      for (const id of ["back-link", "stopped-banner", "paused-banner", "workspace-banner", "wrapup-banner", "goal-status",
                        "mcp-badge", "triggers-badge", "jobs-badge", "team-unread"])
        el(id, { classList: classList(["hidden"]) });
      for (const id of ["swarm-name", "mcp-hint", "pause-btn", "budget-bar", "goal-btn", "agents-count", "workspace-info"]) el(id);
      const attrs = {};
      el("wrap-btn", { setAttribute: (k, v) => { attrs[k] = v; } });
      const state = (wrap_up) => ({ swarm: { name: "S", goal: null, limits: { max_cost_usd: 0, max_agents: 3 }, spent_usd: 0,
                                             mcp_url: "http://h/mcp", paused: false, wrap_up,
                                             session: { id: "s", title: "S", status: "running" } },
                                    agents: [], mcp: [], triggers: [], jobs: [] });
      const look = () => [elements["wrap-btn"].innerHTML, elements["wrap-btn"].title, elements["wrap-btn"].classList.contains("on"),
                          attrs["aria-pressed"], elements["wrapup-banner"].classList.contains("hidden"),
                          elements["wrapup-banner"].textContent];
      app.S.state = state(false); app.renderHeader(); out.off = look();
      calls.length = 0; await app.toggleWrapUp(); out.on_call = calls.map((c) => [c.url, c.body]);
      app.S.state = state(true); app.renderHeader(); out.on = look();
      calls.length = 0; await app.toggleWrapUp(); out.off_call = calls.map((c) => [c.url, c.body]);
      const ev = (on) => ({ type: "swarm.wrap_up", actor: "user", data: { on } });
      out.feed = [app.describeEvent(ev(true)), app.describeEvent(ev(false))];
      app.S.lang = "ru";
      out.feedRu = [app.describeEvent(ev(true)), app.describeEvent(ev(false))];
      const home = loadHomeJs();
      home.H.lang = "en";
      const card = (extra) => home.sessionCard(Object.assign({ id: "s1", title: "S", status: "running", paused: false }, extra));
      out.cards = [card({ wrap_up: true }), card({ wrap_up: false }), card({ wrap_up: true, status: "stopped" })];
    """)
    html, title, pressed, aria, hidden, banner = out["off"]
    assert '<i class="ic ic-check"></i>' in html and '<span class="btn-label">Wrap up</span>' in html
    assert title.startswith("Ask the team to wrap up") and not pressed and aria == "false" and hidden and banner == ""
    assert out["on_call"] == [["/api/act/swarm_control", {"wrap_up": True}]]
    html, title, pressed, aria, hidden, banner = out["on"]
    assert title == "Wrap-up mode is on: click to switch it off" and pressed and aria == "true" and not hidden
    assert banner.startswith("Wrap-up mode: the agents finish their tasks with what they have")
    assert out["off_call"] == [["/api/act/swarm_control", {"wrap_up": False}]]
    assert out["feed"][0].endswith("switched wrap-up mode on: the agents finish their tasks with what they have")
    assert out["feed"][1].endswith("switched wrap-up mode off")
    assert "включил(а) режим завершения" in out["feedRu"][0] and "выключил(а) режим завершения" in out["feedRu"][1]
    on, off, stopped = out["cards"]
    assert '<span class="badge warn" title="Wrap-up mode: the agents finish' in on and ">Wrapping up</span>" in on
    assert "Wrapping up" not in off and "Wrapping up" not in stopped


def test_board_markup_and_i18n():
    from test_ui import APP_JS, INDEX, i18n_blocks

    assert '<button class="btn wrap-btn" id="wrap-btn" type="button" aria-pressed="false"></button>' in INDEX
    assert INDEX.index('id="pause-btn"') < INDEX.index('id="wrap-btn"') < INDEX.index('id="settings-btn"')
    assert 'id="wrapup-banner"' in INDEX
    for lang in ("ru", "en"):
        assert {"wrap_up", "wrap_up_hint", "wrap_up_on_hint", "wrap_up_banner"} <= i18n_blocks()[lang]
    assert 'wrap_up: "Завершать"' in APP_JS and 'wrap_up: "Wrap up"' in APP_JS


def test_mailbox_marks_only_broadcast_recipients(make_fake_swarm):
    from dreamteam.mailbox import Mailbox

    swarm = make_fake_swarm({})
    for name in (ANNA, BORIS, CARL):
        swarm.add_member(name)
    mail = Mailbox(swarm)
    assert mail._recipients(BORIS, "all") == ([ANNA, CARL], {ANNA, CARL})
    assert mail._recipients(BORIS, ["all", ANNA]) == ([ANNA, CARL], {CARL})
    assert mail._recipients(BORIS, [ANNA, CARL]) == ([ANNA, CARL], set())
    with pytest.raises(ToolError):
        mail._recipients(BORIS, [BORIS])
    # a test double without waits_for_job: every recipient is signalled, as before
    swarm.signalled = []
    swarm.signal = swarm.signalled.append
    mail.send(BORIS, "all", "hi")
    assert swarm.signalled == [ANNA, CARL]
