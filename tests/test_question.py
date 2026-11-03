"""Clarify mode and a member's question that pauses the swarm (docs/SPEC.md §3 "Clarify mode", issue #32): the
clarify planning card and its kickoff, send_message(pause_swarm=true), the answer rule, every way out of a question
pause (the answer, a manual resume, the budget, clarify switched off, the asker leaving, a restart), background jobs
during the pause, the auto-reply lease and the snapshot fields. Real Swarms on ScriptedLLM only."""

from __future__ import annotations

import asyncio
import dataclasses
import re
import time
import tomllib
from pathlib import Path
from typing import Any

import pytest

from dreamteam import prompts
from dreamteam import swarm as swarm_module
from dreamteam.board import Board
from dreamteam.config import Config, render_template
from dreamteam.db import loads
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.models import USER, AgentStatus, Status
from dreamteam.shell import truncate_middle
from dreamteam.swarm import AUTO_REPLY_GRACE, QUESTION, QUESTION_CHARS, QUESTION_PREVIEW_CHARS, Swarm
from dreamteam.tools import ToolContext, call_tool
from dreamteam.tools.comm_tools import ANSWER_NOTES, PAUSE_NOTES

from test_job_tools import agent_ctx, fast, job_row, kids, py_cmd, until  # noqa: F401 - `fast`, `kids`: fixtures
from test_job_tools import BORIS as JOB_BORIS
from test_job_tools import ok as ctx_ok
from test_job_tools import team as job_team
from test_runtime import all_text, call, kinds_for, make_swarm, run_until, shutdown

ANNA, BORIS, CARL, EXT = "Anna Smith", "Boris Petrov", "Carl Jones", "Claude Code"
QUESTION_TEXT = "1. SQLite or Postgres? (default: SQLite)"


# ------------------------------------------------------------------------------------------ helpers
def make_config(tmp_path: Path, **overrides: Any) -> Config:
    raw: dict[str, Any] = {
        "swarm": {"name": "Question swarm", "language": "en", "start_agents": 0, "max_agents": 6, "max_cost_usd": 0,
                  "clarify": True},
        "agents": {"stagger_seconds": 0, "max_steps_per_turn": 10},
        "workspace": {"git": False},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
    }
    for section, values in overrides.items():
        raw.setdefault(section, {}).update(values)
    return Config.from_dict(tmp_path / "project", raw)


class FakeRuntime:
    """An LLM agent's context without a ReAct loop (the registry's schema: revision 4)."""

    def note_sent_to_user(self) -> None:
        pass

    def end_turn(self) -> None:
        pass


async def ok(sw: Swarm, actor: str, tool: str, args: Any = None, runtime: Any = None) -> str:
    text, is_error = await call_tool(ToolContext(sw, actor, runtime), tool, args)
    assert not is_error, text
    return text


async def make_team(tmp_path: Path, **overrides: Any) -> Swarm:
    """Clarify on, no agent loop: Anna, Boris and Carl (AI agents) and Claude Code (an external member)."""
    sw = Swarm(config=make_config(tmp_path, **overrides),
               llm_factory=lambda profile, agent: ScriptedLLM(lambda *a: "ok", profile))
    await sw.start(run_agents=False)
    for name in (ANNA, BORIS, CARL):
        await sw.hire(USER, "developer", name=name)
    await ok(sw, USER, "join_swarm", {"role": "Reviewer", "name": EXT})
    sw.db.execute("UPDATE notifications SET delivered_at = ?", (time.time(),))  # their n_hired was read: idle
    return sw


@pytest.fixture
async def team(tmp_path):
    sw = await make_team(tmp_path)
    try:
        yield sw
    finally:
        await sw.stop()
        sw.close()


def sent_id(result: str) -> int:
    match = re.match(r"Sent message #(\d+) to ", result)
    assert match, result
    return int(match.group(1))


async def ask(sw: Swarm, asker: str = ANNA, text: str = QUESTION_TEXT, external: bool = False) -> tuple[str, int]:
    """`asker`'s send_message to the user with pause_swarm: its result and the message id."""
    result = await ok(sw, asker, "send_message", {"to": ["user"], "text": text, "pause_swarm": True},
                      runtime=None if external else FakeRuntime())
    return result, sent_id(result)


async def answer(sw: Swarm, to: Any = (ANNA,), text: str = "Postgres.") -> tuple[str, int]:
    result = await ok(sw, USER, "send_message", {"to": list(to), "text": text})
    return result, sent_id(result)


def notes(sw: Swarm, recipient: str) -> list[tuple[str, dict]]:
    return [(r["kind"], loads(r["data"], {})) for r in sw.db.all(
        "SELECT kind, data FROM notifications WHERE recipient = ? ORDER BY id", (recipient,))]


def last_event(sw: Swarm, kind: str) -> dict:
    return sw.bus.recent(types=(kind,))[-1]


def clarify_off(config: Config) -> Config:
    return dataclasses.replace(config, swarm=dataclasses.replace(config.swarm, clarify=False))


# ------------------------------------------------------------------------------------- the planning card
async def test_set_goal_picks_the_clarify_card(tmp_path, make_fake_swarm):
    for lang in ("en", "ru"):
        for clarify, lease, expected in ((True, False, True), (False, False, False), (True, True, False)):
            sw = Swarm(config=make_config(tmp_path / f"{lang}-{clarify}-{lease}",
                                          swarm={"language": lang, "clarify": clarify}),
                       llm_factory=lambda profile, agent: ScriptedLLM(lambda *a: "ok", profile))
            await sw.start(run_agents=False)
            try:
                if lease:  # a `dreamteam -p` client with automatic replies: the session plans as usual
                    sw.auto_reply_seen()
                sw.set_goal(USER, "Ship the tool", "with tests")
                card = sw.board.get(1)
                title, goal, description = prompts.planning_task(lang, expected)
                assert (card.title, card.goal) == (title, goal), (lang, clarify, lease)
                assert card.description == f"{description}\n\nShip the tool\nwith tests"
                assert card.tags == (["planning", "clarify"] if expected else ["planning"])
                # a later change of the setting never rewrites the card
                sw.config.swarm.clarify = not clarify
                assert sw.board.get(1).title == title
            finally:
                await sw.stop()
                sw.close()
    # the test double plans like Swarm.set_goal
    fake = make_fake_swarm({"swarm": {"clarify": True, "language": "en"}})
    fake.board = Board(fake)
    fake.set_goal(USER, "Ship the tool")
    assert fake.board.get(1).title == prompts.planning_task("en", True)[0]
    assert fake.board.get(1).tags == ["planning", "clarify"]


async def test_clarify_kickoff_wakes_only_the_planner(tmp_path, monkeypatch):
    """One member asks and plans; the others get the kickoff quietly and make no LLM call until the plan's first
    task wakes one of them (n_ready). A planner that never claims hands the card on: the next idle member is offered
    it once the planner is idle without it."""
    monkeypatch.setattr(swarm_module, "SCHEDULER_SECONDS", 0.05)
    names: list[str] = []

    def policy(sw, name, system, messages, tools):
        if name != names[0]:
            return call("wait")
        if sw.board.get(1).assignee is None:
            return call("task_claim", task_id=1)
        if not any(m["sender"] == name for m in sw.mail.conversation(name, USER)):
            return call("send_message", to=["user"], text=QUESTION_TEXT)
        if "Postgres." in all_text(messages) and len(sw.board.list()) == 1:
            return call("task_create", title="Schema", goal="the schema is written")
        return call("wait")

    swarm, rec = make_swarm(tmp_path / "plans", policy, swarm={"start_agents": 3, "clarify": True})
    await swarm.start()
    try:
        names.extend(m["name"] for m in swarm.members())
        planner, others = names[0], names[1:]
        swarm.set_goal(USER, "Ship the tool")
        card = swarm.board.get(1)
        assert card.tags == ["planning", "clarify"]
        kickoff = {n: [d for k, d in notes(swarm, n) if k == "n_kickoff"] for n in names}
        assert len(kickoff[planner]) == 1 and "_quiet" not in kickoff[planner][0]
        assert all(len(kickoff[n]) == 1 and kickoff[n][0]["_quiet"] is True for n in others)
        assert swarm._nudged[1] == {planner}
        await run_until(lambda: bool(swarm.mail.conversation(planner, USER))
                        and swarm.member(planner)["status"] == AgentStatus.IDLE)
        await asyncio.sleep(0.5)  # ten scheduler passes: nobody else is woken or offered the card
        assert [rec.count(n) for n in others] == [0, 0]
        assert swarm.snapshot()["swarm"]["clarify_wait"] == {"name": planner, "task_id": 1}
        swarm.mail.send(USER, [planner], "Postgres.")
        await run_until(lambda: any(rec.count(n) for n in others))
        first = next(n for n in others if rec.count(n))
        prompt = all_text(rec.calls[first][0])
        assert "n_ready" in kinds_for(swarm, first) and "#2 Schema" in prompt
        assert f'Planning task #1 "{card.title}"' in prompt  # the quiet kickoff, read with the first task
        assert rec.violations == []
    finally:
        await shutdown(swarm)

    # the fallback: the planner ends its turn without claiming the card
    names.clear()
    swarm, rec = make_swarm(tmp_path / "fallback", lambda sw, n, s, m, t: call("wait"),
                            swarm={"start_agents": 3, "clarify": True})
    await swarm.start()
    try:
        names.extend(m["name"] for m in swarm.members())
        swarm.set_goal(USER, "Ship the tool")
        await run_until(lambda: "n_ready" in kinds_for(swarm, names[1]), timeout=10)
        text = swarm.db.scalar("SELECT text FROM notifications WHERE recipient = ? AND kind = 'n_ready'", (names[1],))
        assert "#1 " in text and rec.count(names[2]) == 0
    finally:
        await shutdown(swarm)


async def test_a_pause_stops_the_planners_decision_clock(tmp_path, monkeypatch):
    """A pause is no time to decide on an offer: after a clarify kickoff made while paused and a pause longer than
    NUDGE_DECISION_SECONDS, the quiet members are not offered the card while the planner's first call is in flight.
    An offer made before a pause keeps the time it had already spent deciding."""
    monkeypatch.setattr(swarm_module, "SCHEDULER_SECONDS", 0.05)
    monkeypatch.setattr(swarm_module, "NUDGE_DECISION_SECONDS", 1.5)
    names: list[str] = []

    async def slow_claim() -> dict:
        await asyncio.sleep(0.5)  # ten scheduler passes after the resume
        return call("task_claim", task_id=1)

    def policy(sw, name, system, messages, tools):
        if name == names[0] and sw.board.get(1).assignee is None:
            return slow_claim()
        return call("wait")

    swarm, rec = make_swarm(tmp_path, policy, swarm={"start_agents": 3, "clarify": True})
    await swarm.start()
    try:
        names.extend(m["name"] for m in swarm.members())
        swarm.pause(USER, "paused by the user")
        swarm.set_goal(USER, "Ship the tool")  # the kickoff happens during the pause
        await asyncio.sleep(1.8)
        swarm.resume(USER)
        await run_until(lambda: swarm.board.get(1).assignee == names[0])
        await asyncio.sleep(0.2)
        assert [rec.count(n) for n in names[1:]] == [0, 0]
        assert all("n_ready" not in kinds_for(swarm, n) for n in names[1:])
        before = time.time() - 0.4
        swarm._considering[names[1]] = (before, frozenset(), frozenset({1}))
        swarm.pause(USER, "paused by the user")
        paused_at = swarm._paused_at
        await asyncio.sleep(0.3)
        swarm.resume(USER)
        assert swarm._considering[names[1]][0] == pytest.approx(before + time.time() - paused_at, abs=0.05)
    finally:
        await shutdown(swarm)


# ------------------------------------------------------------------------------------- asking and answering
async def test_pause_swarm_pauses_until_the_user_answers(team):
    sw = team
    sw.board.create(USER, "API", goal="the API works", assignee=BORIS)  # Boris has work: the resume wakes him
    question = "Which database?\n" + "x" * 1000
    result, q = await ask(sw, text=question)
    assert result == f"Sent message #{q} to user." + PAUSE_NOTES["asked_llm"]
    slot = sw.db.get("paused")
    assert slot == {"paused": True, "reason": QUESTION, "asker": ANNA, "message_id": q, "since": slot["since"]}
    assert abs(slot["since"] - time.time()) < 60
    assert sw.pending_question() == {"asker": ANNA, "message_id": q, "since": slot["since"]}
    assert sw.pause_text() == f"waiting for the user's answer to {ANNA}'s question #{q}"
    state = sw.snapshot()["swarm"]
    assert state["paused"] and state["pause_reason"] == "question"
    assert state["clarify"] is True and state["auto_reply"] is False
    assert state["question"] == {"asker": ANNA, "message_id": q, "since": slot["since"],
                                 "text": truncate_middle(question, QUESTION_PREVIEW_CHARS)}
    assert len(state["question"]["text"]) < len(question)
    event = last_event(sw, "swarm.paused")
    assert event["actor"] == ANNA
    assert event["data"] == {"reason": "question", "asker": ANNA, "message_id": q, "since": slot["since"]}
    assert ("u_question", {"name": ANNA, "id": q}) in notes(sw, USER)

    before = {name: len(notes(sw, name)) for name in (ANNA, BORIS, CARL, EXT)}
    result, a = await answer(sw)
    assert result == f"Sent message #{a} to {ANNA}." + ANSWER_NOTES["ok"].format(asker=ANNA, id=q)
    assert sw.mail.get(a)["reply_to"] == q  # the asker sees "(reply to #q)"
    assert not sw.paused and sw.pending_question() is None and sw.snapshot()["swarm"]["question"] is None
    event = last_event(sw, "swarm.resumed")
    assert event["actor"] == USER
    assert event["data"] == {"question": {"asker": ANNA, "message_id": q, "outcome": "answered", "answer_id": a}}
    new = {name: notes(sw, name)[count:] for name, count in before.items()}
    told = {"asker": ANNA, "id": q, "question": question, "answer": "Postgres."}
    # Boris (an open task): the question and the answer quietly, then the resume wakes him
    assert new[BORIS] == [("n_user_answer", {**told, "_quiet": True}), ("n_resumed", {})]
    text = sw.db.scalar("SELECT text FROM notifications WHERE recipient = ? AND kind = 'n_user_answer'", (BORIS,))
    assert "Question: Which database?" in text and "Answer: Postgres." in text
    assert new[CARL] == [("n_user_answer", {**told, "_quiet": True})]  # idle, no work: read at its next wake-up
    assert new[EXT] == [("n_user_answer", told)]  # not paused: woken (wait_for_events returns)
    assert new[ANNA] == [("n_resumed", {})]  # it has the answer itself, as a message
    assert [m["id"] for m in sw.mail.unread(ANNA, mark_read=False)] == [a]


async def test_pause_swarm_is_ordinary_otherwise(team):
    sw = team

    def note(result: str, key: str, **info: Any) -> None:
        mid = sent_id(result)
        assert sw.mail.get(mid)["text"] == QUESTION_TEXT  # stored, whatever the note says
        to = ", ".join(sw.mail.get(mid)["recipients"])
        assert result == f"Sent message #{mid} to {to}." + PAUSE_NOTES[key].format(**info), (key, result)

    # without pause_swarm (or with false): byte for byte as before
    for extra in ({}, {"pause_swarm": False}):
        result = await ok(sw, ANNA, "send_message", {"to": ["user"], "text": QUESTION_TEXT, **extra},
                          runtime=FakeRuntime())
        assert result == f"Sent message #{sent_id(result)} to user."
    sw.config.swarm.clarify = False
    note((await ask(sw))[0], "off")
    sw.config.swarm.clarify = True
    note(await ok(sw, ANNA, "send_message", {"to": [BORIS], "text": QUESTION_TEXT, "pause_swarm": True},
                  runtime=FakeRuntime()), "no_user")
    note(await ok(sw, USER, "send_message", {"to": [ANNA], "text": QUESTION_TEXT, "pause_swarm": True}), "user")
    sw.auto_reply_seen()  # a `dreamteam -p` client answers questions automatically
    note((await ask(sw))[0], "auto")
    sw._auto_reply_until = 0.0
    assert not sw.paused
    # one question at a time
    result, q = await ask(sw)
    assert result.endswith(PAUSE_NOTES["asked_llm"])
    note((await ask(sw, BORIS))[0], "pending", asker=ANNA, id=q)
    note((await ask(sw))[0], "own", id=q)
    assert sw.pending_question()["asker"] == ANNA  # the banner names the first asker
    sw.resume(USER)
    # already paused by the user: the pause and its reason stay
    sw.pause(USER, "paused by the user")
    note((await ask(sw))[0], "paused", reason="paused by the user")
    assert sw.pause_reason == "paused by the user" and sw.pending_question() is None
    sw.resume(USER)
    # the budget is spent: the asker's next step pauses for that
    sw.set_limits(USER, max_cost_usd=1)
    sw.db.execute("UPDATE agents SET cost_usd = 2 WHERE name = ?", (ANNA,))
    note((await ask(sw))[0], "budget")
    assert not sw.paused and sw.pending_question() is None


async def test_only_a_message_to_the_asker_answers(team):
    sw = team
    _, q = await ask(sw)
    result, mid = await answer(sw, to=[BORIS], text="Boris, hold on.")
    assert result == f"Sent message #{mid} to {BORIS}."  # no answer note
    assert sw.pending_question()["message_id"] == q and sw.mail.get(mid)["reply_to"] is None
    before = {name: len(notes(sw, name)) for name in (BORIS, CARL, EXT)}
    result, a = await answer(sw, to=["all"], text="Postgres, everyone.")
    assert result.endswith(ANSWER_NOTES["ok"].format(asker=ANNA, id=q)) and not sw.paused
    assert sw.mail.get(a)["reply_to"] == q
    seen = {"asker": ANNA, "id": q, "question": QUESTION_TEXT, "answer_id": a}  # no second copy of the answer
    assert notes(sw, BORIS)[before[BORIS]][0] == "n_user_answer_seen"
    assert notes(sw, BORIS)[before[BORIS]][1] == {**seen, "_quiet": True}
    assert notes(sw, CARL)[before[CARL]][1] == {**seen, "_quiet": True}
    assert notes(sw, EXT)[before[EXT]:] == [("n_user_answer_seen", seen)]
    assert last_event(sw, "swarm.resumed")["data"]["question"]["answer_id"] == a


async def test_manual_resume_drops_the_question(team):
    sw = team
    sw.board.create(USER, "Schema", goal="the schema is written", assignee=ANNA)  # she would get n_resumed
    _, q = await ask(sw)
    assert await ok(sw, USER, "swarm_control", {"action": "pause"}) == \
        f"Done: already paused (waiting for the user's answer to {ANNA}'s question #{q})."
    assert sw.pending_question()["message_id"] == q
    before = len(notes(sw, ANNA))
    assert await ok(sw, USER, "swarm_control", {"action": "resume"}) == "Done: resumed."
    assert not sw.paused and sw.pending_question() is None
    assert last_event(sw, "swarm.resumed")["data"] == {
        "question": {"asker": ANNA, "message_id": q, "outcome": "dropped", "why": "resumed"}}
    assert notes(sw, ANNA)[before:] == [("n_question_unanswered", {"id": q})]  # a wake; no n_resumed
    # a later message to her is ordinary
    result, mid = await answer(sw)
    assert result == f"Sent message #{mid} to {ANNA}." and sw.mail.get(mid)["reply_to"] is None
    resumed = len(sw.bus.recent(types=("swarm.resumed",)))
    assert await ok(sw, USER, "swarm_control", {"action": "resume"}) == "Done: resumed."  # a no-op
    assert len(sw.bus.recent(types=("swarm.resumed",))) == resumed
    assert await ok(sw, USER, "swarm_control", {"action": "pause"}) == "Done: paused."


async def test_a_dropped_question_waits_for_nothing(team):
    """The clarify planner asked with pause_swarm and the user went on without an answer: it was told to take its
    defaults (n_question_unanswered), so the nag guard and the board's clarify_wait are off at once, not when the
    user next writes to it. A new question guards it again; a question dropped by a budget pause in its place counts
    the same."""
    sw = team
    sw.set_goal(USER, "Ship the tool")
    await ok(sw, ANNA, "task_claim", {"task_id": 1})
    _, q = await ask(sw)
    assert sw.asks_user(ANNA, sw.board.get(1))
    await ok(sw, USER, "swarm_control", {"action": "resume"})
    assert ("n_question_unanswered", {"id": q}) in notes(sw, ANNA)
    assert not sw.asks_user(ANNA, sw.board.get(1)) and sw.snapshot()["swarm"]["clarify_wait"] is None
    await ok(sw, ANNA, "send_message", {"to": ["user"], "text": "2. And the colour?"}, runtime=FakeRuntime())
    assert sw.snapshot()["swarm"]["clarify_wait"] == {"name": ANNA, "task_id": 1}
    await answer(sw, text="Blue.")
    # clarify switched off with the budget spent: the question ends in a budget pause
    _, q = await ask(sw)
    sw.set_limits(USER, max_cost_usd=1)
    sw.db.execute("UPDATE agents SET cost_usd = 2 WHERE name = ?", (ANNA,))
    sw.reconfigure(clarify_off(sw.config))
    assert sw.pause_reason == "budget"
    assert last_event(sw, "swarm.paused")["data"]["question"] == {"asker": ANNA, "message_id": q,
                                                                  "outcome": "dropped", "why": "clarify_off"}
    sw.config.swarm.clarify = True
    assert not sw.asks_user(ANNA, sw.board.get(1)) and sw.snapshot()["swarm"]["clarify_wait"] is None


async def test_a_spent_budget_turns_the_answer_into_a_budget_pause(team):
    sw = team
    sw.set_limits(USER, max_cost_usd=1)
    sw.board.create(USER, "API", goal="the API works", assignee=BORIS)
    _, q = await ask(sw)
    sw.db.execute("UPDATE agents SET cost_usd = 2 WHERE name = ?", (ANNA,))  # steps in flight crossed the limit
    result, a = await answer(sw)
    assert result == f"Sent message #{a} to {ANNA}." + ANSWER_NOTES["budget"].format(asker=ANNA, id=q)
    assert sw.paused and sw.pause_reason == "budget" and sw.pending_question() is None
    assert sw.db.get("paused") == {"paused": True, "reason": "budget"}
    event = last_event(sw, "swarm.paused")
    assert event["actor"] == USER and event["data"] == {
        "reason": "budget", "question": {"asker": ANNA, "message_id": q, "outcome": "answered", "answer_id": a}}
    assert "u_budget" in kinds_for(sw, USER)
    assert "n_user_answer" in kinds_for(sw, BORIS) and "n_resumed" not in kinds_for(sw, BORIS)
    assert await ok(sw, USER, "swarm_control", {"max_cost_usd": 10}) == "Done: limits: $10.00, at most 6 AI agents."
    assert not sw.paused and kinds_for(sw, BORIS)[-1] == "n_resumed"
    assert last_event(sw, "swarm.resumed")["data"] == {}
    # spent at ask time: an ordinary message, no question
    sw.set_limits(USER, max_cost_usd=1)
    result, _ = await ask(sw, BORIS)
    assert result.endswith(PAUSE_NOTES["budget"]) and not sw.paused and sw.pending_question() is None


async def test_the_answer_hook_never_fails_the_users_message(team, monkeypatch, caplog):
    sw = team
    _, q = await ask(sw)
    real_notify = sw.notify

    def notify(recipient: str, key: str, **kw: Any) -> None:
        if key.startswith("n_user_answer"):
            raise RuntimeError("disk full")
        real_notify(recipient, key, **kw)

    monkeypatch.setattr(sw, "notify", notify)
    result, _ = await answer(sw)
    assert result.endswith(ANSWER_NOTES["ok"].format(asker=ANNA, id=q)) and not sw.paused
    assert "could not tell the members the answer" in caplog.text
    # the resume fails too: the message is sent, and the result does not claim an answer
    _, q = await ask(sw)

    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("broken")

    monkeypatch.setattr(sw, "resume", broken)
    result, a = await answer(sw)
    assert result == f"Sent message #{a} to {ANNA}." and sw.pending_question()["message_id"] == q
    assert "could not lift the question pause" in caplog.text


# ------------------------------------------------------------------------------------- the other ways out
async def test_clarify_off_lifts_the_question_and_wakes_a_waiting_planner(team, tmp_path):
    sw = team
    sw.set_goal(USER, "Ship the tool")
    await ok(sw, BORIS, "task_claim", {"task_id": 1})
    await ok(sw, BORIS, "task_update", {"task_id": 1, "status": "waiting"})
    _, q = await ask(sw)
    sw.reconfigure(clarify_off(sw.config))
    assert not sw.paused and sw.pending_question() is None
    assert last_event(sw, "swarm.resumed")["data"] == {
        "question": {"asker": ANNA, "message_id": q, "outcome": "dropped", "why": "clarify_off"}}
    assert ("n_clarify_off", {"task_id": 1}) in notes(sw, BORIS)  # a wake: it plans now
    assert ("n_question_unanswered", {"id": q}) in notes(sw, ANNA)
    assert sw.board.get(1).tags == ["planning", "clarify"]  # never rewritten

    # switched off while the session was stopped: start() lifts the question and tells the planner once
    other = await make_team(tmp_path / "stopped")
    try:
        other.set_goal(USER, "Ship the tool")
        await ok(other, BORIS, "task_claim", {"task_id": 1})
        await ok(other, BORIS, "task_update", {"task_id": 1, "status": "waiting"})
        _, q = await ask(other)
        await other.stop()
        other.reconfigure(clarify_off(other.config))  # not running: nothing is applied now
        assert other.pending_question()["message_id"] == q and "n_clarify_off" not in kinds_for(other, BORIS)
        await other.start(run_agents=False)
        assert not other.paused
        assert last_event(other, "swarm.resumed")["data"]["question"]["why"] == "clarify_off"
        assert kinds_for(other, BORIS).count("n_clarify_off") == 1
        await other.stop()
        await other.start(run_agents=False)
        assert kinds_for(other, BORIS).count("n_clarify_off") == 1  # once per card
    finally:
        await other.stop()
        other.close()


async def test_clarify_off_leaves_a_planner_that_plans_already(team, tmp_path):
    """A clarify planner that created a task plans already: its `waiting` card waits for something else (here the
    research it handed out), so switching clarify off tells it nothing, live or at the next start."""
    sw = team
    sw.set_goal(USER, "Ship the tool")
    await ok(sw, BORIS, "task_claim", {"task_id": 1})
    await ok(sw, BORIS, "task_create", {"title": "Research the format", "goal": "the format is known"})
    await ok(sw, BORIS, "task_update", {"task_id": 1, "status": "waiting", "comment": "waiting for #2"})
    sw.reconfigure(clarify_off(sw.config))
    assert "n_clarify_off" not in kinds_for(sw, BORIS)

    # it asked, the user answered, it planned and parked the card; clarify goes off while the session is stopped
    other = await make_team(tmp_path / "stopped")
    try:
        other.set_goal(USER, "Ship the tool")
        await ok(other, ANNA, "task_claim", {"task_id": 1})
        await ok(other, ANNA, "send_message", {"to": ["user"], "text": QUESTION_TEXT}, runtime=FakeRuntime())
        await answer(other)
        await ok(other, ANNA, "task_create", {"title": "Schema", "goal": "the schema is written"})
        await ok(other, ANNA, "task_update", {"task_id": 1, "status": "waiting"})
        await other.stop()
        other.reconfigure(clarify_off(other.config))
        await other.start(run_agents=False)
        assert "n_clarify_off" not in kinds_for(other, ANNA)
    finally:
        await other.stop()
        other.close()


async def test_retiring_the_asker_lifts_its_question(team):
    sw = team
    _, q = await ask(sw)
    await sw.retire(ANNA, ANNA)
    assert not sw.paused and sw.pending_question() is None
    assert last_event(sw, "swarm.resumed")["data"] == {
        "question": {"asker": ANNA, "message_id": q, "outcome": "dropped", "why": "left"}}
    assert ("u_question_left", {"name": ANNA, "id": q}) in notes(sw, USER)
    assert "n_question_unanswered" not in kinds_for(sw, ANNA)  # it is gone
    # the user retires the asker: the user needs no notice
    _, q = await ask(sw, BORIS)
    await sw.retire(USER, BORIS)
    assert not sw.paused and last_event(sw, "swarm.resumed")["data"]["question"]["message_id"] == q
    assert kinds_for(sw, USER).count("u_question_left") == 1


async def test_a_question_pause_survives_a_restart(team):
    sw = team
    sw.set_goal(USER, "Ship the tool")
    sw.board.create(USER, "API", goal="the API works", assignee=BORIS)  # open work: a restart would wake him
    _, q = await ask(sw)
    question = sw.snapshot()["swarm"]["question"]
    await sw.stop()
    await sw.start(run_agents=False)
    assert sw.pending_question()["message_id"] == q and sw.snapshot()["swarm"]["question"] == question
    assert all("n_restart" not in kinds_for(sw, n) for n in (ANNA, BORIS, CARL))
    before = len(notes(sw, BORIS))
    await answer(sw)
    assert not sw.paused
    assert [k for k, _ in notes(sw, BORIS)[before:]] == ["n_user_answer", "n_resumed"]


async def test_agents_mid_activation_stop_and_resume_with_the_answer(tmp_path, monkeypatch):
    """End to end: Anna asks with pause_swarm and ends her turn; Boris, busy in a loop of steps, stops at his next
    step and makes no LLM call while paused; the user's answer resumes him, and his next prompt holds the question
    and the answer."""
    monkeypatch.setattr(swarm_module, "SCHEDULER_SECONDS", 0.05)
    names: list[str] = []

    async def slow_board() -> dict:
        await asyncio.sleep(0.02)
        return call("board")

    def policy(sw, name, system, messages, tools):
        text = all_text(messages)
        if name == names[0]:
            if "Postgres." in text:
                return call("wait")
            return {"tool_calls": [{"name": "send_message", "arguments": {"to": ["user"], "text": QUESTION_TEXT,
                                                                          "pause_swarm": True}},
                                   {"name": "wait", "arguments": {}}]}
        return call("wait") if "Answer: Postgres." in text else slow_board()

    swarm, rec = make_swarm(tmp_path, policy, swarm={"start_agents": 2, "clarify": True})
    await swarm.start()
    try:
        names.extend(m["name"] for m in swarm.members())
        anna, boris = names
        swarm.notify(boris, "n_restart")
        await run_until(lambda: rec.count(boris) >= 3)
        swarm.notify(anna, "n_restart")
        await run_until(lambda: swarm.paused)
        q = swarm.pending_question()
        assert q["asker"] == anna
        await asyncio.sleep(0.2)  # the call in flight and the tools of its response finish
        stopped = rec.count(boris)
        await asyncio.sleep(0.5)
        assert rec.count(boris) == stopped  # no token spent while paused
        swarm.mail.send(USER, [anna], "Postgres.")
        await run_until(lambda: rec.count(boris) > stopped and rec.count(anna) >= 2)
        first = all_text(rec.calls[boris][stopped])
        assert f"Question: {QUESTION_TEXT}" in first and "Answer: Postgres." in first
        await run_until(lambda: all(swarm.member(n)["status"] == AgentStatus.IDLE for n in names))
        assert not swarm.paused
        # Anna read her result note, and the answer as a reply to her question
        after = all_text(rec.calls[anna][1])
        assert PAUSE_NOTES["asked_llm"].strip() in after and f"(reply to #{q['message_id']})" in after
        assert rec.violations == []
    finally:
        await shutdown(swarm)


async def test_an_external_member_may_pause(team):
    sw = team
    result, q = await ask(sw, EXT, text="Deploy where?", external=True)
    assert result == f"Sent message #{q} to user." + PAUSE_NOTES["asked_ext"] and "wait_for_events" in result
    assert sw.paused and sw.pending_question()["asker"] == EXT
    _, a = await answer(sw, to=[EXT], text="Fly.io")
    assert not sw.paused
    assert [(m["id"], m["reply_to"]) for m in sw.mail.unread(EXT, mark_read=False)] == [(a, q)]
    assert "n_user_answer" in kinds_for(sw, ANNA) and "n_user_answer" not in kinds_for(sw, EXT)
    assert "Fly.io" in await ok(sw, EXT, "wait_for_events", {"timeout": 0})


async def test_an_automatic_message_never_answers(team):
    """A user-owned trigger's message to the asker is no answer (Mailbox.send automatic=True)."""
    sw = team
    _, q = await ask(sw)
    message = sw.mail.send(USER, [ANNA], "x", automatic=True)
    assert message["reply_to"] is None and sw.pending_question()["message_id"] == q
    assert sw.triggers._run_action(USER, {"type": "message", "to": [ANNA], "text": "ping"}) is None
    assert sw.mail.history(ANNA)[-1]["text"] == "ping" and sw.pending_question()["message_id"] == q
    await answer(sw)  # the user in person
    assert not sw.paused


async def test_background_jobs_run_on_through_a_question_pause(tmp_path, fast, kids):
    """Background jobs keep running while a question holds the swarm (issue #32): a job that ends on its own during
    the pause is recorded as ended, and its owner's n_job_done waits for the resume; the answer then resumes the
    swarm. (That a pause leaves a job's processes alive: test_tui_app's background jobs test, with /pause.)"""
    swarm, _ = await job_team(tmp_path, swarm={"clarify": True})
    try:
        anna, boris = agent_ctx(swarm, ANNA), agent_ctx(swarm, JOB_BORIS)
        await ctx_ok(anna, "shell", {"command": py_cmd(kids.script, "1", kids.pidfile()), "background": True})
        q = sent_id(await ctx_ok(boris, "send_message", {"to": ["user"], "text": QUESTION_TEXT, "pause_swarm": True}))
        assert swarm.pending_question()["message_id"] == q and job_row(swarm, 1)["status"] == "running"
        await until(lambda: job_row(swarm, 1)["status"] == "ended")
        assert swarm.pending_question()["message_id"] == q and job_row(swarm, 1)["exit_code"] == 0
        assert swarm.db.scalar("SELECT COUNT(*) FROM notifications WHERE recipient = ? AND kind = 'n_job_done'"
                               " AND delivered_at IS NULL", (ANNA,)) == 1
        await answer(swarm, to=[JOB_BORIS])
        assert not swarm.paused and kinds_for(swarm, ANNA)[-1] == "n_resumed"
    finally:
        await shutdown(swarm)


# ------------------------------------------------------------------------------------- lease, settings, texts
async def test_the_auto_reply_lease(team, monkeypatch):
    sw = team
    now = [1000.0]
    monkeypatch.setattr(swarm_module, "_monotonic", lambda: now[0])
    assert AUTO_REPLY_GRACE == 90.0 and not sw.auto_reply_active and sw.clarifying
    sw.auto_reply_seen()
    assert sw.auto_reply_active and not sw.clarifying
    now[0] += AUTO_REPLY_GRACE - 1
    assert sw.auto_reply_active
    now[0] += 2
    assert not sw.auto_reply_active and sw.clarifying
    with sw.auto_reply_stream():
        now[0] += 10 * AUTO_REPLY_GRACE
        assert sw.auto_reply_active and sw.snapshot()["swarm"]["auto_reply"] is True
        with sw.auto_reply_stream():
            pass
        assert sw.auto_reply_active  # one stream is still open
    now[0] += AUTO_REPLY_GRACE - 1
    assert sw.auto_reply_active  # the grace after the last sign
    now[0] += 2
    assert not sw.auto_reply_active and sw.snapshot()["swarm"]["auto_reply"] is False and sw.clarifying
    sw.config.swarm.clarify = False
    assert not sw.clarifying


def test_the_clarify_setting(tmp_path):
    assert Config.from_dict(tmp_path, {}).swarm.clarify is False
    assert Config.from_dict(tmp_path, {"swarm": {"clarify": True}}).swarm.clarify is True
    with pytest.raises(ValueError, match=r"\[swarm\] clarify must be true or false"):
        Config.from_dict(tmp_path / "bad", {"swarm": {"clarify": "yes"}})
    template = render_template("X", "ru", "anthropic")
    assert re.search(r"^clarify = false {15}# true — ", template, re.M)
    assert tomllib.loads(template)["swarm"]["clarify"] is False


async def test_swarm_status_names_the_question_and_the_mode(team):
    sw = team
    status = await ok(sw, USER, "swarm_status", {"brief": True})
    head = status.splitlines()[0]
    assert "running; CLARIFY MODE: the planning task first asks the user, and a member's question to the user with " \
           "pause_swarm=true pauses the swarm until the user answers" in head
    _, q = await ask(sw)
    head = (await ok(sw, USER, "swarm_status", {})).splitlines()[0]
    assert (f"PAUSED: waiting for the user's answer to {ANNA}'s question #{q} (your next send_message to {ANNA} or "
            "to all is the answer: every member gets it with the question; swarm_control action=resume goes on "
            "without it); CLARIFY MODE") in head
    ext = (await ok(sw, EXT, "swarm_status", {})).splitlines()[0]
    assert f"PAUSED: waiting for the user's answer to {ANNA}'s question #{q}; CLARIFY MODE" in ext
    assert "your next send_message" not in ext
    sw.auto_reply_seen()  # questions get an automatic reply: no clarify mode
    assert "CLARIFY MODE" not in await ok(sw, USER, "swarm_status", {})
    sw._auto_reply_until = 0.0
    sw.resume(USER)
    sw.config.swarm.clarify = False
    assert (await ok(sw, USER, "swarm_status", {})).splitlines()[0].endswith("— running")


async def test_the_snapshot_says_when_the_planner_waits_for_the_user(team):
    sw = team
    sw.set_goal(USER, "Ship the tool")
    assert sw.snapshot()["swarm"]["clarify_wait"] is None
    await ok(sw, ANNA, "task_claim", {"task_id": 1})
    assert sw.snapshot()["swarm"]["clarify_wait"] is None  # it has not asked yet
    await ok(sw, ANNA, "send_message", {"to": ["user"], "text": QUESTION_TEXT}, runtime=FakeRuntime())
    assert sw.snapshot()["swarm"]["clarify_wait"] == {"name": ANNA, "task_id": 1}
    assert sw.asks_user(ANNA, sw.board.get(1))
    await answer(sw, text="CLI")
    assert sw.snapshot()["swarm"]["clarify_wait"] is None
    await ok(sw, ANNA, "task_update", {"task_id": 1, "status": "waiting"})
    await ok(sw, ANNA, "send_message", {"to": ["user"], "text": "2. And the colour?"}, runtime=FakeRuntime())
    assert sw.snapshot()["swarm"]["clarify_wait"] == {"name": ANNA, "task_id": 1}
    sw.config.swarm.clarify = False  # the guard follows the live option
    assert sw.snapshot()["swarm"]["clarify_wait"] is None
    sw.config.swarm.clarify = True
    await ok(sw, ANNA, "task_create", {"title": "Schema", "goal": "the schema is written"})
    assert sw.snapshot()["swarm"]["clarify_wait"] is None  # it plans already
    assert Status.WAITING == sw.board.get(1).status


def test_question_constants():
    assert (QUESTION, QUESTION_CHARS, QUESTION_PREVIEW_CHARS) == ("question", 4000, 600)
