"""Stage 1 of the thread mode, end to end (docs/SPEC.md §22.4-§22.6): the goal and its system posts, the kickoff,
the vote on #1 under both rules through the real tool calls, the decision record and summary, reopen, the user's own
completion, the rechecks after a retire, a rule change and a start, wrap-up, the members' prompts, roles, tool sets
and subscriptions, and a board session that behaves as before. ScriptedLLM only, no network."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from pathlib import Path
from typing import Any

import pytest

from dreamteam import swarm as swarmmod
from dreamteam.agent import AgentRuntime
from dreamteam.config import Config
from dreamteam.forum import GOAL_POST, PLAN_POST
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.models import USER, AgentKind, ToolError
from dreamteam.swarm import Swarm
from dreamteam.tools import ToolContext, call_tool, frozen

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

GOAL = "Build the parser"
DESCRIPTION = "A fast one, with tests."


def make_config(tmp_path: Path, mode: str = "thread", *, agents: dict | None = None, **swarm: Any) -> Config:
    raw: dict[str, Any] = {
        "swarm": {"name": "Thread core", "language": "en", "start_agents": 3, "max_agents": 6, "max_cost_usd": 0,
                  "mode": mode, "clarify": False, **swarm},
        "agents": {"stagger_seconds": 0, "max_steps_per_turn": 12, **(agents or {})},
        "workspace": {"git": False},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
    }
    return Config.from_dict(tmp_path / "project", raw)


def new_swarm(tmp_path: Path, mode: str = "thread", llm=None, **kw: Any) -> Swarm:
    factory = llm or (lambda profile, agent: ScriptedLLM(lambda *a: "ok", profile))
    return Swarm(config=make_config(tmp_path, mode, **kw), llm_factory=factory)


@pytest.fixture
def open_swarms():
    made: list[Swarm] = []
    yield made
    for sw in made:
        try:
            sw.close()
        except Exception:  # noqa: BLE001 - closed already
            pass


async def started(tmp_path: Path, open_swarms: list, mode: str = "thread", **kw: Any) -> Swarm:
    sw = new_swarm(tmp_path, mode, **kw)
    open_swarms.append(sw)
    await sw.start(run_agents=False)
    return sw


def agents_of(sw: Swarm) -> list[str]:
    return [m["name"] for m in sw.members() if m["kind"] == AgentKind.LLM]


def notes(sw: Swarm, kind: str | None = None) -> list[dict]:
    rows = sw.db.all("SELECT recipient, kind, data FROM notifications ORDER BY id")
    out = [{"recipient": r["recipient"], "kind": r["kind"], "data": json.loads(r["data"] or "{}")} for r in rows]
    return [n for n in out if kind is None or n["kind"] == kind]


def kinds(sw: Swarm) -> set[str]:
    return {n["kind"] for n in notes(sw)}


def events(sw: Swarm, *types: str) -> list[dict]:
    return [e for e in sw.bus.recent(limit=10000) if e["type"] in types]


def vote_updates(sw: Swarm) -> int:
    """post.updated of #1 for its votes."""
    return sum(1 for e in events(sw, "post.updated")
               if e["data"]["post"]["id"] == GOAL_POST and e["data"]["changes"] == ["votes"])


async def tool(sw: Swarm, actor: str, name: str, args: dict | None = None, runtime: Any = None) -> tuple[str, bool]:
    """A tool call as the agent makes it: an LLM member's runtime serves its frozen tool set."""
    if runtime is None and actor != USER and sw.kind_of(actor) == AgentKind.LLM:
        runtime = AgentRuntime(sw, actor)
    return await call_tool(ToolContext(sw, actor, runtime), name, args or {})


async def ok(sw: Swarm, actor: str, name: str, args: dict | None = None) -> str:
    text, is_error = await tool(sw, actor, name, args)
    assert not is_error, text
    return text


# ===================================================================== the goal, the system posts, the kickoff
async def test_set_goal_before_the_first_start_writes_1_and_2_and_the_start_kicks_off(tmp_path, open_swarms,
                                                                                       monkeypatch):
    sw = new_swarm(tmp_path, agents={"stagger_seconds": 7})
    open_swarms.append(sw)
    sw.set_goal(USER, GOAL, DESCRIPTION)
    one, two = sw.forum.get(GOAL_POST), sw.forum.get(PLAN_POST)
    assert (one["kind"], one["author"], one["title"]) == ("goal", None, "Goal reached?")
    assert (two["kind"], two["author"], two["title"]) == ("plan", None, "Let's plan the work")
    assert "**Goal:** Build the parser" in one["text"] and "rule: democracy" in one["text"]
    assert sw.forum.plan_data() == {"clarify": False, "asker": None}
    assert sw.board.list() == [] and sw.db.scalar("SELECT COUNT(*) FROM tasks") == 0
    sent: list[tuple[float, str, str, dict]] = []
    monkeypatch.setattr(sw, "notify_later", lambda delay, who, key, **kw: sent.append((delay, who, key, kw)))
    await sw.start(run_agents=False)
    try:
        llm = agents_of(sw)
        assert len(llm) == 3
        assert sorted(s[1] for s in sent) == sorted(llm) and {s[2] for s in sent} == {"n_kickoff_thread"}
        assert sorted(s[0] for s in sent) == [0, 7, 14]
        team = sent[0][3]["team"]
        assert all(f"{m['name']} — {m['role']}" in team for m in sw.members())
        assert not {"n_kickoff", "n_goal_updated", "n_goal_updated_thread"} & kinds(sw)
        assert sw.board.list() == []
        post = sw.forum.create(llm[0], "First thoughts", "")
        assert post["id"] == 3
    finally:
        await sw.stop()


async def test_the_starting_team_gets_the_thread_prompt_roles_tools_and_subscriptions(tmp_path, open_swarms):
    sw = await started(tmp_path, open_swarms, starting_roles=["Reviewer", "Developer"])
    try:
        llm = agents_of(sw)
        roles = [m["role"] for m in sw.members()]
        assert roles == ["Critic", "Developer", "Generalist"]
        for name in llm:
            prompt = sw.member(name)["system_prompt"]
            assert "There is no leader and no task board" in prompt and "task_create" not in prompt
            assert [(s["kind"], s["target"], s["mode"], s["auto"]) for s in sw.forum.subscriptions(name)] == [
                ("feed", "", "digest", True), ("reactions", "", "digest", True), ("post", "1", "digest", True),
                ("post", "2", "digest", True)]
            runtime = AgentRuntime(sw, name)
            assert runtime.tools_rev == 6 and "feed" in runtime.tool_names and "task_create" not in runtime.tool_names
            assert frozen.read(sw.db, name)["mode"] == "thread"
        assert "critic" in sw.member(llm[0])["system_prompt"].lower()
        hired = await sw.hire(USER, "tester")
        assert len(sw.forum.subscriptions(hired)) == 4
        assert notes(sw, "n_hired_thread")[-1]["recipient"] == hired and not notes(sw, "n_hired")
        await sw.retire(USER, hired)
        assert sw.forum.subscriptions(hired) == []
    finally:
        await sw.stop()


async def test_a_board_record_in_a_thread_session_is_rewritten(tmp_path, open_swarms):
    sw = await started(tmp_path, open_swarms)
    try:
        name = agents_of(sw)[0]
        frozen.write(sw.db, name, frozen.current_record("en", "board"))
        runtime = AgentRuntime(sw, name)
        assert runtime.tools_rev == 6 and frozen.read(sw.db, name)["mode"] == "thread"
    finally:
        await sw.stop()


async def test_a_new_goal_rewrites_the_system_posts_and_clears_the_votes(tmp_path, open_swarms):
    sw = await started(tmp_path, open_swarms)
    try:
        a, b, c = agents_of(sw)
        sw.set_goal(USER, GOAL, DESCRIPTION)
        assert {n["recipient"] for n in notes(sw, "n_kickoff_thread")} == {a, b, c}
        await ok(sw, a, "vote", {"target": "#1", "value": "like"})
        await ok(sw, a, "vote", {"target": "#2", "value": "like"})
        before = len(events(sw, "post.updated"))
        sw.set_goal(USER, "Build the lexer")
        updated = events(sw, "post.updated")[before:]
        assert [(e["data"]["post"]["id"], e["data"]["changes"], e["actor"]) for e in updated] == [
            (1, ["text"], None), (2, ["text"], None), (1, ["votes"], None)]
        assert sw.forum.votes("p1") == [] and len(sw.forum.votes("p2")) == 1
        assert "**Goal:** Build the lexer" in sw.forum.get(1)["text"]
        assert {n["recipient"] for n in notes(sw, "n_goal_updated_thread")} == {a, b, c}
        assert all(not n["data"].get("_quiet") for n in notes(sw, "n_goal_updated_thread"))
        assert not {"n_goal_updated", "n_kickoff"} & kinds(sw)
        assert sw.db.scalar("SELECT COUNT(*) FROM tasks") == 0
    finally:
        await sw.stop()


async def test_clarify_wakes_only_the_asker(tmp_path, open_swarms):
    sw = new_swarm(tmp_path, clarify=True)
    open_swarms.append(sw)
    sw.set_goal(USER, GOAL, DESCRIPTION)
    assert sw.forum.plan_data() == {"clarify": True, "asker": None}  # nobody yet: the kickoff picks the asker
    await sw.start(run_agents=False)
    try:
        a, b, c = agents_of(sw)
        assert sw.forum.plan_data() == {"clarify": True, "asker": a}
        assert [n["recipient"] for n in notes(sw, "n_kickoff_thread_clarify")] == [a]
        assert not notes(sw, "n_kickoff_thread_clarify")[0]["data"].get("_quiet")
        quiet = notes(sw, "n_kickoff_thread")
        assert {n["recipient"] for n in quiet} == {b, c} and all(n["data"]["_quiet"] for n in quiet)
        assert "First one member asks the user" in sw.forum.get(PLAN_POST)["text"]
        sw.set_goal(USER, "Build the lexer")
        assert sw.forum.plan_data() == {"clarify": True, "asker": a}
        assert [n["recipient"] for n in notes(sw, "n_kickoff_thread_clarify")] == [a, a]
        assert {n["recipient"] for n in notes(sw, "n_goal_updated_thread")} == {b, c}
    finally:
        await sw.stop()


# ===================================================================== the vote on #1 through the tools
async def test_likes_on_1_complete_the_goal_under_democracy(tmp_path, open_swarms):
    sw = await started(tmp_path, open_swarms)
    try:
        a, b, c = agents_of(sw)
        sw.set_goal(USER, GOAL, DESCRIPTION)
        assert "Created #3" in await ok(sw, a, "post_create", {"title": "Parser ready", "text": "In parser.py."}) \
            or sw.forum.get(3)["author"] == a
        await ok(sw, b, "post_read", {"post_id": 3})   # read before write (docs/SPEC.md §22.11)
        await ok(sw, b, "comment", {"post_id": "#3", "text": "Checked: the tests pass."})
        await ok(sw, c, "vote", {"target": "#3", "value": "like"})
        assert sw.forum.get(3)["likes"] == 1 and len(sw.forum.comments(3)) == 1
        await ok(sw, c, "vote", {"target": "#1", "value": "dislike"})
        await ok(sw, a, "vote", {"target": "#1", "value": "like"})
        assert sw.goal()["status"] == "active"
        await ok(sw, b, "vote", {"target": "#1", "value": "like"})
        goal = sw.goal()
        assert goal["status"] == "done" and goal["completed_by"] == b
        assert goal["decision"] == {"rule": "democracy", "for": [a, b], "against": [c], "counted": 3,
                                    "wrap_up": False}
        assert goal["summary"] == (f"The goal is reached by vote (democracy): 2 of 3 for — {a}, {b}; against: {c}. "
                                   "The artifacts are the shared workspace.")
        done = events(sw, "goal.completed")[-1]
        assert done["actor"] == b and done["data"]["goal"]["decision"]["rule"] == "democracy"
        user = notes(sw, "u_goal_decided")
        assert len(user) == 1 and user[0]["data"]["summary"] == goal["summary"]
        assert user[0]["data"]["for"] == [a, b] and not notes(sw, "u_goal_done")
        decided = notes(sw, "n_goal_decided")
        assert {n["recipient"] for n in decided} == {a, b, c} and all(n["data"]["_quiet"] for n in decided)
        assert decided[0]["data"]["for"] == f"{a}, {b}" and decided[0]["data"]["rule"] == "democracy"
        text, is_error = await tool(sw, c, "vote", {"target": "#1", "value": "like"})
        assert is_error and "closed for votes" in text
    finally:
        await sw.stop()


async def test_a_dislike_keeps_the_goal_open_under_veto(tmp_path, open_swarms):
    sw = await started(tmp_path, open_swarms, goal_rule="veto")
    try:
        a, b, c = agents_of(sw)
        sw.set_goal(USER, GOAL)
        assert "rule: veto" in sw.forum.get(1)["text"]
        await ok(sw, a, "vote", {"target": "#1", "value": "like"})
        await ok(sw, b, "vote", {"target": "#1", "value": "like"})
        await ok(sw, c, "vote", {"target": "#1", "value": "dislike"})
        assert sw.goal()["status"] == "active"
        await ok(sw, c, "vote", {"target": "#1", "value": "none"})
        assert sw.goal()["status"] == "active"
        await ok(sw, c, "vote", {"target": "#1", "value": "like"})
        goal = sw.goal()
        assert goal["status"] == "done" and goal["decision"]["rule"] == "veto" and goal["completed_by"] == c
        assert goal["decision"]["for"] == [a, b, c] and goal["decision"]["against"] == []
        assert "against: —." in goal["summary"]
    finally:
        await sw.stop()


async def test_votes_of_the_user_and_external_members_are_not_counted(tmp_path, open_swarms):
    sw = await started(tmp_path, open_swarms, start_agents=2)
    try:
        a, b = agents_of(sw)
        sw.set_goal(USER, GOAL)
        await ok(sw, USER, "join_swarm", {"role": "Reviewer", "name": "Claude Code"})
        assert len(sw.forum.subscriptions("Claude Code")) == 4
        await ok(sw, USER, "vote", {"target": "#1", "value": "like"})
        await ok(sw, "Claude Code", "vote", {"target": "#1", "value": "like"})
        await ok(sw, a, "vote", {"target": "#1", "value": "like"})
        assert sw.goal()["status"] == "active"  # 1 of 2 counted: needs 2
        await ok(sw, b, "vote", {"target": "#1", "value": "like"})
        assert sw.goal()["decision"]["for"] == [a, b] and sw.goal()["decision"]["counted"] == 2
    finally:
        await sw.stop()


async def test_a_board_tool_is_unknown_to_a_thread_member(tmp_path, open_swarms):
    sw = await started(tmp_path, open_swarms)
    try:
        a = agents_of(sw)[0]
        sw.set_goal(USER, GOAL)
        for name, args in (("task_create", {"title": "x", "goal": "y"}), ("goal_complete", {"summary": "done"}),
                           ("board", {})):
            text, is_error = await tool(sw, a, name, args)
            assert is_error and text.startswith(f"Unknown tool '{name}'."), text
            assert "task_create" not in text.split("Available tools:")[1]
        assert sw.db.scalar("SELECT COUNT(*) FROM tasks") == 0 and sw.goal()["status"] == "active"
    finally:
        await sw.stop()


# ===================================================================== reopen and the user's own completion
async def test_reopen_resets_the_votes_and_wakes_every_member(tmp_path, open_swarms):
    sw = await started(tmp_path, open_swarms, start_agents=2)
    try:
        a, b = agents_of(sw)
        sw.set_goal(USER, GOAL)
        await ok(sw, a, "vote", {"target": "#1", "value": "like"})
        await ok(sw, b, "vote", {"target": "#1", "value": "like"})
        assert sw.goal()["status"] == "done"
        before = vote_updates(sw)
        assert await ok(sw, USER, "swarm_control", {"reopen_goal_comment": "The CLI is missing."}) == \
            "Done: goal reopened: the votes on #1 were cleared."
        goal = sw.goal()
        assert goal["status"] == "active" and "decision" not in goal and goal["summary"] == ""
        assert sw.forum.votes("p1") == [] and vote_updates(sw) == before + 1
        comment = sw.forum.comments(GOAL_POST)[-1]
        assert (comment["author"], comment["text"]) == (USER, "The CLI is missing.")
        reopened = notes(sw, "n_goal_reopened_thread")
        assert {n["recipient"] for n in reopened} == {a, b}
        assert all(n["data"]["comment"] == "The CLI is missing." and not n["data"].get("_quiet") for n in reopened)
        assert not notes(sw, "n_goal_reopened")
        await ok(sw, a, "vote", {"target": "#1", "value": "like"})
        assert sw.goal()["status"] == "active"  # one vote of two after the reset
        sw.complete_goal_by_user()
        sw.reopen_goal(USER, "-")
        assert sw.forum.comments(GOAL_POST)[-1]["text"] == "The CLI is missing."  # "-" posts no comment
        assert notes(sw, "n_goal_reopened_thread")[-1]["data"]["comment"] == "-"
    finally:
        await sw.stop()


async def test_the_user_completes_the_goal_through_swarm_control(tmp_path, open_swarms):
    sw = await started(tmp_path, open_swarms)
    try:
        a, b, c = agents_of(sw)
        sw.set_goal(USER, GOAL)
        await ok(sw, a, "vote", {"target": "#1", "value": "like"})
        await ok(sw, b, "vote", {"target": "#1", "value": "dislike"})
        text, is_error = await tool(sw, USER, "swarm_control", {"goal_summary": "x"})
        assert is_error and "goal_summary goes with complete_goal" in text
        assert await ok(sw, USER, "swarm_control", {"complete_goal": True}) == "Done: goal marked reached."
        goal = sw.goal()
        assert goal["status"] == "done" and goal["completed_by"] == USER
        assert goal["decision"] == {"rule": "user", "for": [a], "against": [b], "counted": 3, "wrap_up": False}
        assert goal["summary"] == ("The user marked the goal reached. Goal vote (#1, democracy): 1 of 3 for, "
                                   "1 against; needs 2. The artifacts are the shared workspace.")
        assert not notes(sw, "u_goal_done") and not notes(sw, "u_goal_decided")
        decided = notes(sw, "n_goal_decided")
        assert {n["recipient"] for n in decided} == {a, b, c} and all(n["data"]["_quiet"] for n in decided)
        assert decided[0]["data"]["rule"] == "the user's decision"
        text, is_error = await tool(sw, USER, "swarm_control", {"complete_goal": True})
        assert is_error and "already marked as achieved" in text
        sw.reopen_goal(USER, "")
        assert await ok(sw, USER, "swarm_control", {"complete_goal": True, "goal_summary": "  Shipped.  "}) == \
            "Done: goal marked reached."
        assert sw.goal()["summary"] == "Shipped."
    finally:
        await sw.stop()


# ===================================================================== rechecks
async def test_a_retire_meets_the_rule(tmp_path, open_swarms):
    sw = await started(tmp_path, open_swarms)
    try:
        a, b, c = agents_of(sw)
        sw.set_goal(USER, GOAL)
        await ok(sw, a, "vote", {"target": "#1", "value": "like"})
        await sw.retire(USER, b)
        assert sw.goal()["status"] == "active"  # 1 of 2: needs 2
        hired = await sw.hire(USER, "Tester")
        assert sw.goal()["status"] == "active" and sw.forum.goal_vote()["counted"] == 3
        await sw.retire(USER, hired)
        await sw.retire(USER, c)
        goal = sw.goal()
        assert goal["status"] == "done" and goal["completed_by"] is None and goal["decision"]["for"] == [a]
        assert events(sw, "goal.completed")[-1]["actor"] is None
    finally:
        await sw.stop()


async def test_a_rule_change_through_reconfigure_and_at_a_start(tmp_path, open_swarms):
    sw = await started(tmp_path, open_swarms, goal_rule="veto")
    a, b, c = agents_of(sw)
    sw.set_goal(USER, GOAL)
    await ok(sw, a, "vote", {"target": "#1", "value": "like"})
    await ok(sw, b, "vote", {"target": "#1", "value": "like"})
    assert sw.goal()["status"] == "active"
    sw.reconfigure(dataclasses.replace(sw.config, swarm=dataclasses.replace(sw.config.swarm, goal_rule="democracy")))
    goal = sw.goal()
    assert goal["status"] == "done" and goal["decision"]["rule"] == "democracy" and goal["completed_by"] is None
    assert "rule: democracy" in sw.forum.get(1)["text"]
    sw.reconfigure(dataclasses.replace(sw.config, swarm=dataclasses.replace(sw.config.swarm, goal_rule="veto")))
    assert "rule: veto" in sw.forum.get(1)["text"]  # a done goal's #1 is rewritten too
    sw.reopen_goal(USER, "")
    await ok(sw, a, "vote", {"target": "#1", "value": "like"})
    await ok(sw, b, "vote", {"target": "#1", "value": "like"})
    assert sw.goal()["status"] == "active"
    await sw.stop()
    sw.close()
    # the rule changed while the session was stopped: the start decides before it wakes anyone
    again = new_swarm(tmp_path, goal_rule="democracy")
    open_swarms.append(again)
    await again.start(run_agents=False)
    try:
        goal = again.goal()
        assert goal["status"] == "done" and goal["decision"]["rule"] == "democracy"
        assert "rule: democracy" in again.forum.get(1)["text"]
    finally:
        await again.stop()


# ===================================================================== wrap-up
async def test_wrap_up_on_1_and_the_switch_off_clears_the_votes(tmp_path, open_swarms):
    sw = await started(tmp_path, open_swarms)
    try:
        a, b, c = agents_of(sw)
        sw.set_goal(USER, GOAL)
        await ok(sw, USER, "swarm_control", {"wrap_up": True})
        assert {n["recipient"] for n in notes(sw, "n_wrap_up_thread")} == {a, b, c} and not notes(sw, "n_wrap_up")
        assert "Wrap-up mode (the user)" in sw.render_wake(a) and "like #1" in sw.render_wake(a)
        await ok(sw, a, "vote", {"target": "#1", "value": "like"})
        before = vote_updates(sw)
        await ok(sw, USER, "swarm_control", {"wrap_up": False})
        assert sw.forum.votes("p1") == [] and vote_updates(sw) == before + 1
        cleared = notes(sw, "n_wrap_up_votes_cleared")
        assert {n["recipient"] for n in cleared} == {a, b, c} and all(n["data"]["_quiet"] for n in cleared)
        assert not notes(sw, "n_wrap_up_end")
        await ok(sw, b, "vote", {"target": "#1", "value": "like"})
        assert sw.goal()["status"] == "active"
        # a decision in wrap-up mode
        sw.wrap_up(USER)
        await ok(sw, a, "vote", {"target": "#1", "value": "like"})
        goal = sw.goal()
        assert goal["status"] == "done" and goal["decision"]["wrap_up"] is True
        assert goal["summary"].startswith("The team finished the work by vote in wrap-up mode (democracy): 2 of 3")
        # switched off after the decision: the done goal's votes stay
        sw.end_wrap_up(USER)
        assert len(sw.forum.votes("p1")) == 2 and notes(sw, "n_wrap_up_end")
    finally:
        await sw.stop()


async def test_a_new_goal_and_a_reopen_in_wrap_up_mode_clear_the_votes_once(tmp_path, open_swarms):
    sw = await started(tmp_path, open_swarms)
    try:
        a, b, c = agents_of(sw)
        sw.set_goal(USER, GOAL)
        sw.wrap_up(USER)
        await ok(sw, a, "vote", {"target": "#1", "value": "like"})
        before = vote_updates(sw)
        sw.set_goal(USER, "Build the lexer")
        assert vote_updates(sw) == before + 1 and not sw.wrapping_up
        assert notes(sw, "n_wrap_up_end") and not notes(sw, "n_wrap_up_votes_cleared")
        await ok(sw, a, "vote", {"target": "#1", "value": "like"})
        await ok(sw, b, "vote", {"target": "#1", "value": "like"})
        sw.wrap_up(USER)
        before = vote_updates(sw)
        sw.reopen_goal(USER, "")
        assert vote_updates(sw) == before + 1 and not notes(sw, "n_wrap_up_votes_cleared")
    finally:
        await sw.stop()


# ===================================================================== wake-ups, compaction, the scheduler
async def test_a_thread_wake_up_has_the_vote_line_and_no_task_lines(tmp_path, open_swarms):
    sw = await started(tmp_path, open_swarms)
    try:
        a, b, c = agents_of(sw)
        sw.set_goal(USER, GOAL)
        await ok(sw, b, "vote", {"target": "#1", "value": "like"})
        text = sw.render_wake(a)
        assert "Goal vote (#1, democracy): 1 of 3 for, 0 against; needs 2." in text
        assert "Your open tasks" not in text and "Ready tasks" not in text
    finally:
        await sw.stop()


async def test_compaction_keeps_the_members_vote(tmp_path, open_swarms):
    sw = await started(tmp_path, open_swarms)
    try:
        a = agents_of(sw)[0]
        sw.set_goal(USER, GOAL)
        await ok(sw, a, "vote", {"target": "#1", "value": "dislike"})
        runtime = AgentRuntime(sw, a)
        runtime.history = [{"role": "user", "content": "wake"}, {"role": "assistant", "content": "ok"}]
        runtime._replace_with_summary("I disliked #1: no tests.")
        text = runtime.history[0]["content"]
        assert text.startswith("[Context compacted. Summary of your earlier work:]\nI disliked #1: no tests.")
        assert "The feed is the shared record" in text
        assert "Your vote on #1: dislike. Goal vote (#1, democracy): 0 of 3 for, 1 against; needs 2." in text
        assert runtime._compaction_budget() > 0
    finally:
        await sw.stop()


async def test_no_board_item_runs_in_a_thread_pass(tmp_path, open_swarms, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(swarmmod, "SCHEDULER_SECONDS", 0.01)
    sw = new_swarm(tmp_path)
    open_swarms.append(sw)
    items = ("_nudge_idle", "_wake_stalled", "_wake_idle_holders", "_remind_reviewers", "_check_all_done",
             "_remind_user_reviews", "_tell_crew_gaps", "_wake_team_asleep", "_hint_planner")
    for item in items:
        monkeypatch.setattr(sw, item, lambda *a, _item=item, **k: calls.append(_item))
    monkeypatch.setattr(sw, "_wake_deferred", lambda: calls.append("_wake_deferred"))
    await sw.start(run_agents=False)
    try:
        sw.set_goal(USER, GOAL)
        await asyncio.sleep(0.2)
        assert "_wake_deferred" in calls and not set(items) & set(calls)
        assert sw.stall_ids(agents_of(sw)[0]) == frozenset()
    finally:
        await sw.stop()


# ===================================================================== a scripted run with the agents' runtimes
async def test_a_scripted_thread_run_decides_the_goal(tmp_path, open_swarms):
    seen: dict[str, list[dict]] = {}

    def factory(profile, agent):
        def handler(system, messages, tools):
            calls = seen.setdefault(agent, [])
            calls.append({"system": system, "tools": [t["name"] for t in tools], "messages": messages})
            order = list(seen)  # the agents in the order they first spoke, so each has its own script
            index = order.index(agent)
            step = len(calls)
            if step == 1 and index == 0:
                return {"tool_calls": [{"name": "post_create", "arguments": {"title": "Parser ready",
                                                                             "text": "parser.py, tested."}},
                                       {"name": "vote", "arguments": {"target": "#1", "value": "like"}}]}
            if step == 1 and index == 1:
                return {"tool_calls": [{"name": "comment", "arguments": {"post_id": 2, "text": "I take the tests."}},
                                       {"name": "vote", "arguments": {"target": "#1", "value": "like"}}]}
            if step == 1:
                return {"tool_calls": [{"name": "task_create", "arguments": {"title": "x", "goal": "y"}}]}
            return {"tool_calls": [{"name": "wait", "arguments": {}}]}

        return ScriptedLLM(handler, profile)

    sw = new_swarm(tmp_path, llm=factory)
    open_swarms.append(sw)
    await sw.start()
    try:
        sw.set_goal(USER, GOAL, DESCRIPTION)
        deadline = time.time() + 15
        while time.time() < deadline and (sw.goal()["status"] != "done" or len(seen) < 3
                                          or any(len(v) < 2 for v in seen.values())):
            await asyncio.sleep(0.05)
        goal = sw.goal()
        assert goal["status"] == "done" and goal["decision"]["rule"] == "democracy"
        assert len(goal["decision"]["for"]) == 2 and goal["decision"]["counted"] == 3
        wakes = []
        for calls in seen.values():
            first = calls[0]
            assert "feed" in first["tools"] and "task_create" not in first["tools"]
            assert "There is no leader and no task board" in first["system"]
            wakes.append(first["messages"][0]["content"])
            assert "Your open tasks" not in wakes[-1] and "task_create" not in wakes[-1]
        # the one woken first saw the vote open (the others may wake after the decision)
        assert any("Goal vote (#1, democracy)" in wake for wake in wakes)
        third = [calls for calls in seen.values() if any(
            "Unknown tool 'task_create'" in str(m.get("content")) for m in calls[1]["messages"])]
        assert len(third) == 1
        assert sw.db.scalar("SELECT COUNT(*) FROM tasks") == 0
        assert [p["id"] for p in sw.forum.feed()][:3] == [1, 2, 3]
    finally:
        await sw.stop()


# ===================================================================== a board session as before
async def test_a_board_session_behaves_as_before(tmp_path, open_swarms):
    sw = await started(tmp_path, open_swarms, mode="board")
    try:
        assert sw.mode == "board" and sw.forum is None
        a, b, c = agents_of(sw)
        sw.set_goal(USER, GOAL)
        planning = [t for t in sw.board.list() if "planning" in t.tags]
        assert len(planning) == 1
        assert {n["recipient"] for n in notes(sw, "n_kickoff")} == {a, b, c}
        assert not {k for k in kinds(sw) if k.endswith("_thread")}
        prompt = sw.member(a)["system_prompt"]
        assert "**Task board**" in prompt and "There is no leader and no task board" not in prompt
        runtime = AgentRuntime(sw, a)
        assert runtime.tools_rev == 5 and "task_create" in runtime.tool_names and "feed" not in runtime.tool_names
        assert sw.db.scalar("SELECT COUNT(*) FROM sqlite_master WHERE name = 'posts'") == 0
        with pytest.raises(ToolError, match="In a board session the members complete the goal with goal_complete"):
            sw.complete_goal_by_user()
        text, is_error = await tool(sw, a, "feed", {})
        assert is_error and text.startswith("Unknown tool 'feed'.")
        assert "Your open tasks" in sw.render_wake(b) and "Goal vote" not in sw.render_wake(b)
        sw.complete_goal(a, "Done.")
        assert notes(sw, "u_goal_done") and "decision" not in sw.goal()
        sw.reopen_goal(USER, "more")
        assert len(notes(sw, "n_goal_reopened")) == 1
        sw.wrap_up(USER)
        assert notes(sw, "n_wrap_up") and not notes(sw, "n_wrap_up_thread")
        sw.end_wrap_up(USER)
        assert notes(sw, "n_wrap_up_end") and not notes(sw, "n_wrap_up_votes_cleared")
    finally:
        await sw.stop()
