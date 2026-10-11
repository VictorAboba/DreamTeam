"""Launcher web app (dreamteam.launcher) and launcher MCP (dreamteam.launcher_mcp)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

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
                                            "model": None, "hidden": False}
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
        assert set(tester) == {"key", "name", "description", "instructions", "model", "builtin", "names"}
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
