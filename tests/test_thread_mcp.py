"""The outside MCP surfaces of a thread session (docs/SPEC.md §22.9): the hub MCP's listing and instructions by the
session's mode, the one refusal of a tool of the other mode on every surface, the launcher MCP (the union listing with
its mode marks, session_create's mode and goal_rule, sessions_list's thread line, its instructions), the create
options (sessionopts.overrides_from) and the members who joined over MCP (subscriptions, digests, liveness, results,
counting). Real swarms and sessions on ScriptedLLM only; no network, no port bound."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mcp import Client
from starlette.testclient import TestClient

from dreamteam import db as dbmod
from dreamteam import settings as st
from dreamteam import swarm as swarm_module
from dreamteam.config import Config
from dreamteam.hub_mcp import (INSTRUCTIONS, INSTRUCTIONS_THREAD, MODE_ONLY, build_hub_server, hub_call, hub_specs,
                               list_hub_tools)
from dreamteam.launcher import create_launcher_app
from dreamteam.launcher_mcp import INSTRUCTIONS as LAUNCHER_INSTRUCTIONS
from dreamteam.launcher_mcp import call_launcher_tool, dispatch_launcher_call, list_launcher_tools, session_line
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.models import USER, AgentKind, AgentStatus
from dreamteam.sessionopts import GOAL_RULE_BOARD, OptionError, overrides_from
from dreamteam.sessions import SessionManager
from dreamteam.swarm import Swarm
from dreamteam.tools import MODES, REGISTRY
from dreamteam.tracing import EXTERNAL_IDLE_SECONDS

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

BOARD_ONLY = ["board", "task_get", "task_create", "task_claim", "task_update", "task_complete", "task_review",
              "task_comment", "task_discussion", "goal_complete"]
FORUM = ["feed", "post_read", "post_create", "post_update", "comment", "vote", "subscriptions", "subscribe",
         "unsubscribe"]
MEMBERS_ONLY = " [Members only: call join_swarm first and pass as_agent.]"
WAIT = 60.0


def make_config(tmp_path: Path, mode: str = "thread", **swarm: Any) -> Config:
    raw: dict[str, Any] = {
        "swarm": {"name": "Thread MCP", "language": "en", "start_agents": 0, "max_agents": 6, "max_cost_usd": 0,
                  "mode": mode, "clarify": False, **swarm},
        "agents": {"stagger_seconds": 0, "idle_nudge_seconds": WAIT},
        "workspace": {"git": False},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
    }
    return Config.from_dict(tmp_path / "project", raw)


@pytest.fixture
def make_swarm(tmp_path):
    made: list[Swarm] = []

    def factory(mode: str = "thread", **kw: Any) -> Swarm:
        sw = Swarm(config=make_config(tmp_path / f"s{len(made)}", mode, **kw),
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
def no_loop(monkeypatch):
    """The scheduler's loop never comes round within a test that calls its items itself."""
    monkeypatch.setattr(swarm_module, "SCHEDULER_SECONDS", 3600.0)


async def _no_stop() -> None:
    pass


def set_goal(sw: Swarm) -> dict:
    goal = {"title": "Build the parser", "description": "", "status": "active", "summary": "", "set_by": USER,
            "updated_at": time.time() - 10 ** 6}
    sw.db.set("goal", goal)
    sw.forum.ensure_system_posts(goal)
    return goal


async def ok(sw: Swarm, name: str, args: dict | None = None) -> str:
    text, is_error = await hub_call(sw, name, dict(args or {}))
    assert not is_error, text
    return text


async def err(sw: Swarm, name: str, args: dict | None = None) -> str:
    text, is_error = await hub_call(sw, name, dict(args or {}))
    assert is_error, text
    return text


def externals(sw: Swarm) -> list[str]:
    return [m["name"] for m in sw.members() if m["kind"] == AgentKind.EXTERNAL]


def subs(sw: Swarm, member: str) -> list[tuple]:
    return [(r["id"], r["kind"], r["target"], r["mode"], r["auto"]) for r in sw.db.all(
        "SELECT * FROM subscriptions WHERE member = ? ORDER BY id", (member,))]


def manager_for(tmp_path: Path, **swarm: Any) -> SessionManager:
    home = tmp_path / "home"
    home.mkdir()
    st.write_raw(home, {
        "swarm": {"name": "Home", "language": "en", "start_agents": 1, "max_agents": 3, "max_cost_usd": 2.0, **swarm},
        "agents": {"stagger_seconds": 0, "idle_nudge_seconds": 3600},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
    })
    return SessionManager(home, llm_factory=lambda profile, agent: ScriptedLLM(lambda *a: "Nothing to do.", profile))


def tools_of(listing) -> dict:
    return {t.name: t for t in listing}


def props(tool) -> dict:
    return {k: v for k, v in tool.input_schema["properties"].items() if k not in ("as_agent", "session")}


# ================================================================== hub MCP: the session's mode only
def test_the_hub_lists_the_tools_and_texts_of_the_sessions_mode(make_swarm):
    thread, board = make_swarm(), make_swarm("board")
    listed = tools_of(list_hub_tools(thread))
    assert set(FORUM) <= set(listed) and not set(BOARD_ONLY) & set(listed)
    assert [t.name for t in list_hub_tools(thread)] == [s.name for s in hub_specs("thread")]
    for name in ("subscriptions", "subscribe", "unsubscribe"):
        assert listed[name].description == REGISTRY[name].describe("en", "thread") + MEMBERS_ONLY
        assert "as_agent" in listed[name].input_schema["properties"]
    for name in ("feed", "post_read", "post_create", "post_update", "comment", "vote"):
        assert listed[name].description == REGISTRY[name].describe("en", "thread")   # the user calls them too
    for name, tool in listed.items():
        assert not any(mark in tool.description for mark in MODE_ONLY.values()), name   # one mode: no marks
        assert props(tool) == REGISTRY[name].parameters_for("thread")["properties"], name
    assert listed["swarm_status"].description == REGISTRY["swarm_status"].describe("en", "thread")
    assert "task" not in listed["agent_traces"].input_schema["properties"]
    assert "task_id" not in props(listed["mcp_create"])
    assert props(listed["mcp_create"])["scope"]["enum"] == ["swarm", "private"]
    assert list(props(listed["swarm_control"]))[-2:] == ["complete_goal", "goal_summary"]
    on_board = tools_of(list_hub_tools(board))
    assert set(BOARD_ONLY) <= set(on_board) and not set(FORUM) & set(on_board)
    assert "complete_goal" not in props(on_board["swarm_control"]) and "task" in props(on_board["agent_traces"])
    # a test double without a mode is a board session, as before
    assert set(tools_of(list_hub_tools(SimpleNamespace(language="en")))) == set(on_board)


async def test_the_hub_servers_instructions_follow_the_mode(make_swarm):
    thread, board = make_swarm(), make_swarm("board")
    await thread.start(run_agents=False)
    await board.start(run_agents=False)
    try:
        async with Client(build_hub_server(thread)) as one, Client(build_hub_server(board)) as two:
            assert one.instructions == INSTRUCTIONS_THREAD and two.instructions == INSTRUCTIONS
            names = {t.name for t in (await one.list_tools()).tools}
            assert "feed" in names and "task_create" not in names
            res = await one.call_tool("task_create", {"title": "x"})
            assert res.is_error and "is for board-mode sessions" in res.content[0].text
    finally:
        await thread.stop()
        await board.stop()


def test_the_thread_instructions_name_the_forum_the_vote_and_the_members_side():
    text = INSTRUCTIONS_THREAD
    flat = " ".join(text.split())
    for words in ("without a task board", "`feed`", "`post_read`", "`post_create`", "`post_update`", "`comment`",
                  "`reply_to`", "`vote` like / dislike / none", '"Goal reached?" / «Цель достигнута?»',
                  "\"Let's plan the work\" / «Спланируем работу»", "democracy: more than half",
                  "veto: all of them like it and none dislikes it", "shown and never counted",
                  "complete_goal=true", "goal_summary", "reopen_goal_comment", "clears the votes on #1",
                  "reaches every member at once", "`progress=true`", "new posts, comments and goal events",
                  "`subscriptions`", "`subscribe`", "`unsubscribe`", "`wake` or `digest`", "`by=<member>`",
                  "the feed itself can never be removed", "An @mention reaches the member at once",
                  "[agents] digest_seconds", "its vote on #1 does not count", '"finish with what we have"',
                  "is the answer", "`jobs` and `job_stop`", "`skill_attach`", "`join_swarm(role=...)`",
                  "`as_agent=", "`shell`", "max_agents"):
        assert words in flat, words
    assert "task_claim" not in text and "task_complete" not in text and "`board`" not in text
    assert chr(0x451) not in text   # the plain letter only
    assert all(len(line) <= 120 for line in text.splitlines())
    assert INSTRUCTIONS.startswith("You are connected to a DreamTeam swarm: a self-organizing team")  # unchanged


# ================================================================== one refusal on every surface
async def test_a_tool_of_the_other_mode_is_refused_alike_on_the_hub_and_the_launcher(tmp_path):
    manager = manager_for(tmp_path)
    try:
        text, error = await call_launcher_tool(manager, "session_create", {"goal": "Talk", "mode": "thread"})
        assert not error, text
        thread = manager.ids()[0]
        text, error = await call_launcher_tool(manager, "session_create", {"goal": "Plan", "mode": "board"})
        assert not error, text
        board = manager.ids()[1]
        refusal = ("Tool 'task_create' is for board-mode sessions; this session works in the thread mode (no tasks): "
                   "post to the feed with post_create.")
        hub = await err(manager.swarms[thread], "task_create", {"title": "x"})
        text, error = await dispatch_launcher_call(manager, "task_create", {"session": thread, "title": "x"})
        assert error and text == hub == refusal
        text, error = await dispatch_launcher_call(manager, "post_create", {"session": board, "title": "x"})
        assert error and text == ("Tool 'post_create' is for thread-mode sessions; this session uses the task board: "
                                  "task_create.")
        text, error = await dispatch_launcher_call(manager, "goal_complete", {"session": thread, "summary": "x"})
        assert error and text.endswith("swarm_control complete_goal=true.")
        assert manager.swarms[thread].db.scalar("SELECT COUNT(*) FROM tasks") == 0
        # an unknown name: the close matches come from both modes' tools
        text, error = await dispatch_launcher_call(manager, "post_creat", {"session": board})
        assert error and "Did you mean: post_create" in text and "task_create" in text.split("Available tools: ")[1]
        # a shared tool's board-only argument is checked against the session's mode
        member = await ok(manager.swarms[thread], "join_swarm", {"role": "Helper"})
        name = externals(manager.swarms[thread])[0]
        assert name in member
        args = {"session": thread, "as_agent": name, "name": "x_tools", "description": "d", "code": "c",
                "scope": "task", "task_id": 3}
        text, error = await dispatch_launcher_call(manager, "mcp_create", args)
        assert error and "Argument 'scope' must be one of: swarm, private" in text
        assert "[Ignored unknown argument(s): task_id." in text
    finally:
        await manager.shutdown()


def test_the_web_act_route_answers_the_same_refusal(tmp_path):
    manager = manager_for(tmp_path, mode="thread")
    with TestClient(create_launcher_app(manager), base_url="http://127.0.0.1:8765") as c:
        try:
            resp = c.post("/api/sessions", content=json.dumps({"title": "T", "goal": "Talk"}),
                          headers={"content-type": "application/json"})
            assert resp.status_code == 201, resp.text
            sid = resp.json()["session"]["id"]
            act = c.post(f"/s/{sid}/api/act/task_create", content=json.dumps({"title": "x"}),
                         headers={"content-type": "application/json"}).json()
            assert not act["ok"] and "is for board-mode sessions; this session works in the thread mode" in json.dumps(
                act)
        finally:
            c.portal.call(manager.shutdown)


# ================================================================== launcher MCP: the union listing
def test_the_launcher_lists_both_modes_once_with_their_marks(tmp_path):
    manager = manager_for(tmp_path)
    listing = list_launcher_tools(manager)
    names = [t.name for t in listing]
    assert len(names) == len(set(names))
    tools = tools_of(listing)
    assert set(BOARD_ONLY) | set(FORUM) <= set(tools)
    for name in BOARD_ONLY:
        assert tools[name].description.endswith(MODE_ONLY["board"]), name
    for name in FORUM:
        assert tools[name].description.endswith(MODE_ONLY["thread"]), name
    assert tools["subscribe"].description == (REGISTRY["subscribe"].describe("en", "thread") + MEMBERS_ONLY
                                              + " [Thread sessions only.]")
    assert tools["task_claim"].description.endswith(MEMBERS_ONLY + " [Board sessions only.]")
    for name, tool in tools.items():
        if name in REGISTRY and REGISTRY[name].modes == frozenset(MODES):
            assert not any(mark in tool.description for mark in MODE_ONLY.values()), name
            assert tool.description.startswith(REGISTRY[name].describe("en", "board")), name   # the board text
        if name in REGISTRY:
            assert "session" in tool.input_schema["properties"], name
    # the union of both modes' properties: the board's in their order, then those only the thread variant has
    control = REGISTRY["swarm_control"]
    assert list(props(tools["swarm_control"])) == [*control.parameters["properties"], "complete_goal", "goal_summary"]
    assert props(tools["swarm_control"])["complete_goal"]["description"].startswith("Thread sessions only")
    assert tools["swarm_control"].input_schema["required"] == control.parameters["required"]
    assert "task" in props(tools["agent_traces"])
    assert props(tools["mcp_create"])["scope"] == REGISTRY["mcp_create"].parameters["properties"]["scope"]
    assert "task_id" in props(tools["mcp_create"])
    assert "complete_goal" not in control.parameters["properties"]  # the registry's schema is untouched


def test_session_create_takes_mode_and_goal_rule_and_clarify_speaks_of_both_modes(tmp_path):
    create = tools_of(list_launcher_tools(manager_for(tmp_path)))["session_create"].input_schema["properties"]
    assert create["mode"] == {
        "type": "string", "enum": ["board", "thread"],
        "description": "board: a task board with owners, statuses and reviews; thread: no imposed structure: the "
                       "agents post to a feed, comment, vote and subscribe, and decide by their vote on post #1 that "
                       "the goal is reached. Fixed for the session's life. Default: the global setting ([swarm] "
                       "mode, board)."}
    assert create["goal_rule"] == {
        "type": "string", "enum": ["democracy", "veto"],
        "description": "Thread sessions only: democracy (more than half of the swarm's own AI agents like post #1) "
                       "or veto (all of them like it and none dislikes it). Default: the global setting ([swarm] "
                       "goal_rule, democracy)."}
    assert create["clarify"]["description"] == (
        "true: the agents first ask you about the forks of the goal (board: the planner, before any task, while the "
        "other agents wait; thread: the first agent, in one message, then it comments your answers on post #2), and "
        "an agent's critical question pauses the whole swarm until you answer (send_message to that agent in the "
        "session answers it); false: off. Default: the global setting ([swarm] clarify, off).")


def test_the_launcher_instructions_describe_the_two_modes():
    own = LAUNCHER_INSTRUCTIONS[:-len(INSTRUCTIONS)]
    assert LAUNCHER_INSTRUCTIONS.endswith(INSTRUCTIONS)   # the hub's part stays the board's
    assert ("its own goal, agents, messages and workspace, and either a task board or a feed of posts (its\nmode)."
            in own)
    assert "task board, agents" not in own
    team_line = own.index("`session_create(..., team_name=<name>)`")
    paragraph = own.index("A session works in one of two modes")
    assert team_line < paragraph < own.index("Every other tool works inside one session")
    for words in ('`session_create(..., mode="thread",\ngoal_rule="veto")`', "[Board\nsessions only.]",
                  "[Thread sessions only.]", "`sessions_list` shows a thread session's mode, goal rule and goal vote",
                  "the hub instructions below describe a\nboard session", "`feed` and `post_read`",
                  "`swarm_control` complete_goal=true"):
        assert words in own, words
    assert all(len(line) <= 120 for line in LAUNCHER_INSTRUCTIONS.splitlines())


# ================================================================== launcher MCP: session_create and sessions_list
async def test_session_create_makes_a_thread_session_and_sessions_list_shows_its_vote(tmp_path):
    manager = manager_for(tmp_path)
    try:
        text, error = await call_launcher_tool(manager, "session_create", {
            "title": "Forum", "goal": "Talk it over", "mode": "thread", "goal_rule": "veto", "start": False})
        assert not error, text
        sid = manager.ids()[0]
        assert manager.meta(sid)["overrides"] == {"swarm": {"mode": "thread", "goal_rule": "veto"}}
        path = manager._data_dir(sid) / "swarm.db"
        with sqlite3.connect(path) as conn:   # the forum tables and the mode before the first start
            assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'posts'").fetchone()
            assert dbmod.session_mode(conn) == "thread"
        assert text.splitlines()[-2].startswith(f"{sid} [stopped] Forum | thread (veto) | posts 0 | agents 0")
        text, error = await call_launcher_tool(manager, "session_start", {"session": sid})
        assert not error, text
        sw = manager.swarms[sid]
        assert sw.mode == "thread" and sw.db.one("SELECT 1 FROM posts WHERE id = 1") is not None
        (agent,) = [m["name"] for m in sw.members() if m["kind"] == AgentKind.LLM]
        text, _ = await call_launcher_tool(manager, "sessions_list", {})
        first = text.splitlines()[1]
        assert " | thread (veto) | posts 0 | goal vote 0/1 for (needs 1) | agents 1" in first, first
        assert " | tasks " not in first
        sw.forum.create(agent, "Finding", "text")
        sw.forum.vote(agent, "#1", "dislike")
        first = (await call_launcher_tool(manager, "sessions_list", {}))[0].splitlines()[1]
        assert " | thread (veto) | posts 1 | goal vote 0/1 for, 1 against (needs 1) | agents 1" in first, first
        sw.forum.vote(USER, "#1", "like")   # the user's vote is shown, never counted
        first = (await call_launcher_tool(manager, "sessions_list", {}))[0].splitlines()[1]
        assert "goal vote 0/1 for, 1 against (needs 1)" in first
        assert "\n    goal [active]: Talk it over" in (await call_launcher_tool(manager, "sessions_list", {}))[0]
    finally:
        await manager.shutdown()


async def test_session_create_refuses_a_goal_rule_of_a_board_session_and_leaves_bad_values_to_the_settings(tmp_path):
    manager = manager_for(tmp_path)
    try:
        for args in ({"goal_rule": "veto"}, {"mode": "board", "goal_rule": "democracy"}):
            text, error = await call_launcher_tool(manager, "session_create", {"goal": "g", "start": False, **args})
            assert error and text == f"Error: {GOAL_RULE_BOARD}", text
        text, error = await call_launcher_tool(manager, "session_create", {"goal": "g", "mode": "square"})
        assert error and "swarm.mode must be 'board' or 'thread', got 'square'" in text, text
        text, error = await call_launcher_tool(manager, "session_create", {"goal": "g", "mode": "thread",
                                                                           "goal_rule": "dictator"})
        assert error and "swarm.goal_rule must be 'democracy' or 'veto', got 'dictator'" in text, text
        assert manager.ids() == []   # nothing was created
        text, error = await call_launcher_tool(manager, "session_create", {"goal": "g", "mode": "board",
                                                                           "start": False})
        assert not error, text
        sid = manager.ids()[0]
        assert manager.meta(sid)["overrides"] == {}   # a board session stores no mode
        assert " | tasks 0/0 done | agents" in session_line(manager.summary(sid))
        text, error = await call_launcher_tool(manager, "session_create", {"goal": "g", "mode": "thread",
                                                                           "start": False})
        assert not error, text
        assert manager.meta(manager.ids()[1])["overrides"] == {"swarm": {"mode": "thread", "goal_rule": "democracy"}}
    finally:
        await manager.shutdown()


async def test_a_home_whose_default_is_thread_takes_a_goal_rule_without_mode(tmp_path):
    manager = manager_for(tmp_path, mode="thread")
    try:
        text, error = await call_launcher_tool(manager, "session_create", {"goal": "g", "goal_rule": "veto",
                                                                           "start": False})
        assert not error, text
        assert manager.meta(manager.ids()[0])["overrides"] == {"swarm": {"mode": "thread", "goal_rule": "veto"}}
        text, error = await call_launcher_tool(manager, "session_create", {"goal": "g", "mode": "board",
                                                                           "goal_rule": "veto"})
        assert error and text == f"Error: {GOAL_RULE_BOARD}"
    finally:
        await manager.shutdown()


def test_session_line_of_a_thread_summary():
    base = {"id": "001-t", "status": "running", "title": "T", "agents": {"total": 3, "working": 1},
            "spent_usd": 0.5, "max_cost_usd": 5, "tasks": {}, "mode": "thread", "goal_rule": "democracy", "posts": 4}
    assert session_line({**base, "goal_vote": None}).startswith(
        "001-t [running] T | thread (democracy) | posts 4 | agents 3 (1 working) | $0.50 of $5.00")
    vote = {"rule": "democracy", "counted": 3, "for": ["A", "B"], "against": [], "needed": 2, "met": True}
    assert " | posts 4 | goal vote 2/3 for (needs 2) | agents" in session_line({**base, "goal_vote": vote})
    vote = {**vote, "for": ["A"], "against": ["B", "C"]}
    assert " | goal vote 1/3 for, 2 against (needs 2) | agents" in session_line({**base, "goal_vote": vote})
    board = {**base, "mode": "board", "goal_rule": None, "posts": 0, "tasks": {"done": 1, "total": 2}}
    assert session_line(board).startswith("001-t [running] T | tasks 1/2 done | agents")
    older = {k: v for k, v in board.items() if k not in ("mode", "goal_rule", "posts")}   # an older hub's summary
    assert session_line(older) == session_line(board)


# ================================================================== the create options
def test_overrides_from_takes_mode_and_goal_rule_in_both_paths(tmp_path):
    board_home, thread_home = make_config(tmp_path / "b", "board"), make_config(tmp_path / "t", "thread")
    for strict in (False, True):
        assert overrides_from({"mode": "thread", "goal_rule": "veto"}, board_home, strict=strict) == {
            "swarm": {"mode": "thread", "goal_rule": "veto"}}
        assert overrides_from({"mode": "board"}, thread_home, strict=strict) == {"swarm": {"mode": "board"}}
        assert overrides_from({"goal_rule": "veto"}, thread_home, strict=strict) == {"swarm": {"goal_rule": "veto"}}
        for body, home in (({"goal_rule": "veto"}, board_home), ({"mode": "board", "goal_rule": "veto"}, thread_home)):
            with pytest.raises(OptionError) as exc:
                overrides_from(body, home, strict=strict)
            assert exc.value.code == "bad_request" and str(exc.value) == GOAL_RULE_BOARD
            assert str(exc.value) == 'goal_rule applies to a thread session: give it with mode "thread".'
    for body in ({"mode": "square"}, {"mode": 3}, {"goal_rule": "dictator", "mode": "thread"}):
        with pytest.raises(OptionError, match="must be one of") as exc:
            overrides_from(body, board_home, strict=True)
        assert exc.value.code == "bad_request"
    assert overrides_from({"mode": None, "goal_rule": None}, board_home, strict=True) == {}   # null: not given
    # not strict: a bad value is left to the settings' check, and it decides no goal_rule refusal
    assert overrides_from({"mode": "square", "goal_rule": "veto"}, board_home, strict=False) == {
        "swarm": {"mode": "square", "goal_rule": "veto"}}
    assert overrides_from({"mode": "", "goal_rule": ""}, thread_home, strict=False) == {}


def test_a_strict_create_resolves_the_team_in_the_effective_mode(tmp_path):
    board_home, thread_home = make_config(tmp_path / "b", "board"), make_config(tmp_path / "t", "thread")
    over = overrides_from({"mode": "thread", "team": ["Critic", "reviewer"]}, board_home, strict=True)
    assert over["swarm"]["starting_roles"] == ["Critic", "reviewer"]
    assert overrides_from({"team": ["Critic"]}, thread_home, strict=True)["swarm"]["starting_roles"] == ["Critic"]
    with pytest.raises(OptionError) as exc:
        overrides_from({"team": ["Critic"]}, board_home, strict=True)
    assert exc.value.code == "unknown_role"
    with pytest.raises(OptionError) as exc:
        overrides_from({"mode": "board", "team": ["Critic"]}, thread_home, strict=True)
    assert exc.value.code == "unknown_role"
    assert overrides_from({"team": ["reviewer"]}, board_home, strict=True)["swarm"]["starting_roles"] == ["reviewer"]


# ================================================================== members who joined over MCP
async def test_join_swarm_subscribes_a_new_member_and_a_welcome_back_keeps_its_rows(make_swarm):
    sw = make_swarm()
    await sw.start(run_agents=False)
    try:
        text = await ok(sw, "join_swarm", {"role": "Helper", "name": "Eve Black"})
        assert "Read the feed" in text and "`board`" not in text and "task_claim" not in text
        eve = "Eve Black"
        rows = subs(sw, eve)
        assert [(kind, target, mode, auto) for _, kind, target, mode, auto in rows] == [
            ("feed", "", "digest", 1), ("reactions", "", "digest", 1), ("post", "1", "digest", 1),
            ("post", "2", "digest", 1)]
        listing = await ok(sw, "subscriptions", {"as_agent": eve})
        assert "feed" in listing and "#1" in listing and "#2" in listing
        added = await ok(sw, "subscribe", {"as_agent": eve, "what": "file", "target": "src", "mode": "wake"})
        assert "src" in added
        before = subs(sw, eve)
        text = await ok(sw, "join_swarm", {"role": "Helper", "name": "eve black"})
        assert text.startswith("Welcome back: you continue as Eve Black")
        assert subs(sw, eve) == before   # kept, and none made
        text = await ok(sw, "swarm_status", {"as_agent": eve})
        assert "Feed: 0 posts, 0 comments." in text and "Tasks:" not in text
        await ok(sw, "retire_agent", {"name": eve})
        assert subs(sw, eve) == []
    finally:
        await sw.stop()


async def test_an_external_members_vote_is_shown_and_never_counted(make_swarm):
    sw = make_swarm()
    await sw.start(run_agents=False)
    try:
        anna = sw._create_member(USER, "Generalist", name="Anna Smith")
        set_goal(sw)
        counted = sw.forum.goal_vote()["counted"]
        assert counted == 1
        await ok(sw, "join_swarm", {"role": "Helper", "name": "Eve Black"})
        assert sw.forum.goal_vote()["counted"] == counted   # hiring an external member never changes it
        text = await ok(sw, "vote", {"as_agent": "Eve Black", "target": "#1", "value": "like"})
        assert "Your vote is shown, not counted: only the swarm's AI agents' votes count." in text
        text = await ok(sw, "vote", {"target": "#1", "value": "like"})   # the user's too
        assert "Your vote is shown, not counted" in text
        tally = sw.forum.goal_vote()
        assert tally["for"] == [] and not tally["met"] and sw.goal()["status"] == "active"
        post = sw.forum.get(1)
        assert post["likes"] == 2   # shown on #1
        await ok(sw, "retire_agent", {"name": "Eve Black"})
        assert sw.forum.goal_vote()["counted"] == counted
        sw.forum.vote(anna, "#1", "like")
        assert sw.goal()["status"] == "done"   # the one counted agent decides
    finally:
        await sw.stop()


async def test_a_wait_that_times_out_still_brings_the_digest_gathered_so_far(make_swarm):
    sw = make_swarm()
    await sw.start(run_agents=False)
    try:
        anna = sw._create_member(USER, "Generalist", name="Anna Smith")
        set_goal(sw)
        await ok(sw, "join_swarm", {"role": "Helper", "name": "Eve Black"})
        sw.render_wake("Eve Black")
        sw.forum.create(anna, "Plan", "the steps")   # a digest row, far from due
        due = sw.digest_deadline("Eve Black")
        assert due is not None and due > time.time() + 5
        text = await ok(sw, "wait_for_events", {"as_agent": "Eve Black", "timeout": 0})
        assert "From what you follow" in text and 'Anna Smith posted #3 "Plan"' in text
        assert sw.db.scalar("SELECT COUNT(*) FROM notifications WHERE recipient = ? AND delivered_at IS NULL",
                            ("Eve Black",)) == 0
    finally:
        await sw.stop()


def test_the_scheduler_never_batches_an_external_members_digest(make_swarm, no_loop):
    sw = make_swarm()
    sw._create_member(USER, "Generalist", name="Anna Smith")
    sw._create_member(USER, "Helper", name="Eve Black", kind=AgentKind.EXTERNAL)
    set_goal(sw)
    sw.render_wake("Eve Black")
    sw.forum.create("Anna Smith", "Plan", "")
    sw.db.execute("UPDATE notifications SET ts = ts - 10000")
    sw._digest_floor -= 10000
    assert "Eve Black" in sw._due_digests(time.time())
    signal = sw._signals.setdefault("Eve Black", asyncio.Event())
    signal.clear()
    sw._deliver_digests()
    assert not signal.is_set() and not sw.has_pending("Eve Black")
    assert sw.digest_deadline("Eve Black") <= time.time()   # its own wait_for_events ends at once


def test_a_member_at_work_over_mcp_keeps_the_team_from_being_nudged(make_swarm, no_loop):
    sw = make_swarm()
    anna = sw._create_member(USER, "Generalist", name="Anna Smith")
    sw.runtimes[anna] = SimpleNamespace(state=AgentStatus.IDLE, interrupted=False, _recovery=None, llm_steps=0,
                                        wake=lambda: None, reset_llm=lambda: None, stop=_no_stop)
    eve = sw._create_member(USER, "Helper", name="Eve Black", kind=AgentKind.EXTERNAL)
    set_goal(sw)
    for name in (anna, eve, USER):
        sw.render_wake(name)
    sw.db.execute("UPDATE agents SET last_active = ?", (time.time() - 100 * WAIT,))

    def nudges() -> int:
        return int(sw.db.scalar("SELECT COUNT(*) FROM notifications WHERE kind = 'n_thread_quiet'") or 0)

    def later(seconds: float) -> None:
        sw.db.execute("UPDATE agents SET last_active = last_active - ?", (seconds,))
        if sw._thread_quiet is not None:
            sw._thread_quiet = (sw._thread_quiet[0], sw._thread_quiet[1] - seconds)

    sw._external_seen[eve] = time.time()   # hub_call's stamp: Eve works in a long call
    assert not sw.team_asleep()
    sw._check_quiet()
    later(10 * WAIT)
    sw._check_quiet()
    assert nudges() == 0 and sw._thread_quiet is None
    sw._external_seen[eve] = time.time() - EXTERNAL_IDLE_SECONDS - 1   # gone quiet too
    assert sw.team_asleep()
    sw._check_quiet()
    later(10 * WAIT)
    sw._check_quiet()
    assert nudges() == 1
