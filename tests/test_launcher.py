"""Launcher web app (dreamteam.launcher) and launcher MCP (dreamteam.launcher_mcp)."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from conftest import make_skill
from dreamteam import roles
from dreamteam import settings as st
from dreamteam.launcher import create_launcher_app
from dreamteam.launcher_mcp import build_launcher_server, call_launcher_tool, list_launcher_tools
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.sessions import SessionManager

HOST = {"host": "127.0.0.1:8765"}


def _manager(tmp_path: Path) -> SessionManager:
    home = tmp_path / "home"
    home.mkdir()
    st.write_raw(home, {
        "swarm": {"name": "Home", "language": "en", "start_agents": 1, "max_agents": 3, "max_cost_usd": 2.0},
        "agents": {"stagger_seconds": 0, "idle_nudge_seconds": 0.2},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0,
                               "api_key": "sk-hidden-999999"}},
    })

    def factory(profile, agent):
        return ScriptedLLM(lambda system, messages, tools: "Nothing to do.", profile)

    return SessionManager(home, llm_factory=factory)


@pytest.fixture
def client(tmp_path):
    manager = _manager(tmp_path)
    app = create_launcher_app(manager)
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        c.manager = manager
        yield c
        c.portal.call(manager.shutdown)


def _post(c, path, body):
    return c.post(path, content=json.dumps(body), headers={"content-type": "application/json"})


def test_sessions_rest_and_session_routing(client):
    c = client
    assert c.get("/api/hub").json()["mode"] == "launcher"
    assert c.get("/api/sessions").json() == {"sessions": []}
    resp = _post(c, "/api/sessions", {"title": "Snake", "goal": "Write snake", "description": "Python",
                                      "overrides": {"swarm": {"max_cost_usd": 1.0}}})
    assert resp.status_code == 201, resp.text
    session = resp.json()["session"]
    sid = session["id"]
    assert session["status"] == "running" and session["max_cost_usd"] == 1.0 and session["url"] == f"/s/{sid}/"

    # the session app under /s/{sid}
    state = c.get(f"/s/{sid}/api/state").json()
    assert state["swarm"]["goal"]["title"] == "Write snake"
    assert c.get(f"/s/{sid}", follow_redirects=False).status_code == 307
    page = c.get(f"/s/{sid}/")
    assert page.status_code == 200 and "<html" in page.text.lower()
    act = _post(c, f"/s/{sid}/api/act/task_create", {"title": "Extra", "goal": "More"})
    assert act.json()["ok"], act.text
    assert c.get("/s/nope/api/state").status_code == 404
    assert c.get("/s/nope/").status_code == 404

    detail = c.get(f"/api/sessions/{sid}").json()
    assert detail["config"]["swarm"]["max_cost_usd"] == 1.0
    assert "sk-hidden" not in json.dumps(detail)
    renamed = c.patch(f"/api/sessions/{sid}", content=json.dumps({"title": "Snake game"}),
                      headers={"content-type": "application/json"})
    assert renamed.json()["session"]["title"] == "Snake game"

    # stop: still viewable, read-only
    stopped = _post(c, f"/api/sessions/{sid}/stop", {}).json()["session"]
    assert stopped["status"] == "stopped"
    assert c.get(f"/s/{sid}/api/state").json()["swarm"]["goal"]["title"] == "Write snake"
    blocked = _post(c, f"/s/{sid}/api/act/task_create", {"title": "No", "goal": "No"})
    assert blocked.status_code == 409 and not blocked.json()["ok"]
    assert _post(c, f"/s/{sid}/mcp", {}).status_code == 409
    started = _post(c, f"/api/sessions/{sid}/start", {}).json()["session"]
    assert started["status"] == "running"
    assert _post(c, f"/s/{sid}/api/act/board", {}).json()["ok"]

    assert c.delete(f"/api/sessions/{sid}").status_code == 400
    assert c.delete(f"/api/sessions/{sid}?confirm={sid}").json() == {"ok": True}
    assert c.get("/api/sessions").json() == {"sessions": []}
    assert c.post("/api/sessions/zzz/start", content="{}", headers={"content-type": "application/json"}).status_code == 404


def test_settings_api(client):
    c = client
    data = c.get("/api/settings").json()
    assert data["settings"]["models"]["default"]["api_key_set"] is True
    assert "sk-hidden" not in json.dumps(data)
    assert any(p["id"] == "ollama" for p in data["providers"])
    settings = data["settings"]
    settings["agents"]["max_steps_per_turn"] = 9
    resp = c.put("/api/settings", content=json.dumps({"settings": settings}),
                 headers={"content-type": "application/json"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["settings"]["agents"]["max_steps_per_turn"] == 9
    assert st.read_raw(c.manager.home)["models"]["default"]["api_key"] == "sk-hidden-999999"
    bad = dict(settings, agents=dict(settings["agents"], default_model="missing"))
    resp = c.put("/api/settings", content=json.dumps({"settings": bad}), headers={"content-type": "application/json"})
    assert resp.status_code == 400 and "missing" in resp.json()["error"]
    check = _post(c, "/api/settings/check", {"profile": {"provider": "scripted", "model": "x"}, "name": "default"})
    assert check.status_code == 200 and check.json()["ok"] is False
    assert _post(c, "/api/settings/check", {"profile": {"provider": "openai", "zzz": 1}}).status_code == 400
    # JSON-only POSTs and host guard still apply
    assert c.post("/api/sessions", data={"a": "b"}).status_code == 415
    assert c.get("/api/sessions", headers={"host": "evil.example"}).status_code == 403


def test_settings_api_carries_the_role_catalog(client):
    c = client
    data = c.get("/api/settings").json()
    assert [p["id"] for p in data["role_presets"]] == list(roles.PRESET_IDS)
    assert data["role_presets"][2]["name"] == {"ru": "Тестировщик", "en": "Tester"}
    assert data["settings"]["roles"] == [] and [r["key"] for r in data["role_catalog"]] == list(roles.PRESET_IDS)
    sid = _post(c, "/api/sessions", {"title": "Roles", "goal": "Do it"}).json()["session"]["id"]
    settings = data["settings"]
    settings["roles"] = [{"name": "Тестировщик", "instructions": "Use pytest."},
                         {"name": "Data engineer", "description": "Builds data pipelines."},
                         {"name": "researcher", "hidden": True}]
    resp = c.put("/api/settings", content=json.dumps({"settings": settings}),
                 headers={"content-type": "application/json"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["settings"]["roles"][0] == {"name": "tester", "description": "", "instructions": "Use pytest.",
                                            "model": None, "hidden": False, "skills": []}
    assert body["applied_to"] == [sid] and [r["key"] for r in body["role_catalog"]][-1] == "data engineer"
    assert c.get("/api/settings").json()["role_catalog"] == body["role_catalog"]
    assert [r.name for r in c.manager.swarms[sid].config.roles] == ["tester", "Data engineer", "researcher"]
    # the session's catalog: the visible roles in its language, also while it is stopped
    for state in ("running", "stopped"):
        if state == "stopped":
            assert _post(c, f"/api/sessions/{sid}/stop", {}).json()["session"]["status"] == "stopped"
        listed = c.get(f"/s/{sid}/api/roles").json()
        assert listed["language"] == "en"
        names = [r["name"] for r in listed["roles"]]
        assert names[:2] == ["Generalist", "Developer"] and names[-1] == "Data engineer" and "Researcher" not in names
        tester = next(r for r in listed["roles"] if r["key"] == "tester")
        assert set(tester) == {"key", "name", "description", "instructions", "model", "builtin", "names", "skills"}
        assert tester["instructions"] == "Use pytest." and tester["builtin"] is True and tester["name"] == "Tester"
        # every spelling a hire resolves (roles.find_role), and the hidden roles, which a hire still resolves
        assert tester["names"] == ["Tester", "tester", "Тестировщик"]
        assert listed["roles"][-1]["names"] == ["Data engineer"]
        assert [(r["key"], r["names"]) for r in listed["hidden"]] == [("researcher", ["Researcher", "researcher",
                                                                                       "Исследователь"])]
    # an own role without a description: 400 with the [roles.N] text, nothing saved
    resp = c.put("/api/settings", content=json.dumps({"settings": dict(settings, roles=[{"name": "QA"}])}),
                 headers={"content-type": "application/json"})
    assert resp.status_code == 400 and "[roles.1] 'QA': a role of your own needs a description" in resp.json()["error"]
    assert "QA" not in json.dumps(st.read_raw(c.manager.home)["roles"])
    resp = c.put("/api/settings", content=json.dumps({"settings": {"roles": {"name": "tester"}}}),
                 headers={"content-type": "application/json"})
    assert resp.status_code == 400 and "list of roles" in resp.json()["error"]


def test_a_session_without_a_title_is_named_after_its_goal(client):
    resp = _post(client, "/api/sessions", {"goal": "Line one\nLine two", "start": False})
    assert resp.status_code == 201, resp.text
    session = resp.json()["session"]
    assert session["title"] == "Line one" and session["id"].endswith("-line-one")
    assert session["goal"] == {"title": "Line one", "description": "Line two", "status": "pending", "summary": ""}


def test_launcher_websocket_reports_changes(client):
    c = client
    with c.websocket_connect("ws://127.0.0.1:8765/ws") as ws:
        resp = _post(c, "/api/sessions", {"title": "WS", "goal": "Test", "start": False})
        sid = resp.json()["session"]["id"]
        event = ws.receive_json()
        assert event["type"] == "sessions.changed" and event["session"] == sid


async def test_launcher_mcp_tools(tmp_path):
    manager = _manager(tmp_path)
    names = [t.name for t in list_launcher_tools(manager)]
    assert names[:4] == ["sessions_list", "session_create", "session_start", "session_stop"]
    board = next(t for t in list_launcher_tools(manager) if t.name == "board")
    assert "session" in board.input_schema["properties"] and "as_agent" in board.input_schema["properties"]
    build_launcher_server(manager)

    text, err = await call_launcher_tool(manager, "board", {})
    assert err and "session" in text
    text, err = await call_launcher_tool(manager, "sessions_list", {})
    assert not err and "No sessions" in text
    text, err = await call_launcher_tool(manager, "session_create", {"title": "MCP", "goal": "Do things",
                                                                     "max_cost_usd": 0.5, "start_agents": 1})
    assert not err, text
    sid = manager.ids()[0]
    assert sid in text and manager.summary(sid)["max_cost_usd"] == 0.5
    text, err = await call_launcher_tool(manager, "board", {})  # defaults to the running session
    assert not err and "Do things" in text
    text, err = await call_launcher_tool(manager, "join_swarm", {"session": sid, "role": "Helper"})
    assert not err, text
    member = next(m["name"] for m in manager.swarms[sid].members() if m["kind"] == "external")
    text, err = await call_launcher_tool(manager, "task_get", {"session": sid, "task_id": 1, "as_agent": member})
    assert not err, text
    traces = manager.swarms[sid].tracer.list_traces(member) if hasattr(manager.swarms[sid], "tracer") else None
    if traces is not None:
        assert traces and traces[0]["kind"] == "external" and traces[0]["tool_calls"] >= 1
    text, err = await call_launcher_tool(manager, "session_stop", {"session": sid})
    assert not err and "stopped" in text
    text, err = await call_launcher_tool(manager, "board", {"session": sid})
    assert err and "session_start" in text
    text, err = await call_launcher_tool(manager, "session_start", {"session": "nope"})
    assert err
    await manager.shutdown()


async def test_launcher_mcp_create_with_a_team(tmp_path):
    manager = _manager(tmp_path)
    try:
        create = next(t for t in list_launcher_tools(manager) if t.name == "session_create")
        props = create.input_schema["properties"]
        assert props["team"]["type"] == "array" and props["team"]["items"] == {"type": "string"}
        assert "the first line becomes its title" in props["goal"]["description"]
        text, err = await call_launcher_tool(manager, "session_create", {
            "goal": "Build it\nWith tests", "team": [" architect ", "Разработчик", "developer"]})
        assert not err, text
        sid = manager.ids()[0]
        assert manager.meta(sid)["overrides"]["swarm"] == {"starting_roles": ["architect", "Разработчик", "developer"],
                                                           "start_agents": 3}
        assert manager.summary(sid)["title"] == "Build it"
        assert sorted(m["role"] for m in manager.swarms[sid].members()) == ["Architect", "Developer", "Developer"]
        # start_agents wins over the team's size; [] is no team
        text, err = await call_launcher_tool(manager, "session_create", {
            "goal": "Two", "team": ["tester"], "start_agents": 2, "start": False})
        assert not err, text
        assert manager.meta(manager.ids()[1])["overrides"]["swarm"] == {"start_agents": 2, "starting_roles": ["tester"]}
        text, err = await call_launcher_tool(manager, "session_create", {"goal": "Three", "team": [], "start": False})
        assert not err and manager.meta(manager.ids()[2])["overrides"] == {}
        for bad, why in (("tester", "team must be a list of role names"), (["tester", " "], "list of role names"),
                         (["tester", 3], "list of role names"), (["x" * 61], "longer than 60 characters"),
                         (["tester"] * 201, "at most 200 agents"),
                         # the --team syntax is not taken as a role name: one item per agent
                         (["architect", "developer*2"], "'developer*2': a role name cannot contain ',' or '*'"),
                         (["architect, developer"], "repeat a name for several agents")):
            text, err = await call_launcher_tool(manager, "session_create", {"goal": "Bad", "team": bad})
            assert err and why in text, text
        assert len(manager.ids()) == 3  # nothing was created for a bad team
    finally:
        await manager.shutdown()


def test_session_api_posts_must_be_json(client):
    """The JSON-only POST rule of GuardMiddleware matched only paths starting with /api/, so a text/plain or
    form post (a cross-site form can send one) reached /s/{sid}/api/act/* and ran the tool."""
    c = client
    sid = _post(c, "/api/sessions", {"title": "Guard", "goal": "Guard it"}).json()["session"]["id"]
    sneaky = c.post(f"/s/{sid}/api/act/task_create", content='{"title": "csrf", "goal": "x"}',
                    headers={"content-type": "text/plain"})
    assert sneaky.status_code == 415
    assert c.post(f"/s/{sid}/api/act/swarm_control", data={"action": "pause"}).status_code == 415
    assert c.post(f"/s/{sid}/api/read", content="{}", headers={"content-type": "text/plain"}).status_code == 415
    assert "csrf" not in _post(c, f"/s/{sid}/api/act/board", {}).json()["result"]
    ok = c.post(f"/s/{sid}/api/act/board", content="{}", headers={"content-type": "application/json; charset=utf-8"})
    assert ok.status_code == 200 and ok.json()["ok"]
    for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        for path in (f"/api/sessions/{sid}", "/api/settings", f"/s/{sid}/api/state", "/mcp", f"/s/{sid}/mcp"):
            for bad in ({"origin": "https://evil.example"}, {"origin": "null"}, {"host": "evil.example"}):
                resp = c.request(method, path, content="{}", headers={"content-type": "application/json", **bad})
                assert resp.status_code == 403, (method, path, bad)
    assert c.get(f"/api/sessions/{sid}").status_code == 200  # nothing was deleted on the way


def test_rest_rejects_wrong_body_shapes(client):
    """Wrong JSON types were 500 Internal Server Error (AttributeError in the manager); a body that is not JSON
    at all created and started an untitled session."""
    c = client
    sid = _post(c, "/api/sessions", {"title": "Shapes", "goal": "Shape it", "start": False}).json()["session"]["id"]

    def patch(body: Any, raw: str | None = None):
        return c.patch(f"/api/sessions/{sid}", content=raw if raw is not None else json.dumps(body),
                       headers={"content-type": "application/json"})

    for body in ({"title": 123}, {"overrides": [1]}, {"overrides": "x"}, [1, 2]):
        resp = patch(body)
        assert resp.status_code == 400 and resp.json()["error"], (body, resp.text)
    assert patch(None, raw="{not json").json() == {"error": "The body is not valid JSON."}
    for body in ({"title": "x", "goal": "y", "overrides": [1]}, {"goal": "y", "start": "no"}, {"goal": 5}):
        resp = _post(c, "/api/sessions", body)
        assert resp.status_code == 400, (body, resp.text)
    resp = c.post("/api/sessions", content="{oops", headers={"content-type": "application/json"})
    assert resp.status_code == 400
    resp = c.post("/api/sessions", content="[" * 50_000, headers={"content-type": "application/json"})
    assert resp.status_code == 400
    assert [s["id"] for s in c.get("/api/sessions").json()["sessions"]] == [sid]
    assert c.put("/api/settings", content="{bad", headers={"content-type": "application/json"}).status_code == 400
    assert _post(c, "/api/settings/check", {"profile": {}, "name": ["x"]}).status_code == 400
    assert patch({"title": "  Renamed  "}).json()["session"]["title"] == "Renamed"


def test_session_pages_explain_errors_in_html(client):
    c = client
    page = c.get("/s/nope/")
    assert page.status_code == 404 and "text/html" in page.headers["content-type"] and "nope" in page.text
    assert c.get("/s/nope/api/state").json() == {"error": "Unknown session 'nope'."}
    sid = _post(c, "/api/sessions", {"title": "Broken", "goal": "Break", "start": False}).json()["session"]["id"]
    meta = c.manager.dir_of(sid) / "session.json"
    meta.write_text("{damaged", encoding="utf-8")
    page = c.get(f"/s/{sid}/")
    assert page.status_code == 500 and "text/html" in page.headers["content-type"] and "damaged" in page.text
    assert c.get(f"/s/{sid}/api/state").status_code == 500


async def test_session_app_start_cancelled_stops_its_lifespan():
    """A request cancelled while its session app was being built left the app's lifespan (the session MCP
    manager) running forever in an orphan task."""
    import contextlib as _contextlib

    from starlette.applications import Starlette

    from dreamteam.launcher import _SessionApp

    entered, exited = asyncio.Event(), asyncio.Event()

    @_contextlib.asynccontextmanager
    async def slow_lifespan(app):
        entered.set()
        try:
            await asyncio.sleep(30)
            yield
        finally:
            exited.set()

    entry = _SessionApp(object(), Starlette(lifespan=slow_lifespan))
    starting = asyncio.create_task(entry.start())
    await asyncio.wait_for(entered.wait(), 5)
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting
    await asyncio.wait_for(exited.wait(), 5)
    await asyncio.sleep(0)
    assert entry._task.done()


async def test_launcher_mcp_call_ends_when_its_session_stops(tmp_path):
    """wait_for_events through the launcher MCP waited out its whole timeout after the session stopped and then
    failed with 'Cannot operate on a closed database'."""
    manager = _manager(tmp_path)
    try:
        text, err = await call_launcher_tool(manager, "session_create", {"title": "Poll", "goal": "Wait"})
        assert not err, text
        sid = manager.ids()[0]
        poll = asyncio.create_task(call_launcher_tool(manager, "wait_for_events", {"session": sid, "timeout": 60}))
        await asyncio.sleep(0.3)
        assert not poll.done()
        await manager.stop(sid)
        text, err = await asyncio.wait_for(poll, 5)
        assert err and "stopped" in text and "closed database" not in text
        assert not manager._listeners or all(getattr(fn, "__name__", "") != "on_change" for fn in manager._listeners)
        # a call that finishes normally keeps its own result
        await manager.start(sid)
        text, err = await call_launcher_tool(manager, "wait_for_events", {"session": sid, "timeout": 0})
        assert not err, text
    finally:
        await manager.shutdown()


async def test_launcher_mcp_records_its_surface_on_a_library_decision(tmp_path):
    from conftest import make_skill
    from dreamteam import skills
    from dreamteam.tools import ToolContext, call_tool

    manager = _manager(tmp_path)
    try:
        text, err = await call_launcher_tool(manager, "session_create", {"title": "Skills", "goal": "Propose"})
        assert not err, text
        sid = manager.ids()[0]
        swarm = manager.swarms[sid]
        make_skill(swarm.workspace.root / ".scratch" / "d" / "kept", "kept")
        text, err = await call_tool(ToolContext(swarm, swarm.members()[0]["name"]), "skill_propose",
                                    {"path": ".scratch/d/kept"})
        assert not err, text
        changes = []
        manager.listen(lambda event: changes.append(event.get("change")))
        text, err = await call_launcher_tool(manager, "skill_approve", {"session": sid, "name": "kept",
                                                                        "approve": True})
        assert not err, text
        assert skills.Library.from_config(swarm.config).get("kept")["via"] == "launcher-mcp"
        assert swarm.bus.recent(types=("skill.library",), limit=1)[-1]["data"]["via"] == "launcher-mcp"
        assert changes == ["skills"]   # an open Skills tab of the home page reloads the library
        changes.clear()
        text, err = await call_launcher_tool(manager, "skills", {"session": sid})   # reads: nothing changed
        assert not err and not changes
        text, err = await call_launcher_tool(manager, "skill_approve", {"session": sid, "name": "nope",
                                                                        "approve": True})
        assert err and not changes   # a refused call changed nothing
    finally:
        await manager.shutdown()


async def test_session_mcp_endpoint_tells_the_home_page_about_library_changes(tmp_path):
    """The session app's /mcp (hub MCP) calls on_library_change after a library tool, as /api/act does."""
    from mcp import Client

    from conftest import make_skill
    from dreamteam.hub_mcp import build_hub_server
    from dreamteam.tools import ToolContext, call_tool

    manager = _manager(tmp_path)
    try:
        sid = (await manager.create("Skills", "Propose"))["id"]
        swarm = manager.swarms[sid]
        make_skill(swarm.workspace.root / ".scratch" / "d" / "mine", "mine")
        text, err = await call_tool(ToolContext(swarm, swarm.members()[0]["name"]), "skill_propose",
                                    {"path": ".scratch/d/mine"})
        assert not err, text
        calls = []
        async with Client(build_hub_server(swarm, lambda: calls.append(1))) as client:
            await client.call_tool("skills", {})
            assert not calls
            result = await client.call_tool("skill_approve", {"name": "mine", "approve": True})
            assert not result.is_error and calls == [1]
            assert swarm.bus.recent(types=("skill.library",), limit=1)[-1]["data"]["via"] == "mcp"
    finally:
        await manager.shutdown()


async def test_launcher_mcp_create_reports_start_failures(tmp_path, monkeypatch):
    manager = _manager(tmp_path)

    async def broken_start(sid):
        raise RuntimeError("git is not installed")

    monkeypatch.setattr(manager, "start", broken_start)
    text, err = await call_launcher_tool(manager, "session_create", {"title": "X", "goal": "Y"})
    assert err and "git is not installed" in text
    await manager.shutdown()


async def test_serve_launcher_resumes_only_once_the_port_is_ours(tmp_path):
    """Sessions were resumed before the port was bound: a second launcher losing the race for the port ran
    the same sessions (agents, databases) next to the first one until it noticed. `on_started` comes after the
    resume, so callers (console, hub.json) see the resumed sessions running."""
    import socket

    from dreamteam.launcher import serve_launcher

    manager = _manager(tmp_path)
    summary = await manager.create("Resume me", "Keep going")
    await manager.shutdown()  # keeps the autostart mark, like a server shutdown
    assert manager.meta(summary["id"])["autostart"] is True
    resumed: list[str] = []
    real_resume = manager.resume

    async def resume():
        resumed.extend(await real_resume())
        return resumed

    manager.resume = resume

    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        with pytest.raises(OSError):
            await asyncio.wait_for(serve_launcher(manager, "127.0.0.1", port, resume=True), 30)
    assert resumed == [] and not manager.running and not manager.swarms

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    seen: list[tuple[str, set]] = []
    server = asyncio.create_task(serve_launcher(
        manager, "127.0.0.1", port, resume=True,
        on_listening=lambda: seen.append(("listening", set(manager.running))),  # hub.json goes here
        on_started=lambda: seen.append(("started", set(manager.running)))))
    for _ in range(300):
        if len(seen) == 2:
            break
        await asyncio.sleep(0.05)
    assert seen == [("listening", set()), ("started", {summary["id"]})]
    server.cancel()
    with pytest.raises(asyncio.CancelledError):
        await server
    assert not manager.running and manager.hub_url is None
    assert manager.meta(summary["id"])["autostart"] is True


def test_restart_rebuilds_the_session_app_and_closes_the_old_one(client):
    """stop + start gives the session a new Swarm object: its app is rebuilt, the old app's lifespan (session MCP
    manager) ends, browsers are told to reconnect (1012), and no session-app task is left behind."""
    from starlette.websockets import WebSocketDisconnect

    c = client
    router = c.app.session_router
    sid = _post(c, "/api/sessions", {"title": "Restart", "goal": "Again"}).json()["session"]["id"]
    assert c.get(f"/s/{sid}/api/state").status_code == 200
    old = router.apps[sid]

    async def settle():
        await asyncio.sleep(0.05)
        await asyncio.gather(*router._closing, return_exceptions=True)
        return sum(1 for t in asyncio.all_tasks() if t.get_name() == "session-app" and not t.done())

    with c.websocket_connect(f"ws://127.0.0.1:8765/s/{sid}/ws") as ws:
        assert _post(c, f"/api/sessions/{sid}/stop", {}).status_code == 200
        with pytest.raises(WebSocketDisconnect) as closed:
            while True:
                ws.receive_text()
        assert closed.value.code == 1012
    assert c.portal.call(settle) == 0 and old._task.done()
    state = c.get(f"/s/{sid}/api/state").json()  # a stopped session can still be viewed
    assert state["swarm"]["session"]["status"] == "stopped"
    viewing = router.apps[sid]
    # started: the same Swarm object, the browser reloads its state
    with c.websocket_connect(f"ws://127.0.0.1:8765/s/{sid}/ws") as ws:
        assert _post(c, f"/api/sessions/{sid}/start", {}).status_code == 200
        with pytest.raises(WebSocketDisconnect) as closed:
            while True:
                ws.receive_text()
        assert closed.value.code == 1012
    assert c.get(f"/s/{sid}/api/state").json()["swarm"]["session"]["status"] == "running"
    assert router.apps[sid] is viewing and viewing.swarm is c.manager.swarms[sid]
    assert c.portal.call(settle) == 1
    assert _post(c, f"/s/{sid}/api/act/board", {}).json()["ok"]


def test_session_detail_answers_for_invalid_overrides(client):
    """GET /api/sessions/{sid} answered 400 for a session whose overrides became invalid (public_view raised), so
    the UI could not show that session at all; it also built the view without the manager's merge."""
    c = client
    sid = _post(c, "/api/sessions", {"title": "Odd", "goal": "x", "start": False}).json()["session"]["id"]
    path = c.manager.dir_of(sid) / "session.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta["overrides"] = {"swarm": {"max_agents": "many"}}  # written by an older version
    path.write_text(json.dumps(meta), encoding="utf-8")
    resp = c.get(f"/api/sessions/{sid}")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["session"]["config_error"] and data["config"]["swarm"]["max_agents"] == 3
    assert "sk-hidden" not in resp.text
    assert c.get("/api/sessions/nope").status_code == 404


def test_session_settings_patch(client):
    """The settings dialog of a session page: PATCH {"settings": {section: {key: value or null}}} changes those keys
    only (the overrides given at creation stay) and answers the session and its effective settings."""
    c = client
    sid = _post(c, "/api/sessions", {"title": "Tuned", "goal": "x", "start": True,
                                     "overrides": {"swarm": {"language": "ru"}}}).json()["session"]["id"]

    def patch(body: Any):
        return c.patch(f"/api/sessions/{sid}", content=json.dumps(body), headers={"content-type": "application/json"})

    resp = patch({"settings": {"shell": {"max_jobs": 3, "cpu_threads": 2}, "agents": {"tool_output_chars": 4000}}})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["session"]["overrides"] == {"swarm": {"language": "ru"}, "shell": {"max_jobs": 3, "cpu_threads": 2},
                                            "agents": {"tool_output_chars": 4000}}
    assert data["config"]["shell"]["max_jobs"] == 3 and data["config"]["agents"]["tool_output_chars"] == 4000
    assert "sk-hidden" not in resp.text
    swarm = c.manager.swarms[sid]
    assert swarm.config.shell.max_jobs == 3 and swarm.config.agents.tool_output_chars == 4000  # hot-applied
    data = patch({"settings": {"shell": {"max_jobs": None}}}).json()
    assert data["session"]["overrides"]["shell"] == {"cpu_threads": 2} and data["config"]["shell"]["max_jobs"] == 16
    for body in ({"settings": [1]}, {"settings": {"shell": {"max_jobs": -1}}}, {"settings": {"server": {"port": 1}}},
                 {"settings": {"shell": {}}, "overrides": {}}):
        resp = patch(body)
        assert resp.status_code == 400 and resp.json()["error"], (body, resp.text)
    assert c.get(f"/api/sessions/{sid}").json()["session"]["overrides"]["shell"] == {"cpu_threads": 2}


def _git(root: Path, *args: str) -> str:
    import subprocess

    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8",
                          check=True).stdout


def test_session_workspace_rest(client, tmp_path):
    """POST /api/sessions/{sid}/workspace moves a session or adopts a folder; the summary shows the folder; a
    deleted session keeps a folder outside it."""
    import shutil

    c = client
    sid = _post(c, "/api/sessions", {"title": "Folder", "goal": "Work", "start": False}).json()["session"]["id"]
    info = c.get(f"/api/sessions/{sid}").json()["session"]["workspace"]
    assert info["default"] is True and info["adopted"] is False and info["path"].endswith("workspace")
    target = tmp_path / "out" / "proj"
    resp = _post(c, f"/api/sessions/{sid}/workspace", {"path": str(target)})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["result"]["mode"] == "moved" and data["result"]["to"] == str(target.resolve())
    assert data["session"]["workspace"] == {"path": str(target.resolve()), "default": False, "adopted": False,
                                            "exists": True}
    listed = {s["id"]: s for s in c.get("/api/sessions").json()["sessions"]}
    assert listed[sid]["workspace"]["path"] == str(target.resolve())
    unchanged = _post(c, f"/api/sessions/{sid}/workspace", {"path": str(target)}).json()["result"]
    assert unchanged["mode"] == "unchanged"
    for body, status in (({"path": "relative/dir"}, 400), ({"path": 5}, 400), ({}, 400), ({"default": "yes"}, 400),
                         ({"path": str(tmp_path / "home")}, 400), ({"path": str(target), "expect": "sideways"}, 400),
                         ({"path": str(target), "expect": 3}, 400)):
        resp = _post(c, f"/api/sessions/{sid}/workspace", body)
        assert resp.status_code == status and resp.json()["error"], (body, resp.text)
    assert _post(c, "/api/sessions/nope/workspace", {"path": str(target)}).status_code == 404
    other = _post(c, "/api/sessions", {"title": "Other", "goal": "x", "start": False}).json()["session"]["id"]
    resp = _post(c, f"/api/sessions/{other}/workspace", {"path": str(target)})
    assert resp.status_code == 409 and sid in resp.json()["error"]
    full = tmp_path / "full"  # the user agreed to a move into an empty folder, but it holds files: refused
    full.mkdir()
    (full / "a.txt").write_text("a", encoding="utf-8")
    resp = _post(c, f"/api/sessions/{other}/workspace", {"path": str(full), "expect": "moved"})
    assert resp.status_code == 409 and "now holds files" in resp.json()["error"]
    if shutil.which("git"):  # a repository with uncommitted work is refused
        project = tmp_path / "dirty"
        project.mkdir()
        _git(project, "init", "-q")
        (project / "wip.txt").write_text("work in progress", encoding="utf-8")
        resp = _post(c, f"/api/sessions/{other}/workspace", {"path": str(project)})
        assert resp.status_code == 409 and "wip.txt" in resp.json()["error"]
    back = _post(c, f"/api/sessions/{sid}/workspace", {"default": True}).json()
    assert back["result"]["mode"] == "moved" and back["session"]["workspace"]["default"] is True
    assert _post(c, f"/api/sessions/{sid}/workspace", {"path": str(target)}).status_code == 200
    resp = c.delete(f"/api/sessions/{sid}?confirm={sid}")
    assert resp.json() == {"ok": True, "workspace_kept": str(target.resolve())} and target.is_dir()
    assert c.delete(f"/api/sessions/{other}?confirm={other}").json() == {"ok": True}

    # created right in a folder
    folder = tmp_path / "new-home-for-a-session"
    resp = _post(c, "/api/sessions", {"title": "There", "goal": "Go", "start": False, "workspace": str(folder)})
    assert resp.status_code == 201, resp.text
    assert resp.json()["result"]["mode"] == "created" and "workspace_result" not in resp.json()["session"]
    assert resp.json()["session"]["workspace"]["path"] == str(folder.resolve()) and folder.is_dir()
    for body in ({"goal": "x", "workspace": "relative"}, {"goal": "x", "workspace": 7}):
        assert _post(c, "/api/sessions", body).status_code == 400
    assert len(c.get("/api/sessions").json()["sessions"]) == 1  # nothing half-created
    plain = _post(c, "/api/sessions", {"title": "Plain", "goal": "x", "start": False})
    assert plain.status_code == 201 and "result" not in plain.json()


async def test_launcher_mcp_session_cd(tmp_path):
    manager = _manager(tmp_path)
    try:
        names = [t.name for t in list_launcher_tools(manager)]
        assert "session_cd" in names[:5]
        text, err = await call_launcher_tool(manager, "session_create", {"title": "Cd", "goal": "Work",
                                                                         "start": False})
        assert not err, text
        sid = manager.ids()[0]
        target = tmp_path / "mcp-target"
        text, err = await call_launcher_tool(manager, "session_cd", {"session": sid, "path": str(target)})
        assert not err and "Moved" in text and str(target.resolve()) in text, text
        text, err = await call_launcher_tool(manager, "sessions_list", {})
        assert not err and f"folder: {target.resolve()}" in text
        for args, words in (({"session": sid, "path": "relative"}, "absolute"), ({"session": sid}, "path is required"),
                            ({"session": "nope", "path": str(target)}, "Unknown session")):
            text, err = await call_launcher_tool(manager, "session_cd", args)
            assert err and words in text, text
        text, err = await call_launcher_tool(manager, "session_cd", {"session": sid, "path": "default"})
        assert not err and manager.summary(sid)["workspace"]["default"] is True, text
        project = tmp_path / "project"
        project.mkdir()
        (project / "app.py").write_text("print('hi')\n", encoding="utf-8")
        text, err = await call_launcher_tool(manager, "session_create", {"title": "P", "goal": "Improve app.py",
                                                                         "start": False, "workspace": str(project)})
        assert not err and "works with the files in" in text and str(project.resolve()) in text, text
    finally:
        await manager.shutdown()


# ---- skills (docs/SPEC.md §19) ---------------------------------------------------------------------------------
def _zip_b64(files: dict[str, str]) -> str:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _skill_md(name: str, description: str = "Does a thing.") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\nSteps.\n"


def _library_root() -> Path:
    return Path(os.environ["DREAMTEAM_SKILLS_DIR"])   # (the autouse fixture's)


def _claude_skills() -> Path:
    return Path(os.environ["CLAUDE_CONFIG_DIR"]) / "skills"


def _until(predicate, timeout: float = 20.0, what: str = "") -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.05)


def test_skills_library_routes(client, tmp_path):
    c = client
    events: list[dict] = []
    c.manager.listen(events.append)
    data = c.get("/api/skills").json()
    assert set(data) == {"library_dir", "library", "claude", "host", "sandbox", "limits", "install"}
    assert data["library"] == [] and data["host"] == [] and data["sandbox"] is False
    assert data["library_dir"] == str(_library_root())
    assert data["limits"] == {"max_mb": 10, "max_files": 200, "max_attached": 40, "max_archive_mb": 50}
    assert data["install"] == {"enabled": True, "proposals": True, "scripts": False}
    assert c.get("/api/hub").json()["sandbox"] is False
    # a folder: its candidate says what attaching it would install (no installer runs for a preview)
    folder = make_skill(tmp_path / "src" / "pdf", "pdf", "Reads PDFs.", files={"requirements.txt": "pypdf>=4\n"})
    resp = _post(c, "/api/skills", {"path": str(folder)})
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert [(e["name"], e["origin"], e["via"]) for e in report["imported"]] == [("pdf", "folder", "home")]
    assert report["candidates"][0]["deps"] == {"python": {"files": ["requirements.txt"], "packages": ["pypdf"]}}
    assert report["install"]["enabled"] is True
    assert [e["change"] for e in events if e["session"] is None] == ["skills"]   # the home page reloads its tab
    shown = c.get("/api/skills/pdf").json()
    assert "Steps." in shown["skill_md"] and {f["path"] for f in shown["files_list"]} == {"SKILL.md", "requirements.txt"}
    assert shown["deps"]["python"]["packages"] == ["pypdf"] and shown["install"]["scripts"] is False
    assert [e["name"] for e in c.get("/api/skills").json()["library"]] == ["pdf"]
    # a zip of two skills: pick one; the same name with other content is a 409 until replace
    two = _zip_b64({"a/SKILL.md": _skill_md("alpha"), "b/SKILL.md": _skill_md("beta")})
    first = _post(c, "/api/skills", {"zip": two, "filename": "two.zip"}).json()
    assert first["need_pick"] and not first["imported"]
    assert sorted(x["name"] for x in first["candidates"]) == ["alpha", "beta"]
    picked = _post(c, "/api/skills", {"zip": two, "filename": "two.zip", "pick": ["beta"]}).json()
    assert [e["name"] for e in picked["imported"]] == ["beta"]
    other = _zip_b64({"SKILL.md": _skill_md("beta", "Something else.")})
    clash = _post(c, "/api/skills", {"zip": other, "filename": "beta.zip"})
    assert clash.status_code == 409 and clash.json()["exists"] == ["beta"], clash.text
    assert _post(c, "/api/skills", {"zip": other, "filename": "beta.zip", "replace": True}).status_code == 200
    assert c.get("/api/skills/beta").json()["description"] == "Something else."
    # the body's shape
    for body in ({"path": str(folder), "git": "https://example.invalid/r"}, {}, {"path": "relative/pdf"},
                 {"path": str(folder), "pick": "pdf"}, {"path": str(folder), "attach": True}):
        assert _post(c, "/api/skills", body).status_code == 400, body
    assert c.post("/api/skills", content="x", headers={"content-type": "text/plain"}).status_code == 415
    # delete: the name is the confirmation
    assert c.delete("/api/skills/beta").status_code == 400
    assert c.delete("/api/skills/beta?confirm=beta").json() == {"removed": "beta"}
    assert c.delete("/api/skills/beta?confirm=beta").status_code == 404
    assert c.get("/api/skills/beta").status_code == 404
    assert c.get("/api/skills/pdf?from=elsewhere").status_code == 400


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_skills_import_route_caps_the_body_and_takes_a_git_source(tmp_path, monkeypatch):
    """POST /api/skills: a body over skills.body_limit is a 413 that leaves the library as it was; a Git source (a
    local file:// repository, a subfolder after `#:`) is imported with its URL recorded."""
    from dreamteam import skills
    from dreamteam.workspace import init_repo

    home_dir = tmp_path / "githome"   # git reads only this HOME's (empty) configuration
    home_dir.mkdir()
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(home_dir))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home_dir / ".config"))
    manager = _manager(tmp_path)
    raw = st.read_raw(manager.home)
    raw["skills"] = {"max_archive_mb": 1}   # a small cap: the test needs no 67 MB body
    st.write_raw(manager.home, raw)
    future = time.time() + 5   # (a hand edit is picked up by its modification time)
    os.utime(st.settings_path(manager.home), (future, future))
    with TestClient(create_launcher_app(manager), base_url="http://127.0.0.1:8765") as c:
        try:
            limit = skills.body_limit(manager.global_config())
            assert limit < 2 * 1024 * 1024
            big = _post(c, "/api/skills", {"zip": "A" * (limit + 1), "filename": "big.zip"})
            assert big.status_code == 413 and big.json()["error"], big.text
            assert c.get("/api/skills").json()["library"] == []
            repo = tmp_path / "repo"
            make_skill(repo / "skills" / "a", "alpha", "From Git.")
            assert init_repo(repo) is None
            url = repo.as_uri()
            resp = _post(c, "/api/skills", {"git": f"{url}#:skills/a"})
            assert resp.status_code == 200, resp.text
            entry = resp.json()["imported"][0]
            assert (entry["name"], entry["origin"], entry["via"]) == ("alpha", "git", "home")
            assert entry["source"]["url"] == url and entry["source"]["subdir"] == "skills/a"
            assert [e["name"] for e in c.get("/api/skills").json()["library"]] == ["alpha"]
        finally:
            c.portal.call(manager.shutdown)


def test_claude_code_skills_through_the_launcher(client, tmp_path):
    c = client
    make_skill(_claude_skills() / "review", "review", "Reviews code -> notes.")
    project = tmp_path / "project"
    make_skill(project / ".claude" / "skills" / "local", "local", "The project's own.")
    data = c.get("/api/skills").json()
    assert [(i["name"], i["origin"], i["library"]) for i in data["claude"]] == [("review", "claude-user", "absent")]
    with_project = c.get("/api/skills", params={"workspace": str(project)}).json()["claude"]
    assert [(i["name"], i.get("agent_writable")) for i in with_project] == [("review", None), ("local", True)]
    assert c.get("/api/skills", params={"workspace": "relative"}).status_code == 400
    shown = c.get("/api/skills/review", params={"from": "claude-user"}).json()
    assert shown["description"] == "Reviews code -> notes." and "Steps." in shown["skill_md"]
    assert c.get("/api/skills/local", params={"from": "claude-project", "workspace": str(project)}).status_code == 200
    assert c.get("/api/skills/local", params={"from": "claude-user"}).status_code == 404
    added = _post(c, "/api/skills", {"claude": "review", "origin": "claude-user"}).json()
    assert [(e["name"], e["origin"]) for e in added["imported"]] == [("review", "claude-user")]
    assert c.get("/api/skills").json()["claude"][0]["library"] == "same"


def test_skill_names_never_reach_a_path(client):
    c = client
    sentinel = _library_root().parent / "sessions"
    sentinel.mkdir(parents=True)
    (sentinel / "keep.txt").write_text("x", encoding="utf-8")
    for path in ("/api/skills/..%5C..%5Csessions", "/api/skills/..%5Csessions", "/api/skills/Bad_Name",
                 "/api/skills/%2E%2E"):
        for params in ({}, {"from": "claude-user"}):
            assert c.get(path, params=params).status_code == 400, path
        assert c.delete(path, params={"confirm": path.rsplit("/", 1)[1]}).status_code == 400, path
    assert (sentinel / "keep.txt").exists()


def test_create_attaches_skills_before_the_first_start(client, tmp_path):
    c = client
    assert _post(c, "/api/skills", {"path": str(make_skill(tmp_path / "lib" / "pdf", "pdf",
                                                           "From the library."))}).status_code == 200
    make_skill(_claude_skills() / "pdf", "pdf", "From Claude Code.")
    make_skill(_claude_skills() / "review", "review", "Reviews code.")
    source = make_skill(tmp_path / "src" / "tidy", "tidy", "Tidies files.")
    resp = _post(c, "/api/sessions", {"title": "S", "goal": "Do it", "skills": ["pdf", "review", str(source)]})
    assert resp.status_code == 201, resp.text
    session = resp.json()["session"]
    assert session["skills"] == ["pdf", "review", "tidy"] and session["skills_pending"] == []
    swarm = c.manager.swarms[session["id"]]
    got = {s.name: s for s in swarm.skills.list()}
    assert got["pdf"].frm == "library" and got["pdf"].description == "From the library."
    assert got["review"].frm == "claude-user" and got["tidy"].frm == "library"
    assert {s.attached_via for s in got.values()} == {"create"}
    assert (swarm.workspace.root / ".scratch" / "_skills" / "tidy" / "SKILL.md").is_file()   # mirrored at start
    library = {e["name"]: e for e in c.get("/api/skills").json()["library"]}
    assert library["tidy"]["via"] == "home" and library["tidy"]["origin"] == "folder"
    # a prefixed name picks the source: claude-user:pdf is Claude Code's, not the library's
    other = _post(c, "/api/sessions", {"goal": "Two", "start": False, "skills": ["claude-user:pdf"]}).json()
    listed = c.get(f"/api/sessions/{other['session']['id']}/skills").json()
    assert listed["running"] is False
    assert [(s["from"], s["description"]) for s in listed["attached"]] == [("claude-user", "From Claude Code.")]
    # a session never loaded lists the snapshot's keys too; a loaded one's live state is read on the loop
    calls = []
    public = swarm.skills.public
    swarm.skills.public = lambda: (calls.append(asyncio.get_running_loop()), public())[1]
    running = c.get(f"/api/sessions/{session['id']}/skills").json()["attached"]
    assert calls and set(listed["attached"][0]) == set(running[0])
    assert (listed["attached"][0]["path"], listed["attached"][0]["deps"]) == (".scratch/_skills/pdf/SKILL.md", [])
    # nothing is created, staged or imported when one item cannot be used
    sessions = sorted(p.name for p in (c.manager.home / "sessions").iterdir())
    fresh = make_skill(tmp_path / "src" / "fresh", "fresh")
    for skills, status, words in (([str(fresh), "nope"], 404, "No skill 'nope'"),
                                  ([str(tmp_path / "src")], 400, "holds several skills"),
                                  ("pdf", 400, "must be a list"), ([3], 400, "list of strings")):
        resp = _post(c, "/api/sessions", {"goal": "Bad", "skills": skills})
        assert resp.status_code == status and words in resp.json()["error"], resp.text
    resp = _post(c, "/api/sessions", {"goal": "Bad", "skills": [str(fresh)], "via": "web"})
    assert resp.status_code == 400 and "'via' must be" in resp.json()["error"], resp.text
    assert sorted(p.name for p in (c.manager.home / "sessions").iterdir()) == sessions
    assert "fresh" not in {e["name"] for e in c.get("/api/skills").json()["library"]}
    assert not [p for p in _library_root().iterdir() if p.name.startswith(".t-")]   # no staging left


def test_skills_errors_at_create_never_repeat_a_urls_credentials(client, tmp_path, monkeypatch):
    """DESIGN §3.3: a Git URL's user info is stripped before it is shown, also in create's own clash messages."""
    from dreamteam import skills

    c = client
    repo = make_skill(tmp_path / "repo", "tok", "Git one.")
    monkeypatch.setattr(skills, "clone", lambda src, dest, limits, *a, **k: shutil.copytree(repo, dest) and "c0ffee")
    assert _post(c, "/api/skills", {"path": str(make_skill(tmp_path / "lib" / "tok", "tok",
                                                           "Library one."))}).status_code == 200
    url = "https://alice:ghp_SECRET123@example.com/org/tok"
    for skills_given in ([url], [url, url + "#main"]):
        resp = _post(c, "/api/sessions", {"goal": "x", "skills": skills_given})
        assert resp.status_code == 409 and "example.com/org/tok" in resp.json()["error"], resp.text
        assert "ghp_SECRET123" not in resp.text and "alice:" not in resp.text
    # two different skills of one name, one of them from the URL
    assert c.delete("/api/skills/tok?confirm=tok").status_code == 200
    other = make_skill(tmp_path / "other" / "tok", "tok", "Folder one.")
    dup =_post(c, "/api/sessions", {"goal": "x", "skills": [url, str(other)]})
    assert dup.status_code == 409 and "differ" in dup.json()["error"], dup.text
    assert "ghp_SECRET123" not in dup.text and "https://example.com/org/tok" in dup.json()["error"]


def test_start_attaches_skills_to_a_stopped_or_a_running_session(client, tmp_path):
    c = client
    for name in ("pdf", "csv"):
        assert _post(c, "/api/skills", {"path": str(make_skill(tmp_path / "src" / name, name))}).status_code == 200
    sid = _post(c, "/api/sessions", {"goal": "Work", "start": False}).json()["session"]["id"]
    started = _post(c, f"/api/sessions/{sid}/start", {"skills": ["pdf"]})
    assert started.status_code == 200 and started.json()["session"]["skills"] == ["pdf"], started.text
    swarm = c.manager.swarms[sid]
    assert swarm.started and swarm.skills.get("pdf").attached_via == "create"
    assert (swarm.workspace.root / ".scratch" / "_skills" / "pdf" / "SKILL.md").is_file()
    # running: attached live, the members are told as after an attach on the session's page
    again = _post(c, f"/api/sessions/{sid}/start", {"skills": ["csv", "pdf"]})
    assert again.status_code == 200 and again.json()["session"]["skills"] == ["csv", "pdf"], again.text
    event = swarm.bus.recent(types=("skill.attached",), limit=10)[-1]["data"]
    assert (event["name"], event["via"]) == ("csv", "home")
    member = swarm.members()[0]["name"]
    kinds = [r["kind"] for r in swarm.db.all("SELECT kind FROM notifications WHERE recipient = ?", (member,))]
    assert "n_skill_added" in kinds
    # a detached name the user attaches again at a start leaves the detached list
    assert _post(c, f"/s/{sid}/api/act/skill_detach", {"name": "csv"}).json()["ok"]
    assert "csv" in swarm.skills.detached()
    assert _post(c, f"/api/sessions/{sid}/stop", {}).status_code == 200
    assert _post(c, f"/api/sessions/{sid}/start", {"skills": ["csv"]}).status_code == 200
    swarm = c.manager.swarms[sid]
    assert swarm.skills.get("csv") is not None and "csv" not in swarm.skills.detached()
    # another skill of an attached name is refused; the body's shape is checked
    make_skill(tmp_path / "src" / "pdf", "pdf", "Changed.")
    assert _post(c, "/api/skills", {"path": str(tmp_path / "src" / "pdf"), "replace": True}).status_code == 200
    clash = _post(c, f"/api/sessions/{sid}/start", {"skills": ["pdf"]})
    assert clash.status_code == 409 and clash.json()["exists"] == ["pdf"], clash.text
    assert _post(c, f"/api/sessions/{sid}/start", {"skills": "pdf"}).status_code == 400
    assert _post(c, f"/api/sessions/{sid}/start", {"skills": ["csv"], "via": "web"}).status_code == 400
    # the terminal's client names its surface
    assert _post(c, "/api/skills", {"path": str(make_skill(tmp_path / "src" / "doc", "doc"))}).status_code == 200
    assert _post(c, f"/api/sessions/{sid}/start", {"skills": ["doc"], "via": "cli"}).status_code == 200
    event = swarm.bus.recent(types=("skill.attached",), limit=1)[-1]["data"]
    assert (event["name"], event["via"]) == ("doc", "cli")


def test_proposals_of_a_stopped_session_and_deleting_it(client):
    from dreamteam.tools import ToolContext, call_tool

    c = client

    def propose(sid: str, *names: str) -> None:
        swarm = c.manager.swarms[sid]
        member = swarm.members()[0]["name"]
        for name in names:
            make_skill(swarm.workspace.root / ".scratch" / "d" / name, name)
            text, err = c.portal.call(call_tool, ToolContext(swarm, member), "skill_propose",
                                      {"path": f".scratch/d/{name}"})
            assert not err, text

    sid = _post(c, "/api/sessions", {"goal": "Propose"}).json()["session"]["id"]
    propose(sid, "tidy", "sort")
    assert c.get(f"/api/sessions/{sid}").json()["session"]["skills_pending"] == ["sort", "tidy"]
    assert _post(c, f"/api/sessions/{sid}/stop", {}).status_code == 200
    listed = c.get(f"/api/sessions/{sid}/skills").json()
    assert listed["running"] is False
    assert {s["name"]: s["library"] for s in listed["attached"]} == {"sort": "pending", "tidy": "pending"}
    shown = c.get(f"/api/sessions/{sid}/skills", params={"show": "tidy"}).json()
    assert "Steps." in shown["skill_md"] and shown["install"]["proposals"] is True
    # the home page's proposal dialog words its preview line by these switches (its Skills tab may never have loaded)
    settings = c.get("/api/settings").json()["settings"]
    settings["skills"]["install_proposals"] = False
    assert c.put("/api/settings", content=json.dumps({"settings": settings}),
                 headers={"content-type": "application/json"}).status_code == 200
    shown = c.get(f"/api/sessions/{sid}/skills", params={"show": "tidy"}).json()
    assert shown["install"] == {"enabled": True, "proposals": False, "scripts": shown["install"]["scripts"]}
    assert c.get(f"/api/sessions/{sid}/skills", params={"show": "nope"}).status_code == 404
    assert c.get(f"/api/sessions/{sid}/skills", params={"show": "..%5Cx"}).status_code == 400
    assert c.get("/api/sessions/nope/skills").status_code == 404
    # decided while stopped: saved, with the surface recorded (and an open home Skills tab told of the library)
    events: list[dict] = []
    c.manager.listen(events.append)
    resp = _post(c, f"/api/sessions/{sid}/skills/tidy/decide", {"approve": True})
    assert [e["change"] for e in events if e["session"] is None] == ["skills"]
    assert resp.status_code == 200 and "saved to the skills library" in resp.json()["text"], resp.text
    assert resp.json()["session"]["skills_pending"] == ["sort"]
    entry = next(e for e in c.get("/api/skills").json()["library"] if e["name"] == "tidy")
    assert (entry["origin"], entry["via"]) == ("agent", "stopped-session")
    assert _post(c, f"/api/sessions/{sid}/skills/tidy/decide", {"approve": False}).status_code == 409
    assert _post(c, f"/api/sessions/{sid}/skills/tidy/decide", {}).status_code == 400
    assert _post(c, f"/api/sessions/{sid}/skills/nope/decide", {"approve": True}).status_code == 404
    # a delete never loses a proposal silently
    refused = c.delete(f"/api/sessions/{sid}", params={"confirm": sid})
    assert refused.status_code == 409 and refused.json()["pending_skills"] == ["sort"], refused.text
    assert c.delete(f"/api/sessions/{sid}", params={"confirm": sid, "skills": "maybe"}).status_code == 400
    assert c.delete(f"/api/sessions/{sid}", params={"confirm": sid, "skills": "save", "via": "x"}).status_code == 400
    events.clear()
    assert c.delete(f"/api/sessions/{sid}", params={"confirm": sid, "skills": "save"}).json() == {"ok": True}
    assert "skills" in [e["change"] for e in events if e["session"] is None]
    entry = next(e for e in c.get("/api/skills").json()["library"] if e["name"] == "sort")
    assert (entry["origin"], entry["via"]) == ("agent", "stopped-session")   # as a decision on its files
    # a running session: its own decision (skill_approve), and a delete that discards
    sid = _post(c, "/api/sessions", {"goal": "Again"}).json()["session"]["id"]
    propose(sid, "gone", "kept")
    events.clear()
    resp = _post(c, f"/api/sessions/{sid}/skills/kept/decide", {"approve": False, "comment": "not now"})
    assert resp.status_code == 200 and "will not be saved" in resp.json()["text"]
    assert not [e for e in events if e["session"] is None]   # not saved: the library is as it was
    event = c.manager.swarms[sid].bus.recent(types=("skill.library",), limit=1)[-1]["data"]
    assert (event["status"], event["via"], event["comment"]) == ("rejected", "home", "not now")
    assert c.delete(f"/api/sessions/{sid}", params={"confirm": sid, "skills": "discard"}).json() == {"ok": True}
    assert not {"gone", "kept"} & {e["name"] for e in c.get("/api/skills").json()["library"]}
    # the terminal's client names its surface: a running session's proposals saved on its delete
    sid = _post(c, "/api/sessions", {"goal": "Third"}).json()["session"]["id"]
    propose(sid, "bulk", "paged")
    # saved from the session's own page (skill_approve): the home page hears of the library change too
    events.clear()
    assert _post(c, f"/s/{sid}/api/act/skill_approve", {"name": "paged", "approve": True}).json()["ok"]
    assert [e["change"] for e in events if e["session"] is None] == ["skills"]
    assert c.delete(f"/api/sessions/{sid}", params={"confirm": sid, "skills": "save", "via": "cli"}).json() == {
        "ok": True}
    entry = next(e for e in c.get("/api/skills").json()["library"] if e["name"] == "bulk")
    assert (entry["origin"], entry["via"]) == ("agent", "cli")


def test_session_skills_routes(client, tmp_path):
    c = client
    events: list[dict] = []
    c.manager.listen(events.append)
    sid = _post(c, "/api/sessions", {"goal": "Web"}).json()["session"]["id"]
    base = f"/s/{sid}"
    src = make_skill(tmp_path / "src" / "pdf", "pdf", files={"requirements.txt": "pypdf\n"})
    resp = _post(c, f"{base}/api/skills", {"path": str(src), "attach": True})
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["attached"] == [{"name": "pdf", "status": "attached", "files": 2}] and report["failed"] == []
    assert [e["change"] for e in events if e["session"] is None] == ["skills"]   # the home page hears of it
    data = c.get(f"{base}/api/skills").json()
    item = data["attached"][0]
    assert item["name"] == "pdf" and item["source_digest"] == item["digest"] and data["library"][0]["name"] == "pdf"
    shown = c.get(f"{base}/api/skills/pdf", params={"from": "session"}).json()
    assert shown["from"] == "library" and "Steps." in shown["skill_md"]
    assert shown["path"] == ".scratch/_skills/pdf/SKILL.md" and shown["deps"]["python"]["files"] == ["requirements.txt"]
    # its packages' install log (the test installers run as real processes)
    _until(lambda: c.get(f"{base}/api/state").json()["skills"][0]["deps"][0]["status"] == "ready", 30, "the install")
    log = c.get(f"{base}/api/skills/pdf/deps-log", params={"ecosystem": "python"}).json()
    assert log["path"].endswith("install.log") and (log["ecosystem"], log["status"]) == ("python", "ready")
    assert isinstance(log["log"], str)
    assert c.get(f"{base}/api/skills/pdf/deps-log", params={"ecosystem": "node"}).status_code == 404
    assert c.get(f"{base}/api/skills/pdf/deps-log", params={"ecosystem": "ruby"}).status_code == 400
    assert c.get(f"{base}/api/skills/nope/deps-log").status_code == 404
    # the library's copy changed: the attached one's own source tells
    make_skill(src, "pdf", "Changed.", files={"requirements.txt": "pypdf\n"})
    assert _post(c, "/api/skills", {"path": str(src), "replace": True}).status_code == 200
    item = c.get(f"{base}/api/skills").json()["attached"][0]
    assert item["source_digest"] not in (None, item["digest"])
    # the library delete of a standalone run's Skills tab; the session keeps its copy
    assert c.delete(f"{base}/api/skills/pdf", params={"confirm": "pdf"}).json() == {"removed": "pdf"}
    assert c.get(f"{base}/api/skills").json()["attached"][0]["source_digest"] is None
    # a stopped session imports, but attaches nothing
    assert _post(c, f"/api/sessions/{sid}/stop", {}).status_code == 200
    other = make_skill(tmp_path / "src" / "csv", "csv")
    refused = _post(c, f"{base}/api/skills", {"path": str(other), "attach": True})
    assert refused.status_code == 409 and "stopped" in refused.json()["error"]
    assert _post(c, f"{base}/api/skills", {"path": str(other)}).status_code == 200
    assert c.get(f"{base}/api/skills/pdf", params={"from": "session"}).status_code == 200   # viewed while stopped
    assert c.get(f"{base}/api/hub").json()["sandbox"] is False


async def test_launcher_mcp_skill_library_and_session_skills(tmp_path):
    from dreamteam.skills import Library

    manager = _manager(tmp_path)
    try:
        tools = {t.name: t for t in list_launcher_tools(manager)}
        assert list(tools)[:4] == ["sessions_list", "session_create", "session_start", "session_stop"]
        assert tools["skill_library"].input_schema["properties"]["action"]["enum"] == ["list", "show", "import",
                                                                                      "delete"]
        for name in ("session_create", "session_start"):
            assert tools[name].input_schema["properties"]["skills"]["items"] == {"type": "string"}
        text, err = await call_launcher_tool(manager, "skill_library", {"action": "list"})
        assert not err and "(empty)" in text, text
        folder = make_skill(tmp_path / "src" / "pdf", "pdf", "Reads PDFs.", files={"requirements.txt": "pypdf\n"})
        text, err = await call_launcher_tool(manager, "skill_library", {"action": "import", "source": str(folder)})
        assert not err and 'Skill "pdf" imported into the library (2 files)' in text, text
        assert Library.from_config(manager.config).get("pdf")["via"] == "launcher-mcp"
        text, err = await call_launcher_tool(manager, "skill_library", {"action": "show", "name": "pdf"})
        assert not err and "Reads PDFs." in text and "SKILL.md:" in text and "requirements.txt (python): pypdf" in text
        for name in ("csv", "doc"):
            make_skill(tmp_path / "src" / name, name)
            text, err = await call_launcher_tool(manager, "skill_library",
                                                 {"action": "import", "source": str(tmp_path / "src" / name)})
            assert not err, text
        text, err = await call_launcher_tool(manager, "skill_library", {"action": "list"})
        assert not err and "- csv [folder, 1 file" in text, text
        text, err = await call_launcher_tool(manager, "session_create", {"goal": "Use it", "skills": ["csv"]})
        assert not err and "skills: csv" in text, text
        sid = manager.ids()[0]
        text, err = await call_launcher_tool(manager, "session_start", {"session": sid, "skills": ["doc"]})
        assert not err and manager.summary(sid)["skills"] == ["csv", "doc"], text
        assert manager.swarms[sid].bus.recent(types=("skill.attached",), limit=1)[-1]["data"]["via"] == "launcher-mcp"
        for args, words in ((("session_create", {"goal": "x", "skills": "csv"}), "skills must be a list"),
                            (("session_create", {"goal": "x", "skills": ["nope"]}), "No skill 'nope'"),
                            (("skill_library", {"action": "show", "name": "nope"}), "No skill 'nope'"),
                            (("skill_library", {"action": "import"}), "either source"),
                            (("skill_library", {"action": "zap"}), "action must be")):
            text, err = await call_launcher_tool(manager, *args)
            assert err and words in text, text
        text, err = await call_launcher_tool(manager, "skill_library", {"action": "delete", "name": "pdf"})
        assert not err and "keep their own copies" in text
        assert Library.from_config(manager.config).get("pdf") is None
    finally:
        await manager.shutdown()


def test_skill_envs_cleanup_route(client, monkeypatch):
    from dreamteam.skill_deps import EnvManager

    calls: list[tuple] = []
    monkeypatch.setattr(EnvManager, "cleanup", lambda self, days, every_unused=False: calls.append(
        (days, every_unused)) or {"removed": ["a-py-1"], "kept": []})
    events: list[dict] = []
    client.manager.listen(events.append)
    resp = _post(client, "/api/skill-envs/cleanup", {})
    # the user's "Remove unused now": every unused one, not only those past env_retention_days
    assert resp.json() == {"removed": ["a-py-1"], "kept": []} and calls == [(30, True)]
    assert [e["change"] for e in events] == ["skill-envs"]


async def test_the_launcher_removes_unused_skill_environments_daily(tmp_path, monkeypatch):
    from dreamteam import launcher
    from dreamteam.skill_deps import EnvManager

    manager = _manager(tmp_path)
    calls: list[float] = []
    monkeypatch.setattr(launcher, "ENV_CLEANUP_DELAY", 0)
    monkeypatch.setattr(launcher, "ENV_CLEANUP_EVERY", 0.05)
    monkeypatch.setattr(EnvManager, "cleanup", lambda self, days: calls.append(days) or {"removed": [], "kept": []})
    for days, expected in ((30, True), (0, False)):
        manager.config.skills.env_retention_days = days
        task = asyncio.create_task(launcher._clean_envs_daily(manager))
        await asyncio.sleep(0.4)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert (len(calls) >= 2) is expected   # 0 days: never automatically
        calls.clear()
    await manager.shutdown()


async def test_the_skills_mirror_leaves_the_users_folder_with_the_session(tmp_path):
    """A copy of the session's skills in the user's own folder (a run that never stopped cleanly) goes when the
    session leaves the folder and when it is deleted; the user's files stay."""
    manager = _manager(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("print('hi')\n", encoding="utf-8")
    mirror = project / ".scratch" / "_skills"
    try:
        sid = (await manager.create("P", "Work", overrides={"workspace": {"git": False}}, start=False,
                                    workspace=str(project)))["id"]
        make_skill(mirror / "left", "left")
        result = await manager.set_workspace(sid, None)
        assert result["mode"] == "moved" and not mirror.exists() and (project / "app.py").is_file()
        assert (await manager.set_workspace(sid, str(project)))["mode"] == "adopted"
        make_skill(mirror / "left", "left")
        assert await manager.delete(sid) == str(project.resolve())
        assert not mirror.exists() and (project / "app.py").is_file()
    finally:
        await manager.shutdown()
