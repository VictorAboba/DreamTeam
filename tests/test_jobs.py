"""Background jobs (docs/SPEC.md §7.1). The JobManager core: the timer and initiative paths, leftovers, limits,
lifetimes, stops, the console intercept, the session stop and retirement, one finisher per job, crash recovery,
liveness and logs, the foreground cap and the thread caps of a command; the manager is built directly on a FakeSwarm
with a real Workspace. Then the swarm integration: the owners' notices and the job.* events, and a real Swarm with
scripted agents (snapshot, wake-up text, retire, stop and restart, the scheduler's busy rules). Last, end to end
through the `shell` tool: a two-agent session whose test run goes on in the background until its end wakes its
owner, and print mode with an owned launcher.

Child processes are a Python script outside the workspace; each writes its pid, and the `kids` fixture checks
that none is left running."""

from __future__ import annotations

import asyncio
import dataclasses
import io
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest

from dreamteam import jobs as jobs_module
from dreamteam import settings as settings_module
from dreamteam import swarm as swarm_module
from dreamteam import tui
from dreamteam.board import Board
from dreamteam.config import Config, ShellSettings, render_template
from dreamteam.db import Database, loads
from dreamteam.jobs import (JobManager, ShellOutcome, fmt_duration, format_names, parse_console, parse_job_id,
                            short_command)
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.models import USER, AgentKind, ToolError
from dreamteam.sessions import read_stats
from dreamteam.swarm import QUIET_KEY
from dreamteam.tools import ToolContext, call_tool
from dreamteam.tui.client import connect
from dreamteam.tui.printmode import run_print
from dreamteam.workspace import Workspace
from test_procs import Children, kill_pid, pid_alive, wait_until
from test_runtime import call, history, kinds_for, make_swarm, shutdown

WINDOWS = sys.platform == "win32"
LINUX = sys.platform.startswith("linux")
PROGRAM = "cmd" if WINDOWS else "sh"
ANNA, BORIS, EXT = "Anna Smith", "Boris King", "Claude Code"
ENV = {**os.environ, "PYEXE": sys.executable}
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def py(*args: str) -> str:
    """`$PYEXE args` in the syntax of PROGRAM."""
    if WINDOWS:
        return '"%PYEXE%" ' + " ".join(f'"{a}"' for a in args)
    return '"$PYEXE" ' + " ".join(f"'{a}'" for a in args)


def leave(*args: str, then: float = 0) -> str:
    """A command that starts `$PYEXE args` in the background itself and exits (after `then` seconds)."""
    if WINDOWS:
        wait = f" & ping -n {int(then) + 1} 127.0.0.1 > nul" if then else ""
        return f'start /b "" {py(*args)}{wait} & echo started'
    return f"{py(*args)} & " + (f"sleep {then}; " if then else "") + "echo started"


def shell_set(swarm, **values) -> None:
    for key, value in values.items():
        setattr(swarm.config.shell, key, value)


def row(swarm, job_id: int) -> dict:
    found = swarm.db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    return dict(found) if found else {}


async def until(predicate, timeout: float = 15.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        await asyncio.sleep(0.05)
    assert predicate(), "condition not reached in time"


def dead_if_started(kids, index: int = -1, timeout: float = 3.0) -> bool:
    """A child killed with its command may die before it wrote its pid: then it never ran (a late writer would be
    caught by the `kids` check at the end)."""
    path = kids.files[index]
    if not (path.exists() and path.read_text().strip()):
        return True
    pid = int(path.read_text())
    return wait_until(lambda: not pid_alive(pid), timeout)


async def apid(kids, index: int = -1, timeout: float = 20.0) -> int:
    """kids.pid() without blocking the loop (a run() in flight needs it)."""
    path = kids.files[index]
    await until(lambda: path.exists() and path.read_text().strip(), timeout)
    return int(path.read_text())


@pytest.fixture
def kids(tmp_path):
    folder = tmp_path / "kids"
    folder.mkdir()
    children = Children(folder)
    children.script = str(folder / "child.py")
    yield children
    assert children.check() == [], "child processes were left running"


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(jobs_module, "JOB_TICK", 0.1)
    monkeypatch.setattr(jobs_module, "LEFTOVER_GRACE", 0.3)
    monkeypatch.setattr(jobs_module, "BACKGROUND_GRACE", 0.5)
    monkeypatch.setattr(jobs_module, "STOP_GRACE", 1.0)


async def make_manager(make_fake_swarm, git: bool = False):
    swarm = make_fake_swarm({"swarm": {"language": "en"}, "shell": {"program": PROGRAM}, "workspace": {"git": git}})
    workspace = Workspace(swarm)
    swarm.workspace = workspace
    await workspace.init()
    swarm.board = Board(swarm)
    for name in (ANNA, BORIS):
        swarm.add_member(name)
    swarm.add_member(EXT, kind=AgentKind.EXTERNAL)
    return swarm, JobManager(swarm)


@pytest.fixture
async def setup(make_fake_swarm, fast, kids):
    swarm, manager = await make_manager(make_fake_swarm)
    try:
        yield swarm, manager
    finally:
        await manager.stop_all()


@pytest.fixture
async def git_setup(make_fake_swarm, fast, kids):
    swarm, manager = await make_manager(make_fake_swarm, git=True)
    try:
        yield swarm, manager
    finally:
        await manager.stop_all()


async def run(manager, actor, command, timeout=1, background=False, max_output=4000):
    return await manager.run(actor, command, timeout=timeout, env=ENV, max_output=max_output, background=background)


def git_out(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True).stdout.decode("utf-8")


def job_notices(swarm, name: str) -> list[tuple[str, bool, str]]:
    """(kind, quiet, text) of the job notices `name` got, in order."""
    rows = swarm.db.all("SELECT kind, data, text FROM notifications WHERE recipient = ? AND kind LIKE 'n_job_%'"
                        " ORDER BY id", (name,))
    return [(r["kind"], bool(loads(r["data"], {}).get(QUIET_KEY)), r["text"]) for r in rows]


PUBLIC_KEYS = {"id", "label", "owner", "command", "origin", "status", "reason", "stopped_by", "exit_code",
               "started_at", "background_at", "ended_at", "log", "processes"}


# --- parsing and texts ---------------------------------------------------------------------------------------------

def test_parse_console_and_job_ids():
    assert parse_console("jobs") == ("list", [], False)
    assert parse_console("  jobs -l ;") == ("list", [], False)
    assert parse_console("jobs -p") == ("list", [], False)
    for line in ("jobs j2", "jobs %2", "jobs 2", "jobs -l j2"):
        assert parse_console(line) == ("show", [2], False), line
    assert parse_console("kill j3") == ("kill", [3], False)
    assert parse_console("kill %3") == ("kill", [3], False)
    assert parse_console("kill j3;") == ("kill", [3], False)
    assert parse_console("kill -9 j3 j4") == ("kill", [3, 4], True)
    assert parse_console("kill -s KILL %1") == ("kill", [1], True)
    assert parse_console("kill -TERM j1") == ("kill", [1], False)
    for line in ("kill 3", "kill j3; npm start", "kill -l", "Jobs", "jobs | wc -l", "echo kill j3", "kill", "jobsx",
                 "kill j0"):
        assert parse_console(line) is None, line
    for text in ("j3", "J3", "%3", "#3", "3", " j3 ", 3):
        assert parse_job_id(text) == 3, text
    for text in ("", "j", "x3", "j-3", "0", None, True, "3.5"):
        assert parse_job_id(text) is None, text


def test_small_formats():
    assert [fmt_duration(s) for s in (45, 12 * 60 + 5, 2 * 3600 + 5 * 60)] == ["45 s", "12 min", "2 h 05 min"]
    assert [fmt_duration(s, "ru") for s in (45, 12 * 60, 7500)] == ["45 с", "12 мин", "2 ч 05 мин"]
    assert short_command("npm   run\n dev", 60) == "npm run dev"
    cut = short_command("a" * 50 + "b" * 50, 21)
    assert len(cut) == 21 and cut.startswith("a" * 10) and cut.endswith("b" * 10) and "…" in cut
    assert format_names(["node.exe", "python.exe", "node.exe"]) == "node.exe ×2, python.exe"
    assert format_names([]) == "processes"


def test_render_texts(make_fake_swarm):
    cfg = make_fake_swarm({}).config.shell
    cfg.max_timeout = 1800

    class FakeJob:
        id = 3
        log = log_shown = ".scratch/anna.smith/jobs/j3.log"

    timeout = ShellOutcome("background", None, "so far", "bash", 120, job=FakeJob())
    new, old = timeout.render(has_jobs_tool=True, cfg=cfg), timeout.render(has_jobs_tool=False, cfg=cfg)
    assert new.startswith("STILL RUNNING after 120s: continues in the background as job j3 [bash]\nso far\n\n[Job j3")
    assert "jobs with job_id \"j3\"" in new and "30 min after it started" in new and "8 h 00 min" in new
    assert "run the shell command kill j3" in old
    # an old agent backgrounds a server itself: in PowerShell only Start-Process -NoNewWindow keeps its output in the
    # job (without it the program writes into a window of its own on the user's desktop)
    assert "(end the command with &, in PowerShell use Start-Process -NoNewWindow)" in old
    no_time = ShellOutcome("timeout", None, "", "cmd", 1800, note="no_time").render(has_jobs_tool=False, cfg=cfg)
    assert no_time.endswith("(end it with &, in PowerShell use Start-Process -NoNewWindow).]")
    # the tool that sleeps until the end is announced: `wait`, or `wait_for_events` for an external member
    assert "or call wait if nothing else is left" in new
    external = timeout.render(has_jobs_tool=True, cfg=cfg, has_wait=False)
    assert "or call wait_for_events if nothing else is left" in external and "call wait " not in external
    detached = ShellOutcome("detached", 0, "", "bash", 120, job=FakeJob(), names=["node", "node"])
    old = detached.render(has_jobs_tool=False, cfg=cfg)
    assert old.startswith("exit code: 0 [bash]; processes it started keep running as background job j3\n(no output)")
    assert "Still running: node ×2." in old
    for state in ("background", "detached"):  # old agents are never pointed at tools they lack
        text = ShellOutcome(state, 0, "x", "bash", 1, job=FakeJob()).render(has_jobs_tool=False, cfg=cfg)
        assert "job_stop" not in text and "jobs with" not in text
    cfg.job_max_minutes = 0
    started = ShellOutcome("started", None, "", "bash", 2, job=FakeJob()).render(has_jobs_tool=True, cfg=cfg)
    assert "(no output yet)" in started and "may run up to the end of the session" in started
    plain = ShellOutcome("timeout", None, "out\n", "cmd", 1).render(has_jobs_tool=True, cfg=cfg)
    assert plain == "TIMEOUT after 1s (process tree killed) [cmd]\nout\n"
    limited = ShellOutcome("timeout", None, "", "cmd", 1, note="limit_member",
                           note_args={"n": 2, "ids": "j1, j2"}).render(has_jobs_tool=False, cfg=cfg)
    assert limited.endswith("[It was not moved to the background: you already have 2 background jobs (j1, j2), the "
                            "most allowed. Stop one you no longer need (run the shell command kill jN on its own) or "
                            "wait for one to end.]")
    # the head line and the footer survive any cut: only the output in between is cut
    long = ShellOutcome("background", None, "x" * 50000, "bash", 1, job=FakeJob(), max_output=2000)
    text = long.render(has_jobs_tool=True, cfg=cfg)
    assert len(text) <= 2000 and text.startswith("STILL RUNNING") and text.endswith(".]")


# --- the timer and initiative paths -----------------------------------------------------------------------------

async def test_timer_moves_to_background(setup, kids):
    swarm, manager = setup
    out = await run(manager, ANNA, py(kids.script, "3", kids.pidfile()), timeout=1)
    pid = kids.pid()
    assert out.state == "background" and out.job is not None and out.job.label == "j1"
    assert "child up" in out.output
    text = out.render(has_jobs_tool=True, cfg=swarm.config.shell)
    assert text.startswith(f"STILL RUNNING after 1s: continues in the background as job j1 [{PROGRAM}]\nchild up")
    record = row(swarm, 1)
    assert record["status"] == "running" and record["origin"] == "timeout" and record["owner"] == ANNA
    assert record["log"] == ".scratch/anna.smith/jobs/j1.log" and record["hub_pid"] == os.getpid()
    log = swarm.workspace.root / record["log"]
    assert log.is_file()
    assert manager.busy(ANNA) and not manager.busy(BORIS)
    await until(lambda: row(swarm, 1)["status"] == "ended", 15)
    record = row(swarm, 1)
    assert record["exit_code"] == 0 and record["reason"] is None and "child end" in record["tail"]
    text = log.read_text(encoding="utf-8")
    assert text.startswith(f"# job j1 of {ANNA} [{PROGRAM}]") and "child up" in text and "child end" in text
    assert "# ended" in text.splitlines()[-1] and "exit code 0" in text.splitlines()[-1]
    assert not pid_alive(pid) and manager.running_of() == []


async def test_background_request(setup, kids):
    swarm, manager = setup
    started = time.monotonic()
    out = await run(manager, ANNA, py(kids.script, "20", kids.pidfile()), timeout=120, background=True)
    assert out.state == "started" and time.monotonic() - started < jobs_module.BACKGROUND_GRACE + 3
    assert out.job.origin == "background" and row(swarm, 1)["origin"] == "background"
    assert out.render(has_jobs_tool=True, cfg=swarm.config.shell).startswith(
        f"STARTED in the background as job j1 [{PROGRAM}]")
    quick = await run(manager, ANNA, "echo hi", background=True)
    assert quick.state == "quick" and quick.job is None and quick.output.strip() == "hi"
    assert quick.render(has_jobs_tool=True, cfg=swarm.config.shell).startswith(
        f"exit code: 0 [{PROGRAM}] (it ended within 0.5 s, so it did not stay in the background)")
    assert swarm.db.scalar("SELECT COUNT(*) FROM jobs") == 1


async def test_log_falls_back_when_scratch_is_not_a_folder(setup, kids):
    """`.scratch` replaced by a file (or a link elsewhere): the log goes to the session's data folder, and every
    message says how to read it."""
    swarm, manager = setup
    scratch = swarm.workspace.root / ".scratch"
    shutil.rmtree(scratch)
    scratch.write_text("not a folder", encoding="utf-8")
    out = await run(manager, ANNA, py(kids.script, "20", kids.pidfile()))
    assert out.state == "background"
    expected = Path(swarm.config.data_dir) / "jobs" / "j1.log"
    assert out.job.log == str(expected) and expected.is_file()
    assert f"{expected} (outside the workspace: read it with shell)" in out.render(has_jobs_tool=True,
                                                                                   cfg=swarm.config.shell)
    assert out.job.notice_tail() == "  child up"


async def test_leftovers_become_a_detached_job(setup, kids):
    swarm, manager = setup
    out = await run(manager, ANNA, leave(kids.script, "20", kids.pidfile()), timeout=30)
    pid = kids.pid()
    assert out.state == "detached" and out.code == 0 and "started" in out.output
    job = out.job
    assert job.origin == "detached" and job.command_done and job.exit_code == 0
    assert row(swarm, job.id)["exit_code"] == 0 and row(swarm, job.id)["origin"] == "detached"
    assert any(name.lower().startswith("python") for name in out.names) or not (WINDOWS or LINUX)
    text = out.render(has_jobs_tool=True, cfg=swarm.config.shell)
    assert text.startswith(f"exit code: 0 [{PROGRAM}]; processes it started keep running as background job j1")
    assert not manager.busy(ANNA)  # detached jobs never keep their owner busy
    assert pid_alive(pid)
    assert "Stopped job j1" in await manager.stop(job.id, ANNA, force=True)
    assert not pid_alive(pid)


async def test_a_job_whose_shell_ended_first_ends_with_no_code_of_its_own(setup, kids):
    """background=true with `start /b` / `&`: the shell exits at once with 0 (the call's result says so) and the
    program it started runs on as the job. When that program crashes later, nothing reports the shell's old code as
    the job's end: the row, the event, the notice, the log and the views say that all its processes exited."""
    swarm, manager = setup
    crasher = Path(kids.folder) / "crasher.py"
    crasher.write_text("import os, pathlib, sys, time\npathlib.Path(sys.argv[2]).write_text(str(os.getpid()))\n"
                       "print('server listening', flush=True)\ntime.sleep(float(sys.argv[1]))\n"
                       "print('FATAL: out of memory', flush=True)\nsys.exit(3)\n", encoding="utf-8")
    out = await run(manager, ANNA, leave(str(crasher), "1.5", kids.pidfile()), background=True)
    assert out.state == "started" and out.code == 0 and out.job.origin == "background" and out.job.command_done
    assert row(swarm, 1)["exit_code"] == 0  # while it runs: the shell's code, which the result reported
    await until(lambda: row(swarm, 1).get("status") == "ended" and job_notices(swarm, ANNA), 15)
    record = row(swarm, 1)
    assert record["exit_code"] is None and "FATAL: out of memory" in record["tail"]
    ended = [e for e in swarm.bus.recent(types=("job.*",)) if e["type"] == "job.ended"][-1]["data"]["job"]
    assert (ended["status"], ended["exit_code"]) == ("ended", None)
    [(kind, quiet, text)] = job_notices(swarm, ANNA)
    assert kind == "n_job_done" and not quiet  # the end of a background job wakes its owner (no goal yet)
    assert text.startswith("Your background job j1 has ended (all its processes exited) after running ")
    assert "  FATAL: out of memory" in text
    last = (swarm.workspace.root / record["log"]).read_text(encoding="utf-8").splitlines()[-1]
    assert last.startswith("# ended ") and last.endswith(": all its processes exited")
    assert re.search(r"\nState: ended after \d+ s \(all its processes exited\), \d+ s ago",
                     manager.describe(1, actor=ANNA))
    assert re.search(r"\n- j1 .+: ended after \d+ s \(all its processes exited\), \d+ s ago", manager.listing(ANNA))
    assert manager._end_phrase(record).startswith("ended after ")


async def test_limits(setup, kids):
    swarm, manager = setup
    shell_set(swarm, max_jobs_per_member=1)
    first = await run(manager, ANNA, py(kids.script, "20", kids.pidfile()))
    assert first.state == "background"
    second = await run(manager, ANNA, py(kids.script, "20", kids.pidfile()))
    assert second.state == "timeout" and second.note == "limit_member" and second.job is None
    assert dead_if_started(kids)
    assert "you already have 1 background jobs (j1)" in second.render(has_jobs_tool=True, cfg=swarm.config.shell)
    with pytest.raises(ToolError, match=r"You already have 1 background jobs \(j1\), the most allowed"):
        await run(manager, ANNA, "echo never", background=True)
    third = await run(manager, ANNA, leave(kids.script, "20", kids.pidfile()), timeout=30)
    assert third.state == "leftovers_killed" and third.code == 0
    assert dead_if_started(kids)
    assert third.render(has_jobs_tool=True, cfg=swarm.config.shell).startswith(
        f"exit code: 0 [{PROGRAM}]; the processes it left running were stopped")
    other = await run(manager, BORIS, py(kids.script, "20", kids.pidfile()))  # the limit is per member
    assert other.state == "background"
    shell_set(swarm, max_jobs_per_member=4, max_jobs=2)
    over = await run(manager, ANNA, py(kids.script, "20", kids.pidfile()))
    assert over.state == "timeout" and over.note == "limit_session"
    shell_set(swarm, max_jobs=0)
    disabled = await run(manager, BORIS, py(kids.script, "20", kids.pidfile()))
    assert disabled.state == "timeout" and disabled.note == "disabled"
    with pytest.raises(ToolError, match="Background jobs are turned off"):
        await run(manager, BORIS, "echo never", background=True)
    shell_set(swarm, max_jobs=16, background_on_timeout=False)
    plain = await run(manager, BORIS, py(kids.script, "20", kids.pidfile()))
    assert plain.render(has_jobs_tool=True, cfg=swarm.config.shell) == (
        f"TIMEOUT after 1s (process tree killed) [{PROGRAM}]\nchild up\n")
    shell_set(swarm, background_on_timeout=True, max_timeout=1)
    no_time = await run(manager, BORIS, py(kids.script, "20", kids.pidfile()))
    assert no_time.state == "timeout" and no_time.note == "no_time"
    assert no_time.render(has_jobs_tool=True, cfg=swarm.config.shell).endswith(
        "[1 s is the longest a command may run unless it was started in the background on purpose "
        "(background=true).]")
    for index in (1, 2, 4, 5, 6, 7):  # every child but the two jobs' was killed
        assert dead_if_started(kids, index), index


async def test_lifetimes(setup, kids):
    swarm, manager = setup
    shell_set(swarm, max_timeout=3)
    out = await run(manager, ANNA, py(kids.script, "30", kids.pidfile()))
    assert out.state == "background"
    pid = kids.pid()
    await until(lambda: row(swarm, out.job.id).get("status") == "stopped", 15)
    record = row(swarm, out.job.id)
    assert record["reason"] == "lifetime" and 2.5 <= record["ended_at"] - record["started_at"] < 10
    assert wait_until(lambda: not pid_alive(pid), 3)
    shell_set(swarm, max_timeout=1800, job_max_minutes=0.02)  # 1.2 s
    out = await run(manager, ANNA, leave(kids.script, "30", kids.pidfile()), timeout=30)
    assert out.state == "detached"
    await until(lambda: row(swarm, out.job.id).get("status") == "stopped", 15)
    assert row(swarm, out.job.id)["reason"] == "lifetime"
    assert dead_if_started(kids)


async def test_stop_permissions_and_results(setup, kids):
    swarm, manager = setup
    out = await run(manager, ANNA, py(kids.script, "20", kids.pidfile()))
    pid = kids.pid()
    with pytest.raises(ToolError, match="Job j1 belongs to Anna Smith; only its owner or the user can stop it"):
        await manager.stop(1, BORIS)
    text = await manager.stop(1, ANNA)
    assert text.startswith(f"Stopped job j1 ({py(kids.script, '20', kids.files[0])}"[:40])
    assert "of Anna Smith after" in text and "Last output:\nchild up" in text
    assert not pid_alive(pid)
    record = row(swarm, 1)
    assert (record["status"], record["reason"], record["stopped_by"], record["exit_code"]) == (
        "stopped", "owner", ANNA, None)
    assert (await manager.stop(1, ANNA)).startswith("Job j1 had already ended (stopped by its owner after")
    with pytest.raises(ToolError, match=r"^There is no job j9\. No jobs are running\.$"):
        await manager.stop(9, ANNA)
    await run(manager, ANNA, py(kids.script, "20", kids.pidfile()))
    with pytest.raises(ToolError, match=r"^There is no job j9\. Running jobs: j2\.$"):
        await manager.stop(9, USER)
    started = time.monotonic()
    assert (await manager.stop(2, USER, force=True)).startswith("Stopped job j2")
    assert time.monotonic() - started < jobs_module.STOP_GRACE + 2
    assert (row(swarm, 2)["reason"], row(swarm, 2)["stopped_by"]) == ("user", USER)
    assert dead_if_started(kids)
    del out


async def test_views(setup, kids):
    swarm, manager = setup
    await run(manager, ANNA, py(kids.script, "20", kids.pidfile()))
    await run(manager, BORIS, py(kids.script, "20", kids.pidfile()), background=True)
    listing = manager.listing(ANNA)
    lines = listing.splitlines()
    assert lines[0] == "Running background jobs (2):"
    assert lines[1].startswith("- j1 ") and ": Anna Smith (you), running " in lines[1]
    assert lines[1].endswith("went to the background at its timeout; log .scratch/anna.smith/jobs/j1.log")
    assert ": Boris King, running " in lines[2] and lines[2].endswith("started in the background")
    shown = manager.describe(1, actor=BORIS, lines=5, max_chars=4000)
    assert shown.startswith("Job j1 of Anna Smith: ") and f"\nState: running for " in shown
    assert f"(went to the background at its timeout) [{PROGRAM}]" in shown and "child up" in shown
    await manager.stop(1, ANNA, force=True)
    assert "Your jobs that ended in the last hour:\n- j1 " in manager.listing(ANNA)
    assert manager.listing(ANNA).splitlines()[-1].endswith("stopped by its owner after 0 s, 0 s ago") or \
        "stopped by its owner" in manager.listing(ANNA)
    assert "Your jobs that ended" not in manager.listing(BORIS)
    assert "Jobs that ended in the last hour:\n- j1 " in manager.listing(USER)
    ended = manager.describe(1, actor=ANNA, max_chars=4000)
    assert "State: stopped by its owner after" in ended and "below is its end" in ended and "# stopped" in ended
    (swarm.workspace.root / row(swarm, 1)["log"]).unlink()
    gone = manager.describe(1, actor=ANNA, max_chars=4000)
    assert "(log file not found)" in gone and "child up" in gone  # the stored tail
    with pytest.raises(ToolError, match="There is no job j7"):
        manager.describe(7, actor=ANNA)
    snap = manager.snapshot()
    assert [j["label"] for j in snap] == ["j2", "j1"] and snap[0]["status"] == "running"
    assert set(snap[0]) == {"id", "label", "owner", "command", "origin", "status", "reason", "stopped_by",
                            "exit_code", "started_at", "background_at", "ended_at", "log", "processes"}
    assert manager.get(1)["status"] == "stopped" and manager.get(2)["origin"] == "background"
    assert manager.get(5) is None


async def test_console_intercept(git_setup, kids, monkeypatch):
    swarm, manager = git_setup
    assert await manager.console(ANNA, "echo hi") is None
    await run(manager, ANNA, py(kids.script, "20", kids.pidfile()))
    commits = git_out(swarm.workspace.root, "rev-list", "--all", "--count")

    async def no_spawn(*args, **kwargs):
        raise AssertionError("the intercept started a process")

    monkeypatch.setattr(jobs_module, "start_command", no_spawn)
    listing = await manager.console(ANNA, "jobs")
    assert listing.splitlines()[0] == "Running background jobs (1): [dreamteam]" and "- j1 " in listing
    shown = await manager.console(BORIS, "jobs j1;")
    assert shown.splitlines()[0].endswith(" [dreamteam]") and shown.startswith("Job j1 of Anna Smith")
    with pytest.raises(ToolError, match=r"belongs to Anna Smith.* \[dreamteam\]$"):
        await manager.console(BORIS, "kill j1")
    with pytest.raises(ToolError, match=r"^There is no job j9\. Running jobs: j1\. \[dreamteam\]$"):
        await manager.console(ANNA, "kill %9")
    stopped = await manager.console(ANNA, "kill -9 j1")
    assert stopped.splitlines()[0].startswith("Stopped job j1") and stopped.splitlines()[0].endswith(" [dreamteam]")
    assert dead_if_started(kids)
    assert git_out(swarm.workspace.root, "rev-list", "--all", "--count") == commits  # nothing committed


# --- stopping everything ---------------------------------------------------------------------------------------

async def test_stop_all_kills_and_closes(setup, kids, tmp_path):
    swarm, manager = setup
    timeout_job = await run(manager, ANNA, py(kids.script, "30", kids.pidfile()))
    background_job = await run(manager, BORIS, py(kids.script, "30", kids.pidfile()), background=True)
    detached_job = await run(manager, ANNA, leave(kids.script, "30", kids.pidfile()), timeout=30)
    assert [timeout_job.state, background_job.state, detached_job.state] == ["background", "started", "detached"]
    foreground = asyncio.ensure_future(run(manager, EXT, py(kids.script, "30", kids.pidfile()), timeout=30))
    pids = [await apid(kids, i) for i in range(4)]
    await until(lambda: any(fg.actor == EXT for fg in manager._foreground.values()))
    started = time.monotonic()
    await manager.stop_all()
    assert time.monotonic() - started < 10
    assert foreground.done()  # stop_all waited for the foreground call
    result = foreground.result()
    assert result.state == "killed" and result.render(has_jobs_tool=True, cfg=swarm.config.shell).startswith(
        f"KILLED: the session is stopping [{PROGRAM}]")
    for pid in pids:
        assert not pid_alive(pid)
    assert [(r["status"], r["reason"]) for r in swarm.db.all("SELECT status, reason FROM jobs ORDER BY id")] == [
        ("stopped", "session")] * 3
    assert all(job.cmd.tree.released for job in (timeout_job.job, background_job.job, detached_job.job))
    root = swarm.workspace.root
    moved = root.with_name("moved")
    os.rename(root, moved)  # no handle (log, process, cwd) is left in the workspace
    os.rename(moved, root)
    with pytest.raises(ToolError, match="The session is stopping: no new commands are started."):
        await run(manager, ANNA, "echo hi")
    await manager.stop_all()  # idempotent
    manager.recover()  # a restart opens it again
    assert (await run(manager, ANNA, "echo again")).state == "exited"


async def test_stop_owner_kills_only_that_member(setup, kids):
    swarm, manager = setup
    await run(manager, ANNA, py(kids.script, "30", kids.pidfile()))
    await run(manager, BORIS, py(kids.script, "30", kids.pidfile()))
    anna, boris = kids.pid(0), kids.pid(1)
    assert await manager.stop_owner(ANNA) == [1]
    assert not pid_alive(anna) and pid_alive(boris)
    assert (row(swarm, 1)["status"], row(swarm, 1)["reason"]) == ("stopped", "retired")
    assert row(swarm, 2)["status"] == "running"
    call = asyncio.ensure_future(run(manager, EXT, py(kids.script, "30", kids.pidfile()), timeout=30))
    ext = await apid(kids)
    assert await manager.stop_owner(EXT) == []
    assert call.done() and call.result().state == "killed"  # awaited by stop_owner
    assert call.result().render(has_jobs_tool=True, cfg=swarm.config.shell).startswith("KILLED: you are leaving")
    assert not pid_alive(ext) and swarm.db.scalar("SELECT COUNT(*) FROM jobs") == 2


async def test_forbidden_owner_gets_no_job(setup, kids):
    swarm, manager = setup
    call = asyncio.ensure_future(run(manager, ANNA, py(kids.script, "30", kids.pidfile()), timeout=3))
    pid = await apid(kids)
    manager.forbid(ANNA)
    result = await call
    assert result.state == "killed" and result.note == "retired"
    assert swarm.db.scalar("SELECT COUNT(*) FROM jobs") == 0 and not pid_alive(pid)
    with pytest.raises(ToolError, match="You are leaving the swarm: no new commands are started."):
        await run(manager, ANNA, "echo hi")
    assert (await run(manager, BORIS, "echo hi")).state == "exited"


async def test_one_finisher(setup, kids, monkeypatch):
    """A job ending by itself while its owner stops it: one row update, one commit, one notice, and the stop
    reports that it had already ended."""
    swarm, manager = setup
    commits, ended, notices = [], [], []

    async def slow_commit(actor, message="", **kw):
        commits.append((kw["kind"], kw["detail"].split("\n")[0]))
        await asyncio.sleep(1.0)

    monkeypatch.setattr(swarm.workspace, "autocommit", slow_commit)
    monkeypatch.setattr(manager, "_announce_ended", lambda job: ended.append(job.id))
    monkeypatch.setattr(manager, "_announce_end_notice", lambda job: notices.append(job.id))
    out = await run(manager, ANNA, py(kids.script, "1.5", kids.pidfile()))
    job = out.job
    await until(lambda: job.finishing, 10)  # the monitor claimed it; its commit takes a second
    text = await manager.stop(job.id, ANNA)
    assert text.startswith("Job j1 had already ended (ended with exit code 0 after")
    await manager.stop_all()
    assert (commits, ended, notices) == ([("job j1", job.command)], [1], [1])
    assert row(swarm, 1)["status"] == "ended"


async def test_stop_all_waits_for_finishes_in_flight(setup, kids, monkeypatch):
    swarm, manager = setup
    started, finished = [], []

    async def slow_commit(actor, message="", **kw):
        started.append(kw)
        await asyncio.sleep(1.0)
        finished.append(kw)

    monkeypatch.setattr(swarm.workspace, "autocommit", slow_commit)
    out = await run(manager, ANNA, py(kids.script, "1.5", kids.pidfile()))
    await until(lambda: started, 10)  # the job ended by itself; its finish is inside its commit
    await manager.stop_all()
    assert finished and out.job.done.done() and not manager._finishes and not manager._helpers

    def refuse(*args, **kwargs):
        raise AssertionError("touched after stop_all returned")

    monkeypatch.setattr(swarm.workspace, "autocommit", refuse)
    monkeypatch.setattr(swarm.db, "execute", refuse)
    await asyncio.sleep(0.5)


async def test_shell_exit_with_leftovers_reports_the_command(setup, kids, monkeypatch):
    """A build that ends but leaves a daemon running: the command's own end is reported at once, and the daemon
    goes on as a detached job that no longer keeps its owner busy."""
    swarm, manager = setup
    commits, reports = [], []

    async def commit(actor, message="", **kw):
        commits.append((actor, kw["kind"], kw["detail"]))

    monkeypatch.setattr(swarm.workspace, "autocommit", commit)
    monkeypatch.setattr(manager, "_announce_command_done",
                        lambda job, previous, names: reports.append((job.id, previous, list(names))))
    out = await run(manager, ANNA, leave(kids.script, "30", kids.pidfile(), then=2), timeout=1)
    assert out.state == "background" and manager.busy(ANNA)
    await until(lambda: out.job.command_done, 10)
    await until(lambda: reports, 5)
    record = row(swarm, 1)
    assert (record["status"], record["origin"], record["exit_code"]) == ("running", "detached", 0)
    assert not manager.busy(ANNA) and pid_alive(kids.pid())
    # the commit names the job (its subject then lists the changed files), its body the command and its end
    assert commits == [(ANNA, "job j1", f"{out.job.command}\n\nj1: the command ended with exit code 0; processes it "
                                        f"started still run: {format_names(reports[0][2])}")]
    assert reports[0][:2] == (1, "timeout") and len(reports) == 1
    assert "# command ended" in (swarm.workspace.root / record["log"]).read_text(encoding="utf-8")
    assert "processes it started still run" in manager.describe(1, actor=ANNA)


async def test_recover_marks_lost(setup, kids, monkeypatch):
    swarm, manager = setup
    lost = []
    monkeypatch.setattr(manager, "_announce_lost", lambda r: lost.append(r["id"]))
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    tokens = {"dead": "deadhub" + os.urandom(4).hex(), "alive": "livehub" + os.urandom(4).hex()}
    procs_by_token = {}
    if LINUX:  # only Linux sweeps by token
        for key, token in tokens.items():
            pidfile = kids.pidfile()
            procs_by_token[key] = subprocess.Popen([sys.executable, kids.script, "30", pidfile],
                                                   env={**os.environ, "DREAMTEAM_PROCESS_TOKEN": token})
    hub = subprocess.Popen([sys.executable, kids.script, "30", kids.pidfile()])  # a live "other hub"
    try:
        for owner, token, hub_pid in ((ANNA, tokens["dead"], dead.pid), ("Gone Member", tokens["alive"], hub.pid)):
            swarm.db.insert("INSERT INTO jobs(owner, command, origin, status, token, hub_pid, started_at)"
                            " VALUES (?, 'npm run dev', 'background', 'running', ?, ?, ?)",
                            (owner, token, hub_pid, time.time()))
        manager.recover()
        assert [(r["status"], r["reason"]) for r in swarm.db.all("SELECT * FROM jobs ORDER BY id")] == [
            ("lost", "crash"), ("lost", "crash")]
        assert lost == [1]  # only owners that are still members
        if LINUX:
            assert procs_by_token["dead"].wait(5) is not None
            assert procs_by_token["alive"].poll() is None
    finally:
        for proc in [hub, *procs_by_token.values()]:
            proc.kill()
            proc.wait(10)


async def test_cancelled_caller_kills_the_foreground_but_not_a_job(setup, kids):
    swarm, manager = setup
    call = asyncio.ensure_future(run(manager, ANNA, py(kids.script, "30", kids.pidfile()), timeout=30))
    pid = await apid(kids)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert not pid_alive(pid) and swarm.db.scalar("SELECT COUNT(*) FROM jobs") == 0
    out = await run(manager, ANNA, py(kids.script, "30", kids.pidfile()))
    assert out.state == "background" and pid_alive(kids.pid())
    # a stop under way when the session stops: one of them finishes the job, the stopper gets a result (never a
    # CancelledError) that says which
    stopper = asyncio.ensure_future(manager.stop(out.job.id, ANNA))
    await manager.stop_all()
    text = await stopper
    reason = row(swarm, out.job.id)["reason"]
    assert (reason, text.split(" (")[0]) in (("owner", "Stopped job j1"), ("session", "Job j1 had already ended"))
    assert dead_if_started(kids)


async def test_a_failed_call_kills_its_command(setup, kids, monkeypatch):
    """A call that fails before its command became a job (here the adoption itself) kills the command: nothing runs
    on unwatched."""
    swarm, manager = setup

    def broken(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(manager, "_adopt", broken)
    with pytest.raises(RuntimeError, match="database is locked"):
        await run(manager, ANNA, py(kids.script, "30", kids.pidfile()))
    assert dead_if_started(kids) and not manager._foreground


async def test_busy_rules(setup, kids):
    swarm, manager = setup
    timeout_job = await run(manager, ANNA, py(kids.script, "30", kids.pidfile()))
    assert manager.busy(ANNA)  # with or without a task
    await manager.stop(timeout_job.job.id, ANNA, force=True)
    assert not manager.busy(ANNA)
    background = await run(manager, ANNA, py(kids.script, "30", kids.pidfile()), background=True)
    assert not manager.busy(ANNA)  # a server of a free member does not keep it busy
    task = swarm.board.create(USER, "Try the server")
    swarm.db.execute("UPDATE tasks SET status = 'in_progress', assignee = ? WHERE id = ?", (ANNA, task.id))
    assert manager.busy(ANNA)
    swarm.db.execute("UPDATE tasks SET status = 'review', reviewer = ?, assignee = ? WHERE id = ?",
                     (ANNA, BORIS, task.id))
    assert manager.busy(ANNA)  # holds a review
    shell_set(swarm, max_timeout=1)
    await asyncio.sleep(1.1)
    assert not manager.busy(ANNA)  # older than max_timeout
    shell_set(swarm, max_timeout=1800)
    detached = await run(manager, BORIS, leave(kids.script, "30", kids.pidfile()), timeout=30)
    assert detached.state == "detached" and not manager.busy(BORIS)
    assert background.job.origin == "background"


async def test_wakes_and_awaited(setup, kids):
    swarm, manager = setup
    timeout_job = (await run(manager, ANNA, py(kids.script, "30", kids.pidfile()))).job
    detached_job = (await run(manager, ANNA, leave(kids.script, "30", kids.pidfile()), timeout=30)).job
    assert manager.wakes(timeout_job) and not manager.wakes(detached_job)  # no goal
    swarm.set_goal(USER, "Ship it")
    assert manager.awaited(ANNA) == [timeout_job]
    task = swarm.board.create(USER, "Build")
    swarm.db.execute("UPDATE tasks SET status = 'in_progress', assignee = ? WHERE id = ?", (ANNA, task.id))
    assert manager.awaited(ANNA) == [timeout_job, detached_job]
    swarm.complete_goal(USER, "Done")
    assert manager.awaited(ANNA) == []  # a done goal makes every end quiet


def test_log_cleanup_uses_rows_not_the_folder(make_fake_swarm, monkeypatch):
    """Two names share a scratch folder ("Анна Смирнова" and "Anna Smirnova"): one's cleanup never deletes the
    other's logs."""
    swarm = make_fake_swarm({"workspace": {"git": False}})
    swarm.workspace = Workspace(swarm)
    manager = JobManager(swarm)
    monkeypatch.setattr(jobs_module, "JOB_LOGS_KEPT", 1)
    folder = swarm.workspace.root / ".scratch" / "anna.smirnova" / "jobs"
    folder.mkdir(parents=True)
    for job_id, owner in ((1, "Анна Смирнова"), (2, "Anna Smirnova"), (3, "Anna Smirnova"), (4, "Анна Смирнова"),
                          (5, "Anna Smirnova")):
        log = f".scratch/anna.smirnova/jobs/j{job_id}.log"
        (swarm.workspace.root / log).write_text("x", encoding="utf-8")
        swarm.db.insert("INSERT INTO jobs(id, owner, command, origin, status, log, started_at, ended_at)"
                        " VALUES (?, ?, 'x', 'timeout', 'ended', ?, 0, 1)", (job_id, owner, log))
    (folder / "j2.prev.log").write_text("x", encoding="utf-8")
    manager._prune_logs("Anna Smirnova")
    assert sorted(p.name for p in folder.iterdir()) == ["j1.log", "j4.log", "j5.log"]


@needs_git
async def test_job_end_commits_as_owner(git_setup, kids):
    swarm, manager = git_setup
    script = Path(kids.folder) / "writer.py"
    script.write_text("import pathlib, sys, time\ntime.sleep(1.5)\npathlib.Path('out.txt').write_text('made')\n",
                      encoding="utf-8")
    out = await run(manager, ANNA, py(str(script)), timeout=1)
    assert out.state == "background"
    await until(lambda: row(swarm, 1).get("status") == "ended", 15)
    await until(lambda: not manager._finishes, 15)
    root = swarm.workspace.root
    # the subject names the job and the files it changed, the body keeps the command and how the job ended
    assert git_out(root, "log", "-1", "--format=%an|%s").strip() == f"{ANNA}|job j1: out.txt"
    body = git_out(root, "log", "-1", "--format=%b")
    assert out.job.command in body and "j1: ended with exit code 0 after" in body
    assert "out.txt" in git_out(root, "show", "--name-only", "--format=", "HEAD")
    assert git_out(root, "ls-files", ".scratch").strip() == ""  # logs are never committed
    assert (root / ".scratch" / "anna.smith" / "jobs" / "j1.log").is_file()


@needs_git
async def test_a_job_ending_while_the_session_stops_is_committed_by_stop_all(git_setup, kids):
    """Swarm.stop marks the session stopping first and stops the jobs last (after the runtimes): a job that ends by
    itself in between makes no commit of its own then, so stop_all commits what it changed under its owner, in the
    owner's one commit with the jobs it stopped."""
    swarm, manager = git_setup
    script = Path(kids.folder) / "writer.py"
    script.write_text("import os, pathlib, sys, time\npathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
                      "time.sleep(1.5)\npathlib.Path('report.txt').write_text('all passed')\n", encoding="utf-8")
    tests = await run(manager, ANNA, py(str(script), kids.pidfile()), timeout=0.5)
    server = await run(manager, ANNA, py(kids.script, "30", kids.pidfile()), background=True)
    assert (tests.state, server.state) == ("background", "started")
    swarm.stopping = True  # what Swarm.stop does first
    await until(lambda: row(swarm, 1).get("status") == "ended" and not manager._finishes, 15)
    root = swarm.workspace.root
    assert "report.txt" in git_out(root, "status", "--porcelain")  # no git work starts while the session stops
    await manager.stop_all()
    assert git_out(root, "status", "--porcelain").strip() == ""
    assert git_out(root, "log", "-1", "--format=%an|%s").strip() == f"{ANNA}|jobs j1, j2: report.txt"
    body = git_out(root, "log", "-1", "--format=%b")
    assert f"Stopped with the session:\nj2: {server.job.command}\nEnded while the session stopped:\n" in body
    assert f"j1: {tests.job.command} (ended with exit code 0 after " in body


async def test_output_cap_and_memory(setup, kids):
    swarm, manager = setup
    shell_set(swarm, job_log_mb=1)
    script = Path(kids.folder) / "loud.py"
    script.write_text("import sys, time\ntime.sleep(1.3)\nline = 'z' * 99 + '\\n'\n"
                      "for i in range(50000):\n    sys.stdout.write(line)\nprint('last line')\n", encoding="utf-8")
    out = await run(manager, ANNA, py(str(script)), timeout=1)
    assert out.state == "background"
    await until(lambda: row(swarm, 1).get("status") == "ended", 30)
    capture = out.job.cmd.capture
    assert len(capture._head) <= jobs_module.JOB_HEAD_CHARS and len(capture._tail) <= jobs_module.JOB_TAIL_CHARS
    log = swarm.workspace.root / row(swarm, 1)["log"]
    prev = log.with_name("j1.prev.log")
    assert log.stat().st_size + prev.stat().st_size <= 2 ** 20
    assert "last line" in log.read_text(encoding="utf-8")
    assert row(swarm, 1)["tail"].rstrip().endswith("last line")


def test_sandbox_command_has_init(make_fake_swarm, tmp_path):
    """In the Docker sandbox an init is PID 1: it reaps the orphans of agents' commands (dreamteam never does), so
    a zombie never keeps a job's process group alive, and it forwards docker stop's SIGTERM to dreamteam."""
    from dreamteam import docker as docker_module

    config = make_fake_swarm({}).config
    cmd = docker_module.sandbox_command(tmp_path, 9202, config, docker="docker", home=False)
    assert cmd[:4] == ["docker", "run", "--rm", "--init"]


# --- the foreground cap and the thread caps -------------------------------------------------------------------

async def test_an_ai_agents_call_waits_at_most_foreground_max(setup, kids):
    """[shell] foreground_max: an AI agent's call returns after at most that long, whatever its timeout, and the
    command goes on as a timeout job (its lifetime still counts from its start), so the agent sees messages and
    comments meanwhile. An external member is not capped by it; without background_on_timeout the timeout alone
    counts, as before (the command is killed then)."""
    swarm, manager = setup
    shell_set(swarm, foreground_max=1)
    started = time.monotonic()
    out = await run(manager, ANNA, py(kids.script, "30", kids.pidfile()), timeout=30)
    assert time.monotonic() - started < 5
    assert out.state == "background" and out.wait == 1 and out.job.origin == "timeout" and manager.busy(ANNA)
    assert out.render(has_jobs_tool=True, cfg=swarm.config.shell).startswith(
        f"STILL RUNNING after 1s: continues in the background as job j1 [{PROGRAM}]")
    assert manager._deadline(out.job) == out.job.started_at + swarm.config.shell.max_timeout
    await manager.stop(out.job.id, ANNA, force=True)
    external = await run(manager, EXT, py(kids.script, "2", kids.pidfile()), timeout=10)
    assert external.state == "exited" and external.code == 0 and "child end" in external.output
    shell_set(swarm, background_on_timeout=False)
    started = time.monotonic()
    killed = await run(manager, ANNA, py(kids.script, "30", kids.pidfile()), timeout=2)
    assert killed.state == "timeout" and killed.wait == 2 and time.monotonic() - started >= 2
    assert dead_if_started(kids) and swarm.db.scalar("SELECT COUNT(*) FROM jobs") == 1


async def test_a_command_that_cannot_become_a_job_gets_its_own_timeout(setup, kids, monkeypatch):
    """foreground_max only moves an AI agent's command to the background sooner. When the command cannot become a
    job now (jobs turned off, the member's limit reached), the call waits for it up to its own timeout, as before
    the cap, and kills it only then; a slot that frees meanwhile takes it as a job at once. An external member's cap
    stays: it protects the MCP transport."""
    swarm, manager = setup
    shell_set(swarm, foreground_max=1, max_jobs_per_member=0)
    out = await run(manager, ANNA, py(kids.script, "2", kids.pidfile()), timeout=10)
    assert out.state == "exited" and out.code == 0 and "child end" in out.output
    killed = await run(manager, ANNA, py(kids.script, "30", kids.pidfile()), timeout=3)
    assert killed.state == "timeout" and killed.note == "disabled" and killed.wait == pytest.approx(3)
    assert killed.render(has_jobs_tool=True, cfg=swarm.config.shell).startswith(
        f"TIMEOUT after 3s (process tree killed) [{PROGRAM}]")
    assert dead_if_started(kids)
    shell_set(swarm, max_jobs_per_member=1)
    server = await run(manager, ANNA, py(kids.script, "30", kids.pidfile()), background=True)
    assert server.state == "started"
    out = await run(manager, ANNA, py(kids.script, "2", kids.pidfile()), timeout=10)
    assert out.state == "exited" and out.code == 0
    call = asyncio.ensure_future(run(manager, ANNA, py(kids.script, "30", kids.pidfile()), timeout=20))
    await apid(kids)
    await asyncio.sleep(1.5)  # past foreground_max: the call waits on, since no job may start
    assert not call.done()
    await manager.stop(server.job.id, ANNA, force=True)  # a slot frees: the command goes on as a job
    out = await call
    assert out.state == "background" and out.job.origin == "timeout" and 1.5 < out.wait < 20
    await manager.stop(out.job.id, ANNA, force=True)
    monkeypatch.setattr(jobs_module, "EXTERNAL_WAIT_MAX", 1)
    shell_set(swarm, max_jobs_per_member=0)
    external = await run(manager, EXT, py(kids.script, "30", kids.pidfile()), timeout=10)
    assert external.state == "timeout" and external.wait == 1 and external.note == "disabled"
    assert dead_if_started(kids) and swarm.db.scalar("SELECT COUNT(*) FROM jobs") == 2


async def test_cpu_thread_caps(setup, kids):
    """[shell] cpu_threads caps the thread pools of numeric libraries in every command: "auto" shares the cores
    among the AI agents (external members do not count), N is N, 0 sets nothing. A value from the hub's own
    environment stays, and one the command sets itself wins."""
    swarm, manager = setup
    script = Path(kids.folder) / "threads.py"
    script.write_text(f"import os\nprint(','.join(os.environ.get(k, '-') for k in {jobs_module.THREAD_ENV!r}))\n",
                      encoding="utf-8")
    clean = {k: v for k, v in ENV.items() if k not in jobs_module.THREAD_ENV}

    async def values(command: str | None = None, env: dict | None = None) -> list[str]:
        out = await manager.run(ANNA, command or py(str(script)), timeout=30, env=env or clean, max_output=4000)
        assert out.state == "exited" and out.code == 0, out.output
        return out.output.strip().split(",")

    cores = os.cpu_count() or 1
    assert manager.cpu_threads() == max(1, cores // 2)  # Anna and Boris; the external member does not count
    assert await values() == [str(max(1, cores // 2))] * 6
    swarm.add_member("Carl Jung")
    assert manager.cpu_threads() == max(1, cores // 3)
    shell_set(swarm, cpu_threads=3)
    assert await values() == ["3"] * 6
    assert await values(env={**clean, "OMP_NUM_THREADS": "7"}) == ["7"] + ["3"] * 5
    own = f"set OMP_NUM_THREADS=1&& {py(str(script))}" if WINDOWS else f"OMP_NUM_THREADS=1 {py(str(script))}"
    assert await values(own) == ["1"] + ["3"] * 5
    shell_set(swarm, cpu_threads=0)
    assert await values() == ["-"] * 6
    shell_set(swarm, cpu_threads="4")
    assert manager.cpu_threads() == 4


def test_shell_settings_and_template(tmp_path):
    """Every [shell] key is in the settings template with its default; cpu_threads is "auto" or a count, as a
    number or a string; foreground_max and job_log_mb are at least 1, the job limits may be 0."""
    raw = tomllib.loads(render_template("X", "ru", "anthropic"))
    assert raw["shell"] == dataclasses.asdict(ShellSettings())
    for value, parsed in (("auto", "auto"), (" Auto ", "auto"), ("4", 4), (4, 4), (0, 0), ("0", 0)):
        assert Config.from_dict(tmp_path, {"shell": {"cpu_threads": value}}).shell.cpu_threads == parsed
    for key, value in (("cpu_threads", "lots"), ("cpu_threads", "-2"), ("cpu_threads", -1), ("cpu_threads", True),
                       ("cpu_threads", 2.5), ("foreground_max", 0), ("job_log_mb", 0), ("max_jobs", -1),
                       ("background_on_timeout", "yes")):
        with pytest.raises(ValueError, match=rf"^\[shell\] {key} must be"):
            Config.from_dict(tmp_path, {"shell": {key: value}})
    shell = Config.from_dict(tmp_path, {"shell": {"max_jobs": 0, "max_jobs_per_member": 0, "job_max_minutes": 0,
                                                  "background_on_timeout": False}}).shell
    assert (shell.max_jobs, shell.max_jobs_per_member, shell.job_max_minutes, shell.foreground_max) == (0, 0, 0, 180)


def test_session_stats_count_running_jobs(tmp_path):
    path = tmp_path / "swarm.db"
    db = Database(path)
    try:
        for status in ("running", "running", "ended"):
            db.insert("INSERT INTO jobs(owner, command, origin, status) VALUES (?, 'x', 'timeout', ?)", (ANNA, status))
        assert read_stats(path, db.conn)["jobs"] == {"running": 2}
        db.execute("DROP TABLE jobs")  # a database of a version before background jobs
        stats = read_stats(path, db.conn)
        assert stats["jobs"] == {"running": 0} and stats["tasks"]["total"] == 0 and stats["last_activity"] is None
    finally:
        db.close()
    assert read_stats(path)["jobs"] == {"running": 0}  # its own read-only connection


# --- notices and events ------------------------------------------------------------------------------------------

async def test_notices_of_jobs_that_end_by_themselves(setup, kids):
    """n_job_done, in the owner's language, with the end of the output (capped). It wakes the owner of a timeout or
    background job unless the goal is done; a detached job's end wakes only an owner holding a task in progress, for
    the others it is quiet (shown at their next wake-up)."""
    swarm, manager = setup
    ivan = swarm.add_member("Ivan Petrov", language="ru")
    swarm.set_goal(USER, "Ship it")
    task = swarm.board.create(USER, "Build")
    swarm.db.execute("UPDATE tasks SET status = 'in_progress', assignee = ? WHERE id = ?", (BORIS, task.id))
    loud = Path(kids.folder) / "loud.py"
    loud.write_text("import time\nfor i in range(40):\n    print(f'line {i:02d} ' + 'x' * 100)\n"
                    "time.sleep(1.5)\nprint('done')\n", encoding="utf-8")
    outs = [await run(manager, ANNA, py(str(loud)), timeout=1),
            await run(manager, ANNA, leave(kids.script, "1.5", kids.pidfile()), timeout=30),
            await run(manager, BORIS, leave(kids.script, "1.5", kids.pidfile()), timeout=30),
            await run(manager, ivan, py(kids.script, "1.5", kids.pidfile()), timeout=1)]
    assert [o.state for o in outs] == ["background", "detached", "detached", "background"]
    await until(lambda: len(job_notices(swarm, ANNA)) == 2 and job_notices(swarm, BORIS) and job_notices(swarm, ivan),
                20)
    by_job = {int(re.search(r"j(\d+)", text).group(1)): (kind, quiet, text)
              for name in (ANNA, BORIS, ivan) for kind, quiet, text in job_notices(swarm, name)}
    assert {i: (kind, quiet) for i, (kind, quiet, _) in by_job.items()} == {
        1: ("n_job_done", False), 2: ("n_job_done", True), 3: ("n_job_done", False), 4: ("n_job_done", False)}
    text = by_job[1][2]
    assert text.startswith("Your background job j1 has ended (exit code 0) after running ")
    assert text.endswith("\nThe whole output: .scratch/anna.smith/jobs/j1.log.")
    tail = text.split("Last lines of its output:\n", 1)[1].split("\nThe whole output: ", 1)[0]
    lines = tail.split("\n")
    assert len(lines) <= jobs_module.NOTICE_TAIL_LINES and lines[-1] == "  done"
    assert all(line.startswith("  ") for line in lines)
    assert len(tail) <= jobs_module.NOTICE_TAIL_CHARS + 2 * jobs_module.NOTICE_TAIL_LINES
    assert by_job[2][2].startswith("Your background job j2 has ended (all its processes exited) after running ")
    assert re.match(r"Твой фоновый процесс j4 завершился \(код возврата 0\), проработав \d+ с\. Команда: ",
                    by_job[4][2])
    assert "Последние строки его вывода:\n  child up\n  child end\nВесь вывод: " in by_job[4][2]
    assert (swarm.signals.count(ANNA), swarm.signals.count(BORIS), swarm.signals.count(ivan)) == (1, 1, 1)
    swarm.complete_goal(USER, "Done")  # a done goal makes every later end quiet
    late = await run(manager, ANNA, py(kids.script, "1.5", kids.pidfile()), timeout=1)
    await until(lambda: len(job_notices(swarm, ANNA)) == 3, 15)
    assert job_notices(swarm, ANNA)[-1][:2] == ("n_job_done", True) and late.job.id == 5


async def test_notices_of_stopped_and_lost_jobs(setup, kids):
    """The user's stop: n_job_stopped; the owner's own stop: none (its tool result says it); the lifetime:
    n_job_limit; the shell exiting while processes it started run on: n_job_command_done with the shell's code at
    once; a retired owner: none; the session stop and a crash: a quiet n_job_gone."""
    swarm, manager = setup
    swarm.set_goal(USER, "Ship it")
    for _ in range(2):
        await run(manager, ANNA, py(kids.script, "30", kids.pidfile()))
    await manager.stop(1, USER, force=True)
    await manager.stop(2, ANNA, force=True)
    shell_set(swarm, max_timeout=2)
    await run(manager, BORIS, py(kids.script, "30", kids.pidfile()))
    await until(lambda: row(swarm, 3).get("status") == "stopped", 15)
    shell_set(swarm, max_timeout=1800)
    out = await run(manager, BORIS, leave(kids.script, "30", kids.pidfile(), then=2), timeout=1)
    assert out.state == "background"
    await until(lambda: len(job_notices(swarm, BORIS)) == 2, 15)
    assert (await run(manager, EXT, py(kids.script, "30", kids.pidfile()))).state == "background"
    swarm.db.execute("UPDATE agents SET status = 'retired' WHERE name = ?", (EXT,))
    manager.forbid(EXT)
    assert await manager.stop_owner(EXT) == [5]
    await manager.stop_all()
    swarm.db.insert("INSERT INTO jobs(owner, command, origin, status, log, hub_pid, started_at) VALUES"
                    " (?, 'npm run dev', 'background', 'running', '.scratch/anna.smith/jobs/j6.log', 0, ?)",
                    (ANNA, time.time()))
    manager.recover()
    anna, boris = job_notices(swarm, ANNA), job_notices(swarm, BORIS)
    assert [(kind, quiet) for kind, quiet, _ in anna] == [("n_job_stopped", False), ("n_job_gone", True)]
    assert anna[0][2].startswith("The user stopped your background job j1 after it ran ")
    assert anna[1][2] == ("Your background job j6 is no longer running: the session was stopped or restarted. "
                          "Command: npm run dev\nIts output until then: .scratch/anna.smith/jobs/j6.log.\nStart it "
                          "again if you still need it (e.g. a server that later steps use).")
    assert [(kind, quiet) for kind, quiet, _ in boris] == [("n_job_limit", False), ("n_job_command_done", False),
                                                           ("n_job_gone", True)]
    assert re.match(r"Your background job j3 was stopped: it ran \d+ s, the longest allowed\. Command: ", boris[0][2])
    # the command ended, the job did not: the notice never says that j4 has ended
    assert re.match(r"The command of your background job j4 has finished \(exit code 0\) after \d+ s, but programs "
                    r"it started still run, so j4 goes on: .+\. Command: ", boris[1][2])
    assert "has ended" not in boris[1][2]
    assert boris[2][2].startswith("Your background job j4 is no longer running")
    assert job_notices(swarm, EXT) == []


async def test_events_are_journaled(setup, kids):
    swarm, manager = setup
    out = await run(manager, ANNA, py(kids.script, "1.5", kids.pidfile()), timeout=1)
    await until(lambda: len(swarm.bus.recent(types=("job.*",))) == 2, 15)
    started, ended = swarm.bus.recent(types=("job.*",))
    assert [(e["type"], e["actor"]) for e in (started, ended)] == [("job.started", ANNA), ("job.ended", ANNA)]
    job = started["data"]["job"]
    assert set(job) == PUBLIC_KEYS and (job["label"], job["origin"], job["status"]) == ("j1", "timeout", "running")
    job = ended["data"]["job"]
    assert set(job) == PUBLIC_KEYS | {"duration"}
    assert (job["status"], job["exit_code"], job["reason"]) == ("ended", 0, None) and 1 <= job["duration"] < 15
    assert out.job.id == job["id"]


async def test_a_failed_commit_costs_no_notice(setup, kids, monkeypatch):
    swarm, manager = setup

    async def broken(*args, **kwargs):
        raise RuntimeError("git is broken")

    monkeypatch.setattr(swarm.workspace, "autocommit", broken)
    await run(manager, ANNA, py(kids.script, "1.5", kids.pidfile()), timeout=1)
    await until(lambda: job_notices(swarm, ANNA), 15)
    assert row(swarm, 1)["status"] == "ended" and job_notices(swarm, ANNA)[0][0] == "n_job_done"


# --- a real swarm with scripted agents ---------------------------------------------------------------------------

@pytest.fixture
def ticking(monkeypatch):
    monkeypatch.setattr(swarm_module, "SCHEDULER_SECONDS", 0.05)


def waiting(*args):
    """A scripted agent that ends every turn with `wait`."""
    return call("wait")


async def team(tmp_path: Path, *, run_agents: bool = True, **overrides):
    """A started swarm (with this platform's test shell) and the AI agents Anna and Boris."""
    swarm, rec = make_swarm(tmp_path, waiting, swarm={"start_agents": 0, **overrides.pop("swarm", {})},
                            shell={"program": PROGRAM}, **overrides)
    await swarm.start(run_agents=run_agents)
    for name in (ANNA, BORIS):
        await swarm.hire(USER, "Developer", name=name)
    return swarm, rec


async def srun(swarm, actor: str, command: str, timeout: float = 1, background: bool = False):
    """A member's `shell` call, as the tool makes it."""
    return await swarm.jobs.run(actor, command, timeout=timeout, env=ENV, max_output=4000, background=background)


def settled(swarm, *names: str) -> bool:
    return all(swarm.member(n)["status"] == "idle" and not swarm.has_pending(n) for n in names)


async def run_until_settled(swarm, rec, timeout: float = 15.0) -> None:
    """Both agents answered their hiring notice and sleep."""
    await until(lambda: rec.count(ANNA) and rec.count(BORIS) and settled(swarm, ANNA, BORIS), timeout)


async def test_swarm_snapshot_retire_stop_and_restart(tmp_path, fast, kids, ticking):
    swarm, _ = await team(tmp_path, run_agents=False)
    await swarm.hire(USER, "Assistant", name=EXT, kind=AgentKind.EXTERNAL)
    try:
        out = await srun(swarm, ANNA, py(kids.script, "30", kids.pidfile()))
        anna_pid = await apid(kids)
        assert out.state == "background"
        snap = swarm.snapshot()
        assert [(j["label"], j["status"]) for j in snap["jobs"]] == [("j1", "running")]
        assert {a["name"]: a["jobs"] for a in snap["agents"]} == {ANNA: 1, BORIS: 0, EXT: 0}
        assert re.search(r"\nYour background jobs: j1 .+ \(running \d+ s\)\n", swarm.render_wake(ANNA))
        assert "Your background jobs" not in swarm.render_wake(BORIS)
        # an external member retired while its shell call runs: the call is killed and no job is left of it
        call_task = asyncio.ensure_future(srun(swarm, EXT, py(kids.script, "30", kids.pidfile()), timeout=3))
        ext_pid = await apid(kids)
        await swarm.retire(USER, EXT)
        assert call_task.done() and call_task.result().state == "killed"
        assert not pid_alive(ext_pid) and not swarm.db.scalar("SELECT COUNT(*) FROM jobs WHERE owner = ?", (EXT,))
        # a retiring member's jobs stop with it; it is told nothing, and the event lists them
        await swarm.retire(USER, ANNA, "done")
        assert not pid_alive(anna_pid) and (row(swarm, 1)["status"], row(swarm, 1)["reason"]) == ("stopped", "retired")
        event = [e for e in swarm.bus.recent(types=("agent.retired",)) if e["data"]["name"] == ANNA][-1]
        assert event["data"]["jobs"] == [1] and job_notices(swarm, ANNA) == []
        with pytest.raises(ToolError, match="You are leaving the swarm"):
            await srun(swarm, ANNA, "echo hi")
        await srun(swarm, BORIS, py(kids.script, "30", kids.pidfile()))
        boris_pid = await apid(kids)
    finally:
        await swarm.stop()
        swarm.close()
    assert not pid_alive(boris_pid)
    # the session stopped its jobs and told their owners quietly; then a crash leaves a job `running` in its rows
    view, _ = make_swarm(tmp_path, waiting, swarm={"start_agents": 0}, shell={"program": PROGRAM})
    try:
        assert (row(view, 2)["status"], row(view, 2)["reason"]) == ("stopped", "session")
        assert [(kind, quiet) for kind, quiet, _ in job_notices(view, BORIS)] == [("n_job_gone", True)]
        view.db.insert("INSERT INTO jobs(owner, command, origin, status, hub_pid, started_at)"
                       " VALUES (?, 'npm run dev', 'background', 'running', 0, ?)", (BORIS, time.time()))
    finally:
        view.close()
    again, _ = make_swarm(tmp_path, waiting, swarm={"start_agents": 0}, shell={"program": PROGRAM})
    assert row(again, 3)["status"] == "running"  # a view-only load (cli status, the launcher) changes nothing
    await again.start(run_agents=False)
    try:
        assert (row(again, 3)["status"], row(again, 3)["reason"]) == ("lost", "crash")
        assert [(kind, quiet) for kind, quiet, _ in job_notices(again, BORIS)] == [("n_job_gone", True)] * 2
        assert "Your background job j3 is no longer running" in again.render_wake(BORIS)
    finally:
        await shutdown(again)


async def test_a_scripted_agent_is_woken_by_its_jobs_end(tmp_path, fast, kids, ticking):
    swarm, rec = await team(tmp_path)
    try:
        await run_until_settled(swarm, rec)
        out = await srun(swarm, ANNA, py(kids.script, "1.5", kids.pidfile()))
        assert out.state == "background"
        calls = rec.count(ANNA)
        await until(lambda: rec.count(ANNA) > calls, 15)
        wake = rec.calls[ANNA][calls][-1]["content"]
        assert "• Your background job j1 has ended (exit code 0) after running " in wake and "  child end" in wake
        # the goal is achieved while a server runs: the user is told, and the server's end wakes nobody
        swarm.set_goal(USER, "Ship it")
        server = await srun(swarm, BORIS, py(kids.script, "30", kids.pidfile()), timeout=120, background=True)
        assert server.state == "started"
        swarm.complete_goal(BORIS, "Done")
        told = swarm.db.all("SELECT text FROM notifications WHERE recipient = ? AND kind = 'u_jobs_running'", (USER,))
        assert len(told) == 1 and re.search(r": j2 \(Boris King: .+, \d+ s\)\. They stop when the session stops",
                                            told[0]["text"])
        await swarm.jobs.stop(server.job.id, USER, force=True)
        assert job_notices(swarm, BORIS)[-1][:2] == ("n_job_stopped", True)
    finally:
        await shutdown(swarm)


async def test_a_busy_reviewer_is_not_handed_over_meanwhile(tmp_path, fast, kids, ticking):
    """A reviewer reminded of a review starts a test run that goes on as a job: while it is busy the review is not
    handed over, and the hand-over clock restarts when the job ends (instead of firing at the first idle tick)."""
    swarm, rec = await team(tmp_path, agents={"idle_nudge_seconds": 1.0})
    try:
        await run_until_settled(swarm, rec)
        task = swarm.board.create(USER, "t", goal="g", reviewer=ANNA)
        swarm.board.claim(BORIS, task.id)
        swarm.board.complete(BORIS, task.id, "done")
        await until(lambda: "n_review_reminder" in kinds_for(swarm, ANNA), 15)
        out = await srun(swarm, ANNA, py(kids.script, "2.5", kids.pidfile()), timeout=0.2)
        assert out.state == "background" and swarm.jobs.busy(ANNA)
        await until(lambda: row(swarm, 1).get("status") == "ended", 15)
        assert swarm.board.get(task.id).reviewer == ANNA  # not handed over while it was busy
        await until(lambda: swarm.board.get(task.id).reviewer != ANNA, 15)
        assert time.time() - row(swarm, 1)["ended_at"] >= 0.8  # a full idle_nudge_seconds after the busy window
        assert kinds_for(swarm, ANNA).index("n_job_done") > kinds_for(swarm, ANNA).index("n_review_reminder")
    finally:
        await shutdown(swarm)


async def test_busy_rules_in_the_scheduler(tmp_path, fast, kids, ticking):
    """A free member running a server (background=true) is still offered ready tasks; with a task in progress the
    server keeps it busy: no offer and no stalled wake meanwhile. All tasks done: a running timeout job holds the
    goal check back until it ends, a server does not."""
    swarm, rec = await team(tmp_path, swarm={"independent_review": False}, agents={"idle_nudge_seconds": 0.2})
    try:
        await run_until_settled(swarm, rec)
        swarm.set_goal(USER, "Ship it")  # the planning task: offered to both by the kickoff
        await until(lambda: "n_kickoff" in kinds_for(swarm, ANNA) and settled(swarm, ANNA, BORIS))
        await swarm.retire(USER, BORIS)  # one agent: ready tasks reach it only through the scheduler's offers
        server = await srun(swarm, ANNA, py(kids.script, "60", kids.pidfile()), timeout=120, background=True)
        assert server.state == "started" and not swarm.jobs.busy(ANNA)
        first = swarm.board.create(USER, "Next", goal="g")
        await until(lambda: "n_ready" in kinds_for(swarm, ANNA), 10)
        await until(lambda: settled(swarm, ANNA))
        swarm.board.claim(ANNA, first.id)
        assert swarm.jobs.busy(ANNA)
        offers = kinds_for(swarm, ANNA).count("n_ready")
        second = swarm.board.create(USER, "Another", goal="g")
        await asyncio.sleep(1.0)
        assert kinds_for(swarm, ANNA).count("n_ready") == offers and "n_stalled" not in kinds_for(swarm, ANNA)
        timer = await srun(swarm, ANNA, py(kids.script, "2", kids.pidfile()), timeout=0.2)
        assert timer.state == "background"
        planning = next(t for t in swarm.board.list(include_closed=True) if "planning" in t.tags)
        swarm.board.complete(ANNA, first.id, "ok")
        for task in (planning, second):
            swarm.board.claim(ANNA, task.id)
            swarm.board.complete(ANNA, task.id, "ok")
        await asyncio.sleep(0.5)
        assert "n_all_done" not in kinds_for(swarm, ANNA)  # its timeout job may still change the result
        await until(lambda: "n_all_done" in kinds_for(swarm, ANNA), 15)
        kinds = kinds_for(swarm, ANNA)
        assert kinds.index("n_job_done") < kinds.index("n_all_done")  # after the job, despite the server
        assert row(swarm, server.job.id)["status"] == "running"
    finally:
        await shutdown(swarm)


async def test_a_member_waiting_for_its_jobs_is_not_woken_as_stalled(tmp_path, fast, kids, ticking):
    """A member that holds a task in progress and sleeps while jobs whose end will wake it run (what `wait` told it:
    here a detached job, never busy, and a server past its busy window) is waiting for them, not stalled: no
    n_stalled meanwhile; the end of a job wakes it."""
    swarm, rec = await team(tmp_path, swarm={"independent_review": False}, agents={"idle_nudge_seconds": 0.5})
    swarm.config.shell.max_timeout = 2
    try:
        await run_until_settled(swarm, rec)
        swarm.set_goal(USER, "Ship it")
        await until(lambda: settled(swarm, ANNA, BORIS))
        task = swarm.board.create(USER, "Build", goal="g", assignee=ANNA)
        swarm.board.claim(ANNA, task.id)
        detached = await srun(swarm, ANNA, leave(kids.script, "6", kids.pidfile()), timeout=10)
        server = await srun(swarm, ANNA, py(kids.script, "6", kids.pidfile()), timeout=2, background=True)
        assert (detached.state, server.state) == ("detached", "started")
        assert swarm.jobs.awaited(ANNA) == [detached.job, server.job] and swarm.jobs.busy(ANNA)
        await asyncio.sleep(3.5)  # well past idle_nudge_seconds, and past the server's busy window (max_timeout)
        assert not swarm.jobs.busy(ANNA) and swarm.jobs.awaited(ANNA)
        assert [row(swarm, i)["status"] for i in (1, 2)] == ["running", "running"]
        assert "n_stalled" not in kinds_for(swarm, ANNA)
        await until(lambda: "n_job_done" in kinds_for(swarm, ANNA), 15)
        kinds = kinds_for(swarm, ANNA)
        assert "n_stalled" not in kinds or kinds.index("n_stalled") > kinds.index("n_job_done")
    finally:
        await shutdown(swarm)


async def test_jobs_stop_even_when_an_earlier_step_of_the_stop_fails(tmp_path, fast, kids, ticking, monkeypatch):
    """Swarm.stop stops every job whatever failed before: the close of the external members' traces (a locked
    database) is logged and the stop goes on; a runtime that fails to stop raises, after the jobs were stopped (the
    session is closed and dropped after a failed stop: no process may outlive it)."""
    swarm, rec = await team(tmp_path)
    await run_until_settled(swarm, rec)
    await srun(swarm, ANNA, py(kids.script, "30", kids.pidfile()), background=True)
    pid = await apid(kids)
    failed: list[str] = []

    def locked(*args, **kwargs):
        failed.append("traces")
        raise sqlite3.OperationalError("database is locked")

    runtime = swarm.runtimes[BORIS]
    assert list(swarm.runtimes)[-1] == BORIS  # the last one: every runtime is stopped before it fails
    stop_runtime = runtime.stop

    async def failing_stop():
        await stop_runtime()
        failed.append("runtime")
        raise RuntimeError("closing its LLM client failed")

    monkeypatch.setattr(swarm.tracer, "close_external", locked)
    monkeypatch.setattr(runtime, "stop", failing_stop)
    try:
        with pytest.raises(RuntimeError, match="closing its LLM client failed"):
            await swarm.stop()
    finally:
        swarm.close()  # what SessionManager._unload does after a failed stop
    assert failed == ["traces", "runtime"]
    assert swarm.jobs.closing and swarm.jobs.running_of() == [] and not pid_alive(pid)


async def test_a_runtime_that_fails_to_stop_does_not_keep_the_others_running(tmp_path, fast, monkeypatch):
    """The first runtime's stop fails: the later ones are stopped all the same, then the failure is raised."""
    swarm, rec = await team(tmp_path)
    await run_until_settled(swarm, rec)
    first, *others = list(swarm.runtimes.values())
    assert others
    stop_first = first.stop

    async def failing_stop():
        await stop_first()
        raise RuntimeError("closing its LLM client failed")

    monkeypatch.setattr(first, "stop", failing_stop)
    try:
        with pytest.raises(RuntimeError, match="closing its LLM client failed"):
            await swarm.stop()
    finally:
        swarm.close()
    assert all(r._task is None or r._task.done() for r in others)
    assert swarm.runtimes == {}


async def test_an_external_members_calls_and_the_session_stop(tmp_path, fast, kids, ticking, monkeypatch):
    """An external member's call waits at most EXTERNAL_WAIT_MAX (below the MCP proxy's read timeout), and the end
    of its job reaches it through wait_for_events. A session stop with its call in flight: the call returns killed
    before Swarm.stop returns, and nothing touches the database after that."""
    monkeypatch.setattr(jobs_module, "EXTERNAL_WAIT_MAX", 1)
    swarm, _ = await team(tmp_path, run_agents=False)
    await swarm.hire(USER, "Assistant", name=EXT, kind=AgentKind.EXTERNAL)
    in_flight = None
    try:
        started = time.monotonic()
        out = await srun(swarm, EXT, py(kids.script, "2", kids.pidfile()), timeout=30)
        assert out.state == "background" and out.wait == 1 and time.monotonic() - started < 5
        text, is_error = await call_tool(ToolContext(swarm, EXT), "wait_for_events", {"timeout": 15})
        assert not is_error and "Your background job j1 has ended (exit code 0) after running " in text
        in_flight = asyncio.ensure_future(srun(swarm, EXT, py(kids.script, "30", kids.pidfile()), timeout=30))
        ext_pid = await apid(kids)
    finally:
        await swarm.stop()
    try:
        assert in_flight.done() and in_flight.result().state == "killed" and not pid_alive(ext_pid)
        assert row(swarm, 1)["status"] == "ended" and swarm.db.scalar("SELECT COUNT(*) FROM jobs") == 1
        writes = []
        for method in ("execute", "insert"):
            setattr(swarm.db, method, lambda *args, **kw: writes.append(args))
        await asyncio.sleep(0.5)
        assert writes == []
    finally:
        swarm.close()


# --- end to end: scripted agents through the shell tool --------------------------------------------------------

# a test suite of 3 tests: it writes its report into the current folder (the workspace) at its end
SUITE = ("import os, pathlib, sys, time\n"
         "pathlib.Path(sys.argv[2]).write_text(str(os.getpid()))\n"
         "print('collected 3 items', flush=True)\n"
         "time.sleep(float(sys.argv[1]))\n"
         "pathlib.Path('test-report.txt').write_text('3 passed\\n', encoding='utf-8')\n"
         "print('3 passed', flush=True)\n")


def seen_texts(messages: list[dict]) -> list[str]:
    """What a scripted agent has read so far: its wake-ups and its tool results."""
    return [m.get("content") or "" for m in messages if m["role"] in ("user", "tool")]


def saw(seen: list[str], *parts: str) -> bool:
    return any(all(part in text for part in parts) for text in seen)


def review_flow(suite: str, server: str):
    """Anna's task is a test run: it goes on in the background at its timeout, she waits, and its end wakes her to
    send the task to review. Boris, the reviewer, starts the server in the background to try it and approves.
    Whoever hears that every task is finished completes the goal. (Later steps are checked first: a policy sees
    the whole history.)"""

    def policy(swarm, name, system, messages, tools):
        seen = seen_texts(messages)
        if saw(seen, "marked as achieved") or saw(seen, "already marked"):
            return call("wait")
        if saw(seen, "All tasks on the board are finished"):
            return call("goal_complete", summary="The build is tested and reviewed.")
        if name == ANNA:
            if saw(seen, "Task #1 was sent to"):
                return call("wait")  # the verdict wakes her
            if saw(seen, "Your background job j1 has ended (exit code 0)"):
                return call("task_complete", task_id=1, result="The suite passes: 3 passed (test-report.txt).")
            if saw(seen, "STILL RUNNING after 1s: continues in the background as job j1"):
                return call("wait")
            if saw(seen, "You now work on"):
                return call("shell", command=suite, timeout=1)
            if saw(seen, "Task #1", "was assigned to you"):
                return call("task_claim", task_id=1)
            return call("wait")
        if saw(seen, "Task #1 is now done"):
            return call("wait")
        if saw(seen, "STARTED in the background as job j2"):
            return call("task_review", task_id=1, approve=True, comment="The suite passes and the server starts.")
        if saw(seen, "Task #1", "is waiting for your review"):
            return call("shell", command=server, background=True)
        return call("wait")

    return policy


def first_notices(swarm) -> dict[tuple[str, str], int]:
    """(recipient, kind) -> the id of its first notification: the order things happened in."""
    order: dict[tuple[str, str], int] = {}
    for r in swarm.db.all("SELECT id, recipient, kind FROM notifications ORDER BY id"):
        order.setdefault((r["recipient"], r["kind"]), r["id"])
    return order


async def test_a_test_run_in_the_background_from_task_to_goal(tmp_path, fast, kids, ticking, monkeypatch):
    """DESIGN §11.4: Anna's test run outlives its `shell` call and goes on as job j1; while it runs she is busy (no
    stalled wake for the task she holds), and her reviewer is not asked yet. The job's end wakes her with its
    output, commits its report under her name, and she sends the task to review; Boris starts the server in the
    background to try it, approves, and the goal is completed while the server still runs (the caller and the
    user are told). Swarm.stop leaves no process behind."""
    monkeypatch.setenv("PYEXE", sys.executable)  # the agents' commands get the hub's environment (scrubbed)
    suite = Path(kids.folder) / "suite.py"
    suite.write_text(SUITE, encoding="utf-8")
    flow = review_flow(py(str(suite), "3", kids.pidfile()), py(kids.script, "60", kids.pidfile()))
    has_git = shutil.which("git") is not None
    swarm, rec = make_swarm(tmp_path, flow, swarm={"start_agents": 0}, shell={"program": PROGRAM},
                            workspace={"git": has_git}, agents={"idle_nudge_seconds": 0.5})
    await swarm.start()
    try:
        await swarm.hire(USER, "Developer", name=ANNA)
        await swarm.hire(USER, "Reviewer", name=BORIS)
        await until(lambda: rec.count(ANNA) and rec.count(BORIS) and settled(swarm, ANNA, BORIS))
        swarm.board.create(USER, "Run the test suite", goal="The suite passes", assignee=ANNA)
        swarm.set_goal(USER, "Ship a tested build")
        await until(lambda: (swarm.goal() or {}).get("status") == "done", 60)
        suite_pid, server_pid = kids.pid(0), kids.pid(1)
        assert not pid_alive(suite_pid) and pid_alive(server_pid)
        j1, j2 = row(swarm, 1), row(swarm, 2)
        assert (j1["owner"], j1["origin"], j1["status"], j1["exit_code"]) == (ANNA, "timeout", "ended", 0)
        assert (j2["owner"], j2["origin"], j2["status"]) == (BORIS, "background", "running")
        # while the job ran: Anna was busy (not woken as stalled), and the review was asked only after its end
        order = first_notices(swarm)
        assert (ANNA, "n_stalled") not in order
        assert order[(ANNA, "n_job_done")] < order[(BORIS, "n_review_requested")] < order[(ANNA, "n_review_approved")]
        anna = history(swarm, ANNA)
        assert any(m["role"] == "tool" and m["name"] == "wait"
                   and m["content"].endswith("also when your background job j1 ends.") for m in anna)
        woken = [m["content"] for m in anna if m["role"] == "user" and "Your background job j1 has ended" in m["content"]]
        assert len(woken) == 1 and "(exit code 0) after running " in woken[0]
        assert "Last lines of its output:\n  collected 3 items\n  3 passed\n" in woken[0]
        assert "The whole output: .scratch/anna.smith/jobs/j1.log." in woken[0]
        # the goal was completed while Boris's server runs: the caller may stop it, the user is told
        done = [m["content"] for name in (ANNA, BORIS) for m in history(swarm, name)
                if m["role"] == "tool" and m["name"] == "goal_complete"]
        assert len(done) == 1 and re.search(r"Note: background jobs are still running: j2 \(Boris King: .+, \d+ s\)\. "
                                            r"Stop the ones nobody needs with job_stop; the rest stop with the "
                                            r"session\.$", done[0])
        assert "u_jobs_running" in kinds_for(swarm, USER)
        if has_git:  # the job's end committed what it wrote under its owner, its log never
            root = swarm.workspace.root
            assert f"{ANNA}|job j1: test-report.txt" in git_out(root, "log", "--format=%an|%s").splitlines()
            assert git_out(root, "ls-files", ".scratch").strip() == ""
        assert rec.violations == []
    finally:
        await swarm.stop()
    try:
        assert not pid_alive(server_pid)  # no leftover process
        assert (row(swarm, 2)["status"], row(swarm, 2)["reason"]) == ("stopped", "session")
        assert job_notices(swarm, BORIS)[-1][:2] == ("n_job_gone", True)
    finally:
        swarm.close()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def print_home(tmp_path: Path) -> Path:
    """A launcher home with one scripted agent per session and this platform's test shell."""
    home = tmp_path / "home"
    home.mkdir()
    settings_module.write_raw(home, {
        "swarm": {"name": "Home", "language": "en", "start_agents": 1, "max_agents": 3, "max_cost_usd": 1.0},
        "agents": {"stagger_seconds": 0, "idle_nudge_seconds": 30},
        "server": {"port": _free_port()},
        "shell": {"program": PROGRAM},
        "workspace": {"git": False},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
    })
    return home


def serving(server: str):
    """The only agent starts a server in the background, then declares the goal achieved while it runs."""

    def policy(system, messages, tools):
        seen = seen_texts(messages)
        if saw(seen, "marked as achieved") or saw(seen, "already marked"):
            return call("wait")
        if saw(seen, "STARTED in the background as job j1"):
            return call("goal_complete", summary="The haiku server runs as job j1.")
        if saw(seen, "haiku"):
            return call("shell", command=server, background=True)
        return call("wait")

    return policy


def test_print_mode_with_an_owned_launcher_leaves_no_job_behind(tmp_path, kids, monkeypatch):
    """`dreamteam -p` that runs the launcher itself: the server a scripted agent left running in the background is
    gone once the run exited (it stops its session at the end, and Swarm.stop kills the session's jobs)."""
    monkeypatch.setenv("PYEXE", sys.executable)
    home = print_home(tmp_path)
    policy = serving(py(kids.script, "60", kids.pidfile()))
    out, err = io.StringIO(), io.StringIO()
    code = tui.main_print(home, "Serve a haiku", llm_factory=lambda profile, agent: ScriptedLLM(policy, profile),
                          web=False, output_format="json", timeout=60, out=out, err=err)
    assert code == 0, err.getvalue()
    result = json.loads(out.getvalue().strip().splitlines()[-1])
    assert result["status"] == "done" and result["summary"] == "The haiku server runs as job j1."
    assert not pid_alive(kids.pid(timeout=5))
    db = Database(home / "sessions" / result["session"] / ".dreamteam" / "swarm.db")
    try:
        assert [(r["owner"], r["origin"], r["status"], r["reason"]) for r in db.all("SELECT * FROM jobs")] == [
            (result["agents"][0], "background", "stopped", "session")]
    finally:
        db.close()


async def test_closing_an_owned_launcher_stops_the_jobs_of_its_sessions(tmp_path, kids, monkeypatch):
    """The interactive client exits (or is interrupted) while its session still runs: the owned launcher's
    shutdown stops the session, and with it the job."""
    monkeypatch.setenv("PYEXE", sys.executable)
    home = print_home(tmp_path)
    policy = serving(py(kids.script, "60", kids.pidfile()))
    conn = await connect(home, web=False, llm_factory=lambda profile, agent: ScriptedLLM(policy, profile))
    try:
        assert conn.owned
        code = await run_print(conn, "Serve a haiku", out=io.StringIO(), err=io.StringIO(), timeout=60)
        assert code == 0
        pid = await apid(kids, timeout=5)
        (sid,) = conn.manager.running
        assert pid_alive(pid) and [j.label for j in conn.manager.swarms[sid].jobs.running_of()] == ["j1"]
    finally:
        await conn.close()
    assert not pid_alive(pid)
