"""The thread mode's surfaces end to end (docs/SPEC.md §22.8): a thread session made through the launcher's sessions
REST with its goal rule, the PATCH refusals of its mode and rule, a start with ScriptedLLM agents, the snapshot (mode,
posts #1 and #2, the vote on #1), the user's post, comment, votes and the completed goal through /api/act, the forum
events on the event stream and the WebSocket, the same events in the three describers (eventtext, the TUI, app.js),
the TUI client's render of that snapshot, and a board session whose snapshot and REST keep their board shape.
In-process test clients, ScriptedLLM, no network, no real port."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Callable

import httpx2
import pytest
from starlette.testclient import TestClient
from test_ui import NODE, run_js

from dreamteam import eventtext
from dreamteam import settings as st
from dreamteam.launcher import create_launcher_app
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.sessions import SessionManager
from dreamteam.tui import render
from dreamteam.tui.client import HubClient
from dreamteam.tui.render import RenderContext, to_plain

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

BASE = "http://127.0.0.1:8765"
WS = "ws://127.0.0.1:8765"   # (TestClient's websocket_connect ignores base_url)
FORUM_EVENTS = ("post.created", "post.updated", "post.comment", "post.vote", "subscription.changed",
                "files.committed")
WAIT_CALL = {"name": "wait", "arguments": {}}

# the snapshot's keys before the thread mode, and what §22.8 adds to them (a board session's values: below)
OLD_SWARM_KEYS = {"name", "language", "paused", "pause_reason", "wrap_up", "limits", "agent_count", "spent_usd", "goal",
                  "counts", "mcp_url", "models", "mcp_token", "session", "tracing", "independent_review", "workspace",
                  "network", "question", "clarify", "auto_reply", "clarify_wait"}
OLD_TOP_KEYS = {"swarm", "tasks", "agents", "triggers", "jobs", "mcp", "skills", "locks", "user"}
OLD_AGENT_KEYS = {"name", "kind", "role", "status", "model", "language", "cost_usd", "tokens_in", "tokens_out",
                  "hired_by", "created_at", "last_active", "instructions", "context_tokens", "open_tasks", "unread",
                  "jobs"}


class Team:
    """The agents' scripts: the first agent that wakes posts «Lexer first» and likes #1 once; every turn ends in
    `wait`."""

    def __init__(self) -> None:
        self.first: str | None = None
        self.acted = False

    def factory(self, profile: Any, agent: str) -> ScriptedLLM:
        def handler(system: str, messages: list[dict], tools: list[dict]) -> dict:
            names = {t.get("name") for t in tools}
            if not self.acted and "post_create" in names:
                self.acted, self.first = True, agent
                return {"tool_calls": [
                    {"name": "post_create", "arguments": {"title": "Lexer first", "text": "The lexer, then the rest."}},
                    {"name": "vote", "arguments": {"target": "#1", "value": "like"}}, WAIT_CALL]}
            return {"tool_calls": [WAIT_CALL]} if "wait" in names else "Nothing to do."

        return ScriptedLLM(handler, profile)


def make_manager(tmp_path: Path, team: Team) -> SessionManager:
    home = tmp_path / "home"
    home.mkdir()
    st.write_raw(home, {
        "swarm": {"name": "Home", "language": "en", "start_agents": 2, "max_agents": 3, "max_cost_usd": 0,
                  "clarify": False},
        "agents": {"stagger_seconds": 0, "idle_nudge_seconds": 3600},
        "workspace": {"git": False},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
    })
    return SessionManager(home, llm_factory=team.factory)


def send(c: TestClient, method: str, path: str, body: Any) -> Any:
    return c.request(method, path, content=json.dumps(body), headers={"content-type": "application/json"})


def act(c: TestClient, sid: str, tool: str, body: dict) -> dict:
    resp = send(c, "POST", f"/s/{sid}/api/act/{tool}", body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def until(predicate: Callable[[], Any], what: str, timeout: float = 30.0) -> Any:
    """Polls a condition the running swarm reaches by itself (its agents' turns), not the passing of time."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"not reached in time: {what}")


def hub_client(c: TestClient) -> HubClient:
    """The TUI's HubClient whose HTTP goes to the launcher app in process (no port)."""
    def forward(request: httpx2.Request) -> httpx2.Response:
        url = request.url
        path = url.raw_path.decode("ascii")
        headers = {"content-type": request.headers["content-type"]} if "content-type" in request.headers else {}
        resp = c.request(request.method, path, content=request.content or None, headers=headers)
        return httpx2.Response(resp.status_code, content=resp.content,
                               headers={"content-type": resp.headers.get("content-type", "application/json")})

    return HubClient(BASE, http=httpx2.AsyncClient(transport=httpx2.MockTransport(forward)))


def plain(renderable: Any) -> str:
    return " ".join(to_plain(renderable, 120).split())


def test_a_thread_session_through_every_surface(tmp_path):
    team = Team()
    manager = make_manager(tmp_path, team)
    with TestClient(create_launcher_app(manager), base_url=BASE) as c:
        try:
            # ---- created through the sessions REST, with its own rule
            made = send(c, "POST", "/api/sessions", {"title": "Parser", "goal": "Build the parser",
                                                     "description": "With tests.",
                                                     "overrides": {"swarm": {"mode": "thread", "goal_rule": "veto"}}})
            assert made.status_code == 201, made.text
            session = made.json()["session"]
            sid = session["id"]
            assert (session["mode"], session["goal_rule"], session["status"]) == ("thread", "veto", "running")
            assert session["overrides"]["swarm"] == {"mode": "thread", "goal_rule": "veto"}
            assert session["goal"]["decision"] is None

            # ---- the mode and the rule stay: PATCH refusals, nothing written
            before = c.get(f"/api/sessions/{sid}").json()
            assert before["config"]["swarm"]["mode"] == "thread"
            fixed = "the mode is chosen when the session is created"
            for body, text in (({"settings": {"swarm": {"mode": "board"}}}, fixed),
                               ({"settings": {"swarm": {"mode": None}}}, fixed),
                               ({"overrides": {"swarm": {"mode": "board", "goal_rule": "veto"}}}, fixed),
                               ({"settings": {"swarm": {"goal_rule": ""}}},
                                "the goal rule of a thread session cannot be cleared")):
                resp = send(c, "PATCH", f"/api/sessions/{sid}", body)
                assert resp.status_code == 400 and resp.json() == {"error": text}, (body, resp.text)
            after = c.get(f"/api/sessions/{sid}").json()
            assert after["session"]["overrides"] == before["session"]["overrides"]
            assert after["config"]["swarm"]["goal_rule"] == "veto"

            # ---- the agents run (ScriptedLLM): one posts and likes #1, which veto does not count as met
            def voted() -> dict | None:
                state = c.get(f"/s/{sid}/api/state").json()
                return state if state["goal_vote"] and state["goal_vote"]["for"] and len(state["posts"]) >= 3 else None

            state = until(voted, "the agent's post and vote")
            agent = team.first
            swarm = state["swarm"]
            assert (swarm["mode"], swarm["goal_rule"]) == ("thread", "veto")
            assert swarm["goal"]["status"] == "active" and "decision" not in swarm["goal"]
            assert state["tasks"] == [] and all(n == 0 for n in swarm["counts"].values())
            assert swarm["clarify_wait"] is None
            posts = state["posts"]
            assert [(p["id"], p["kind"], p["author"]) for p in posts[:2]] == [(1, "goal", None), (2, "plan", None)]
            lexer = next(p for p in posts if p["title"] == "Lexer first")
            assert lexer["kind"] == "post" and lexer["author"] == agent and lexer["my_vote"] is None
            assert all("text" not in p for p in posts)
            names = [a["name"] for a in state["agents"] if a["kind"] == "llm"]
            assert len(names) == 2 and agent in names
            assert state["goal_vote"] == {"rule": "veto", "counted": 2, "for": [agent], "against": [], "needed": 2,
                                          "met": False}
            mine = next(a for a in state["agents"] if a["name"] == agent)
            assert {(s["kind"], s["target"], s["auto"]) for s in mine["subscriptions"]} == {
                ("feed", "", True), ("reactions", "", True), ("post", "1", True), ("post", "2", True)}
            assert all(a["open_tasks"] == [] for a in state["agents"])
            goal_post = c.get(f"/s/{sid}/api/posts/1").json()
            assert goal_post["post"]["kind"] == "goal" and goal_post["goal_vote"] == state["goal_vote"]
            assert goal_post["votes"] == [{"voter": agent, "value": "like", "counted": True}]

            # ---- the user writes the thread through /api/act, with the WebSocket open
            with c.websocket_connect(f"{WS}/s/{sid}/ws") as ws:
                made_post = act(c, sid, "post_create", {"title": "Which tests?", "text": f"@{agent} unit or golden?"})
                assert made_post["ok"], made_post
                own = int(made_post["result"].split("#", 1)[1].split()[0])
                said = act(c, sid, "comment", {"post_id": lexer["id"], "text": "Agreed, lexer first."})
                assert said["ok"], said
                detail = c.get(f"/s/{sid}/api/posts/{lexer['id']}").json()
                first = detail["comments"][-1]
                assert (first["author"], first["text"], first["reply_to"]) == ("user", "Agreed, lexer first.", None)
                reply = act(c, sid, "comment", {"post_id": lexer["id"], "text": "And a test per token.",
                                                "reply_to": f"c{first['id']}"})
                assert reply["ok"], reply
                later = c.get(f"/s/{sid}/api/posts/{lexer['id']}?since_id=c{first['id']}").json()["comments"]
                assert [(x["text"], x["reply_to"]) for x in later] == [("And a test per token.", first["id"])]
                assert act(c, sid, "vote", {"target": f"#{lexer['id']}", "value": "like"})["ok"]
                refused = act(c, sid, "vote", {"target": f"#{own}", "value": "like"})   # never one's own post
                assert refused["ok"] is False and refused["result"].startswith("Error:")
                assert act(c, sid, "vote", {"target": "#1", "value": "like"})["ok"]    # shown, never counted
                one = c.get(f"/s/{sid}/api/state").json()
                assert one["goal_vote"]["for"] == [agent] and one["goal_vote"]["met"] is False
                assert next(p for p in one["posts"] if p["id"] == lexer["id"])["my_vote"] == "like"
                assert next(p for p in one["posts"] if p["id"] == own)["author"] == "user"
                # #1 and #2 are not the user's to edit; an ordinary post of an agent is
                assert act(c, sid, "post_update", {"post_id": 1, "title": "Mine"})["ok"] is False
                assert act(c, sid, "post_update", {"post_id": lexer["id"], "title": "Lexer first!"})["ok"]
                done = act(c, sid, "swarm_control", {"complete_goal": True, "goal_summary": "Parser done by hand"})
                assert done["ok"], done
                assert act(c, sid, "vote", {"target": "#1", "value": "none"})["ok"] is False   # closed once done
                streamed: list[dict] = []
                while not any(e["type"] == "goal.completed" for e in streamed) and len(streamed) < 5000:
                    streamed.append(json.loads(ws.receive_text()))
            types = {e["type"] for e in streamed}
            assert {"post.created", "post.comment", "post.vote", "post.updated", "goal.completed"} <= types
            created = next(e for e in streamed if e["type"] == "post.created")
            assert created["actor"] == "user" and created["data"]["post"]["id"] == own

            # ---- the decision in the snapshot, the summary and the event stream
            final = c.get(f"/s/{sid}/api/state").json()
            decision = final["swarm"]["goal"]["decision"]
            assert final["swarm"]["goal"]["status"] == "done" and decision["rule"] == "user"
            summary = c.get(f"/api/sessions/{sid}").json()["session"]
            assert (summary["mode"], summary["goal_rule"], summary["posts"]) == ("thread", "veto", 2)
            assert summary["goal"]["decision"]["rule"] == "user" and summary["goal_vote"]["rule"] == "veto"
            events = c.get(f"/s/{sid}/api/events", params={"limit": 2000}).json()["events"]
            kinds = [e["type"] for e in events]
            for kind in ("post.created", "post.updated", "post.comment", "post.vote", "subscription.changed",
                         "goal.completed"):
                assert kind in kinds, kind
            completed = next(e for e in events if e["type"] == "goal.completed")
            assert completed["data"]["goal"]["decision"]["rule"] == "user"
            notices = [e["data"] for e in events
                       if e["type"] == "notification" and e["data"].get("recipient") == "user"]
            assert not [n for n in notices if n.get("kind") == "u_goal_decided"]   # the user decided it
            forum_events = [e for e in events if e["type"] in FORUM_EVENTS or e["type"] == "goal.completed"]

            # ---- the same events in the three describers: eventtext, the TUI's renderer, app.js
            for ev in forum_events:
                assert eventtext.event_text(ev, "en"), ev["type"]
                assert eventtext.event_text(ev, "ru"), ev["type"]
                assert render.event(RenderContext(lang="en", verbosity="verbose", width=120), ev) is not None, ev
            lines = {ev["type"]: plain(render.event(RenderContext(lang="en", verbosity="normal", width=120), ev))
                     for ev in forum_events if ev["type"] in ("post.created", "goal.completed")
                     and (ev["type"] != "post.created" or ev["actor"] == "user")}
            assert f"#{own} Which tests?" in lines["post.created"]
            assert "Goal completed by the user's decision" in lines["goal.completed"]
            if NODE is not None:
                out = run_js(r"""
                  app.S.lang = "en";
                  app.S.state = { swarm: { mode: "thread" }, agents: [] };
                  out.lines = JSON.parse(process.env.EVENTS).map((ev) => [ev.type, app.describeEvent(ev)]);
                """, env={"EVENTS": json.dumps(forum_events)})
                for kind, line in out["lines"]:
                    assert line and not line.startswith(kind + " "), (kind, line)   # never the generic fallback
                assert ["goal.completed", "goal completed by the user — Parser done by hand"] in out["lines"]

            # ---- the TUI client reads that session and renders its thread panels
            async def tui() -> tuple[dict, dict, dict, bool]:
                async with hub_client(c) as client:
                    info = await client.hub_info()
                    snap = await client.state(sid)
                    detail = await client.post(sid, lexer["id"])
                    ok, text = await client.act(sid, "feed", {"query": "lexer"})
                    assert ok and "Lexer first!" in text, text
                    listed = await client.events(sid, types=list(FORUM_EVENTS))
                    assert listed and {e["type"] for e in listed} <= set(FORUM_EVENTS)
                    return info, snap, detail, client.mode == "launcher"

            info, snap, detail, launcher = asyncio.run(tui())
            assert launcher and info["mode"] == "launcher"
            ctx = RenderContext(lang="en", verbosity="normal", width=120)
            panel = plain(render.goal_panel(ctx, snap["swarm"]["goal"], vote=snap["goal_vote"]))
            assert "Build the parser" in panel and "Goal completed by the user's decision" in panel
            vote_line = plain(render.goal_vote_line(ctx, snap["goal_vote"]))
            assert vote_line == "📌 Goal vote (#1, veto): 1 of 2 for, 0 against; needs all 2 and none against."
            rows = plain(render.feed_rows(ctx, snap["posts"], 5))
            assert "#1" in rows and "#2" in rows and "Lexer first!" in rows and "Which tests?" in rows
            view = plain(render.post_view(ctx, detail))
            assert "Lexer first!" in view and f"c{first['id']}" in view and "And a test per token." in view
        finally:
            c.portal.call(manager.shutdown)


def test_a_board_session_keeps_its_snapshot_and_rest(tmp_path):
    manager = make_manager(tmp_path, Team())
    with TestClient(create_launcher_app(manager), base_url=BASE) as c:
        try:
            made = send(c, "POST", "/api/sessions", {"title": "Board", "goal": "Plan it"})
            assert made.status_code == 201, made.text
            session = made.json()["session"]
            sid = session["id"]
            assert (session["mode"], session["goal_rule"], session["goal_vote"], session["posts"]) == (
                "board", None, None, 0)
            assert "decision" not in session["goal"] and "swarm" not in session["overrides"]
            state = c.get(f"/s/{sid}/api/state").json()
            # the board's keys as they were, plus the four of §22.8 with their board values
            assert set(state) == OLD_TOP_KEYS | {"posts", "goal_vote"}
            assert set(state["swarm"]) == OLD_SWARM_KEYS | {"mode", "goal_rule"}
            assert state["agents"] and all(set(a) == OLD_AGENT_KEYS | {"subscriptions"} for a in state["agents"])
            assert (state["swarm"]["mode"], state["posts"], state["goal_vote"]) == ("board", [], None)
            assert all(a["subscriptions"] == [] for a in state["agents"])
            assert "decision" not in state["swarm"]["goal"]
            assert any("planning" in t["tags"] for t in state["tasks"])
            # the board's REST answers as before; the forum's is not there
            created = act(c, sid, "task_create", {"title": "Extra", "goal": "More"})
            assert created["ok"], created
            task_id = next(t["id"] for t in c.get(f"/s/{sid}/api/state").json()["tasks"] if t["title"] == "Extra")
            detail = c.get(f"/s/{sid}/api/tasks/{task_id}").json()
            assert set(detail) == {"task", "discussion", "events"} and detail["task"]["title"] == "Extra"
            assert act(c, sid, "board", {})["ok"]
            for path in ("/api/posts/1", "/api/posts"):
                resp = c.get(f"/s/{sid}{path}")
                assert resp.status_code == 404 and resp.json() == {
                    "error": "This session works on a task board: it has no posts."}
            for tool, body in (("post_create", {"title": "No"}), ("vote", {"target": "#1", "value": "like"}),
                               ("swarm_control", {"complete_goal": True})):
                assert act(c, sid, tool, body)["ok"] is False, tool
            events = c.get(f"/s/{sid}/api/events", params={"limit": 2000}).json()["events"]
            assert not [e for e in events if e["type"] in FORUM_EVENTS]
            # PATCH: "board" or null for the mode stores nothing; a rule is refused
            assert send(c, "PATCH", f"/api/sessions/{sid}", {"settings": {"swarm": {"mode": "board"}}}).status_code \
                == 200
            resp = send(c, "PATCH", f"/api/sessions/{sid}", {"settings": {"swarm": {"goal_rule": "veto"}}})
            assert resp.status_code == 400 and resp.json() == {"error": "goal_rule applies to a thread session"}
            resp = send(c, "PATCH", f"/api/sessions/{sid}", {"settings": {"swarm": {"mode": "thread"}}})
            assert resp.status_code == 400
            assert resp.json() == {"error": "the mode is chosen when the session is created"}
            stored = c.get(f"/api/sessions/{sid}").json()
            assert "swarm" not in stored["session"]["overrides"] and stored["config"]["swarm"]["mode"] == "board"
        finally:
            c.portal.call(manager.shutdown)
