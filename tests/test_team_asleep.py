"""The whole team asleep (docs/SPEC.md §3 scheduler item 9 `_wake_team_asleep`, `team_asleep`, `stall_ids`; §8
"Unattended triggers"; §12 `wait`; §13 hub_call's external stamp): one idle member woken with `n_team_asleep` once the
team has slept TEAM_ASLEEP_FACTOR x 2 ** sent x idle_nudge_seconds with work open and nothing set to wake anyone, USER
told with the first (`u_team_asleep`) or alone (`u_team_waits`); `TriggerEngine.unattended`; the honest clause of
`wait`'s result. The item is called directly with aged clocks; the runtimes are stand-ins that never activate, so
nothing but the test changes the board (one test runs real agents through the scheduler loop)."""

from __future__ import annotations

import asyncio
import io
import time
from types import SimpleNamespace

import pytest

from dreamteam import prompts
from dreamteam import swarm as swarm_module
from dreamteam import triggers as triggers_module
from dreamteam.db import loads
from dreamteam.hub_mcp import hub_call
from dreamteam.models import USER, AgentKind, AgentStatus, Status
from dreamteam.tools import ToolContext, call_tool
from dreamteam.tools.swarm_tools import WAIT_UNWATCHED
from dreamteam.tui import printmode, render
from test_runtime import call, make_swarm, run_until, shutdown

WAIT = 60.0                  # agents.idle_nudge_seconds of these tests
DUE = 5 * WAIT               # TEAM_ASLEEP_FACTOR x WAIT: the first wake
EXT = "Claude Code"
PLAIN = "OK, waiting. You will be woken up by new events."


@pytest.fixture(autouse=True)
def no_scheduler_loop(monkeypatch):
    """The items are called directly; the loop never comes round within a test (one test speeds it up again)."""
    monkeypatch.setattr(swarm_module, "SCHEDULER_SECONDS", 3600.0)


async def _no_stop() -> None:
    pass


def sleeper(**kw) -> SimpleNamespace:
    """A runtime that never activates: a wake leaves its news pending."""
    return SimpleNamespace(**{"state": AgentStatus.IDLE, "stalled": frozenset(), "stalled_at": 0.0,
                              "interrupted": False, "_recovery": None, "_task": None, "llm_steps": 0,
                              "wake": lambda: None, "reset_llm": lambda: None, "stop": _no_stop, **kw})


def drain(swarm, *names: str) -> None:
    for name in names or [m["name"] for m in swarm.members() if m["kind"] == AgentKind.LLM]:
        swarm.render_wake(name)


def rested(swarm, seconds: float = 2 * DUE) -> None:
    """Every member went idle `seconds` ago."""
    swarm.db.execute("UPDATE agents SET last_active = ?", (time.time() - seconds,))


def age(swarm, seconds: float) -> None:
    """The team-asleep clock and the members' idleness are `seconds` older."""
    swarm.db.execute("UPDATE agents SET last_active = last_active - ?", (seconds,))
    if swarm._team_asleep is not None:
        key, since = swarm._team_asleep
        swarm._team_asleep = (key, since - seconds)


def rows(swarm, kind: str) -> list[dict]:
    return [{"recipient": r["recipient"], "text": r["text"], **loads(r["data"], {})}
            for r in swarm.db.all("SELECT recipient, text, data FROM notifications WHERE kind = ? ORDER BY id",
                                  (kind,))]


def woken(swarm) -> list[str]:
    return [r["recipient"] for r in rows(swarm, "n_team_asleep")]


def set_goal(swarm, status: str = "active") -> None:
    swarm.db.set("goal", {"title": "G", "description": "", "status": status, "summary": "", "set_by": USER,
                          "updated_at": time.time() - 10 * DUE})


def task(swarm, title: str, assignee: str | None, status: str = Status.IN_PROGRESS, **raw) -> int:
    t = swarm.board.create(USER, title, goal="g", assignee=assignee)
    swarm.db.execute("UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?", (status, time.time() - 2 * DUE, t.id))
    for column, value in raw.items():
        swarm.db.execute(f"UPDATE tasks SET {column} = ? WHERE id = ?", (value, t.id))
    return t.id


async def team(tmp_path, agents: int = 1, **swarm_settings):
    swarm, _ = make_swarm(tmp_path, lambda *a: call("wait"), swarm={"start_agents": agents, **swarm_settings},
                          agents={"idle_nudge_seconds": WAIT})
    await swarm.start(run_agents=False)
    names = [m["name"] for m in swarm.members()]
    for name in names:
        swarm.runtimes[name] = sleeper()
    set_goal(swarm)
    return swarm, names


async def asleep_with_a_task(tmp_path, **settings):
    """A lone member holding an in_progress task, idle for long, nothing pending."""
    swarm, (anna,) = await team(tmp_path, **settings)
    task(swarm, "Run the experiment", anna)
    drain(swarm)
    rested(swarm)
    return swarm, anna


def back_to_sleep(swarm, name: str) -> None:
    """`name` read its news and went idle again just now."""
    drain(swarm, name)
    swarm.db.execute("UPDATE agents SET last_active = ? WHERE name = ?", (time.time(), name))


def due_pass(swarm, seconds: float = DUE) -> None:
    """One pass that starts the clock, then one `seconds` later."""
    swarm._wake_team_asleep()
    age(swarm, seconds)
    swarm._wake_team_asleep()


# --- the wake, its delay and its cap ------------------------------------------------------------------------------

async def test_a_lone_member_is_woken_once_at_the_due_time_and_the_user_is_told(tmp_path):
    swarm, anna = await asleep_with_a_task(tmp_path)
    signalled: list[str] = []
    signal = swarm.signal
    swarm.signal = lambda name: (signalled.append(name), signal(name))[1]
    try:
        assert swarm.team_asleep() and swarm.stall_ids(anna)
        swarm._wake_team_asleep()
        age(swarm, DUE - 5)
        swarm._wake_team_asleep()
        assert woken(swarm) == []                                   # not yet due
        age(swarm, 5)
        swarm._wake_team_asleep()
        assert woken(swarm) == [anna] and signalled == [anna, USER]  # both waking
        (member,) = rows(swarm, "n_team_asleep")
        assert member["elapsed"] == "5 min" and member["jobs"] == ""
        assert member["text"].startswith("Nobody in the team has worked for 5 min, and nothing is set to wake anyone")
        assert "A process started with & or nohup that `jobs` does not list is not tracked" in member["text"]
        assert "file_exists(\"out/result.json\") or elapsed(1800)" in member["text"]
        assert "call `goal_complete`" in member["text"]
        (user,) = rows(swarm, "u_team_asleep")
        assert user["name"] == anna and user["text"] == prompts.t("en", "u_team_asleep", elapsed="5 min", name=anna)
        back_to_sleep(swarm, anna)                                  # it read the notice and went back to sleep
        swarm._wake_team_asleep()
        age(swarm, 2 * DUE - 5)
        swarm._wake_team_asleep()
        assert len(woken(swarm)) == 1                               # the second waits twice as long
        age(swarm, 5)
        swarm._wake_team_asleep()
        assert woken(swarm) == [anna, anna] and len(rows(swarm, "u_team_asleep")) == 1  # the user once per key
        back_to_sleep(swarm, anna)
        age(swarm, 100 * DUE)
        swarm._wake_team_asleep()
        assert len(woken(swarm)) == 2                               # never a third for one board state
    finally:
        await shutdown(swarm)


async def test_a_changed_board_re_arms_it_but_the_delay_keeps_doubling_under_the_goal(tmp_path):
    swarm, anna = await asleep_with_a_task(tmp_path)
    try:
        waits = []
        for n in range(3):
            swarm._wake_team_asleep()
            for step in range(1, 40):
                age(swarm, WAIT)
                swarm._wake_team_asleep()
                if len(woken(swarm)) > n:
                    waits.append(step)
                    break
            drain(swarm, anna)
            # the member flips its task at each wake: a new board key
            tid = swarm.board.list(status=Status.IN_PROGRESS)[0].id
            swarm.db.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (time.time() + n, tid))
            rested(swarm)
        assert waits == [5, 10, 20]                                 # 5, 10, 20 x idle_nudge_seconds
        assert len(rows(swarm, "u_team_asleep")) == 3               # the first wake of each board state
        swarm.db.set("goal", {**swarm.goal(), "updated_at": time.time()})  # a new goal: the count starts again
        swarm.db.execute("UPDATE tasks SET updated_at = ?", (time.time() + 10,))
        rested(swarm)
        due_pass(swarm)
        assert len(woken(swarm)) == 4
    finally:
        await shutdown(swarm)


async def test_the_jobs_whose_end_is_quiet_are_named(tmp_path, monkeypatch):
    """Detached jobs of a member that holds no in_progress task: their end wakes nobody (JobManager.wakes)."""
    swarm, (anna,) = await team(tmp_path)
    try:
        task(swarm, "Waits for an answer", anna, status=Status.WAITING)
        drain(swarm)
        rested(swarm)
        jobs = [SimpleNamespace(id=i, label=f"j{i}", owner=anna, origin="detached", started_at=time.time())
                for i in (3, 5)]
        with monkeypatch.context() as m:  # scoped: the autouse patch of the loop stays for the whole test
            m.setattr(swarm.jobs, "running_of", lambda owner=None: jobs if owner in (None, anna) else [])
            due_pass(swarm)
        (member,) = rows(swarm, "n_team_asleep")
        assert member["jobs"] == (" Your background jobs j3, j5 still run, but their end will not wake you while you "
                                  "hold no task in progress.")
        assert member["jobs"] in member["text"]
    finally:
        await shutdown(swarm)


async def test_a_russian_member_and_user_get_russian_texts(tmp_path):
    swarm, anna = await asleep_with_a_task(tmp_path, language="ru")
    try:
        due_pass(swarm)
        (member,) = rows(swarm, "n_team_asleep")
        assert member["text"].startswith("Уже 5 мин никто в команде не работает")
        assert "Процесс, запущенный через & или nohup, которого нет в списке `jobs`" in member["text"]
        assert rows(swarm, "u_team_asleep")[0]["text"].startswith("Уже 5 мин никто в команде не работает")
    finally:
        await shutdown(swarm)


# --- when it still fires --------------------------------------------------------------------------------------------

async def test_it_fires_with_a_task_in_the_users_review(tmp_path):
    swarm, anna = await asleep_with_a_task(tmp_path)  # independent review on: a lone member's work goes to USER
    try:
        assert swarm.board.independent_review
        task(swarm, "Reviewed by the user", anna, status=Status.REVIEW, reviewer=USER)
        drain(swarm)
        rested(swarm)
        due_pass(swarm)
        assert woken(swarm) == [anna]
    finally:
        await shutdown(swarm)


async def test_it_fires_after_n_all_done_was_answered_with_wait(tmp_path):
    swarm, (anna,) = await team(tmp_path)
    try:
        task(swarm, "Done", anna, status=Status.DONE)
        drain(swarm)
        rested(swarm)
        swarm._wake_team_asleep()
        due_pass(swarm)
        assert woken(swarm) == []                                   # item 6 has not spoken yet
        swarm._check_all_done()
        assert [r["recipient"] for r in rows(swarm, "n_all_done")] == [anna]
        drain(swarm, anna)
        rested(swarm)
        due_pass(swarm)
        assert woken(swarm) == [anna] and "verify the result and call `goal_complete`" in rows(
            swarm, "n_team_asleep")[0]["text"]
    finally:
        await shutdown(swarm)


@pytest.mark.parametrize("condition, owner, aged", [
    ("status(1) == done", "anna", 0),                     # board state only: it waits for someone to act
    ("done(1)", "anna", 0),
    ("elapsed(1) and done(99)", "anna", 60),              # its moment passed: held back by board state
    ("file_exists(\"out/result.json\")", "anna", 31 * 60),  # files only, older than UNATTENDED_FILE_SECONDS
    ("every(60)", USER, 0),                               # the user's own reminder wakes nobody of the team
])
async def test_it_fires_with_a_trigger_that_waits_for_someone(tmp_path, condition, owner, aged):
    swarm, anna = await asleep_with_a_task(tmp_path)
    try:
        swarm.triggers.create(anna if owner == "anna" else owner, condition)
        swarm.db.execute("UPDATE triggers SET created_at = created_at - ?", (aged,))
        assert not swarm.triggers.unattended(time.time())
        due_pass(swarm)
        assert woken(swarm) == [anna]
    finally:
        await shutdown(swarm)


async def test_a_member_in_error_with_no_retry_counts_as_asleep(tmp_path):
    swarm, (anna, boris) = await team(tmp_path, agents=2)
    try:
        task(swarm, "Mine", anna)
        drain(swarm)
        rested(swarm)
        swarm.db.execute("UPDATE agents SET status = 'error' WHERE name = ?", (boris,))
        assert swarm.team_asleep()
        swarm.runtimes[boris]._recovery = object()                  # a retry on its way: it counts as awake
        assert not swarm.team_asleep()
        swarm.runtimes[boris]._recovery = None
        due_pass(swarm)
        assert woken(swarm) == [anna]                               # never the one in error
    finally:
        await shutdown(swarm)


# --- when it stays silent -------------------------------------------------------------------------------------------

async def test_silent_while_a_job_whose_end_wakes_the_member_runs_or_it_waits_for_a_file(tmp_path, monkeypatch):
    swarm, anna = await asleep_with_a_task(tmp_path)
    try:
        with monkeypatch.context() as m:
            m.setattr(swarm.jobs, "awaited", lambda name: [SimpleNamespace(id=1, label="j1")])
            due_pass(swarm)
        assert woken(swarm) == [] and swarm._team_asleep is None
        swarm.db.execute("INSERT INTO lock_waiters(agent, path, since) VALUES (?, 'a.txt', ?)", (anna, time.time()))
        due_pass(swarm)
        assert woken(swarm) == []
        swarm.db.execute("DELETE FROM lock_waiters")
        due_pass(swarm)
        assert woken(swarm) == [anna]
    finally:
        await shutdown(swarm)


@pytest.mark.parametrize("condition", ["elapsed(600)", "file_exists(\"out/result.json\")",
                                       "file_exists(\"out/result.json\") or elapsed(1800)", "every(600)"])
async def test_silent_with_a_trigger_that_can_still_fire(tmp_path, condition):
    swarm, anna = await asleep_with_a_task(tmp_path)
    try:
        swarm.triggers.create(anna, condition)
        due_pass(swarm)
        assert woken(swarm) == []
    finally:
        await shutdown(swarm)


async def test_silent_while_a_delayed_notice_is_pending(tmp_path):
    swarm, anna = await asleep_with_a_task(tmp_path)
    try:
        swarm.notify_later(3600, anna, "n_restart")                 # e.g. a staggered kickoff
        due_pass(swarm)
        assert woken(swarm) == []
    finally:
        await shutdown(swarm)


async def test_silent_with_an_empty_board_or_without_an_active_goal(tmp_path):
    swarm, (anna,) = await team(tmp_path)
    try:
        drain(swarm)
        rested(swarm)
        due_pass(swarm)
        assert woken(swarm) == []                                   # an empty board
        task(swarm, "Mine", anna)
        drain(swarm)
        rested(swarm)
        set_goal(swarm, "done")
        due_pass(swarm)
        assert woken(swarm) == [] and swarm._team_asleep is None
    finally:
        await shutdown(swarm)


async def test_silent_while_an_ai_reviewer_has_a_review_or_only_the_users_reviews_are_left(tmp_path):
    swarm, (anna, boris) = await team(tmp_path, agents=2)
    try:
        task(swarm, "Mine", anna)
        review = task(swarm, "Under review", anna, status=Status.REVIEW, reviewer=boris)
        drain(swarm)
        rested(swarm)
        due_pass(swarm)
        assert woken(swarm) == []                                   # item 5 reminds and hands it over
        # the user's review while anna's only task waits: item 7 tells the user, item 9 stands down
        swarm.db.execute("UPDATE tasks SET reviewer = ? WHERE id = ?", (USER, review))
        swarm.db.execute("UPDATE tasks SET status = 'waiting' WHERE id = 1")
        due_pass(swarm)
        assert woken(swarm) == []
        swarm.config.swarm.independent_review = False               # off: a review blocks nothing
        swarm.db.execute("UPDATE tasks SET reviewer = ? WHERE id = ?", (boris, review))
        due_pass(swarm)
        assert woken(swarm) == [anna]
    finally:
        await shutdown(swarm)


async def test_silent_while_another_member_works_or_has_news(tmp_path):
    swarm, (anna, boris) = await team(tmp_path, agents=2)
    try:
        task(swarm, "Mine", anna)
        drain(swarm)
        rested(swarm)
        swarm.db.execute("UPDATE agents SET status = 'working' WHERE name = ?", (boris,))
        due_pass(swarm)
        assert woken(swarm) == []
        swarm.db.execute("UPDATE agents SET status = 'idle' WHERE name = ?", (boris,))
        swarm.notify(boris, "n_restart")
        due_pass(swarm)
        assert woken(swarm) == []
    finally:
        await shutdown(swarm)


async def test_silent_while_an_external_member_called_recently_but_not_while_it_only_listens(tmp_path):
    swarm, anna = await asleep_with_a_task(tmp_path)
    try:
        await swarm.hire(USER, "Assistant", name=EXT, kind=AgentKind.EXTERNAL)
        drain(swarm)
        rested(swarm)
        text, is_error = await hub_call(swarm, "wait_for_events", {"timeout": 0, "as_agent": EXT})
        assert not is_error and EXT not in swarm._external_seen     # a client that only listens is asleep
        before = time.time()
        text, is_error = await hub_call(swarm, "board", {"as_agent": EXT})
        assert not is_error and swarm._external_seen[EXT] >= before
        await hub_call(swarm, "swarm_status", {})                   # the user's own call stamps nothing
        assert set(swarm._external_seen) == {EXT}
        due_pass(swarm)
        assert woken(swarm) == []
        swarm._external_seen[EXT] = time.time() - swarm_module.EXTERNAL_IDLE_SECONDS - 1
        due_pass(swarm)
        assert woken(swarm) == [anna]
    finally:
        await shutdown(swarm)


async def test_silent_while_a_clarify_planner_waits_for_the_users_answers(tmp_path, monkeypatch):
    swarm, anna = await asleep_with_a_task(tmp_path, clarify=True)
    try:
        plan = task(swarm, "Clarify the goal", anna, status=Status.WAITING)
        swarm.db.execute("UPDATE tasks SET tags = ? WHERE id = ?", ('["planning", "clarify"]', plan))
        monkeypatch.setattr(swarm, "asks_user", lambda name, t: t.id == plan)
        drain(swarm)
        rested(swarm)
        due_pass(swarm)
        assert woken(swarm) == []
        monkeypatch.setattr(swarm, "asks_user", lambda name, t: False)
        due_pass(swarm)
        assert woken(swarm) == [anna]
    finally:
        await shutdown(swarm)


async def test_the_loop_skips_it_while_paused_and_resume_drops_the_clock(tmp_path, monkeypatch):
    monkeypatch.setattr(swarm_module, "SCHEDULER_SECONDS", 0.02)
    monkeypatch.setattr(swarm_module, "TEAM_ASLEEP_FACTOR", 0)      # due at the first pass that finds it asleep
    swarm, (anna,) = await team(tmp_path)
    try:
        swarm.pause(USER, swarm_module.USER_PAUSE)
        task(swarm, "Mine", anna)
        drain(swarm)
        rested(swarm)
        await asyncio.sleep(0.3)
        assert woken(swarm) == []
        swarm._team_asleep = (("key",), time.time())
        swarm.resume(USER)
        assert swarm._team_asleep is None
        drain(swarm, anna)                                          # n_resumed read
        await run_until(lambda: woken(swarm) == [anna], timeout=5)
    finally:
        await shutdown(swarm)


# --- whom ---------------------------------------------------------------------------------------------------------

async def test_the_holder_of_a_task_in_progress_is_woken_first(tmp_path):
    swarm, (anna, boris, carl) = await team(tmp_path, agents=3)
    try:
        task(swarm, "Anna's", anna)
        task(swarm, "Boris's", boris, status=Status.TODO)
        drain(swarm)
        rested(swarm)
        swarm.db.execute("UPDATE agents SET last_active = ? WHERE name IN (?, ?)", (time.time() - DUE, boris, carl))
        swarm._wake_team_asleep()
        age(swarm, DUE - 60)
        swarm.db.execute("UPDATE agents SET last_active = ? WHERE name = ?", (time.time() - 10, carl))
        swarm._wake_team_asleep()                                   # carl was active in between: the clock restarts
        age(swarm, DUE - 20)
        swarm._wake_team_asleep()
        assert woken(swarm) == []
        age(swarm, 10)
        swarm._wake_team_asleep()
        assert woken(swarm) == [anna] and [r["name"] for r in rows(swarm, "u_team_asleep")] == [anna]
        assert rows(swarm, "n_team_asleep")[0]["elapsed"] == "5 min"  # since the last of them went idle
    finally:
        await shutdown(swarm)


async def test_a_todo_holder_then_anyone_when_a_task_has_no_assignee(tmp_path):
    swarm, (anna, boris) = await team(tmp_path, agents=2)
    try:
        task(swarm, "Boris's", boris, status=Status.TODO)
        drain(swarm)
        rested(swarm)
        due_pass(swarm)
        assert woken(swarm) == [boris]
        swarm.db.execute("UPDATE tasks SET assignee = NULL, updated_at = ?", (time.time() + 1,))
        swarm.db.execute("UPDATE agents SET last_active = ? WHERE name = ?", (time.time() - 2 * DUE + 1, anna))
        drain(swarm)
        due_pass(swarm, 2 * DUE)                                    # the second wake under the goal: twice the delay
        assert woken(swarm) == [boris, anna]                        # any idle member: the one active last
    finally:
        await shutdown(swarm)


async def test_only_the_user_is_told_when_every_open_task_is_theirs(tmp_path):
    swarm, (anna,) = await team(tmp_path)
    try:
        await swarm.hire(USER, "Assistant", name=EXT, kind=AgentKind.EXTERNAL)
        task(swarm, "The user's", USER)
        task(swarm, "The client's", EXT, status=Status.WAITING)
        drain(swarm)
        rested(swarm)
        due_pass(swarm)
        assert woken(swarm) == [] and rows(swarm, "u_team_asleep") == []
        (user,) = rows(swarm, "u_team_waits")
        assert user["tasks"] == "#1, #2" and user["text"] == prompts.t("en", "u_team_waits", elapsed="5 min",
                                                                       tasks="#1, #2")
        age(swarm, 100 * DUE)
        swarm._wake_team_asleep()
        assert len(rows(swarm, "u_team_waits")) == 1                # once per board state
    finally:
        await shutdown(swarm)


async def test_open_tasks_held_only_by_a_member_in_error_wake_nobody(tmp_path):
    swarm, (anna, boris) = await team(tmp_path, agents=2)
    try:
        task(swarm, "Boris's", boris)
        drain(swarm)
        rested(swarm)
        swarm.db.execute("UPDATE agents SET status = 'error' WHERE name = ?", (boris,))
        due_pass(swarm)
        assert woken(swarm) == [] and rows(swarm, "u_team_waits") == []
    finally:
        await shutdown(swarm)


# --- the real loop ----------------------------------------------------------------------------------------------------

async def test_a_real_agent_that_waits_for_an_untracked_process_is_woken_and_told_in_its_wait_result(tmp_path,
                                                                                                      monkeypatch):
    """A lone agent holds an in_progress task and only calls `wait`: item 3 wakes it once (n_stalled), then item 9
    (5 and 10 x idle_nudge_seconds), never a third time for that board state; its `wait` result says that nothing of
    its own is set to wake it."""
    monkeypatch.setattr(swarm_module, "SCHEDULER_SECONDS", 0.05)
    results: list[str] = []

    def policy(swarm, name, system, messages, tools):
        results.extend(m.get("content") or "" for m in messages if m["role"] == "tool")
        return call("wait")

    swarm, rec = make_swarm(tmp_path, policy, agents={"idle_nudge_seconds": 0.1})
    await swarm.start()
    try:
        (anna,) = [m["name"] for m in swarm.members()]
        set_goal(swarm)
        tid = task(swarm, "Run the experiment", anna)
        await run_until(lambda: len(woken(swarm)) == 2, timeout=10)
        await asyncio.sleep(0.6)
        assert woken(swarm) == [anna, anna]
        # asleep again with that board state: aged far past the third wake's delay (5 x 2 ** 2 x 0.1 s), still none
        assert swarm._team_asleep is not None
        age(swarm, 1000)
        await asyncio.sleep(0.3)
        assert woken(swarm) == [anna, anna] and len(rows(swarm, "u_team_asleep")) == 1
        assert [r["kind"] for r in swarm.db.all("SELECT kind FROM notifications WHERE recipient = ? AND kind IN "
                                                "('n_stalled', 'n_team_asleep') ORDER BY id", (anna,))] == [
            "n_stalled", "n_team_asleep", "n_team_asleep"]
        assert WAIT_UNWATCHED in results
        assert swarm.board.get(tid).status == Status.IN_PROGRESS
    finally:
        await shutdown(swarm)


# --- the user's surfaces (docs/TUI.md) ------------------------------------------------------------------------------

def test_print_mode_writes_the_users_notices_to_stderr_once_and_the_terminal_warns():
    notes = {"u_team_asleep", "u_team_waits"}
    assert notes <= printmode.STDERR_NOTES and notes <= render._WARN_NOTES
    out, err = io.StringIO(), io.StringIO()
    run = printmode._PrintRun(SimpleNamespace(client=SimpleNamespace(auto_reply=False)), "x", session=None,
                              continue_last=False, title=None, overrides=None, output_format="text", verbose=False,
                              timeout=None, auto_reply=False, lang="en", out=out, err=err)
    for kind, kw in (("u_team_asleep", {"name": "Anna"}), ("u_team_waits", {"tasks": "#1"})):
        text = prompts.t("en", kind, elapsed="5 min", **kw)
        event = {"type": "notification", "data": {"recipient": USER, "kind": kind, "text": text}}
        asyncio.run(run.on_event(event))
        asyncio.run(run.on_event(event))                            # the same text: once
        assert err.getvalue().endswith(text + "\n") and err.getvalue().count(text) == 1
    assert out.getvalue() == ""


# --- TriggerEngine.unattended ------------------------------------------------------------------------------------

async def test_unattended_reaches_an_llm_member(tmp_path):
    swarm, (anna,) = await team(tmp_path)
    try:
        await swarm.hire(USER, "Assistant", name=EXT, kind=AgentKind.EXTERNAL)
        engine, now = swarm.triggers, time.time()

        def counts(owner: str, actions=None) -> bool:
            swarm.db.execute("DELETE FROM triggers")
            engine.create(owner, "elapsed(600)", actions=actions)
            return engine.unattended(now)

        assert counts(anna)                                         # the default action wakes its owner
        assert not counts(USER) and not counts(EXT)                 # wakes only the user or a client
        assert counts(USER, [{"type": "message", "to": anna, "text": "go"}])
        assert counts(USER, [{"type": "message", "to": "all", "text": "go"}])
        assert counts(EXT, [{"type": "message", "to": [EXT, anna.split()[0]], "text": "go"}])  # a first name
        assert not counts(anna, [{"type": "message", "to": "user", "text": "done"}])
        assert not counts(USER, [{"type": "message", "to": EXT, "text": "go"}])
        assert counts(USER, [{"type": "set_status", "task_id": 1, "status": "todo"}])
        assert counts(EXT, [{"type": "create_task", "title": "Check"}])
    finally:
        await shutdown(swarm)


async def test_unattended_may_fire_on_a_time_a_file_or_held_fires(tmp_path, monkeypatch):
    swarm, (anna,) = await team(tmp_path)
    try:
        engine = swarm.triggers
        grace, files = triggers_module.UNATTENDED_GRACE, triggers_module.UNATTENDED_FILE_SECONDS

        def made(condition: str, *, once: bool = True, **row) -> float:
            swarm.db.execute("DELETE FROM triggers")
            t = engine.create(anna, condition, once=once)
            for column, value in row.items():
                swarm.db.execute(f"UPDATE triggers SET {column} = ? WHERE id = ?", (value, t["id"]))
            return float(swarm.db.scalar("SELECT created_at FROM triggers"))

        created = made("elapsed(600)")
        assert engine.unattended(created + 600 + grace) and not engine.unattended(created + 600 + grace + 1)
        created = made("elapsed(60) and done(3)")
        assert engine.unattended(created + 30) and not engine.unattended(created + 120)
        created = made("elapsed(60) or elapsed(1800)")                # the latest moment
        assert engine.unattended(created + 1000)
        created = made("after(\"2999-01-01 00:00\")")
        assert engine.unattended(created)
        created = made("after(\"2001-01-01 00:00\") and done(3)")
        assert not engine.unattended(created)
        created = made("every(600)", last_fired=time.time() + 1000)
        assert engine.unattended(created + 1500) and not engine.unattended(created + 1000 + 600 + grace + 1)
        created = made("file_exists(\"out/result.json\")")
        assert engine.unattended(created + files - 1) and not engine.unattended(created + files)
        created = made("file_changed(\"out/log.txt\")", once=False, last_fired=time.time() + 100)
        assert engine.unattended(created + 100 + files - 1)
        created = made("file_exists(\"out/result.json\") and done(3)")  # files and board state: like board state
        assert not engine.unattended(created)
        created = made("file_exists(\"out/result.json\") or elapsed(3600)")  # a time function: its deadline
        assert engine.unattended(created + files + 60) and not engine.unattended(created + 3600 + grace + 1)
        created = made("status(3) == done")
        assert not engine.unattended(created)
        created = made("elapsed(60)", once=False, last_value=1)       # repeating, true now: fires only after false
        assert not engine.unattended(created)
        created = made("every(600)", once=False, last_value=1)        # periodic: it fires again at its next period
        assert engine.unattended(created + 100)                       # even while true
        created = made("status(3) == done", once=False, held=2)       # held fires: their release wakes the owner
        assert engine.unattended(created)
        swarm.db.execute("UPDATE triggers SET active = 0")
        assert not engine.unattended(created)
        assert not engine.unattended(time.time())
    finally:
        await shutdown(swarm)


# --- the honest `wait` result (§12) ----------------------------------------------------------------------------------

async def wait_text(swarm, name: str) -> str:
    text, is_error = await call_tool(ToolContext(swarm, name), "wait", {})
    assert not is_error, text
    return text


async def test_wait_says_nothing_of_the_callers_is_set_when_the_team_sleeps(tmp_path):
    swarm, anna = await asleep_with_a_task(tmp_path)
    try:
        swarm.db.execute("UPDATE agents SET status = 'working' WHERE name = ?", (anna,))  # the caller is at work
        assert await wait_text(swarm, anna) == WAIT_UNWATCHED
        assert WAIT_UNWATCHED.startswith(PLAIN[:-1] + ", but nothing of yours is set to wake you")
        assert "& or nohup" in WAIT_UNWATCHED and "`&`" not in WAIT_UNWATCHED and "`nohup`" not in WAIT_UNWATCHED
        swarm.triggers.create(anna, "status(1) == done")              # any trigger of its own: it set it on purpose
        assert await wait_text(swarm, anna) == PLAIN
    finally:
        await shutdown(swarm)


async def test_wait_keeps_the_plain_text_otherwise(tmp_path, monkeypatch):
    swarm, (anna, boris) = await team(tmp_path, agents=2)
    try:
        tid = task(swarm, "Mine", anna)
        drain(swarm)
        rested(swarm)
        assert await wait_text(swarm, anna) == WAIT_UNWATCHED
        assert await wait_text(swarm, boris) == PLAIN                 # it holds no task
        swarm.db.execute("UPDATE agents SET status = 'working' WHERE name = ?", (boris,))
        assert await wait_text(swarm, anna) == PLAIN                  # another member works
        swarm.db.execute("UPDATE agents SET status = 'idle' WHERE name = ?", (boris,))
        swarm.db.execute("UPDATE tasks SET status = 'waiting' WHERE id = ?", (tid,))
        assert await wait_text(swarm, anna) == PLAIN                  # a waiting task: answers wake it
        swarm.db.execute("UPDATE tasks SET status = 'in_progress' WHERE id = ?", (tid,))
        with monkeypatch.context() as m:
            m.setattr(swarm, "asks_user", lambda name, t: True)
            assert await wait_text(swarm, anna) == PLAIN              # a clarify planner waiting for the user
        swarm.db.execute("INSERT INTO lock_waiters(agent, path, since) VALUES (?, 'a.txt', ?)", (anna, time.time()))
        assert await wait_text(swarm, anna) == ("OK, waiting. You will be woken up by new events, also when a.txt, "
                                                "which you were refused, is free.")
        swarm.db.execute("DELETE FROM lock_waiters")
        with monkeypatch.context() as m:
            m.setattr(swarm.jobs, "awaited", lambda name: [SimpleNamespace(id=4, label="j4")])
            assert await wait_text(swarm, anna) == ("OK, waiting. You will be woken up by new events, also when your "
                                                    "background job j4 ends.")
        assert await wait_text(swarm, anna) == WAIT_UNWATCHED
        set_goal(swarm, "done")
        assert await wait_text(swarm, anna) == PLAIN                  # a done goal
    finally:
        await shutdown(swarm)
