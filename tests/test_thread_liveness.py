"""The thread mode's liveness (docs/SPEC.md §22.7): the scheduler's thread branch (T1 digest batching, then T2 the
quiet check with n_thread_quiet and u_thread_quiet), team_asleep's thread clauses, `wait`'s unwatched clause, the
jobs' thread reading (_works_on and the shell result's texts), clarify, wrap-up, money, and pauses and restarts. A
real thread Swarm with runtime stand-ins that never activate; the items are called directly with aged clocks (a few
tests run the scheduler loop, sped up). ScriptedLLM only, no network."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dreamteam import swarm as swarm_module
from dreamteam.agent import AgentRuntime
from dreamteam.config import Config
from dreamteam.forum import GOAL_POST
from dreamteam.jobs import (FOOTERS, FOOTERS_THREAD, NOTES, NOTES_THREAD, WRAP_UP_ERROR_THREAD, ShellOutcome,
                            fmt_duration)
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.models import USER, AgentKind, AgentStatus, ToolError
from dreamteam.swarm import DIGEST_KEY, QUIET_KEY, Swarm
from dreamteam.tools import ToolContext, call_tool
from dreamteam.tools.swarm_tools import WAIT_UNWATCHED

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

ANNA, BOB, CARL, EVE = "Anna Smith", "Bob Stone", "Carl Brown", "Eve Black"
WAIT = 60.0  # agents.idle_nudge_seconds of these tests: the first nudge of a goal
PLAIN = "OK, waiting. You will be woken up by new events."
BOARD_ITEMS = ("_nudge_idle", "_wake_stalled", "_wake_idle_holders", "hint_planner", "_remind_reviewers",
               "_check_all_done", "_remind_user_reviews", "_tell_crew_gaps", "_wake_team_asleep")


def make_config(tmp_path: Path, *, agents: dict | None = None, **swarm: Any) -> Config:
    raw: dict[str, Any] = {
        "swarm": {"name": "Liveness", "language": "en", "start_agents": 0, "max_agents": 8, "max_cost_usd": 0,
                  "mode": "thread", "clarify": False, **swarm},
        "agents": {"stagger_seconds": 0, "idle_nudge_seconds": WAIT, **(agents or {})},
        "workspace": {"git": False},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
    }
    return Config.from_dict(tmp_path / "project", raw)


@pytest.fixture
def no_loop(monkeypatch):
    """The scheduler's loop never comes round within a test that calls the items itself."""
    monkeypatch.setattr(swarm_module, "SCHEDULER_SECONDS", 3600.0)


@pytest.fixture
def make_swarm(tmp_path):
    made: list[Swarm] = []

    def factory(**kw: Any) -> Swarm:
        sw = Swarm(config=make_config(tmp_path / f"s{len(made)}", **kw),
                   llm_factory=lambda profile, agent: ScriptedLLM(lambda *a: "ok", profile))
        made.append(sw)
        return sw

    yield factory
    for sw in made:
        try:
            sw.close()
        except Exception:  # noqa: BLE001 - closed already
            pass


@pytest.fixture
def sw(make_swarm):
    return make_swarm()


async def _no_stop() -> None:
    pass


def sleeper(sw: Swarm, name: str, on_wake: Any = None) -> list[int]:
    """An idle runtime stand-in that never activates: its wakes are counted, its news stays pending."""
    woken: list[int] = []

    def wake() -> None:
        woken.append(1)
        if on_wake is not None:
            on_wake()

    sw.runtimes[name] = SimpleNamespace(state=AgentStatus.IDLE, interrupted=False, _recovery=None, llm_steps=0,
                                        wake=wake, reset_llm=lambda: None, stop=_no_stop)
    return woken


def team(sw: Swarm, *names: str) -> dict[str, list[int]]:
    """LLM members with stand-in runtimes, their first news read, idle for long."""
    woken = {}
    for name in names:
        sw._create_member(USER, "Generalist", name=name, kind=AgentKind.LLM)
        woken[name] = sleeper(sw, name)
    drain(sw)
    sw.db.execute("UPDATE agents SET last_active = ?", (time.time() - 100 * WAIT,))
    return woken


def set_goal(sw: Swarm, status: str = "active") -> dict:
    goal = {"title": "Build the parser", "description": "", "status": status, "summary": "", "set_by": USER,
            "updated_at": time.time() - 10 ** 6}
    sw.db.set("goal", goal)
    sw.forum.ensure_system_posts(goal)
    return goal


def drain(sw: Swarm, *names: str) -> None:
    for name in names or [m["name"] for m in sw.members() if m["kind"] == AgentKind.LLM]:
        sw.render_wake(name)
    sw.render_wake(USER)


def back_to_sleep(sw: Swarm, name: str) -> None:
    """`name` read its news and went idle again just now."""
    sw.render_wake(name)
    sw.db.execute("UPDATE agents SET last_active = ? WHERE name = ?", (time.time(), name))


def age(sw: Swarm, seconds: float) -> None:
    """Every clock T1 and T2 read `seconds` older: the quiet clock, the members' activity, the notifications and the
    digest floor."""
    sw.db.execute("UPDATE agents SET last_active = last_active - ?", (seconds,))
    sw.db.execute("UPDATE notifications SET ts = ts - ?", (seconds,))
    sw._digest_floor -= seconds
    if sw._thread_quiet is not None:
        key, since = sw._thread_quiet
        sw._thread_quiet = (key, since - seconds)


def rows(sw: Swarm, kind: str | None = None, recipient: str | None = None) -> list[dict]:
    out = []
    for r in sw.db.all("SELECT * FROM notifications ORDER BY id"):
        if (kind is None or r["kind"] == kind) and (recipient is None or r["recipient"] == recipient):
            out.append({**dict(r), "data": json.loads(r["data"] or "{}")})
    return out


def nudged(sw: Swarm) -> list[str]:
    return [r["recipient"] for r in rows(sw) if r["kind"] in ("n_thread_quiet", "n_thread_quiet_wrap_up")]


def told(sw: Swarm) -> int:
    return len(rows(sw, "u_thread_quiet"))


def quiet_after(sw: Swarm, seconds: float) -> None:
    """One pass that starts (or keeps) the clock, then one `seconds` later."""
    sw._check_quiet()
    age(sw, seconds)
    sw._check_quiet()


async def wait_text(sw: Swarm, actor: str) -> str:
    text, is_error = await call_tool(ToolContext(sw, actor, AgentRuntime(sw, actor)), "wait", {})
    assert not is_error, text
    return text


# ============================================================================== the scheduler's thread branch
async def test_a_thread_pass_runs_t1_then_t2_after_the_deferred_wakes_and_never_a_board_item(make_swarm,
                                                                                              monkeypatch):
    monkeypatch.setattr(swarm_module, "SCHEDULER_SECONDS", 0.01)
    sw = make_swarm()
    calls: list[str] = []
    for item in BOARD_ITEMS + ("_wake_deferred", "_deliver_digests", "_check_quiet"):
        monkeypatch.setattr(sw, item, lambda *a, _item=item, **k: calls.append(_item))
    await sw.start(run_agents=False)
    try:
        for _ in range(200):
            if len(calls) >= 6:
                break
            await asyncio.sleep(0.01)
        assert calls[:6] == ["_wake_deferred", "_deliver_digests", "_check_quiet"] * 2
        assert not set(BOARD_ITEMS) & set(calls)
        sw.db.set("paused", {"paused": True, "reason": "paused by the user"})
        await asyncio.sleep(0.05)
        count = len(calls)
        await asyncio.sleep(0.1)
        assert len(calls) == count  # paused: neither T1 nor T2
    finally:
        await sw.stop()


# ============================================================================== T2: the quiet check
def test_a_lone_member_is_nudged_then_the_user_told_with_the_second_nudge_of_the_same_key(sw, no_loop):
    team(sw, ANNA)
    set_goal(sw)
    quiet_after(sw, WAIT - 1)
    assert nudged(sw) == []  # not before idle_nudge_seconds
    age(sw, 2)
    sw._check_quiet()
    assert nudged(sw) == [ANNA] and told(sw) == 0  # the first nudge of a goal: USER is not told
    notice = rows(sw, "n_thread_quiet")[0]
    assert QUIET_KEY not in notice["data"] and "Goal vote (#1" in notice["text"]
    assert "like #1 (`vote`)" in notice["text"] and notice["data"]["jobs"] == ""
    sw._check_quiet()
    assert nudged(sw) == [ANNA]  # its notice is pending: not idle
    back_to_sleep(sw, ANNA)
    quiet_after(sw, 2 * WAIT - 1)
    assert nudged(sw) == [ANNA]  # the delay doubles: sent = 1
    age(sw, 2)
    sw._check_quiet()
    assert nudged(sw) == [ANNA, ANNA] and told(sw) == 1
    user = rows(sw, "u_thread_quiet")[0]
    assert user["recipient"] == USER and QUIET_KEY not in user["data"]
    assert "Nobody in the team has worked for 2 min" in user["text"] and "Goal vote (#1" in user["text"]
    back_to_sleep(sw, ANNA)
    quiet_after(sw, 10 ** 5)
    assert nudged(sw) == [ANNA, ANNA] and told(sw) == 1  # TEAM_ASLEEP_MAX per key


def test_a_comment_or_a_vote_of_the_nudged_member_makes_a_new_key_and_the_delay_keeps_doubling(sw, no_loop):
    team(sw, ANNA)
    set_goal(sw)
    quiet_after(sw, WAIT)
    assert nudged(sw) == [ANNA] and told(sw) == 0
    back_to_sleep(sw, ANNA)
    sw.forum.comment(ANNA, GOAL_POST, "Nothing to do for me.")  # a new key
    quiet_after(sw, 2 * WAIT - 1)
    assert len(nudged(sw)) == 1
    age(sw, 2)
    sw._check_quiet()
    assert len(nudged(sw)) == 2 and told(sw) == 1  # it follows an earlier nudge of this goal
    back_to_sleep(sw, ANNA)
    sw.forum.vote(ANNA, f"#{GOAL_POST}", "dislike")  # another new key
    quiet_after(sw, 4 * WAIT - 1)
    assert len(nudged(sw)) == 2
    age(sw, 2)
    sw._check_quiet()
    assert len(nudged(sw)) == 3 and told(sw) == 2  # once per key
    back_to_sleep(sw, ANNA)
    sw.forum.vote(ANNA, f"#{GOAL_POST}", "dislike")  # the same vote again: the same key
    quiet_after(sw, 8 * WAIT)
    assert len(nudged(sw)) == 4 and told(sw) == 2  # never twice for one key
    back_to_sleep(sw, ANNA)
    quiet_after(sw, 10 ** 5)
    assert len(nudged(sw)) == 4  # two per key


@pytest.mark.parametrize("change", ["file.changed", "files.committed", "message"])
def test_a_workspace_change_or_a_message_makes_a_new_key(sw, no_loop, change):
    team(sw, ANNA)
    set_goal(sw)
    quiet_after(sw, WAIT)
    back_to_sleep(sw, ANNA)
    quiet_after(sw, 2 * WAIT)
    assert nudged(sw) == [ANNA, ANNA] and told(sw) == 1
    back_to_sleep(sw, ANNA)
    quiet_after(sw, 10 ** 5)
    assert len(nudged(sw)) == 2  # TEAM_ASLEEP_MAX for this key
    back_to_sleep(sw, ANNA)
    key = sw._thread_key()
    if change == "file.changed":
        sw.bus.emit("file.changed", ANNA, path="a.md", op="write")
    elif change == "files.committed":
        sw.bus.emit("files.committed", ANNA, kind="shell", commit="abc", files=[{"path": "a.md", "change": "M"}],
                    suspects=[ANNA])
    else:
        sw.mail.send(ANNA, [USER], "hi")
    assert sw._thread_key() != key
    quiet_after(sw, 4 * WAIT - 1)
    assert len(nudged(sw)) == 2  # sent = 2: the delay doubles again
    age(sw, 2)
    sw._check_quiet()
    assert len(nudged(sw)) == 3 and told(sw) == 2  # a new key: nudged again, the user told again


def test_a_member_in_error_does_not_restart_the_clock(sw, no_loop):
    team(sw, ANNA, BOB)
    set_goal(sw)
    sw.db.execute("UPDATE agents SET status = ? WHERE name = ?", (AgentStatus.ERROR, BOB))
    sw._check_quiet()
    since = sw._thread_quiet[1]
    sw.db.execute("UPDATE agents SET last_active = ? WHERE name = ?", (time.time() + 5, BOB))  # a failing retry
    sw._check_quiet()
    assert sw._thread_quiet[1] == since
    age(sw, WAIT)
    sw._check_quiet()
    assert nudged(sw) == [ANNA]


def test_wrap_up_mode_sends_its_form_and_tells_the_user_with_the_first_nudge(sw, no_loop):
    team(sw, ANNA)
    set_goal(sw)
    sw.wrap_up(USER)
    drain(sw)
    quiet_after(sw, WAIT)
    assert [r["kind"] for r in rows(sw) if r["kind"].startswith("n_thread_quiet")] == ["n_thread_quiet_wrap_up"]
    assert told(sw) == 1
    assert "Wrap-up mode is on" in rows(sw, "n_thread_quiet_wrap_up")[0]["text"]


def test_wrap_up_switched_on_after_a_keys_first_nudge(sw, no_loop):
    team(sw, ANNA)
    set_goal(sw)
    quiet_after(sw, WAIT)
    assert nudged(sw) == [ANNA] and told(sw) == 0
    sw.wrap_up(USER)
    back_to_sleep(sw, ANNA)
    quiet_after(sw, 2 * WAIT)
    assert [r["kind"] for r in rows(sw) if r["kind"].startswith("n_thread_quiet")] == [
        "n_thread_quiet", "n_thread_quiet_wrap_up"]
    assert told(sw) == 1


def test_the_least_recently_active_idle_member_is_nudged_and_russian_texts(make_swarm, no_loop):
    sw = make_swarm(language="ru")
    team(sw, ANNA, BOB)
    set_goal(sw)
    now = time.time()
    sw.db.execute("UPDATE agents SET last_active = ? WHERE name = ?", (now - 50 * WAIT, ANNA))
    sw.db.execute("UPDATE agents SET last_active = ? WHERE name = ?", (now - 90 * WAIT, BOB))
    quiet_after(sw, WAIT)
    assert nudged(sw) == [BOB]
    text = rows(sw, "n_thread_quiet")[0]["text"]
    assert text.startswith("В треде тихо") and "Голосование по цели (#1" in text and "ё" not in text
    back_to_sleep(sw, BOB)
    quiet_after(sw, 2 * WAIT)
    assert nudged(sw) == [BOB, ANNA]  # round the team
    user = rows(sw, "u_thread_quiet")[0]["text"]
    assert user.startswith("Уже 2 мин") and "ё" not in user


def test_the_jobs_whose_end_is_quiet_are_named(sw, no_loop, monkeypatch):
    """Under an active goal every job of a thread member is awaited (_works_on), so the list is empty whenever T2
    fires; the placeholder keeps the text right should the job rule change."""
    team(sw, ANNA)
    set_goal(sw)
    job = SimpleNamespace(id=3, label="j3", origin="detached", started_at=time.time())
    monkeypatch.setattr(sw.jobs, "running_of", lambda owner=None: [job])
    monkeypatch.setattr(sw.jobs, "awaited", lambda owner: [])
    quiet_after(sw, WAIT)
    assert rows(sw, "n_thread_quiet")[0]["data"]["jobs"] == (
        " Your background jobs j3 still run, but their end will not wake you.")


def test_silent_while_an_idle_member_holds_digest_rows_but_not_for_votes_or_a_member_in_error(sw, no_loop):
    team(sw, ANNA, BOB)
    set_goal(sw)
    sw.forum.create(BOB, "Lexer", "")  # a digest row of Anna's
    quiet_after(sw, 10 ** 4)
    assert nudged(sw) == [] and sw.team_asleep() is False
    assert sw.team_asleep(ANNA) is True  # the caller's own rows do not count
    sw.render_wake(ANNA)
    post = sw.forum.create(ANNA, "Parser", "")
    sw.forum.vote(BOB, f"#{post['id']}", "like")  # a vote alone: never a batch, never keeps the team awake
    assert [r["kind"] for r in rows(sw, recipient=ANNA) if r["delivered_at"] is None] == ["f_vote"]
    assert [r["kind"] for r in rows(sw, recipient=BOB) if r["delivered_at"] is None] == ["f_post"]
    sw.db.execute("UPDATE agents SET status = 'error' WHERE name = ?", (BOB,))  # in error, no retry scheduled
    quiet_after(sw, WAIT)
    assert nudged(sw) == [ANNA]  # Bob's digest keeps nobody awake


def test_silent_while_a_file_item_settles(sw, no_loop):
    team(sw, ANNA)
    set_goal(sw)
    now = time.time()
    sw.db.insert("INSERT INTO notifications(ts, recipient, kind, text, data) VALUES (?, ?, 'f_file', 'x', ?)",
                 (now, ANNA, json.dumps({QUIET_KEY: True, "_settle": {"first": now, "last": now}})))
    quiet_after(sw, 10 ** 4)
    assert nudged(sw) == []
    sw.db.execute("UPDATE notifications SET delivered_at = ?", (time.time(),))
    quiet_after(sw, WAIT)
    assert nudged(sw) == [ANNA]


async def test_silent_while_a_staggered_notice_waits_or_a_job_is_awaited(sw, no_loop, monkeypatch):
    team(sw, ANNA)
    set_goal(sw)
    handle = asyncio.get_running_loop().call_later(3600, lambda: None)
    sw._timers.add(handle)
    quiet_after(sw, 10 ** 4)
    assert nudged(sw) == []
    sw._timers.clear()
    handle.cancel()
    with monkeypatch.context() as m:
        m.setattr(sw.jobs, "awaited", lambda owner: [SimpleNamespace(id=4, label="j4")])
        quiet_after(sw, 10 ** 4)
        assert nudged(sw) == []
    quiet_after(sw, WAIT)
    assert nudged(sw) == [ANNA]


def test_silent_without_an_active_goal_and_while_the_asker_waits_for_the_user(sw, no_loop, monkeypatch):
    team(sw, ANNA)
    quiet_after(sw, 10 ** 4)
    assert nudged(sw) == []  # no goal
    set_goal(sw, status="done")
    quiet_after(sw, 10 ** 4)
    assert nudged(sw) == []
    set_goal(sw)
    drain(sw)
    with monkeypatch.context() as m:
        m.setattr(sw, "_clarify_asker", lambda: ANNA)
        m.setattr(sw, "asks_user", lambda name, task=None: name == ANNA)
        quiet_after(sw, 10 ** 4)
        assert nudged(sw) == [] and sw._thread_quiet is None
    quiet_after(sw, WAIT)
    assert nudged(sw) == [ANNA]


def test_the_clarify_asker_gets_its_hint_and_is_nudged_first(make_swarm, no_loop):
    sw = make_swarm(clarify=True, agents={"planning_hint_steps": 2, "planning_hint_minutes": 0})
    team(sw, ANNA, BOB)
    set_goal(sw)
    assert sw._clarify_asker() == ANNA and sw._in_clarify_phase(ANNA)
    drain(sw)
    runtime = sw.runtimes[ANNA]
    sw.hint_planner(ANNA)  # the watch starts
    runtime.llm_steps = 2
    sw.hint_planner(ANNA)
    assert [r["kind"] for r in rows(sw, recipient=ANNA) if "hint" in r["kind"]] == ["n_clarify_hint_thread"]
    drain(sw)
    now = time.time()
    sw.db.execute("UPDATE agents SET last_active = ? WHERE name = ?", (now - 10 * WAIT, ANNA))
    sw.db.execute("UPDATE agents SET last_active = ? WHERE name = ?", (now - 90 * WAIT, BOB))
    quiet_after(sw, WAIT)
    assert nudged(sw) == [ANNA]  # the asker, though Bob was idle longer
    assert not rows(sw, "n_planning_hint")


async def test_a_question_pause_stops_the_loop_and_the_resume_drops_the_quiet_clock(make_swarm, monkeypatch):
    monkeypatch.setattr(swarm_module, "SCHEDULER_SECONDS", 0.01)
    sw = make_swarm(agents={"idle_nudge_seconds": 0})
    await sw.start(run_agents=False)
    try:
        team(sw, ANNA)
        set_goal(sw)
        drain(sw)
        sw._thread_quiet_woken = ("old", 1, True)
        sw.db.set("paused", {"paused": True, "reason": "question", "asker": ANNA, "message_id": 1,
                             "since": time.time()})
        await asyncio.sleep(0.15)
        assert nudged(sw) == []
        sw.resume(USER)  # the question is dropped: the asker is told
        assert sw._thread_quiet is None and sw._thread_quiet_woken is None
        drain(sw)
        for _ in range(100):
            if nudged(sw):
                break
            await asyncio.sleep(0.01)
        assert nudged(sw) == [ANNA] and told(sw) == 0
    finally:
        await sw.stop()


# ============================================================================== T1: digest batching
def test_other_quiet_notices_never_start_a_batch(sw, no_loop):
    woken = team(sw, ANNA, BOB)
    set_goal(sw)
    sw.notify(ANNA, "n_wrap_up_end", wake=False)
    sw.notify(ANNA, "n_network_on", wake=False)
    age(sw, 10 ** 4)
    sw._deliver_digests()
    assert woken[ANNA] == []
    sw.forum.create(BOB, "Lexer", "")
    age(sw, WAIT + 1)
    sw._deliver_digests()
    assert woken[ANNA] == [1] and sw.has_pending(ANNA)
    notices = [r for r in rows(sw, recipient=ANNA) if r["delivered_at"] is None]
    assert {r["kind"]: QUIET_KEY in r["data"] for r in notices} == {
        "n_wrap_up_end": True, "n_network_on": True, "f_post": False}
    assert "From what you follow" in sw.render_wake(ANNA)


async def test_a_start_moves_the_floor_and_quiets_a_released_digest_again(make_swarm, no_loop):
    sw = make_swarm()
    team(sw, ANNA, BOB)
    set_goal(sw)
    sw.forum.create(BOB, "Lexer", "")
    sw.forum.create(BOB, "Parser", "")
    sw.db.execute("UPDATE notifications SET ts = ts - 10000")
    sw._release_digest(ANNA)  # released before the stop, never read
    assert sw.has_pending(ANNA)
    sw.runtimes.clear()
    before = time.time()
    await sw.start(run_agents=False)
    try:
        assert sw._digest_floor >= before and sw._thread_quiet is None and sw._thread_quiet_woken is None
        assert not sw.has_pending(ANNA) and not rows(sw, "n_restart")
        assert all(r["data"].get(DIGEST_KEY) and r["data"].get(QUIET_KEY)
                   for r in rows(sw, recipient=ANNA) if r["delivered_at"] is None)
        assert sw._due_digests(time.time()) == []
        deadline = sw.digest_deadline(ANNA)
        assert deadline is not None and deadline >= before + WAIT
    finally:
        await sw.stop()


async def test_a_reopen_wakes_each_member_once_with_its_notice_and_its_old_digest(make_swarm, no_loop):
    sw = make_swarm(agents={"stagger_seconds": 0.05})
    await sw.start(run_agents=False)
    try:
        team(sw, ANNA, BOB, CARL)
        set_goal(sw)
        sw.complete_goal_by_user("Done.")
        for name in (ANNA, BOB, CARL):
            sw.forum.create(name, f"Notes of {name}", "")  # digest rows for the others, while the goal is done
        texts: dict[str, list[str]] = {name: [] for name in (ANNA, BOB, CARL)}
        order: list[str] = []
        for name in (ANNA, BOB, CARL):
            def read(_name=name) -> None:
                order.append(_name)
                texts[_name].append(sw.render_wake(_name))
            sleeper(sw, name, read)
        age(sw, 10 ** 4)
        sw._deliver_digests()
        assert order == []  # a done goal: no batch
        goal = sw.reopen_goal(USER, "More tests")
        sw._deliver_digests()  # staggered notices to come: no batch either
        await asyncio.sleep(0.3)
        assert not sw._timers
        sw._deliver_digests()
        assert order == sw._llm_order(goal["updated_at"]) and sorted(order) == sorted(texts)
        for name in (ANNA, BOB, CARL):
            (text,) = texts[name]
            assert "The user reopened the global goal" in text and "From what you follow" in text
            assert text.count("posted #") == 2
    finally:
        await sw.stop()


# ============================================================================== wait's unwatched clause
async def test_wait_gives_the_unwatched_text_to_a_lone_member_with_no_trigger(sw, no_loop):
    team(sw, ANNA)
    set_goal(sw)
    assert await wait_text(sw, ANNA) == WAIT_UNWATCHED
    sw._create_member(USER, "Generalist", name=BOB, kind=AgentKind.LLM)
    sleeper(sw, BOB)
    drain(sw, BOB)
    sw.forum.create(ANNA, "Plan", "")  # Bob's digest row: a batch is about to wake him
    assert await wait_text(sw, ANNA) == PLAIN
    sw.render_wake(BOB)
    assert await wait_text(sw, ANNA) == WAIT_UNWATCHED
    set_goal(sw, status="done")
    assert await wait_text(sw, ANNA) == PLAIN
    set_goal(sw)
    assert await wait_text(sw, ANNA) == WAIT_UNWATCHED
    sw.triggers.create(ANNA, 'file_exists("out/result.json")')
    assert await wait_text(sw, ANNA) == PLAIN  # a trigger of its own


# ============================================================================== jobs, wrap-up and money
def test_a_thread_session_works_on_the_active_goal(sw, no_loop):
    team(sw, ANNA)
    set_goal(sw)
    now = time.time()
    detached = SimpleNamespace(id=1, owner=ANNA, origin="detached", started_at=now, label="j1")
    background = SimpleNamespace(id=2, owner=ANNA, origin="background", started_at=now, label="j2")
    sw.jobs._jobs.update({1: detached, 2: background})
    try:
        assert sw.jobs.wakes(detached) and sw.jobs.busy(ANNA)
        assert [j.label for j in sw.jobs.awaited(ANNA)] == ["j1", "j2"]
        set_goal(sw, status="done")
        assert not sw.jobs.wakes(detached) and not sw.jobs.wakes(background) and not sw.jobs.busy(ANNA)
    finally:
        sw.jobs._jobs.clear()


def test_a_board_session_reads_jobs_as_before(make_swarm, no_loop):
    sw = make_swarm(mode="board")
    sw._create_member(USER, "Generalist", name=ANNA, kind=AgentKind.LLM)
    sw.db.set("goal", {"title": "G", "description": "", "status": "active", "summary": "", "set_by": USER,
                       "updated_at": time.time()})
    detached = SimpleNamespace(id=1, owner=ANNA, origin="detached", started_at=time.time(), label="j1")
    sw.jobs._jobs[1] = detached
    try:
        assert not sw.jobs.wakes(detached) and not sw.jobs.busy(ANNA)  # no task in progress
    finally:
        sw.jobs._jobs.clear()


def test_shell_outcomes_render_with_no_task_in_a_thread_session_and_as_before_on_the_board():
    cfg = SimpleNamespace(max_timeout=600, job_max_minutes=480)
    job = SimpleNamespace(id=3, log_shown=".scratch/anna/jobs/j3.log")
    board = ShellOutcome("detached", 0, "out", "bash", 1.0, job=job, names=["python"]).render(cfg=cfg)
    thread = ShellOutcome("detached", 0, "out", "bash", 1.0, job=job, names=["python"], mode="thread").render(cfg=cfg)
    footer = FOOTERS["detached"].format(id=3, log=job.log_shown, max=fmt_duration(600, "en"),
                                        job_max=fmt_duration(480 * 60, "en"), names="python", wait="wait")
    assert board == "exit code: 0 [bash]; processes it started keep running as background job j3\nout\n\n" + footer
    assert "task" not in thread and "(at once while the goal is active, else at your next wake-up)" in thread
    assert FOOTERS_THREAD["detached"] != FOOTERS["detached"]
    board = ShellOutcome("timeout", None, "out", "bash", 5.0, note="wrap_up").render(cfg=cfg)
    thread = ShellOutcome("timeout", None, "out", "bash", 5.0, note="wrap_up", mode="thread").render(cfg=cfg)
    assert board.endswith(f"\n\n[{NOTES['wrap_up']}]") and "task" in board
    assert thread.endswith(f"\n\n[{NOTES_THREAD['wrap_up']}]") and "task" not in thread
    started = ShellOutcome("started", None, "", "bash", 2.0, job=job)
    assert dataclasses.replace(started, mode="thread").render(cfg=cfg) == started.render(cfg=cfg)  # shared footer


async def test_a_background_command_in_wrap_up_mode_is_refused_with_the_thread_text(sw, no_loop):
    team(sw, ANNA)
    set_goal(sw)
    sw.wrap_up(USER)
    with pytest.raises(ToolError) as refused:
        await sw.jobs.run(ANNA, "echo hi", timeout=5, env={}, max_output=1000, background=True)
    assert str(refused.value) == WRAP_UP_ERROR_THREAD and "task" not in WRAP_UP_ERROR_THREAD
    assert rows(sw, "n_wrap_up_thread") and not rows(sw, "n_wrap_up")


def test_the_money_notice_wakes_only_working_members_or_those_with_waking_news(make_swarm, no_loop):
    sw = make_swarm(max_cost_usd=10)
    team(sw, ANNA, BOB, CARL)
    set_goal(sw)
    sw.db.execute("UPDATE agents SET status = 'working' WHERE name = ?", (ANNA,))
    sw.notify(BOB, "n_network_on")  # waking news
    sw.forum.create(ANNA, "Plan", "")  # digest rows only for Carl (and Bob)
    sw.db.execute("UPDATE agents SET cost_usd = 8.5 WHERE name = ?", (ANNA,))
    sw._budget_low()
    sent = {r["recipient"]: QUIET_KEY not in r["data"] for r in rows(sw, "n_budget_low_thread")}
    assert sent == {ANNA: True, BOB: True, CARL: False}
    assert not rows(sw, "n_budget_low") and len(rows(sw, "u_budget_low")) == 1
    assert "vote on #1" in rows(sw, "n_budget_low_thread")[0]["text"]
