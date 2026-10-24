"""The planning hint (docs/SPEC.md §3, scheduler item 4): the member holding the planning task that has created no
task after agents.planning_hint_steps LLM steps or planning_hint_minutes is told to create the independent tasks now,
once more at twice the value, never a third time; quiet once it created tasks, and never for a task without the
planning tag. Scripted swarms only (ScriptedLLM)."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

import pytest

from dreamteam import prompts
from dreamteam import swarm as swarm_module
from dreamteam.config import Config
from dreamteam.models import USER, Status

from test_runtime import call, kinds_for, make_swarm, run_until, shutdown

HINT = "have created no task yet"  # in the English n_planning_hint


def planner_team(tmp_path, script: Callable[[int], Any], **agents: Any):
    """A team of two: the first member follows `script(step)` (its 1-based LLM call number), the other one waits.
    idle_nudge_seconds is long, so no n_stalled wakes the planner while it waits holding its task."""
    seen: dict[str, Any] = {"steps": 0, "hints_at": []}

    def policy(swarm, name, system, messages, tools):
        if name != seen.get("planner"):
            return call("wait")
        seen["steps"] += 1
        hints = sum(str(m.get("content") or "").count(HINT) for m in messages if m["role"] == "user")
        if hints > len(seen["hints_at"]):  # the first call that sees a new hint
            seen["hints_at"].append(seen["steps"])
        return script(seen["steps"])

    swarm, rec = make_swarm(tmp_path, policy, swarm={"start_agents": 2},
                            agents={"idle_nudge_seconds": 60, "max_steps_per_turn": 40, **agents})
    return swarm, seen


async def start(swarm, seen, goal: bool = True) -> str:
    await swarm.start()
    seen["planner"] = swarm.members()[0]["name"]
    if goal:
        swarm.set_goal(USER, "Ship the tool")
    return seen["planner"]


def research(after: int, *, create_at: int | None = None) -> Callable[[int], Any]:
    """Claim #1, then look at the board (research) up to step `after`, then wait; `create_at`: create a task then."""
    def script(step: int) -> Any:
        if step == 1:
            return call("task_claim", task_id=1)
        if step == create_at:
            return call("task_create", title="Read the docs", goal="the docs are summarized")
        return call("board") if step <= after else call("wait")
    return script


async def test_the_hint_arrives_after_n_steps_in_the_same_activation_and_once_more_at_2n(tmp_path):
    swarm, seen = planner_team(tmp_path, research(16), planning_hint_steps=3, planning_hint_minutes=0)
    try:
        planner = await start(swarm, seen)
        await run_until(lambda: seen["steps"] >= 17)
        await asyncio.sleep(0.2)
        # the claim is step 1; 3 more steps (2-4) -> the hint is in the incoming text of step 5; 6 more -> step 8
        assert seen["hints_at"] == [5, 8]
        assert kinds_for(swarm, planner).count("n_planning_hint") == 2  # never a third time
        text = swarm.db.scalar("SELECT text FROM notifications WHERE kind = 'n_planning_hint' ORDER BY id")
        assert "#1" in text and "(steps: 3)" in text and "`task_create`" in text
        assert swarm.board.get(1).status == Status.IN_PROGRESS  # a hint, no hard limit
        other = swarm.members()[1]["name"]
        assert "n_planning_hint" not in kinds_for(swarm, other)
    finally:
        await shutdown(swarm)


async def test_no_hint_when_the_planner_created_a_task(tmp_path):
    swarm, seen = planner_team(tmp_path, research(14, create_at=2), planning_hint_steps=3, planning_hint_minutes=0)
    try:
        planner = await start(swarm, seen)
        await run_until(lambda: seen["steps"] >= 15)
        await asyncio.sleep(0.2)
        assert "n_planning_hint" not in kinds_for(swarm, planner) and seen["hints_at"] == []
    finally:
        await shutdown(swarm)


async def test_a_task_created_after_the_first_hint_stops_the_repeat(tmp_path):
    swarm, seen = planner_team(tmp_path, research(14, create_at=6), planning_hint_steps=3, planning_hint_minutes=0)
    try:
        planner = await start(swarm, seen)
        await run_until(lambda: seen["steps"] >= 15)
        await asyncio.sleep(0.2)
        assert seen["hints_at"] == [5]
        assert kinds_for(swarm, planner).count("n_planning_hint") == 1
    finally:
        await shutdown(swarm)


async def test_no_hint_for_a_task_without_the_planning_tag(tmp_path):
    swarm, seen = planner_team(tmp_path, research(14), planning_hint_steps=3, planning_hint_minutes=0)
    try:
        planner = await start(swarm, seen, goal=False)
        swarm.board.create(USER, "Investigate the bug", goal="the cause is known")  # #1, not a planning task
        swarm.set_goal(USER, "Fix the bug")  # the board is not empty: no planning task, n_goal_updated
        await run_until(lambda: seen["steps"] >= 15)
        await asyncio.sleep(0.2)
        assert "planning" not in swarm.board.get(1).tags and swarm.board.get(1).assignee == planner
        assert "n_planning_hint" not in kinds_for(swarm, planner)
    finally:
        await shutdown(swarm)


async def test_no_hint_with_nobody_else_on_the_team(tmp_path):
    swarm, seen = planner_team(tmp_path, research(10), planning_hint_steps=3, planning_hint_minutes=0)
    swarm.config.swarm.start_agents = 1
    try:
        planner = await start(swarm, seen)
        assert len(swarm.members()) == 1
        await run_until(lambda: seen["steps"] >= 11)
        await asyncio.sleep(0.2)
        assert "n_planning_hint" not in kinds_for(swarm, planner)
    finally:
        await shutdown(swarm)


async def test_the_time_trigger_wakes_an_idle_planner_twice_at_most(tmp_path, monkeypatch):
    monkeypatch.setattr(swarm_module, "SCHEDULER_SECONDS", 0.05)
    # claim, then wait holding the task: only the time trigger (0.3 s, then 0.6 s) can bring the hint
    swarm, seen = planner_team(tmp_path, research(1), planning_hint_steps=0, planning_hint_minutes=0.005)
    try:
        planner = await start(swarm, seen)
        await run_until(lambda: len(seen["hints_at"]) >= 2, timeout=10)
        await asyncio.sleep(0.8)
        # each hint woke the waiting planner: its wake-up text holds it (steps 3 and 4 after claim and wait)
        assert seen["hints_at"] == [3, 4] and seen["steps"] == 4
        assert kinds_for(swarm, planner).count("n_planning_hint") == 2
        text = swarm.db.scalar("SELECT text FROM notifications WHERE kind = 'n_planning_hint' ORDER BY id")
        assert "(steps: 1)" in text  # the wait after the claim
    finally:
        await shutdown(swarm)


async def test_the_time_trigger_reaches_a_planner_in_the_middle_of_its_activation(tmp_path, monkeypatch):
    """A long step (here a slow LLM call) does not hide the time trigger: the scheduler's hint is shown as incoming
    news after that step, in the same activation."""
    monkeypatch.setattr(swarm_module, "SCHEDULER_SECONDS", 0.05)

    async def slow_board() -> dict:
        await asyncio.sleep(0.6)  # the 0.3 s trigger passes during this call
        return call("board")

    def script(step: int) -> Any:
        if step == 1:
            return call("task_claim", task_id=1)
        if step == 3:
            return slow_board()
        return call("board") if step == 2 else call("wait")

    swarm, seen = planner_team(tmp_path, script, planning_hint_steps=0, planning_hint_minutes=0.005)
    try:
        planner = await start(swarm, seen)
        await run_until(lambda: seen["steps"] >= 4)
        await asyncio.sleep(0.1)
        # the step after the slow one, which ran tools: the same activation (the planner never went idle before)
        assert seen["hints_at"][0] == 4
        assert "n_planning_hint" in kinds_for(swarm, planner)
    finally:
        await shutdown(swarm)


async def test_a_pause_stops_the_clock(tmp_path, monkeypatch):
    """A pause is no planning time: after a pause longer than 2T the first hint still needs T of holding and the
    repeat another T (before, both went out at once right after the resume)."""
    monkeypatch.setattr(swarm_module, "SCHEDULER_SECONDS", 0.05)
    swarm, seen = planner_team(tmp_path, research(1), planning_hint_steps=0, planning_hint_minutes=0.005)  # T 0.3 s
    try:
        await start(swarm, seen)
        await run_until(lambda: seen["steps"] >= 2)  # claimed (the after-step look starts the clock), then waits
        since = swarm._planning_watch[1]["since"]
        swarm.pause(USER, "user")
        held_before = time.time() - since
        await asyncio.sleep(1.0)
        assert swarm.db.scalar("SELECT COUNT(*) FROM notifications WHERE kind = 'n_planning_hint'") == 0
        resumed = time.time()
        swarm.resume(USER)
        await run_until(lambda: len(seen["hints_at"]) >= 2, timeout=10)
        first, second = (r["ts"] for r in swarm.db.all(
            "SELECT ts FROM notifications WHERE kind = 'n_planning_hint' ORDER BY id"))
        assert first - resumed >= 0.3 - held_before - 0.02  # T of holding, the pause left out
        assert second - first >= 0.3 - 0.06  # the repeat at 2T, not at once (one scheduler pass of slack)
    finally:
        await shutdown(swarm)


async def test_no_hint_in_wrap_up_mode(tmp_path):
    swarm, seen = planner_team(tmp_path, research(12), planning_hint_steps=3, planning_hint_minutes=0)
    try:
        planner = await start(swarm, seen)
        swarm.wrap_up(USER)  # the team finishes with what it has: nobody is told to create tasks
        await run_until(lambda: seen["steps"] >= 12)
        await asyncio.sleep(0.2)
        assert "n_wrap_up" in kinds_for(swarm, planner) and "n_planning_hint" not in kinds_for(swarm, planner)
    finally:
        await shutdown(swarm)


async def test_the_cap_of_two_survives_a_lost_count(tmp_path):
    """The counts live in memory; a restart (here: the watch forgotten) starts them again, but the hints the member
    already had for the task are counted from the notifications table: never a third one."""
    steps = research(30)

    def script(step: int) -> Any:
        if step == 10:  # after both hints (steps 5 and 8): what a new process starts with
            swarm._planning_watch.clear()
        return steps(step)

    swarm, seen = planner_team(tmp_path, script, planning_hint_steps=3, planning_hint_minutes=0)
    try:
        planner = await start(swarm, seen)
        await run_until(lambda: seen["steps"] >= 31)
        await asyncio.sleep(0.2)
        assert seen["hints_at"] == [5, 8] and kinds_for(swarm, planner).count("n_planning_hint") == 2
    finally:
        await shutdown(swarm)


def test_the_settings_are_checked(tmp_path):
    cfg = Config.from_dict(tmp_path, {})
    assert (cfg.agents.planning_hint_steps, cfg.agents.planning_hint_minutes) == (8, 10.0)
    ok = Config.from_dict(tmp_path, {"agents": {"planning_hint_steps": 0, "planning_hint_minutes": 2.5}})
    assert (ok.agents.planning_hint_steps, ok.agents.planning_hint_minutes) == (0, 2.5)
    for key, value, expected in (("planning_hint_steps", -1, ">= 0"), ("planning_hint_steps", 2.5, "a whole number"),
                                 ("planning_hint_minutes", "ten", "a number"), ("planning_hint_minutes", -1, ">= 0")):
        with pytest.raises(ValueError, match=rf"\[agents\] {key} must be {expected}"):
            Config.from_dict(tmp_path, {"agents": {key: value}})


def test_the_planning_task_sends_research_to_the_others():
    """Step 1 of the planning task: read only enough to split the work; research, experiments and environment checks
    are tasks for the others. Both languages say so; the hint names only `task_create`."""
    step1 = {}
    for lang in ("en", "ru"):
        description = prompts.PLANNING_TASK[lang][2]
        step1[lang] = next(line for line in description.splitlines() if line.startswith("1. "))
    for snippet in ("Read only as much as you need to split the work", "research, experiments", "checks of the "
                    "environment", "separate tasks for the others", "The team waits for your tasks"):
        assert snippet in step1["en"], snippet
    for snippet in ("Читай ровно столько, сколько нужно, чтобы разбить работу", "исследования", "эксперименты",
                    "проверки окружения", "отдельные задачи для остальных", "Команда тем временем ждёт"):
        assert snippet in step1["ru"], snippet
    for lang in ("en", "ru"):
        text = prompts.t(lang, "n_planning_hint", id=1, steps=8, elapsed="10 min")
        assert "#1" in text and "8" in text and "10 min" in text and "`task_create`" in text
