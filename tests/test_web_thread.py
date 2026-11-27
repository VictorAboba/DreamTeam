"""The thread mode's snapshot and REST (docs/SPEC.md §22.8 "Snapshot and REST"): swarm.mode and goal_rule, the
posts and the goal vote, the members' subscriptions, GET /api/posts/{id}, the user's forum actions through
/api/act and a board session that has none of it. In-process test client, ScriptedLLM, no network."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from dreamteam import web
from dreamteam.config import Config
from dreamteam.forum import GOAL_POST, PLAN_POST
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.models import USER, AgentKind, Status
from dreamteam.swarm import Swarm
from dreamteam.tools import ToolContext, call_tool
from helpers.forum_reads import read_all

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

BASE = "http://127.0.0.1:8765"


def make_swarm(tmp_path: Path, mode: str, **swarm: Any) -> Swarm:
    raw: dict[str, Any] = {
        "swarm": {"name": "Web thread", "language": "en", "start_agents": 3, "max_agents": 6, "max_cost_usd": 0,
                  "mode": mode, "clarify": False, **swarm},
        "agents": {"stagger_seconds": 0},
        "workspace": {"git": False},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
    }
    config = Config.from_dict(tmp_path / "project", raw)
    return Swarm(config=config, llm_factory=lambda profile, agent: ScriptedLLM(lambda *a: "ok", profile))


@pytest.fixture
async def thread(tmp_path):
    sw = make_swarm(tmp_path, "thread")
    await sw.start(run_agents=False)
    try:
        yield sw
    finally:
        await sw.stop()
        sw.close()


@pytest.fixture
async def board(tmp_path):
    sw = make_swarm(tmp_path, "board")
    await sw.start(run_agents=False)
    try:
        yield sw
    finally:
        await sw.stop()
        sw.close()


def llm_names(sw: Swarm) -> list[str]:
    return [m["name"] for m in sw.members() if m["kind"] == AgentKind.LLM]


async def as_member(sw: Swarm, actor: str, name: str, args: dict) -> str:
    text, is_error = await call_tool(ToolContext(sw, actor), name, args)
    assert not is_error, text
    return text


def act(c: TestClient, tool: str, body: dict) -> dict:
    resp = c.post(f"/api/act/{tool}", content=json.dumps(body), headers={"content-type": "application/json"})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_a_board_session_has_no_thread_fields_and_no_posts(board):
    with TestClient(web.create_app(board), base_url=BASE) as c:
        state = c.get("/api/state").json()
        assert state["swarm"]["mode"] == "board" and state["swarm"]["goal_rule"] == "democracy"
        assert state["posts"] == [] and state["goal_vote"] is None
        assert state["agents"] and all(a["subscriptions"] == [] for a in state["agents"])
        assert state["swarm"]["clarify_wait"] is None
        for path in ("/api/posts/1", "/api/posts/3", "/api/posts", "/api/posts/x/y", "/api/posts/1?since_id=c2"):
            resp = c.get(path)
            assert resp.status_code == 404 and resp.json()["error"], path
        # the forum tools are refused there, as every thread tool
        result = act(c, "post_create", {"title": "Hello"})
        assert result["ok"] is False


async def test_the_snapshot_of_a_thread_session(thread):
    a, b, c_ = llm_names(thread)
    thread.set_goal(USER, "Build the parser", "With tests.")
    first = thread.forum.create(a, "Lexer first", "The plan.")
    read_all(thread, b)
    second = thread.forum.create(b, "Parser", "")
    read_all(thread, c_)
    thread.forum.comment(c_, first["id"], "Agreed")
    thread.forum.vote(USER, f"#{first['id']}", "like")
    thread.forum.vote(USER, f"#{second['id']}", "dislike")
    thread.forum.vote(a, "#1", "like")
    # the comment made the first post the more recently active one (shifted: time.time() may not move on Windows)
    thread.db.execute("UPDATE posts SET active_at = active_at + 10 WHERE id = ?", (first["id"],))
    with TestClient(web.create_app(thread), base_url=BASE) as client:
        state = client.get("/api/state").json()
    swarm = state["swarm"]
    assert swarm["mode"] == "thread" and swarm["goal_rule"] == "democracy"
    assert state["tasks"] == [] and set(swarm["counts"]) == set(Status.ALL)
    assert all(n == 0 for n in swarm["counts"].values())
    assert all(agent["open_tasks"] == [] for agent in state["agents"])
    assert swarm["clarify_wait"] is None
    posts = state["posts"]
    # the system posts first, then by activity
    assert [p["id"] for p in posts] == [GOAL_POST, PLAN_POST, first["id"], second["id"]]
    assert set(posts[0]) == {"id", "kind", "title", "author", "created_at", "edited_at", "active_at", "likes",
                             "dislikes", "comments", "my_vote"}
    assert all("text" not in p for p in posts)
    by_id = {p["id"]: p for p in posts}
    assert (by_id[GOAL_POST]["kind"], by_id[GOAL_POST]["author"], by_id[GOAL_POST]["likes"]) == ("goal", None, 1)
    assert by_id[first["id"]]["my_vote"] == "like" and by_id[first["id"]]["comments"] == 1
    assert by_id[second["id"]]["my_vote"] == "dislike" and by_id[second["id"]]["dislikes"] == 1
    assert by_id[GOAL_POST]["my_vote"] is None
    assert state["goal_vote"] == {"rule": "democracy", "counted": 3, "for": [a], "against": [], "needed": 2,
                                  "met": False}
    subs = {agent["name"]: agent["subscriptions"] for agent in state["agents"]}
    assert [(s["kind"], s["target"], s["by"], s["mode"], s["auto"]) for s in subs[a]] == [
        ("feed", "", "", "digest", True), ("reactions", "", "", "digest", True), ("post", "1", "", "digest", True),
        ("post", "2", "", "digest", True)]
    assert set(subs[a][0]) == {"id", "kind", "target", "by", "mode", "auto"}
    await as_member(thread, b, "subscribe", {"what": "post", "target": first["id"], "by": a, "mode": "wake"})
    mine = thread.snapshot()["agents"]
    row = next(s for agent in mine if agent["name"] == b for s in agent["subscriptions"] if s["by"] == a)
    assert (row["kind"], row["target"], row["mode"], row["auto"]) == ("post", str(first["id"]), "wake", False)


async def test_a_snapshot_poll_costs_a_fixed_number_of_queries(thread):
    a, b, _ = llm_names(thread)
    thread.set_goal(USER, "Build the parser")

    def queries() -> int:
        counted: list[str] = []
        thread.db.conn.set_trace_callback(counted.append)
        try:
            thread.snapshot()
        finally:
            thread.db.conn.set_trace_callback(None)
        return len(counted)

    few = queries()
    for n in range(12):
        read_all(thread, a, b)
        post = thread.forum.create(a if n % 2 else b, f"Post {n}", "text")
        read_all(thread, a, b)
        thread.forum.comment(b if n % 2 else a, post["id"], "comment")
        thread.forum.vote(USER, f"#{post['id']}", "like")
    await as_member(thread, a, "subscribe", {"what": "file", "target": "src"})
    assert queries() == few


async def test_a_post_and_its_comments(thread):
    a, b, c_ = llm_names(thread)
    thread.set_goal(USER, "Build the parser")
    post = thread.forum.create(a, "Lexer first", "The **plan**.")
    read_all(thread, b)
    one = thread.forum.comment(b, post["id"], "Agreed")
    read_all(thread, c_)
    reply = thread.forum.comment(c_, post["id"], "Me too", reply_to=one["id"])
    thread.forum.vote(USER, f"c{one['id']}", "like")
    thread.forum.vote(b, f"#{post['id']}", "dislike")
    thread.forum.vote(a, "#1", "like")
    thread.forum.vote(USER, "#1", "like")
    with TestClient(web.create_app(thread), base_url=BASE) as client:
        data = client.get(f"/api/posts/{post['id']}").json()
        assert set(data) == {"post", "comments", "votes"}
        assert data["post"] == {**thread.forum.get(post["id"]), "my_vote": None}
        assert data["post"]["text"] == "The **plan**." and data["post"]["comments"] == 2
        assert data["comments"] == [
            {"id": one["id"], "author": b, "text": "Agreed", "reply_to": None, "ts": one["ts"], "likes": 1,
             "dislikes": 0, "my_vote": "like"},
            {"id": reply["id"], "author": c_, "text": "Me too", "reply_to": one["id"], "ts": reply["ts"], "likes": 0,
             "dislikes": 0, "my_vote": None}]
        assert data["votes"] == [{"voter": b, "value": "dislike", "counted": False}]
        later = client.get(f"/api/posts/{post['id']}?since_id={one['id']}").json()
        assert [c["id"] for c in later["comments"]] == [reply["id"]]
        assert client.get(f"/api/posts/%23{post['id']}?since_id=c{reply['id']}").json()["comments"] == []
        goal = client.get("/api/posts/1").json()
        assert goal["post"]["kind"] == "goal" and goal["post"]["my_vote"] == "like"
        assert goal["votes"] == [{"voter": a, "value": "like", "counted": True},
                                 {"voter": USER, "value": "like", "counted": False}]
        assert goal["goal_vote"] == thread.forum.goal_vote() and goal["goal_vote"]["for"] == [a]
        assert "goal_vote" not in client.get("/api/posts/2").json()
        for path in ("/api/posts/99", "/api/posts/abc", "/api/posts", "/api/posts/3/more"):
            resp = client.get(path)
            assert resp.status_code == 404 and resp.json()["error"], path
        assert client.get("/api/posts/99").json()["error"] == "No post #99"
        resp = client.get(f"/api/posts/{post['id']}?since_id=x1")
        assert resp.status_code == 400 and resp.json()["error"]


async def test_the_user_writes_the_forum_and_completes_the_goal(thread):
    a, b, _ = llm_names(thread)
    thread.set_goal(USER, "Build the parser")
    agent_post = thread.forum.create(a, "Lexer first", "")
    with TestClient(web.create_app(thread), base_url=BASE) as client:
        made = act(client, "post_create", {"title": "Use a PEG", "text": "Why not?"})
        assert made["ok"], made
        mine = next(p for p in client.get("/api/state").json()["posts"] if p["author"] == USER)
        assert mine["title"] == "Use a PEG" and mine["id"] > agent_post["id"]
        assert act(client, "post_update", {"post_id": mine["id"], "title": "Use a PEG parser"})["ok"]
        assert act(client, "post_update", {"post_id": agent_post["id"], "text": "Edited by the user"})["ok"]
        assert not act(client, "post_update", {"post_id": 1, "text": "x"})["ok"]   # a system post
        assert act(client, "comment", {"post_id": agent_post["id"], "text": "Looks good"})["ok"]
        comment = client.get(f"/api/posts/{agent_post['id']}").json()["comments"][0]
        assert act(client, "comment", {"post_id": agent_post["id"], "text": "Also", "reply_to": f"c{comment['id']}"})[
            "ok"]
        assert act(client, "vote", {"target": f"#{agent_post['id']}", "value": "like"})["ok"]
        assert not act(client, "vote", {"target": f"#{mine['id']}", "value": "like"})["ok"]   # its own post
        detail = client.get(f"/api/posts/{agent_post['id']}").json()
        assert detail["post"]["my_vote"] == "like" and detail["post"]["text"] == "Edited by the user"
        assert [c["reply_to"] for c in detail["comments"]] == [None, comment["id"]]
        assert act(client, "vote", {"target": f"#{agent_post['id']}", "value": "none"})["ok"]
        assert client.get(f"/api/posts/{agent_post['id']}").json()["post"]["my_vote"] is None
        assert "Use a PEG parser" in act(client, "feed", {})["result"]
        assert "Lexer first" in act(client, "post_read", {"post_id": agent_post["id"]})["result"]
        # the user completes the goal: the decision is in the snapshot's goal while it is done
        done = act(client, "swarm_control", {"complete_goal": True, "goal_summary": "Good enough"})
        assert done["ok"], done
        goal = client.get("/api/state").json()["swarm"]["goal"]
        assert goal["status"] == "done" and goal["summary"] == "Good enough"
        assert goal["decision"] == {"rule": "user", "for": [], "against": [], "counted": 3, "wrap_up": False}
        assert act(client, "swarm_control", {"reopen_goal_comment": "-"})["ok"]
        state = client.get("/api/state").json()
        assert state["swarm"]["goal"]["status"] == "active" and "decision" not in state["swarm"]["goal"]
        assert state["goal_vote"]["for"] == [] and client.get("/api/posts/1").json()["comments"] == []
