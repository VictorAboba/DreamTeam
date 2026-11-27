"""A thread session from outside, end to end (docs/SPEC.md §22.9): created through the HTTP API v1 with a goal rule,
read back through the options and the summary, instructed with `continue` (a post of the user), seen through the
launcher MCP and the hub MCP (the thread tools with their marks, the one refusal of a board tool), joined over MCP by
an external member whose vote on #1 is shown and not counted, completed by the user through PATCH, its result read,
exported and imported into another home, where v1 and MCP answer as before. A board session goes the same way and is
unchanged. ScriptedLLM, tmp_path homes, in-process clients only: no port, no network."""

from __future__ import annotations

import json
from typing import Any

import pytest
from mcp import Client

from dreamteam.forum import GOAL_POST, PLAN_POST
from dreamteam.hub_mcp import INSTRUCTIONS, INSTRUCTIONS_THREAD, MODE_ONLY, build_hub_server, hub_specs
from dreamteam.launcher_mcp import build_launcher_server
from dreamteam.models import USER, AgentKind
from dreamteam.sessions import read_posts
from dreamteam.tools import REGISTRY
from dreamteam.transfer import MANIFEST, apply_import, export_home, plan_import
from test_api_v1 import MANUAL, Hub, _from_user, _home, _manager
from test_api_v1_guard import api_token, hub_token  # noqa: F401 - fixtures

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

BOARD_REFUSAL = ("Tool 'task_create' is for board-mode sessions; this session works in the thread mode (no tasks): "
                 "post to the feed with post_create.")
THREAD_REFUSAL = "Tool 'post_create' is for thread-mode sessions; this session uses the task board: task_create."
NOT_COUNTED = "Your vote is shown, not counted: only the swarm's AI agents' votes count."
# what a session's v1 summary keeps across an export and an import
KEPT = ("id", "title", "state", "mode", "goal", "goal_vote", "posts", "tasks", "language", "model", "max_cost_usd",
        "answers", "stop_when_done")
RESULT_KEPT = ("status", "session", "goal", "summary", "tasks", "agents", "mode", "posts", "decision")


def text_of(result: Any) -> str:
    return "\n".join(part.text for part in result.content)


async def get(hub: Hub, path: str, **kw: Any) -> dict[str, Any]:
    status, answer = await hub.call("GET", path, **kw)
    assert status == 200, (path, answer)
    return answer


def externals(swarm) -> list[str]:
    return [m["name"] for m in swarm.members() if m["kind"] == AgentKind.EXTERNAL]


def marked(tools: dict[str, Any], mode: str) -> set[str]:
    return {name for name, tool in tools.items() if tool.description.endswith(MODE_ONLY[mode])}


async def call(client: Client, name: str, args: dict[str, Any]) -> tuple[str, bool]:
    result = await client.call_tool(name, args)
    return text_of(result), bool(result.is_error)


async def test_a_thread_session_from_outside_and_a_board_session_through_the_same_paths(tmp_path, api_token):
    home = _home(tmp_path)
    hub = Hub(_manager(home))
    try:
        # ---- create through v1, with the veto rule; the options and the summary say so
        status, answer = await hub.create({"goal": "Write a haiku\nAbout spring.", "mode": "thread",
                                           "goal_rule": "veto", "start_agents": 2, **MANUAL})
        assert status == 202, answer
        status, made_board = await hub.create({"goal": "Write a limerick", **MANUAL})
        assert status == 202, made_board
        await hub.settled()
        uid, sid = answer["session"]["id"], answer["session"]["name"]
        board_uid, board_sid = made_board["session"]["id"], made_board["session"]["name"]
        swarm, board = hub.manager.swarms[sid], hub.manager.swarms[board_sid]
        assert swarm.mode == "thread" and board.mode == "board"
        assert hub.manager.meta(sid)["overrides"]["swarm"] == {"mode": "thread", "goal_rule": "veto",
                                                               "start_agents": 2}
        assert "mode" not in (hub.manager.meta(board_sid)["overrides"].get("swarm") or {})
        options = await get(hub, "/api/v1/options")
        assert (options["modes"], options["mode"]) == (["board", "thread"], "board")
        assert (options["goal_rules"], options["goal_rule"]) == (["democracy", "veto"], "democracy")
        session = (await get(hub, f"/api/v1/sessions/{uid}"))["session"]
        assert session["state"] == "running" and session["mode"] == "thread" and session["tasks"] == {}
        assert session["posts"] == 0 and session["goal"]["decision"] is None
        assert session["goal_vote"] == {"rule": "veto", "counted": 2, "for": [], "against": [], "needed": 2,
                                        "met": False}
        board_session = (await get(hub, f"/api/v1/sessions/{board_uid}"))["session"]
        assert board_session["mode"] == "board" and board_session["goal_vote"] is None
        assert board_session["posts"] == 0 and board_session["goal"]["decision"] is None

        # ---- an outside instruction: a post of the user in a thread session, a message in a board one
        sent = len(_from_user(swarm))
        status, answer = await hub.call("POST", f"/api/v1/sessions/{uid}/continue",
                                        body={"text": "Use the word cherry\nIn the last line."})
        assert status == 200 and answer["session"]["posts"] == 1, answer
        (post,) = [p for p in swarm.forum.listing() if p["kind"] == "post"]
        assert (post["author"], post["title"]) == (USER, "Use the word cherry")
        assert swarm.forum.get(post["id"])["text"] == "In the last line."
        assert len(_from_user(swarm)) == sent   # (no message to a coordinator)
        sent = len(_from_user(board))
        status, answer = await hub.call("POST", f"/api/v1/sessions/{board_uid}/continue",
                                        body={"text": "Make it rhyme."})
        assert status == 200 and answer["session"]["posts"] == 0, answer
        assert len(_from_user(board)) == sent + 1 and _from_user(board)[-1][1] == "Make it rhyme."

        # ---- the launcher MCP: both modes' tools once, marked; the one refusal; the thread line of sessions_list
        async with Client(build_launcher_server(hub.manager)) as launcher:
            tools = {tool.name: tool for tool in (await launcher.list_tools()).tools}
            only_board = {s.name for s in hub_specs(None) if s.modes == frozenset({"board"})}
            only_thread = {s.name for s in hub_specs(None) if s.modes == frozenset({"thread"})}
            assert marked(tools, "board") == only_board and marked(tools, "thread") == only_thread
            assert {"task_create", "goal_complete"} <= only_board and {"feed", "post_create", "vote"} <= only_thread
            assert "complete_goal" in tools["swarm_control"].input_schema["properties"]
            assert {"mode", "goal_rule"} <= set(tools["session_create"].input_schema["properties"])
            # every tool marked for one mode is refused in a session of the other, with call_tool's one text
            for name in sorted(only_board):
                text, error = await call(launcher, name, {"session": sid})
                assert error and text.startswith(f"Tool '{name}' is for board-mode sessions; this session works "
                                                 "in the thread mode"), (name, text)
            for name in sorted(only_thread):
                text, error = await call(launcher, name, {"session": board_sid})
                assert error and text.startswith(f"Tool '{name}' is for thread-mode sessions"), (name, text)
            assert await call(launcher, "task_create", {"session": sid, "title": "x"}) == (BOARD_REFUSAL, True)
            assert await call(launcher, "post_create", {"session": board_sid, "title": "x"}) == (THREAD_REFUSAL, True)
            assert swarm.db.scalar("SELECT COUNT(*) FROM tasks") == 0
            lines = (await call(launcher, "sessions_list", {}))[0].splitlines()
            (line,) = [line for line in lines if line.startswith(f"{sid} [")]
            assert " | thread (veto) | posts 1 | goal vote 0/2 for (needs 2) | agents 2" in line, line
            (board_line,) = [line for line in lines if line.startswith(f"{board_sid} [")]
            assert " | tasks " in board_line and " thread " not in board_line, board_line

            # ---- the hub MCP of each session: its own mode's tools, no marks, the same refusal
            async with Client(build_hub_server(swarm)) as thread_mcp, Client(build_hub_server(board)) as board_mcp:
                assert thread_mcp.instructions == INSTRUCTIONS_THREAD and board_mcp.instructions == INSTRUCTIONS
                thread_tools = {tool.name: tool for tool in (await thread_mcp.list_tools()).tools}
                board_tools = {tool.name: tool for tool in (await board_mcp.list_tools()).tools}
                assert not only_board & set(thread_tools) and only_thread <= set(thread_tools)
                assert not only_thread & set(board_tools) and only_board <= set(board_tools)
                assert set(thread_tools) | set(board_tools) == {s.name for s in hub_specs(None)}
                for tool in (*thread_tools.values(), *board_tools.values()):
                    assert not any(mark in tool.description for mark in MODE_ONLY.values()), tool.name
                assert "complete_goal" in thread_tools["swarm_control"].input_schema["properties"]
                assert "complete_goal" not in board_tools["swarm_control"].input_schema["properties"]
                assert "task" not in thread_tools["agent_traces"].input_schema["properties"]
                assert thread_tools["feed"].description.startswith(REGISTRY["feed"].describe("en", "thread"))
                assert await call(thread_mcp, "task_create", {"title": "x"}) == (BOARD_REFUSAL, True)
                assert await call(board_mcp, "post_create", {"title": "x"}) == (THREAD_REFUSAL, True)

                # ---- an external member joins over MCP and votes on #1: shown, never counted
                text, error = await call(thread_mcp, "join_swarm", {"role": "Helper"})
                assert not error, text
                (member,) = externals(swarm)
                assert member in text
                text, error = await call(thread_mcp, "vote", {"as_agent": member, "target": "#1", "value": "like"})
                assert not error and NOT_COUNTED in text, text
            text, error = await call(launcher, "vote", {"session": sid, "target": "#1", "value": "like"})
            assert not error and "Your vote is shown, not counted" in text, text   # the user's, over the launcher
            session = (await get(hub, f"/api/v1/sessions/{uid}"))["session"]
            assert session["state"] == "running" and session["goal"]["status"] == "active"
            assert session["goal_vote"]["counted"] == 2 and session["goal_vote"]["for"] == []
            assert not session["goal_vote"]["met"]
            lines = (await call(launcher, "sessions_list", {}))[0].splitlines()
            (line,) = [line for line in lines if line.startswith(f"{sid} [")]
            assert " | goal vote 0/2 for (needs 2) | agents 3" in line, line
        result = await get(hub, f"/api/v1/sessions/{uid}/result")
        assert [p["id"] for p in result["posts"]] == [GOAL_POST, PLAN_POST, post["id"]]
        assert result["posts"][0]["likes"] == 2 and result["decision"] is None   # (shown on #1)

        # ---- the user completes the goal through v1 PATCH; a board session refuses it
        status, answer = await hub.call("PATCH", f"/api/v1/sessions/{board_uid}", body={"complete_goal": True})
        assert status == 400 and answer["code"] == "bad_request", answer
        assert board.goal()["status"] == "active"
        status, answer = await hub.call("PATCH", f"/api/v1/sessions/{uid}",
                                        body={"complete_goal": True, "goal_summary": "The haiku is in haiku.txt."})
        assert status == 200, answer
        session = answer["session"]
        assert session["state"] == "done" and session["goal"]["summary"] == "The haiku is in haiku.txt."
        decision = session["goal"]["decision"]
        assert decision["rule"] == "user" and decision["for"] == [] and decision["counted"] == 2
        result = await get(hub, f"/api/v1/sessions/{uid}/result")
        assert result["status"] == "done" and result["mode"] == "thread" and result["tasks"] == []
        assert result["decision"] == decision and result["summary"] == "The haiku is in haiku.txt."
        assert [p["id"] for p in result["posts"]] == [GOAL_POST, PLAN_POST, post["id"]]
        assert list(result)[list(result).index("jobs") + 1:][:3] == ["mode", "posts", "decision"]
        board_result = await get(hub, f"/api/v1/sessions/{board_uid}/result")
        assert board_result["mode"] == "board" and board_result["posts"] == [] and board_result["decision"] is None
        assert board_result["tasks"], board_result   # (the planner's task)

        for one in (uid, board_uid):
            assert (await hub.call("POST", f"/api/v1/sessions/{one}/stop"))[0] == 200
        before = {one: (await get(hub, f"/api/v1/sessions/{one}"))["session"] for one in (uid, board_uid)}
        results = {one: await get(hub, f"/api/v1/sessions/{one}/result") for one in (uid, board_uid)}
        posts = read_posts(hub.manager._data_dir(sid) / "swarm.db")
        assert [p["id"] for p in posts] == [GOAL_POST, PLAN_POST, post["id"]]
    finally:
        await hub.close()

    # ---- export both, import them into another home
    dest = tmp_path / "usb"
    report = export_home(home, dest, include=["sessions"])
    assert report.ok
    manifest = json.loads((dest / MANIFEST).read_text(encoding="utf-8"))
    assert manifest["schema"]["toolset_revisions"] == {"board": 5, "thread": 6}
    assert {e["id"]: e["swarm_mode"] for e in manifest["sessions"]} == {sid: "thread", board_sid: "board"}
    (tmp_path / "other").mkdir()
    other = _manager(_home(tmp_path / "other"))
    plan = plan_import(other, dest)
    applied = await apply_import(other, plan)
    assert applied.ok, [(i.name, i.status, i.error) for i in applied.items]
    hub = Hub(other)
    try:
        assert other.stored_mode(sid) == "thread" and other.stored_mode(board_sid) == "board"
        assert other.meta(sid)["overrides"]["swarm"]["mode"] == "thread"
        assert "mode" not in (other.meta(board_sid)["overrides"].get("swarm") or {})
        for one in (uid, board_uid):
            after = (await get(hub, f"/api/v1/sessions/{one}"))["session"]
            assert {key: after[key] for key in KEPT} == {key: before[one][key] for key in KEPT}, one
            kept = await get(hub, f"/api/v1/sessions/{one}/result")
            assert {key: kept[key] for key in RESULT_KEPT} == {key: results[one][key] for key in RESULT_KEPT}, one
        assert sid not in other.swarms   # (read from its file)
        assert read_posts(other._data_dir(sid) / "swarm.db") == posts

        # ---- the imported sessions answer MCP as before
        async with Client(build_launcher_server(other)) as launcher:
            for one in (sid, board_sid):
                text, error = await call(launcher, "session_start", {"session": one})
                assert not error, text
            swarm, board = other.swarms[sid], other.swarms[board_sid]
            assert swarm.mode == "thread" and board.mode == "board"
            assert swarm.forum.goal_vote()["rule"] == "veto"
            text, error = await call(launcher, "feed", {"session": sid})
            assert not error and "Use the word cherry" in text, text
            assert await call(launcher, "task_create", {"session": sid, "title": "x"}) == (BOARD_REFUSAL, True)
            assert await call(launcher, "post_create", {"session": board_sid, "title": "x"}) == (THREAD_REFUSAL, True)
            lines = (await call(launcher, "sessions_list", {}))[0].splitlines()
            (line,) = [line for line in lines if line.startswith(f"{sid} [")]
            assert " | thread (veto) | posts 1 | goal vote 0/2 for (needs 2)" in line, line
            async with Client(build_hub_server(swarm)) as thread_mcp, Client(build_hub_server(board)) as board_mcp:
                assert thread_mcp.instructions == INSTRUCTIONS_THREAD and board_mcp.instructions == INSTRUCTIONS
                names = {tool.name for tool in (await thread_mcp.list_tools()).tools}
                assert "feed" in names and "task_create" not in names
                names = {tool.name for tool in (await board_mcp.list_tools()).tools}
                assert "task_create" in names and "feed" not in names
        status, answer = await hub.call("PATCH", f"/api/v1/sessions/{uid}", body={"complete_goal": True})
        assert status == 409 and answer["code"] == "conflict" and "already" in answer["error"], answer
        status, answer = await hub.call("PATCH", f"/api/v1/sessions/{board_uid}", body={"goal_rule": "veto"})
        assert status == 400 and answer["code"] == "bad_request", answer
        session = (await get(hub, f"/api/v1/sessions/{uid}"))["session"]
        assert session["mode"] == "thread" and session["goal"]["decision"] == decision
    finally:
        await hub.close()
