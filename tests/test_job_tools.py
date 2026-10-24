"""The agent API of background jobs (docs/SPEC.md §7.1, tools/job_tools.py): the `shell` tool's results for callers
that have the `jobs` tool (revision 2 of the frozen tool sets, external members) and for revision-1 agents (the
console intercept and the footers that teach it), `jobs` and `job_stop`, the `wait` result, the job lines of
`goal_complete`, `agents` and `swarm_status`, the hub's instructions and its stop-time tracing, and scripted agents
that use all of it through their runtimes.

Child processes are a Python script outside the workspace; each writes its pid, and the `kids` fixture checks that
none is left running."""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from dreamteam import jobs as jobs_module
from dreamteam import swarm as swarm_module
from dreamteam.agent import AgentRuntime
from dreamteam.db import dumps
from dreamteam.hub_mcp import INSTRUCTIONS, hub_call
from dreamteam.models import USER, AgentKind
from dreamteam.tools import ToolContext, call_tool, frozen
from dreamteam.workspace import scratch_rel
from test_procs import Children, pid_alive
from test_runtime import call, make_swarm, shutdown

WINDOWS = sys.platform == "win32"
PROGRAM = "cmd" if WINDOWS else "sh"  # the fastest shell to start
ANNA, BORIS, EXT = "Anna Smith", "Boris King", "Claude Code"
CHATTY = ("import os, pathlib, sys, time\n"
          "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
          "for i in range(500):\n"
          "    print(f'line {i}')\n"
          "sys.stdout.flush()\n"
          "time.sleep(30)\n")


def py_cmd(script: str, *args: str) -> str:
    """`$PYEXE script args` in the syntax of PROGRAM (PYEXE: this interpreter, set by the `kids` fixture)."""
    if WINDOWS:
        return '"%PYEXE%" ' + " ".join(f'"{a}"' for a in (script, *args))
    return '"$PYEXE" ' + " ".join(f"'{a}'" for a in (script, *args))


def leave(script: str, *args: str) -> str:
    """A command that starts `script args` in the background itself (cmd `start /b`, sh `&`) and exits."""
    if WINDOWS:
        return f'start /b "" {py_cmd(script, *args)} & echo started'
    return f"{py_cmd(script, *args)} & echo started"


@pytest.fixture
def kids(tmp_path, monkeypatch):
    monkeypatch.setenv("PYEXE", sys.executable)  # the agents' shell inherits it (scrubbed_env keeps it)
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
    monkeypatch.setattr(swarm_module, "SCHEDULER_SECONDS", 0.05)


async def until(predicate, timeout: float = 15.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        await asyncio.sleep(0.05)
    assert predicate(), "condition not reached in time"


async def apid(kids, index: int = -1, timeout: float = 20.0) -> int:
    """The pid a child wrote, without blocking the loop."""
    path = kids.files[index]
    await until(lambda: path.exists() and path.read_text().strip(), timeout)
    return int(path.read_text())


async def team(tmp_path: Path, policy: Any = None, *, run_agents: bool = False, **overrides: Any):
    """A started swarm (with this platform's quickest shell) and the AI agents Anna and Boris."""
    swarm, rec = make_swarm(tmp_path, policy or (lambda *a: call("wait")),
                            swarm={"start_agents": 0, **overrides.pop("swarm", {})},
                            shell={"program": PROGRAM, **overrides.pop("shell", {})}, **overrides)
    await swarm.start(run_agents=run_agents)
    for name in (ANNA, BORIS):
        await swarm.hire(USER, "Developer", name=name)
    return swarm, rec


def freeze_revision_1(swarm, name: str) -> None:
    """`name` keeps the tool set of revision 1 (no `jobs`, `job_stop` or `background`), as an agent whose last
    compaction came before revision 2 does."""
    frozen.write(swarm.db, name, frozen.record(1, "en", frozen.snapshot(1, "en")))


def agent_ctx(swarm, name: str, revision: int | None = None) -> ToolContext:
    """The tool context of `name` with a runtime of its own (and so its own frozen tool set)."""
    if revision == 1:
        freeze_revision_1(swarm, name)
    return ToolContext(swarm, name, AgentRuntime(swarm, name))


async def ok(ctx: ToolContext, tool: str, args: Any = None) -> str:
    text, is_error = await call_tool(ctx, tool, args)
    assert not is_error, text
    return text


async def err(ctx: ToolContext, tool: str, args: Any = None) -> str:
    text, is_error = await call_tool(ctx, tool, args)
    assert is_error, text
    return text


def job_row(swarm, job_id: int) -> dict:
    found = swarm.db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    return dict(found) if found else {}


def tool_results(swarm, name: str) -> list[tuple[str, str]]:
    """(tool, result) of every tool call in `name`'s stored history, in order."""
    msgs = [json.loads(r["msg"]) for r in swarm.db.all("SELECT msg FROM history WHERE agent = ? ORDER BY seq",
                                                        (name,))]
    return [(m.get("name"), m.get("content") or "") for m in msgs if m["role"] == "tool"]


def jobs_count(swarm) -> int:
    return int(swarm.db.scalar("SELECT COUNT(*) FROM jobs"))


def commit_spy(swarm, monkeypatch) -> list[tuple[str, str]]:
    """Record every autocommit (actor, kind) instead of making it."""
    commits: list[tuple[str, str]] = []

    async def autocommit(actor: str, message: str = "", *, kind: str = "", detail: str = "", skipped=None,
                         hold=False, ended=None):
        commits.append((actor, kind or message))

    monkeypatch.setattr(swarm.workspace, "autocommit", autocommit)
    return commits


# --- the shell tool's results ------------------------------------------------------------------------------------

async def test_shell_results_point_at_the_tools_the_caller_has(tmp_path, fast, kids):
    """A revision-2 agent is pointed at `jobs` and `job_stop`; a revision-1 agent, whose tool set lacks both (and
    `background`), at the console commands `jobs jN` and `kill jN`, never at a tool it does not have."""
    swarm, _ = await team(tmp_path)
    try:
        new, old = agent_ctx(swarm, ANNA), agent_ctx(swarm, BORIS, revision=1)
        assert (new.runtime.tools_rev, old.runtime.tools_rev) == (frozen.TOOLSET_REVISION, 1)
        assert new.has_tool("jobs") and new.has_tool("job_stop") and not old.has_tool("jobs")
        # the timer path (footer T)
        text = await ok(new, "shell", {"command": py_cmd(kids.script, "30", kids.pidfile()), "timeout": 1})
        assert text.startswith(f"STILL RUNNING after 1s: continues in the background as job j1 [{PROGRAM}]\n"), text
        assert (f"[Job j1 keeps running in the background; its output goes on into {scratch_rel(ANNA)}jobs/j1.log. "
                "You will be notified when it ends") in text
        assert ('jobs with job_id "j1" shows its latest output; job_stop stops it. It is stopped if it still runs '
                "30 min after it started; start servers and long runs with background=true instead") in text
        assert "do other work, or call wait if nothing else is left." in text
        text = await ok(old, "shell", {"command": py_cmd(kids.script, "30", kids.pidfile()), "timeout": 1})
        assert text.startswith(f"STILL RUNNING after 1s: continues in the background as job j2 [{PROGRAM}]\n"), text
        assert ("To see its latest output, run the shell command jobs j2; to stop it, run the shell command kill j2 "
                "(each as a command of its own)") in text
        assert "(end the command with &, in PowerShell use Start-Process -NoNewWindow)" in text
        # background=true (footer S); a revision-1 agent's schema has no such parameter: it is ignored
        text = await ok(new, "shell", {"command": py_cmd(kids.script, "30", kids.pidfile()), "background": True})
        assert text.startswith(f"STARTED in the background as job j3 [{PROGRAM}]\n"), text
        assert "It may run up to 8 h 00 min and stops when the session stops.]" in text
        text = await ok(old, "shell", {"command": "echo hi", "background": True})
        assert text.startswith(f"exit code: 0 [{PROGRAM}]\nhi"), text
        assert text.endswith("[Ignored unknown argument(s): background. Usage: shell(command, timeout?)]"), text
        text = await ok(new, "shell", {"command": "echo hi", "background": "true"})
        assert text.startswith(f"exit code: 0 [{PROGRAM}] (it ended within 0.5 s, so it did not stay in the "
                               "background)\nhi"), text
        # processes a command left running (footer D)
        text = await ok(new, "shell", {"command": leave(kids.script, "30", kids.pidfile())})
        assert text.startswith(f"exit code: 0 [{PROGRAM}]; processes it started keep running as background job "
                               "j4\n"), text
        assert 'jobs with job_id "j4" shows their latest output; job_stop stops them.]' in text
        text = await ok(old, "shell", {"command": leave(kids.script, "30", kids.pidfile())})
        assert text.startswith(f"exit code: 0 [{PROGRAM}]; processes it started keep running as background job "
                               "j5\n"), text
        assert "to stop them, run the shell command kill j5 (each as a command of its own).]" in text
        for job_id, owner, origin in ((1, ANNA, "timeout"), (2, BORIS, "timeout"), (3, ANNA, "background"),
                                      (4, ANNA, "detached"), (5, BORIS, "detached")):
            found = job_row(swarm, job_id)
            assert (found["owner"], found["origin"], found["status"]) == (owner, origin, "running"), job_id
        # an old agent is never pointed at the tools it lacks, and cannot call them
        for text in (*jobs_module.FOOTERS_OLD.values(), *(how[1] for how in jobs_module.NOTE_HOW.values())):
            assert "job_stop" not in text and "jobs with" not in text and "background=" not in text, text
        text = await err(old, "jobs", {})
        assert text.startswith("Unknown tool 'jobs'.") and "job_stop" not in text
        # a limit reached: background=true is refused before anything starts
        swarm.config.shell.max_jobs_per_member = 3
        text = await err(new, "shell", {"command": "echo never", "background": True})
        assert text == ("Error: You already have 3 background jobs (j1, j3, j4), the most allowed: stop one with "
                        "job_stop, or run this command without background.")
        assert jobs_count(swarm) == 5
        # an external member has no `wait`: it sleeps with wait_for_events, and its footers say so
        await swarm.hire(USER, "Assistant", name=EXT, kind=AgentKind.EXTERNAL)
        ext = ToolContext(swarm, EXT)
        assert ext.has_tool("jobs") and ext.has_tool("wait_for_events") and not ext.has_tool("wait")
        text = await ok(ext, "shell", {"command": py_cmd(kids.script, "30", kids.pidfile()), "timeout": 1})
        assert text.startswith(f"STILL RUNNING after 1s: continues in the background as job j6 [{PROGRAM}]\n"), text
        assert "do other work, or call wait_for_events if nothing else is left." in text and "call wait " not in text
        text = await ok(ext, "shell", {"command": py_cmd(kids.script, "30", kids.pidfile()), "background": True})
        assert text.startswith(f"STARTED in the background as job j7 [{PROGRAM}]\n"), text
        assert "do other work, or call wait_for_events if nothing else is left." in text and "call wait " not in text
    finally:
        await shutdown(swarm)
    assert not any(pid_alive(pid) for pid in kids.pids())  # the session stop ended every job


async def test_jobs_and_job_stop(tmp_path, fast, kids):
    swarm, _ = await team(tmp_path)
    await swarm.hire(USER, "Assistant", name=EXT, kind=AgentKind.EXTERNAL)
    try:
        anna, boris = agent_ctx(swarm, ANNA), agent_ctx(swarm, BORIS)
        ext, user = ToolContext(swarm, EXT), ToolContext(swarm, USER)
        assert await ok(anna, "jobs") == "No background jobs are running."
        chatty = kids.folder / "chatty.py"
        chatty.write_text(CHATTY, encoding="utf-8")
        await ok(anna, "shell", {"command": py_cmd(str(chatty), kids.pidfile()), "background": True})
        await ok(ext, "shell", {"command": py_cmd(kids.script, "30", kids.pidfile()), "background": True})
        pid = await apid(kids, 0)
        await until(lambda: swarm.jobs.describe(1, actor=ANNA).endswith("line 499"))
        # the list: every running job, the caller's own marked, with its log
        text = await ok(anna, "jobs", {})
        assert text.startswith("Running background jobs (2):\n- j1 "), text
        assert f": {ANNA} (you), running " in text and f"; log {scratch_rel(ANNA)}jobs/j1.log" in text
        assert f": {EXT}, running " in text and "started in the background" in text
        assert (await ok(anna, "jobs", {"job_id": " "})).startswith("Running background jobs (2):\n")  # the list
        # one job, by any id form; any member may read any job
        for form in ("j1", "J1", "%1", "#1", "1", 1):
            text = await ok(boris, "jobs", {"job_id": form})
            assert text.startswith(f"Job j1 of {ANNA}: ") and "\nState: running for " in text, form
        text = await ok(anna, "jobs", {"job_id": "j1"})
        assert "\n--- last 40 lines ---\nline 460\n" in text and text.endswith("\nline 499"), text
        text = await ok(anna, "jobs", {"job_id": "j1", "tail": 3})
        assert text.endswith("\n--- last 3 lines ---\nline 497\nline 498\nline 499"), text
        text = await ok(anna, "jobs", {"job_id": "j1", "tail": 1000})  # at most 400 lines
        assert "\n--- last 400 lines ---\nline 100\n" in text and "line 99\n" not in text
        assert "Not a job id: 'abc'" in await err(anna, "jobs", {"job_id": "abc"})
        assert "There is no job j9. Running jobs: j1, j2." in await err(anna, "jobs", {"job_id": "j9"})
        assert "Missing required argument(s) for job_stop: job_id" in await err(anna, "job_stop", {})
        # only its owner or the user stops a job
        text = await err(boris, "job_stop", {"job_id": "j1"})
        assert text == (f"Error: Job j1 belongs to {ANNA}; only its owner or the user can stop it (ask them with "
                        "send_message).")
        text = await ok(anna, "job_stop", {"job_id": "%1"})
        assert text.startswith("Stopped job j1 (") and f") of {ANNA} after " in text, text
        assert "together with the processes it started. Last output:\n" in text and text.endswith("\nline 499")
        assert not pid_alive(pid) and (job_row(swarm, 1)["status"], job_row(swarm, 1)["reason"]) == ("stopped", "owner")
        text = await ok(anna, "job_stop", {"job_id": "j1"})
        assert text.startswith("Job j1 had already ended (stopped by its owner after ") and "nothing to stop" in text
        text = await ok(user, "job_stop", {"job_id": "j2"})
        assert text.startswith("Stopped job j2 (") and job_row(swarm, 2)["reason"] == "user"
        # the jobs that ended in the last hour: the caller's own, everyone's for the user
        text = await ok(anna, "jobs")
        assert text.startswith("No background jobs are running.\nYour jobs that ended in the last hour:\n- j1 ")
        assert ": stopped by its owner after " in text and "j2" not in text
        text = await ok(user, "jobs")
        assert "\nJobs that ended in the last hour:\n- j2 " in text and f": {EXT}, stopped by the user after " in text
        # an ended job is read from the end of its log
        text = await ok(ext, "jobs", {"job_id": "j1"})
        assert "\nState: stopped by its owner after " in text and "\nline 499\n# stopped " in text, text
    finally:
        await shutdown(swarm)


async def test_console_intercept_through_the_shell_tool(tmp_path, fast, kids, monkeypatch):
    """A revision-1 agent lists, shows and stops its jobs with whole command lines in `shell` (`jobs`, `jobs j1`,
    `kill j1`): DreamTeam answers them (" [dreamteam]" on the first line), nothing is spawned or committed. A line
    with anything else goes to the real shell."""
    swarm, _ = await team(tmp_path)
    try:
        old, new = agent_ctx(swarm, BORIS, revision=1), agent_ctx(swarm, ANNA)
        commits = commit_spy(swarm, monkeypatch)
        await ok(old, "shell", {"command": py_cmd(kids.script, "30", kids.pidfile()), "timeout": 1})
        pid = await apid(kids)
        assert commits == [(BORIS, "shell")] and jobs_count(swarm) == 1
        for line in ("jobs", "jobs -l", "  jobs;  "):
            text = await ok(old, "shell", {"command": line})
            assert text.split("\n")[0] == "Running background jobs (1): [dreamteam]", (line, text)
            assert f"\n- j1 " in text and f": {BORIS} (you), running " in text
        for line in ("jobs j1", "jobs %1", "jobs -l 1"):
            text = await ok(old, "shell", {"command": line})
            assert text.startswith(f"Job j1 of {BORIS}: ") and text.split("\n")[0].endswith(" [dreamteam]"), line
        text = await err(old, "shell", {"command": "kill j7"})
        assert text == "Error: There is no job j7. Running jobs: j1. [dreamteam]"
        # a revision-2 agent is answered too (the intercept is never advertised to it)
        assert (await ok(new, "shell", {"command": "jobs"})).startswith("Running background jobs (1): [dreamteam]")
        text = await err(new, "shell", {"command": "kill j1"})
        assert f"Job j1 belongs to {BORIS}; only its owner or the user can stop it" in text
        text = await ok(old, "shell", {"command": "kill -9 %1"})
        first = text.split("\n")[0]
        assert first.startswith("Stopped job j1 (") and first.endswith(" [dreamteam]"), text
        assert not pid_alive(pid) and job_row(swarm, 1)["reason"] == "owner"
        assert commits == [(BORIS, "shell"), (BORIS, "job j1")]  # only the job's own end commit
        assert jobs_count(swarm) == 1
        # anything more on the line is a real command
        text = await ok(old, "shell", {"command": "echo kill j1"})
        assert text.startswith(f"exit code: 0 [{PROGRAM}]\nkill j1") and "[dreamteam]" not in text
        assert commits[-1] == (BORIS, "shell")
    finally:
        await shutdown(swarm)


# --- wait, goal_complete, agents, swarm_status ----------------------------------------------------------------

async def test_wait_names_the_jobs_whose_end_wakes_the_caller(tmp_path, fast, kids):
    swarm, _ = await team(tmp_path)
    try:
        anna = agent_ctx(swarm, ANNA)
        assert await ok(anna, "wait") == "OK, waiting. You will be woken up by new events."
        await ok(anna, "shell", {"command": py_cmd(kids.script, "30", kids.pidfile()), "timeout": 1})
        assert await ok(anna, "wait") == ("OK, waiting. You will be woken up by new events, also when your "
                                          "background job j1 ends.")
        await ok(anna, "shell", {"command": py_cmd(kids.script, "30", kids.pidfile()), "background": True})
        # processes a command left running wake their owner only while it holds a task in progress
        await ok(anna, "shell", {"command": leave(kids.script, "30", kids.pidfile())})
        both = "OK, waiting. You will be woken up by new events, also when your background jobs j1, j2 end."
        assert await ok(anna, "wait") == both
        swarm.set_goal(USER, "Ship it")
        planning = swarm.board.list()[0]
        swarm.board.claim(ANNA, planning.id)
        assert await ok(anna, "wait") == both.replace("j1, j2", "j1, j2, j3")
        assert await ok(agent_ctx(swarm, BORIS), "wait") == "OK, waiting. You will be woken up by new events."
        # once the goal is achieved the ends are quiet: no wake is promised
        swarm.complete_goal(ANNA, "done")
        assert await ok(anna, "wait") == "OK, waiting. You will be woken up by new events."
    finally:
        await shutdown(swarm)


async def test_goal_complete_agents_and_swarm_status_show_the_jobs(tmp_path, fast, kids):
    swarm, _ = await team(tmp_path)
    await swarm.hire(USER, "Assistant", name=EXT, kind=AgentKind.EXTERNAL)
    try:
        anna, boris = agent_ctx(swarm, ANNA), agent_ctx(swarm, BORIS, revision=1)
        ext, user = ToolContext(swarm, EXT), ToolContext(swarm, USER)
        await ok(anna, "shell", {"command": py_cmd(kids.script, "30", kids.pidfile()), "timeout": 1})
        await ok(ext, "shell", {"command": py_cmd(kids.script, "30", kids.pidfile()), "background": True})
        # agents: every member line names its running jobs
        text = await ok(boris, "agents")
        assert re.search(rf"^- {ANNA}: Developer \[idle\].*, background jobs: j1$", text, re.M), text
        assert re.search(rf"^- {EXT}: Assistant \[[a-z]+, external\].*, background jobs: j2$", text, re.M), text
        assert re.search(rf"^- {BORIS} \(you\): .*active [^,]*$", text, re.M), text
        # swarm_status: the swarm's running jobs; a member also sees its own
        line = re.compile(rf"^Background jobs: 2 running \(j1 {ANNA}: .+, \d+ s; j2 {EXT}: .+, \d+ s\)\.$", re.M)
        text = await ok(user, "swarm_status")
        assert line.search(text) and "Your background jobs" not in text, text
        text = await ok(ext, "swarm_status")
        assert line.search(text) and "\nYour background jobs: j2.\n" in text, text
        # goal_complete: the jobs go on, and the caller is told how to stop the ones nobody needs
        swarm.set_goal(USER, "Ship it")
        jobs_line = rf"Note: background jobs are still running: j1 \({ANNA}: .+, \d+ s\); j2 \({EXT}: .+, \d+ s\)\. "
        text = await ok(boris, "goal_complete", {"summary": "done"})
        assert re.search(jobs_line + r"Stop the ones nobody needs with the shell command kill jN \(as a command of "
                                     r"its own\); the rest stop with the session\.$", text), text
        swarm.reopen_goal(USER, "more")
        text = await ok(ext, "goal_complete", {"summary": "done"})
        assert re.search(jobs_line + r"Stop the ones nobody needs with job_stop; the rest stop with the session\.$",
                         text), text
        for job_id in ("j1", "j2"):
            await ok(user, "job_stop", {"job_id": job_id})
        swarm.reopen_goal(USER, "more")
        text = await ok(anna, "goal_complete", {"summary": "done"})
        assert "background jobs" not in text and "background jobs" not in await ok(anna, "agents")
        assert "Background jobs" not in await ok(ext, "swarm_status")
    finally:
        await shutdown(swarm)


async def test_silent_jobs_are_shown_but_left_alone(tmp_path, fast, kids):
    """A running job that printed nothing for more than SILENT_MINUTES (run 006: one ran an hour without output)
    says so in `jobs` (list and one job), swarm_status, its owner's wake-up and the state for the UI, counted from
    its last output or its start; a live job.silent event marks each change. Nothing stops it, nobody is notified."""
    swarm, _ = await team(tmp_path)
    flips: list[tuple[int, Any]] = []
    swarm.bus.listen(lambda ev: ev["type"] == "job.silent"
                     and flips.append((ev["data"]["job"]["id"], ev["data"]["job"]["silent_since"])))
    try:
        anna, user = agent_ctx(swarm, ANNA), ToolContext(swarm, USER)
        for _ in range(2):
            await ok(anna, "shell", {"command": py_cmd(kids.script, "30", kids.pidfile()), "background": True})
        await apid(kids, 1)
        quiet, talking = swarm.jobs.running_of(ANNA)
        await until(lambda: quiet.last_output_at and talking.last_output_at)  # "child up"
        now = time.time()
        quiet.started_at, quiet.cmd.capture.last_output_at = now - 41 * 60, None  # never printed: since its start
        talking.started_at, talking.cmd.capture.last_output_at = now - 50 * 60, now - 5 * 60
        assert quiet.silent_phrase("en") == "no output for 41 min" and talking.silent_phrase("en") == ""
        assert quiet.silent_phrase("ru") == "41 мин без вывода"
        text = await ok(anna, "jobs")
        assert re.search(r"^- j1 .+: Anna Smith \(you\), running 41 min, started in the background, no output for "
                         r"41 min; log ", text, re.M), text
        assert re.search(r"^- j2 .+: Anna Smith \(you\), running 50 min, started in the background; log ", text,
                         re.M), text
        text = await ok(user, "jobs", {"job_id": "j1"})
        assert "\nState: running for 41 min (started in the background); no output for 41 min" in text, text
        assert "no output" not in await ok(user, "jobs", {"job_id": "j2"})
        text = await ok(user, "swarm_status")
        assert re.search(r"^Background jobs: 2 running \(j1 Anna Smith: .+, 41 min, no output for 41 min; j2 Anna "
                         r"Smith: .+, 50 min\)\.$", text, re.M), text
        wake = swarm.render_wake(ANNA)
        assert re.search(r"Your background jobs: j1 .+ \(running 41 min, no output for 41 min\); j2 .+ \(running 50 "
                         r"min\)\n", wake), wake
        jobs = {j["id"]: j for j in swarm.snapshot()["jobs"]}
        assert jobs[1]["silent_since"] == pytest.approx(quiet.started_at, abs=1) and jobs[1]["last_output_at"] is None
        assert jobs[2]["silent_since"] is None and jobs[2]["last_output_at"] == pytest.approx(now - 300, abs=1)
        await until(lambda: (1, pytest.approx(quiet.started_at, abs=2)) in flips)
        assert [job_id for job_id, _ in flips] == [1]
        quiet.cmd.capture.last_output_at = time.time()  # it prints again: the marker goes
        await until(lambda: flips[-1] == (1, None))
        assert "no output" not in await ok(anna, "jobs") and quiet.status == "running"
        assert not any("no output" in r["text"] for r in swarm.db.all("SELECT text FROM notifications"))
        assert not swarm.db.scalar("SELECT COUNT(*) FROM events WHERE type = 'job.silent'")  # live only
    finally:
        await shutdown(swarm)


def test_a_job_whose_output_ended_is_not_silent():
    """Its shell ended and what it left running has its own or no stdout: it cannot print here any more, so it is
    not shown as silent (the marker would invite stopping a healthy server)."""
    from types import SimpleNamespace

    cmd = SimpleNamespace(eof=False, capture=SimpleNamespace(last_output_at=None))
    now = time.time()
    job = jobs_module.Job(id=1, owner=ANNA, command="npm run dev", origin="detached", cmd=cmd, log="j1.log",
                          log_path=Path("j1.log"), started_at=now - 41 * 60, background_at=now - 41 * 60)
    assert job.silent_phrase("en", now) == "no output for 41 min"
    cmd.eof = True
    assert job.silent_for(now) is None and job.silent_phrase("en", now) == ""


# --- scripted agents ---------------------------------------------------------------------------------------------

async def test_scripted_agents_use_background_jobs_through_their_runtimes(tmp_path, fast, kids):
    """Anna (revision 2): a test run goes on as a job at its timeout, she calls `wait` and its end wakes her; then she
    starts a server with background=true, reads its output with `jobs` and stops it with `job_stop`. Boris
    (revision 1, frozen before): a command leaves a process running (`&`, `start /b`), which becomes his job; he
    lists and stops it with the console commands `jobs` and `kill jN`. Each keeps sending its own tool set."""
    state: dict[str, Any] = {}

    def policy(swarm, name, system, messages, tools):
        last = messages[-1]
        text = str(last.get("content") or "")
        tool = last.get("name") if last["role"] == "tool" else None
        if name == ANNA:
            if tool is None and "has ended (exit code 0)" in text:
                return call("shell", command=py_cmd(kids.script, "30", kids.pidfile()), background=True)
            if tool is None and not state.get("anna"):
                state["anna"] = True
                return call("shell", command=py_cmd(kids.script, "2", kids.pidfile()), timeout=1)
            started = re.search(r"STARTED in the background as job (j\d+)", text) if tool == "shell" else None
            if started:
                return call("jobs", job_id=started.group(1))
            shown = re.match(r"Job (j\d+) of ", text) if tool == "jobs" else None
            if shown:
                return call("job_stop", job_id=shown.group(1))
            return call("wait")
        if tool is None and not state.get("boris"):
            state["boris"] = True
            return call("shell", command=leave(kids.script, "30", kids.pidfile()))
        kept = re.search(r"keep running as background job (j\d+)", text) if tool == "shell" else None
        if kept:
            state["boris_job"] = kept.group(1)
            return call("shell", command="jobs")
        if tool == "shell" and text.startswith("Running background jobs"):
            return call("shell", command=f"kill {state['boris_job']}")
        return call("wait")

    swarm, rec = await team(tmp_path, policy)
    freeze_revision_1(swarm, BORIS)
    await swarm.stop()
    await swarm.start()  # the runtimes start (Anna freezes revision 2) and their hiring notices wake them
    try:
        await until(lambda: [t for t, _ in tool_results(swarm, ANNA)] == ["shell", "wait", "shell", "jobs",
                                                                          "job_stop", "wait"]
                    and [t for t, _ in tool_results(swarm, BORIS)] == ["shell", "shell", "shell", "wait"], 30)
        assert swarm.runtimes[ANNA].tools_rev == frozen.TOOLSET_REVISION and swarm.runtimes[BORIS].tools_rev == 1
        assert dumps(swarm.runtimes[BORIS].tools) == dumps(frozen.snapshot(1, "en"))
        (_, timer), (_, waited), (_, server), (_, shown), (_, stopped), _ = tool_results(swarm, ANNA)
        timer_id = re.search(r"as job (j\d+) ", timer).group(1)
        server_id = re.search(r"as job (j\d+) ", server).group(1)
        assert timer.startswith(f"STILL RUNNING after 1s: continues in the background as job {timer_id} [{PROGRAM}]")
        assert f"jobs with job_id \"{timer_id}\"" in timer
        assert waited == (f"OK, waiting. You will be woken up by new events, also when your background job "
                          f"{timer_id} ends.")
        woken = [m[-1]["content"] for m in rec.calls[ANNA] if "has ended (exit code 0)" in str(m[-1]["content"])]
        assert f"Your background job {timer_id} has ended (exit code 0) after running " in woken[0]
        assert "  child end" in woken[0]  # the end of its output
        assert server.startswith(f"STARTED in the background as job {server_id} [{PROGRAM}]")
        assert shown.startswith(f"Job {server_id} of {ANNA}: ") and "State: running for " in shown
        assert stopped.startswith(f"Stopped job {server_id} (")
        (_, kept), (_, listed), (_, killed), _ = tool_results(swarm, BORIS)
        boris_id = state["boris_job"]
        assert kept.startswith(f"exit code: 0 [{PROGRAM}]; processes it started keep running as background job "
                               f"{boris_id}\n")
        assert f"run the shell command kill {boris_id}" in kept and "job_stop" not in kept
        assert listed.split("\n")[0].endswith(" [dreamteam]") and f"\n- {boris_id} " in listed
        assert killed.startswith(f"Stopped job {boris_id} (") and killed.split("\n")[0].endswith(" [dreamteam]")
        statuses = {job_row(swarm, int(i[1:]))["reason"] for i in (server_id, boris_id)}
        assert statuses == {"owner"} and job_row(swarm, int(timer_id[1:]))["status"] == "ended"
    finally:
        await shutdown(swarm)


# --- the hub -----------------------------------------------------------------------------------------------------

async def test_hub_instructions_and_a_call_killed_by_the_session_stop(tmp_path, fast, kids, monkeypatch):
    """MCP clients learn the jobs from the hub's instructions. An external member's `shell` call killed by the session
    stop returns KILLED, and nothing is committed or traced for it once the swarm stops (its database closes right
    after)."""
    for snippet in ("`jobs` and `job_stop` list and stop every member's\nbackground jobs.",
                    "A `shell` command still running at its timeout (at most 600\nseconds over MCP) continues as a "
                    "background job, and background=true starts one at once",
                    "`job_stop` stops one, and a job's end reaches you\nthrough `wait_for_events`, so don't poll."):
        assert snippet in INSTRUCTIONS, snippet
    swarm, _ = await team(tmp_path)
    await swarm.hire(USER, "Assistant", name=EXT, kind=AgentKind.EXTERNAL)
    commits = commit_spy(swarm, monkeypatch)
    traced: list[str] = []
    real = swarm.tracer.external_call

    def external_call(agent, tool, *args, **kw):
        traced.append(tool)
        return real(agent, tool, *args, **kw)

    monkeypatch.setattr(swarm.tracer, "external_call", external_call)
    in_flight = None
    try:
        text, is_error = await hub_call(swarm, "shell", {"as_agent": EXT, "command": "echo hi"})
        assert not is_error and text.startswith(f"exit code: 0 [{PROGRAM}]")
        assert traced == ["shell"] and commits == [(EXT, "shell")]
        in_flight = asyncio.ensure_future(hub_call(swarm, "shell", {
            "as_agent": EXT, "command": py_cmd(kids.script, "30", kids.pidfile()), "timeout": 30}))
        pid = await apid(kids)
    finally:
        await swarm.stop()
    try:
        text, is_error = await asyncio.wait_for(in_flight, 10)
        assert not is_error and text.startswith(f"KILLED: the session is stopping [{PROGRAM}]"), text
        assert traced == ["shell"] and commits == [(EXT, "shell")] and not pid_alive(pid)
        assert jobs_count(swarm) == 0
    finally:
        swarm.close()
