"""The hub token (docs/SPEC.md §14 "Hub token", src/dreamteam/hubauth.py): in the Docker sandbox the hub listens on
0.0.0.0, so every container on the same bridge reaches it; with a token every request needs it, from the hub's own
loopback too (a header, or the cookie of a one-time `?token=` URL). `dreamteam sandbox` makes a token per
container, keeps it on the host outside the folders it mounts, and DreamTeam's clients send it; the container gets only
its SHA-256. The real-Docker check is opt-in (DREAMTEAM_DOCKER_TESTS=1: it builds an image under a tag of its own and
removes it)."""

from __future__ import annotations

import asyncio
import contextlib
import http.client
import http.server
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from dreamteam import cli, console as console_module, docker as docker_module, hub_mcp, hubauth, hubgit, shell, web
from dreamteam import settings as st
from dreamteam.config import Config
from dreamteam.launcher import create_launcher_app
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.sessions import SessionManager
from dreamteam.swarm import Swarm
from dreamteam.tui import client as tui_client

TOKEN = "Tk-" + "a1B2c3D4" * 6
PEER = ("172.17.0.1", 40000)        # the host through Docker's port mapping: never the container's loopback
NEIGHBOUR = ("172.17.0.3", 40000)   # another container on the same bridge
BASE = "http://127.0.0.1:8765"
BEARER = {"authorization": f"Bearer {TOKEN}"}
DIGEST = hubauth.digest(TOKEN)
COOKIE = hubauth.cookie_name(DIGEST)


@pytest.fixture
def token(monkeypatch):
    """The hub's token, as main() takes it out of DREAMTEAM_HUB_TOKEN."""
    monkeypatch.setattr(hubauth, "_taken", TOKEN)
    return TOKEN


# ---------------------------------------------------------------------------------------------- the guard (ASGI)
async def _ok_app(scope, receive, send):
    if scope["type"] == "http":
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


async def _call(guard, *, client=NEIGHBOUR, headers: dict[str, str] | None = None, path: str = "/api/state",
                query: str = "", method: str = "GET", kind: str = "http", server: str = "172.17.0.2",
                raw_path: bytes | None = None, host: str = "127.0.0.1:8765") -> tuple[int, dict[str, list[str]]]:
    """(status, response headers): 200 when the app was reached, 1008 for a closed WebSocket. `server`: the address
    the connection reached."""
    sent_headers = [(b"host", host.encode()), *((k.encode(), v.encode()) for k, v in (headers or {}).items())]
    scope = {"type": kind, "method": method, "path": path, "headers": sent_headers, "query_string": query.encode(),
             "scheme": "ws" if kind == "websocket" else "http", "root_path": "", "server": (server, 8765)}
    if client is not None:
        scope["client"] = client
    if raw_path is not None:
        scope["raw_path"] = raw_path
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await guard(scope, receive, send)
    if not sent:
        return 200, {}
    if sent[0]["type"] == "websocket.close":
        return 1008, {}
    out: dict[str, list[str]] = {}
    for k, v in sent[0].get("headers", []):
        out.setdefault(k.decode("latin-1").lower(), []).append(v.decode("latin-1"))
    return sent[0]["status"], out


async def test_a_peer_outside_the_container_needs_the_token(token):
    guard = web.GuardMiddleware(_ok_app, host="0.0.0.0")
    status, headers = await _call(guard)
    assert status == 401 and headers["www-authenticate"] == ['Bearer realm="DreamTeam"']
    assert (await _call(guard, headers={"authorization": "Bearer wrong"}))[0] == 401
    assert (await _call(guard, headers={"authorization": f"Basic {TOKEN}"}))[0] == 401
    assert (await _call(guard, headers={"authorization": f"Bearer {TOKEN}x"}))[0] == 401
    assert (await _call(guard, headers=BEARER))[0] == 200
    assert (await _call(guard, headers={"authorization": f"bearer  {TOKEN} "}))[0] == 200
    cookie = COOKIE
    assert (await _call(guard, headers={"cookie": f"other=1; {cookie}={TOKEN}"}))[0] == 200
    assert (await _call(guard, headers={"cookie": f"{cookie}={DIGEST}"}))[0] == 401          # the digest is no token
    assert (await _call(guard, headers={"cookie": f"{cookie}=wrong"}))[0] == 401
    assert (await _call(guard, headers={"cookie": f"dreamteam_hub_000000000000={TOKEN}"}))[0] == 401  # another hub's
    # every route: the board, static files, the MCP endpoint, a session's API
    for path in ("/", "/static/app.js", "/mcp", "/s/001-x/api/state", "/api/hub"):
        assert (await _call(guard, path=path))[0] == 401, path
    assert (await _call(guard, method="POST", path="/mcp", query=f"token={TOKEN}"))[0] == 401   # a query: GET only
    # WebSockets: the header (TUI) or the cookie (the board); never the query
    assert (await _call(guard, kind="websocket", path="/ws"))[0] == 1008
    assert (await _call(guard, kind="websocket", path="/ws", query=f"token={TOKEN}"))[0] == 1008
    assert (await _call(guard, kind="websocket", path="/ws", headers=BEARER))[0] == 200
    assert (await _call(guard, kind="websocket", path="/ws", headers={"cookie": f"{cookie}={TOKEN}"}))[0] == 200


async def test_a_board_url_with_the_token_sets_the_cookie_and_redirects_to_the_clean_url(token):
    guard = web.GuardMiddleware(_ok_app, host="0.0.0.0")
    status, headers = await _call(guard, path="/s/001-x/", query=f"a=1&token={TOKEN}&b=")
    assert status == 303
    assert headers["location"] == ["/s/001-x/?a=1&b="] and headers["cache-control"] == ["no-store"]
    [cookie] = headers["set-cookie"]
    parts = [p.strip() for p in cookie.split(";")]
    assert parts[0] == f"{COOKIE}={TOKEN}"
    assert {"HttpOnly", "Path=/", "SameSite=strict"} <= set(parts) and not any(p.startswith("Max-Age") for p in parts)
    assert (await _call(guard, path="/", query="token=wrong"))[0] == 401
    # a path that starts with two slashes must not become a redirect to another host
    status, headers = await _call(guard, path="//evil.example.com/", raw_path=b"//evil.example.com/",
                                  query=f"token={TOKEN}")
    assert status == 303 and headers["location"] == ["/evil.example.com/"]


@pytest.mark.parametrize("peer", [
    ("127.0.0.1", 5), ("127.8.0.1", 5), ("::1", 5), ("::ffff:127.0.0.1", 5), PEER, NEIGHBOUR, ("10.0.0.5", 5),
    ("testclient", 5), None,
])
async def test_a_hub_with_a_token_asks_every_peer_for_it(token, peer):
    """The hub's own loopback too (docs/SPEC.md §14.2): the agents' commands in the sandbox's container come from
    there. With the header or the cookie any peer gets in; a hub without a token asks no peer, loopback or not."""
    guard = web.GuardMiddleware(_ok_app, host="0.0.0.0")
    assert (await _call(guard, client=peer))[0] == 401
    assert (await _call(guard, client=peer, path="/mcp", method="POST",
                        headers={"content-type": "application/json"}))[0] == 401
    assert (await _call(guard, client=peer, kind="websocket", path="/ws"))[0] == 1008
    assert (await _call(guard, client=peer, headers=BEARER))[0] == 200
    assert (await _call(guard, client=peer, headers={"cookie": f"{COOKIE}={TOKEN}"}))[0] == 200
    assert (await _call(guard, client=peer, kind="websocket", path="/ws", headers=BEARER))[0] == 200
    assert (await _call(guard, client=peer, kind="websocket", path="/ws",
                        headers={"cookie": f"{COOKIE}={TOKEN}"}))[0] == 200
    without = web.GuardMiddleware(_ok_app, host="0.0.0.0", token=None)
    assert (await _call(without, client=peer))[0] == 200
    assert (await _call(without, client=peer, kind="websocket", path="/ws"))[0] == 200


async def test_without_a_token_nothing_changes():
    assert hubauth.env_token() is None
    for bind in ("127.0.0.1", "0.0.0.0"):
        guard = web.GuardMiddleware(_ok_app, host=bind)
        assert guard.digest is None
        assert (await _call(guard))[0] == 200
        assert (await _call(guard, kind="websocket", path="/ws"))[0] == 200
        assert (await _call(guard, path="/", query="token=anything"))[0] == 200   # no cookie, no redirect


async def test_an_ip_literal_host_is_not_enough_by_itself(token):
    guard = web.GuardMiddleware(_ok_app, host="0.0.0.0")

    async def status(host: str, headers: dict[str, str] | None = None) -> int:
        return (await _call(guard, host=host, headers=headers, server="172.17.0.2"))[0]

    assert await status("10.1.2.3:8765") == 403                    # neither the address reached nor the token
    assert await status("10.1.2.3:8765", BEARER) == 200            # a port mapping that rewrites the address
    assert await status("172.17.0.2:8765") == 401                  # the address reached: the Host is fine, no token
    assert await status("172.17.0.2:8765", BEARER) == 200
    assert await status("rebind.example.com:8765", BEARER) == 403  # a name is never let through by the token


async def test_the_token_is_compared_in_constant_time(token, monkeypatch):
    calls = []
    real = hubauth.hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(hubauth.hmac, "compare_digest", spy)
    guard = web.GuardMiddleware(_ok_app, host="0.0.0.0")
    assert (await _call(guard, headers=BEARER))[0] == 200
    assert calls and calls[-1] == (DIGEST.encode(), DIGEST.encode())
    calls.clear()
    assert (await _call(guard, headers={"cookie": f"{COOKIE}=nope"}))[0] == 401
    assert calls == [(hubauth.digest("nope").encode(), DIGEST.encode())]
    assert not hubauth.matches("", DIGEST) and not hubauth.matches(None, DIGEST)


async def test_the_sandbox_hub_keeps_only_the_tokens_digest(monkeypatch):
    """`dreamteam sandbox` gives the container DREAMTEAM_HUB_TOKEN_SHA256, and the digest lets nobody in."""
    monkeypatch.setenv(hubauth.DIGEST_ENV, DIGEST.upper())
    assert hubauth.env_token() is None and hubauth.hub_digest() == DIGEST
    guard = web.GuardMiddleware(_ok_app, host="0.0.0.0")
    assert TOKEN not in repr(vars(guard))
    assert (await _call(guard))[0] == 401
    assert (await _call(guard, headers=BEARER))[0] == 200
    assert (await _call(guard, headers={"authorization": f"Bearer {DIGEST}"}))[0] == 401
    status, headers = await _call(guard, path="/", query=f"token={TOKEN}")   # the cookie: the token the URL brought
    assert status == 303 and headers["set-cookie"][0].startswith(f"{COOKIE}={TOKEN};")
    assert (await _call(guard, headers={"cookie": f"{COOKIE}={TOKEN}"}))[0] == 200
    monkeypatch.setenv(hubauth.DIGEST_ENV, "not-a-sha256")   # a broken digest: a token nobody has, never none
    broken = web.GuardMiddleware(_ok_app, host="0.0.0.0")
    assert broken.digest is not None and (await _call(broken, headers=BEARER))[0] == 401
    assert (await _call(broken, headers={"authorization": "Bearer not-a-sha256"}))[0] == 401


@pytest.mark.parametrize("raw, location", [
    (b"//evil.example.com/", "/evil.example.com/"),
    (b"/\\evil.example.com/", "/%5Cevil.example.com/"),       # a browser reads "/\host" as "//host"
    (b"/\\/evil.example.com/", "/%5C/evil.example.com/"),
    (b"/\t/evil.example.com/", "/%09/evil.example.com/"),     # ...and drops tabs and newlines
    (b"/s/001-x/%2F/", "/s/001-x/%2F/"),
])
async def test_the_token_redirect_never_leaves_the_hub(token, raw, location):
    guard = web.GuardMiddleware(_ok_app, host="0.0.0.0")
    status, headers = await _call(guard, path=raw.decode("latin-1"), raw_path=raw, query=f"token={TOKEN}")
    assert status == 303 and headers["location"] == [location]


async def test_the_cookie_takes_no_other_origin_than_the_hubs_own(token):
    """Browsers send the cookie to every port of 127.0.0.1, and SameSite counts them as the same site: a page on
    another port (a dev server, another local tool) must not read the board's stream with it. The header is no
    browser's: it keeps the usual Origin rule (host and port, any scheme), which takes no other port either."""
    guard = web.GuardMiddleware(_ok_app, host="0.0.0.0")
    with_cookie = {"cookie": f"{COOKIE}={TOKEN}"}
    for kind, path in (("websocket", "/ws"), ("http", "/api/state")):
        refused = 1008 if kind == "websocket" else 403
        assert (await _call(guard, kind=kind, path=path,
                            headers={**with_cookie, "origin": "http://127.0.0.1:8765"}))[0] == 200
        for origin in ("http://127.0.0.1:5173", "http://localhost:8765", "https://127.0.0.1:8765", "null"):
            assert (await _call(guard, kind=kind, path=path, headers={**with_cookie, "origin": origin}))[0] == refused
        assert (await _call(guard, kind=kind, path=path,
                            headers={**BEARER, "origin": "https://127.0.0.1:8765"}))[0] == 200
        assert (await _call(guard, kind=kind, path=path,
                            headers={**BEARER, "origin": "http://127.0.0.1:5173"}))[0] == refused
    status, _ = await _call(guard, method="POST", path="/api/act/board",
                            headers={**with_cookie, "origin": "http://127.0.0.1:5173", "content-type": "application/json"})
    assert status == 403
    # a name the user allows (DREAMTEAM_ALLOWED_HOSTS, a reverse proxy that ends TLS): its origin is fine
    proxied = web.GuardMiddleware(_ok_app, host="0.0.0.0", extra_hosts={"board.example.com"})
    assert (await _call(proxied, kind="websocket", path="/ws", host="board.example.com",
                        headers={**with_cookie, "origin": "https://board.example.com"}))[0] == 200


async def test_no_proxy_header_makes_a_peer_loopback(token, monkeypatch):
    """uvicorn would take X-Forwarded-For from FORWARDED_ALLOW_IPS (the environment, often *): never for a hub."""
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    config = web.hub_server_config(web.GuardMiddleware(_ok_app, host="0.0.0.0"), "0.0.0.0", 1, "warning")
    assert config.proxy_headers is False
    config.load()
    status, _ = await _call(config.loaded_app, headers={"x-forwarded-for": "127.0.0.1", "x-real-ip": "127.0.0.1",
                                                        "forwarded": "for=127.0.0.1"})
    assert status == 401


# ---------------------------------------------------------------------------------------------- the apps
def _scripted_swarm(tmp_path: Path) -> Swarm:
    raw = {"swarm": {"name": "Token swarm", "language": "en", "start_agents": 0, "max_cost_usd": 0},
           "mcp": {"library_dir": str(tmp_path / "library")},
           "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}}}

    def factory(profile, agent):
        return ScriptedLLM(lambda system, messages, tools: "Nothing to do.", profile)

    return Swarm(config=Config.from_dict(tmp_path / "project", raw), llm_factory=factory)


WS = "ws://127.0.0.1:8765"   # (TestClient's websocket_connect ignores base_url: Host would be "testserver")


def _expect_ws_refused(c: TestClient, path: str, headers: dict[str, str] | None = None) -> None:
    with c.websocket_connect(WS + path, headers=BEARER):   # the same path is fine with the token...
        pass
    with pytest.raises(WebSocketDisconnect) as refused:     # ...and closed before it is accepted without it
        with c.websocket_connect(WS + path, headers=headers or {}) as ws:
            ws.receive_text()
    assert refused.value.code == 1008


def _no_token_logged(caplog) -> None:
    """Nothing DreamTeam logs carries the token (the test client's own httpx2 lines show the URLs it requested)."""
    assert not [r for r in caplog.records if TOKEN in r.getMessage() and not r.name.startswith("httpx")]


async def test_a_swarm_hub_with_a_token_from_outside(tmp_path, token, caplog):
    caplog.set_level(logging.DEBUG)
    swarm = _scripted_swarm(tmp_path)
    await swarm.start()
    try:
        app = web.create_app(swarm, "0.0.0.0")
        with TestClient(app, base_url=BASE, client=PEER) as c:
            assert c.get("/api/state").status_code == 401
            assert c.get("/").status_code == 401
            mcp = c.post("/mcp", content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
                         headers={"content-type": "application/json", "accept": "application/json, text/event-stream"})
            assert mcp.status_code == 401
            _expect_ws_refused(c, "/ws")
            state = c.get("/api/state", headers=BEARER)
            assert state.status_code == 200 and state.json()["swarm"]["mcp_token"] is True
            assert TOKEN not in state.text and TOKEN not in c.get("/api/hub", headers=BEARER).text
            # the browser: the one-time URL, then the cookie
            first = c.get(f"/?token={TOKEN}", follow_redirects=False)
            assert first.status_code == 303 and first.headers["location"] == "/"
            assert c.get("/").status_code == 200 and c.get("/api/state").status_code == 200
            with c.websocket_connect(WS + "/ws", headers={"cookie": f"{COOKIE}={TOKEN}"}):
                pass
        local_app = web.create_app(swarm, "0.0.0.0")   # (an MCP manager runs once per app)
        with TestClient(local_app, base_url=BASE, client=("127.0.0.1", 50000)) as local:   # the container's loopback
            assert local.get("/api/state").status_code == 401   # (the agents' commands come from there)
            mcp = local.post("/mcp", content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
                             headers={"content-type": "application/json",
                                      "accept": "application/json, text/event-stream"})
            assert mcp.status_code == 401
            _expect_ws_refused(local, "/ws")
            assert local.get("/api/state", headers=BEARER).status_code == 200
            assert local.get(f"/?token={TOKEN}", follow_redirects=False).status_code == 303
            assert local.get("/api/state").status_code == 200   # the cookie
    finally:
        await swarm.stop()
        swarm.close()
    _no_token_logged(caplog)


def _manager(tmp_path: Path) -> SessionManager:
    home = tmp_path / "home"
    home.mkdir()
    st.write_raw(home, {
        "swarm": {"name": "Home", "language": "en", "start_agents": 0, "max_cost_usd": 1.0},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
    })

    def factory(profile, agent):
        return ScriptedLLM(lambda system, messages, tools: "Nothing to do.", profile)

    return SessionManager(home, llm_factory=factory)


def test_the_launcher_and_its_sessions_with_a_token_from_outside(tmp_path, token, caplog):
    caplog.set_level(logging.DEBUG)
    manager = _manager(tmp_path)
    with TestClient(create_launcher_app(manager, "0.0.0.0"), base_url=BASE, client=PEER) as c:
        try:
            assert c.get("/api/sessions").status_code == 401
            assert c.get("/api/settings").status_code == 401
            _expect_ws_refused(c, "/ws")
            settings = c.get("/api/settings", headers=BEARER)
            assert settings.status_code == 200 and TOKEN not in settings.text
            made = c.post("/api/sessions", content=json.dumps({"title": "T", "goal": "G"}),
                          headers={"content-type": "application/json", **BEARER})
            assert made.status_code == 201, made.text
            sid = made.json()["session"]["id"]
            assert c.get(f"/s/{sid}/api/state").status_code == 401
            _expect_ws_refused(c, f"/s/{sid}/ws")
            state = c.get(f"/s/{sid}/api/state", headers=BEARER)
            assert state.status_code == 200 and state.json()["swarm"]["mcp_token"] is True and TOKEN not in state.text
            detail = c.get(f"/api/sessions/{sid}", headers=BEARER)
            assert detail.status_code == 200 and TOKEN not in detail.text
            first = c.get(f"/s/{sid}/?token={TOKEN}", follow_redirects=False)
            assert first.status_code == 303 and first.headers["location"] == f"/s/{sid}/"
            assert c.get(f"/s/{sid}/api/state").status_code == 200   # the cookie, now
            with c.websocket_connect(f"{WS}/s/{sid}/ws",
                                     headers={"cookie": f"{COOKIE}={TOKEN}"}):
                pass
        finally:
            c.portal.call(manager.shutdown)
    _no_token_logged(caplog)


def test_the_launchers_zip_upload_needs_the_token(tmp_path, token):
    """POST /api/uploads (a zip for a new session's copy, the one POST that is not JSON) is behind the token as
    every route: 401 without it; the header or the cookie let it in, and a cookie request from another origin is
    refused like any other."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.txt", "A")
    data = buf.getvalue()
    manager = _manager(tmp_path)
    zip_type = {"content-type": "application/zip"}
    cookie = {"cookie": f"{COOKIE}={TOKEN}"}
    with TestClient(create_launcher_app(manager, "0.0.0.0"), base_url=BASE, client=PEER) as c:
        try:
            def upload(**headers: str) -> int:
                return c.post("/api/uploads?filename=a.zip", content=data, headers={**zip_type, **headers}).status_code

            assert upload() == 401
            assert upload(authorization="Bearer wrong") == 401
            assert upload(**BEARER) == 201
            assert upload(**cookie) == 201
            assert upload(**cookie, origin=BASE) == 201
            assert upload(**cookie, origin="https://evil.example") == 403
            assert upload(**cookie, origin="http://127.0.0.1:9999") == 403   # another program on this computer
            assert upload(**BEARER, **{"content-type": "text/plain"}) == 415
            assert len([p for p in manager.uploads.root.iterdir() if p.suffix == ".zip"]) == 3
        finally:
            c.portal.call(manager.shutdown)


def test_the_launcher_and_its_sessions_ask_their_own_loopback_for_the_token(tmp_path, token):
    """A launcher with a token (the sandbox's, compose's) asks a peer on its own loopback for it too: the sessions API,
    the launcher's and a session's MCP endpoint, a session's API and both streams; the header or the cookie let it in."""
    manager = _manager(tmp_path)
    with TestClient(create_launcher_app(manager, "0.0.0.0"), base_url=BASE, client=("127.0.0.1", 50000)) as c:
        try:
            rpc = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            mcp_headers = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
            assert c.get("/api/sessions").status_code == 401
            assert c.post("/mcp", content=rpc, headers=mcp_headers).status_code == 401
            _expect_ws_refused(c, "/ws")
            made = c.post("/api/sessions", content=json.dumps({"title": "T", "goal": "G"}),
                          headers={"content-type": "application/json", **BEARER})
            assert made.status_code == 201, made.text
            sid = made.json()["session"]["id"]
            assert c.get(f"/s/{sid}/api/state").status_code == 401
            assert c.post(f"/s/{sid}/mcp", content=rpc, headers=mcp_headers).status_code == 401
            _expect_ws_refused(c, f"/s/{sid}/ws")
            assert c.get(f"/s/{sid}/api/state", headers=BEARER).status_code == 200
            assert c.get(f"/s/{sid}/?token={TOKEN}", follow_redirects=False).status_code == 303
            assert c.get("/api/sessions").status_code == 200   # the cookie, now
            with c.websocket_connect(f"{WS}/s/{sid}/ws", headers={"cookie": f"{COOKIE}={TOKEN}"}):
                pass
        finally:
            c.portal.call(manager.shutdown)


def test_a_hub_reachable_from_other_hosts_does_not_start_without_a_token(caplog, monkeypatch):
    """Fail closed: the image's default command, a plain `docker run` or compose without a token would let every
    container of the network in. DREAMTEAM_HUB_NO_TOKEN=1 allows it, with a warning."""
    caplog.set_level(logging.WARNING)
    web.check_hub_token("127.0.0.1")
    web.check_hub_token("localhost")
    assert not caplog.records
    for bind in ("0.0.0.0", "192.168.1.5", "::"):
        with pytest.raises(web.NoTokenError, match=hubauth.TOKEN_ENV):
            web.check_hub_token(bind)
    monkeypatch.setenv(hubauth.NO_TOKEN_ENV, "1")
    web.check_hub_token("0.0.0.0")
    assert hubauth.NO_TOKEN_ENV in caplog.text
    caplog.clear()
    monkeypatch.delenv(hubauth.NO_TOKEN_ENV)
    monkeypatch.setenv(hubauth.DIGEST_ENV, DIGEST)
    web.check_hub_token("0.0.0.0")
    assert not caplog.records


def test_the_no_token_error_in_the_image_names_the_digest_first(monkeypatch):
    """docker-compose.yml passes DREAMTEAM_HUB_TOKEN_SHA256 alone: a compose user who exported DREAMTEAM_HUB_TOKEN
    was told to set it, and it never reaches the container. In the image (its marker DREAMTEAM_IMAGE=1) the error
    names the digest first."""
    from dreamteam import netoff

    monkeypatch.setenv(netoff.IMAGE_ENV, "1")
    with pytest.raises(web.NoTokenError) as error:
        web.check_hub_token("0.0.0.0")
    assert f"Set {hubauth.DIGEST_ENV} " in str(error.value) and "docker-compose.yml" in str(error.value)
    monkeypatch.delenv(netoff.IMAGE_ENV)
    monkeypatch.setattr(netoff, "in_container", lambda: False)
    with pytest.raises(web.NoTokenError, match=f"Set {hubauth.TOKEN_ENV} "):
        web.check_hub_token("0.0.0.0")


def test_the_no_token_error_in_another_container_names_either_variable(monkeypatch):
    """A pip install in a devcontainer or a CI container (/.dockerenv, no image marker) is not compose: there
    DREAMTEAM_HUB_TOKEN works as on any host, so the error names it and its digest, not docker-compose.yml."""
    from dreamteam import netoff

    monkeypatch.delenv(netoff.IMAGE_ENV, raising=False)
    monkeypatch.setattr(netoff, "in_container", lambda: True)
    with pytest.raises(web.NoTokenError, match=f"Set {hubauth.TOKEN_ENV} ") as error:
        web.check_hub_token("0.0.0.0")
    assert hubauth.DIGEST_ENV in str(error.value) and "docker-compose.yml" not in str(error.value)


async def test_every_loopback_address_is_a_loopback_bind(caplog):
    """A hub on 127.0.0.2 (any address of 127.0.0.0/8, ::1 however written) is reached from this computer alone: it
    starts without a token, as before there were tokens; only the names 127.0.0.1, localhost and ::1 did. The guard
    agrees: it takes the bind's own address as the Host (a token, when there is one, is asked of its loopback peers
    all the same). Besides the names, only an IP literal exactly as written counts: a string with brackets
    or a `%` scope is a name to the server (on Linux [::1] too), so it needs a token.
    (Nothing binds such an address here: macOS has 127.0.0.1 alone.)"""
    caplog.set_level(logging.WARNING)
    for bind in ("127.0.0.1", "localhost", "::1", "127.0.0.2", "127.255.0.9", "::ffff:127.0.0.1", "0:0:0:0:0:0:0:1"):
        web.check_hub_token(bind)
    assert not caplog.records
    for bind in ("128.0.0.1", "::2", "127.0.0.1.example.com",   # a name is no address, whatever it resolves to
                 "127.0.0.2%x.example", "127.0.0.1%", "::1%x.example", "]127.0.0.1[", "[127.0.0.1]", "[::1]"):
        with pytest.raises(web.NoTokenError):
            web.check_hub_token(bind)
    for secret in (None, TOKEN):
        guard = web.GuardMiddleware(_ok_app, host="127.0.0.2", token=secret)
        for peer in (("127.0.0.2", 5), ("127.0.0.1", 5)):
            for host, status in (("127.0.0.2:8765", 200), ("localhost:8765", 200), ("127.0.0.3:8765", 403),
                                 ("rebind.example.com:8765", 403)):
                expected = 401 if secret and status == 200 else status   # a token: asked of loopback peers too
                assert (await _call(guard, client=peer, server="127.0.0.2", host=host))[0] == expected, (secret, host)


async def test_serve_refuses_a_non_loopback_address_without_a_token(tmp_path):
    from dreamteam.launcher import serve_launcher

    swarm = _scripted_swarm(tmp_path)
    try:
        with pytest.raises(web.NoTokenError):
            await web.serve(swarm, "0.0.0.0", 1)   # (refused before anything listens)
        assert swarm.hub_url is None
        with pytest.raises(web.NoTokenError):
            await serve_launcher(_manager(tmp_path), "0.0.0.0", 1)
    finally:
        swarm.close()


def test_the_token_is_in_no_snapshot_without_a_token(tmp_path):
    swarm = _scripted_swarm(tmp_path)
    try:
        assert swarm.snapshot()["swarm"]["mcp_token"] is False
    finally:
        swarm.close()


# ---------------------------------------------------------------------------------------------- the environment
def test_dreamteam_takes_the_token_out_of_its_environment(tmp_path, monkeypatch):
    """main() moves it out of os.environ before anything starts a process; the agents' shell, DreamTeam's git and
    every other filter drop it as well, whatever shell.pass_env says."""
    monkeypatch.setenv(hubauth.TOKEN_ENV, TOKEN)
    monkeypatch.setenv(hubauth.DIGEST_ENV, DIGEST)
    assert cli.main(["init", str(tmp_path / "p"), "--lang", "en"]) == 0
    assert hubauth.TOKEN_ENV not in os.environ and hubauth.env_token() == TOKEN
    assert hubauth.DIGEST_ENV not in os.environ and hubauth.hub_digest() == DIGEST
    monkeypatch.setenv(hubauth.DIGEST_ENV, DIGEST)   # the digest is no business of the agents' commands either
    digest_config = Config.from_dict(tmp_path / "r", {"shell": {"pass_env": [hubauth.DIGEST_ENV]}})
    assert hubauth.DIGEST_ENV not in shell.scrubbed_env(digest_config)
    assert hubauth.DIGEST_ENV not in hubgit.environment()
    monkeypatch.delenv(hubauth.DIGEST_ENV)
    monkeypatch.setenv(hubauth.TOKEN_ENV, TOKEN)   # (a process that did not go through main())
    config = Config.from_dict(tmp_path / "q", {"shell": {"pass_env": [hubauth.TOKEN_ENV]}})
    assert hubauth.TOKEN_ENV not in shell.scrubbed_env(config)
    assert hubauth.TOKEN_ENV not in shell.secret_free_env([hubauth.TOKEN_ENV.lower()])
    assert hubauth.TOKEN_ENV not in hubgit.environment()
    assert not any(TOKEN in v for v in {**shell.scrubbed_env(config), **hubgit.environment()}.values())


def test_an_agents_command_never_sees_the_token(tmp_path, monkeypatch):
    monkeypatch.setenv(hubauth.TOKEN_ENV, TOKEN)
    config = Config.from_dict(tmp_path / "p", {"shell": {"pass_env": [hubauth.TOKEN_ENV]}})
    env = shell.scrubbed_env(config)
    out = subprocess.run([sys.executable, "-c", f"import os; print(os.environ.get('{hubauth.TOKEN_ENV}', 'none'))"],
                         env=env, capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "none"


# ---------------------------------------------------------------------------------------------- the host's clients
def test_clients_send_a_token_only_to_a_loopback_hub(tmp_path, monkeypatch):
    mounted = tmp_path / "home"
    mounted.mkdir()
    path = hubauth.store_token(9311, TOKEN, mounted)
    assert path is not None and path.parent == hubauth.tokens_dir().resolve()
    assert json.loads(path.read_text(encoding="utf-8"))["token"] == TOKEN
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
    for url in ("http://127.0.0.1:9311/mcp", "http://localhost:9311", "http://[::1]:9311/s/x/api/state"):
        assert hubauth.auth_headers(url) == {"Authorization": f"Bearer {TOKEN}"}, url
    # a hub.json is in the mounted folder: its address must not take the token anywhere else
    for url in ("http://evil.example.com:9311", "http://10.0.0.5:9311", "http://127.0.0.1.evil.example.com:9311"):
        assert hubauth.auth_headers(url) == {}, url
    assert hubauth.auth_headers("http://127.0.0.1:9312") == {}
    monkeypatch.setenv(hubauth.TOKEN_ENV, "from-env")
    assert hubauth.auth_headers("http://127.0.0.1:9312") == {"Authorization": "Bearer from-env"}
    assert hubauth.client_token("http://127.0.0.1:9311") == TOKEN            # the stored one wins
    assert hubauth.auth_headers("http://10.0.0.5:9312") == {}
    monkeypatch.delenv(hubauth.TOKEN_ENV)
    assert hubauth.browser_url("http://127.0.0.1:9311/#settings") == f"http://127.0.0.1:9311/?token={TOKEN}#settings"
    assert hubauth.browser_url("http://127.0.0.1:9312/") == "http://127.0.0.1:9312/"
    hubauth.forget_token(9311, "another sandbox's")
    assert hubauth.stored_token(9311) == TOKEN
    hubauth.forget_token(9311, TOKEN)
    assert hubauth.stored_token(9311) is None and not path.exists()


def test_the_token_is_never_stored_inside_the_mounted_folder(tmp_path, monkeypatch):
    mounted = tmp_path / "home"
    monkeypatch.setenv(hubauth.TOKENS_DIR_ENV, str(mounted / ".dreamteam" / "hub-tokens"))
    mounted.mkdir()
    assert hubauth.store_token(9311, TOKEN, mounted) is None
    assert not (mounted / ".dreamteam").exists()
    monkeypatch.setenv(hubauth.TOKENS_DIR_ENV, str(mounted))
    assert hubauth.store_token(9311, TOKEN, mounted) is None
    # every folder the container mounts counts (the host's skills library), and so does a path typed in another case
    # on a file system that ignores case (Windows, macOS)
    library = tmp_path / "library"
    library.mkdir()
    monkeypatch.setenv(hubauth.TOKENS_DIR_ENV, str(library / "hub-tokens"))
    assert hubauth.store_token(9311, TOKEN, mounted, also_mounted=[library]) is None
    assert not (library / "hub-tokens").exists()
    if Path(str(mounted).upper()).exists():
        monkeypatch.setenv(hubauth.TOKENS_DIR_ENV, str(mounted / "hub-tokens"))
        assert hubauth.store_token(9311, TOKEN, Path(str(mounted).upper())) is None
        assert not (mounted / "hub-tokens").exists()
    monkeypatch.setenv(hubauth.TOKENS_DIR_ENV, str(tmp_path / "elsewhere"))
    assert hubauth.store_token(9311, TOKEN, mounted, also_mounted=[library]) is not None


class _Answer:
    """What urllib.request.urlopen returns, as far as DreamTeam's clients read it."""

    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


def _refusal(request) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(request.full_url, 401, "Unauthorized",
                                  {"WWW-Authenticate": 'Bearer realm="DreamTeam"'}, None)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _no_token_taken_before(monkeypatch):
    """No test sees which token a hub of another test took (hubauth.token_taken)."""
    monkeypatch.setattr(hubauth, "_accepted", {})


def test_dreamteams_clients_send_the_stored_token(tmp_path, monkeypatch):
    hubauth.store_token(9313, TOKEN, tmp_path / "home")
    seen: list[urllib.request.Request] = []

    def fake_urlopen(request, timeout=None):
        seen.append(request)
        return _Answer(b'{"pid": 1, "sessions": [], "result": "ok"}')

    monkeypatch.setattr(hubauth, "_urlopen",fake_urlopen)
    assert cli.hub_identity("http://127.0.0.1:9313") == {"pid": 1, "sessions": [], "result": "ok"}
    cli._hub_json("http://127.0.0.1:9313", "/api/sessions")
    cli._hub_delete_session("http://127.0.0.1:9313", "001-x")
    hub = console_module.Hub("http://127.0.0.1:9313/s/001-x")
    hub.get("/api/state", since=1)
    hub.post("/api/act/board", {})
    assert len(seen) == 5
    assert all(r.get_header("Authorization") == f"Bearer {TOKEN}" for r in seen), [r.headers for r in seen]
    seen.clear()
    console_module.Hub("http://10.0.0.5:9313").get("/api/state")
    assert seen[0].get_header("Authorization") is None
    # the `dreamteam mcp` proxy: per request, never one header for good (a restarted sandbox has a new token)
    proxy = hub_mcp.proxy_http_client("http://127.0.0.1:9313/mcp")
    assert hub_mcp.CLIENT_HEADER in proxy.headers and "authorization" not in proxy.headers
    sent = next(proxy.auth.auth_flow(proxy.build_request("POST", "http://127.0.0.1:9313/mcp", json={})))
    assert sent.headers["authorization"] == f"Bearer {TOKEN}"
    assert "authorization" not in next(proxy.auth.auth_flow(proxy.build_request("POST", "http://10.0.0.5:9313/mcp"))).headers


async def test_the_tuis_client_sends_the_token_over_rest_and_the_stream(tmp_path, monkeypatch):
    import httpx2

    hubauth.store_token(9314, TOKEN, tmp_path / "home")
    options: list[dict] = []
    seen: list[str | None] = []

    def fake_connect(url, **kwargs):
        options.append(kwargs)
        raise OSError("refused")

    def answer(request):
        seen.append(request.headers.get("authorization"))
        return httpx2.Response(200, json={"mode": "launcher"})

    monkeypatch.setattr(tui_client, "ws_connect", fake_connect)
    for base, sent in (("http://127.0.0.1:9314", f"Bearer {TOKEN}"), ("http://10.0.0.5:9314", None)):
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(answer)) as http:
            client = tui_client.HubClient(base, http=http)
            try:
                await client.hub_info()
                stream = client.stream(None)
                assert (await stream.__anext__())["data"]["state"] == "lost"
                await stream.aclose()
            finally:
                await client.aclose()
        assert seen[-1] == sent, base
        assert options[-1]["additional_headers"] == ({"Authorization": sent} if sent else None), base


def test_a_hub_that_asks_for_a_missing_token_is_not_taken_for_a_dead_one(tmp_path, monkeypatch, capsys):
    """Without the token, the sandbox's hub.json must not look stale: `serve` or the TUI would start a second launcher
    on the same home. Every command says what to set instead."""
    home = tmp_path / "home"
    cli._load_home(home)
    cli.hub_file(home).parent.mkdir(parents=True, exist_ok=True)
    cli.hub_file(home).write_text(json.dumps(
        {"base_url": "http://127.0.0.1:9317", "pid": 7, "project": "/swarm"}), encoding="utf-8")

    def refuse(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized",
                                     {"WWW-Authenticate": 'Bearer realm="DreamTeam"'}, None)

    monkeypatch.setattr(hubauth, "_urlopen",refuse)
    with pytest.raises(hubauth.TokenRequired, match=hubauth.TOKEN_ENV):
        cli.running_hub(home)
    for argv in (["status", "--home", str(home)], ["serve", "--home", str(home), "--no-browser", "--no-console"]):
        assert cli.main(argv) == 1, argv
        assert hubauth.TOKEN_ENV in capsys.readouterr().err
    started = []
    monkeypatch.setattr(tui_client, "_start_launcher", lambda *a, **kw: started.append(a))

    async def attach():
        with pytest.raises(tui_client.HubError) as refused:
            await tui_client.connect(home)
        assert refused.value.status == 401 and not started

    asyncio.run(attach())

    def other_401(request, timeout=None):   # some other server's 401 is no DreamTeam hub
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(hubauth, "_urlopen",other_401)
    assert cli.running_hub(home) is None


async def test_the_clients_follow_a_sandbox_restart_on_the_same_port(tmp_path, monkeypatch):
    """`dreamteam sandbox` makes a new token at every start, on the same port, while the console, the TUI and the
    `dreamteam mcp` proxy (which Claude Code keeps running) stay attached: each looks the token up for every request
    and every reconnect of the stream, so all of them carry on without being made again."""
    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route, WebSocketRoute

    async def stream_once(ws):
        await ws.accept()
        await ws.send_text(json.dumps({"type": "hello", "data": {}}))
        await ws.close(code=1012)   # "restarted": the client reconnects at once

    inner = Starlette(routes=[Route("/api/hub", lambda request: JSONResponse({"pid": 1, "mode": "launcher"})),
                              Route("/mcp", lambda request: JSONResponse({}), methods=["POST"]),
                              WebSocketRoute("/ws", stream_once)])
    hub = {"guard": web.GuardMiddleware(inner, host="0.0.0.0", token=TOKEN)}

    async def through_docker(scope, receive, send):   # the published port: the peer is never the hub's loopback
        if scope["type"] in ("http", "websocket"):
            scope = {**scope, "client": PEER}
        await hub["guard"](scope, receive, send)

    def restart(token: str, *, store: bool = True) -> None:   # the hub of the same port starts with a new token
        hub["guard"] = web.GuardMiddleware(inner, host="0.0.0.0", token=token)
        if store:
            hubauth.store_token(port, token, tmp_path / "home")

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(through_docker, host="127.0.0.1", port=port, log_level="error",
                                           lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        assert thread.is_alive()
        await asyncio.sleep(0.05)
    hubauth.store_token(port, TOKEN, tmp_path / "home")
    console = console_module.Hub(base)
    tui = tui_client.HubClient(base)
    proxy = hub_mcp.proxy_http_client(base + "/mcp")
    stream = tui.stream(None)

    async def next_states(count: int) -> list[str]:
        return [(event["data"].get("state") if event["type"] == "_connection" else event["type"])
                for event in [await asyncio.wait_for(stream.__anext__(), 15) for _ in range(count)]]

    async def all_answer() -> None:   # (urllib blocks the loop: the hub runs in its own thread)
        assert console.get("/api/hub")["mode"] == "launcher"
        assert (await tui.hub_info())["mode"] == "launcher"
        assert (await proxy.post(base + "/mcp", json={})).status_code == 200

    try:
        await all_answer()
        assert await next_states(2) == ["connected", "hello"]
        restart("Tk-second-start-" + "x" * 30)
        await all_answer()
        assert await next_states(3) == ["lost", "connected", "hello"]   # the reconnect sends the new token
        # a stale stored token (the sandbox killed before it could remove it) and DREAMTEAM_HUB_TOKEN set to the
        # token of a hub started otherwise: the stored one is refused, the user's goes next
        restart("Tk-the-users-own-" + "y" * 30, store=False)
        monkeypatch.setenv(hubauth.TOKEN_ENV, "Tk-the-users-own-" + "y" * 30)
        await all_answer()
        assert await next_states(3) == ["lost", "connected", "hello"]
        # none fits: every client says what was tried and what to set
        restart("Tk-nobody-has-it-" + "z" * 30, store=False)
        with pytest.raises(hubauth.TokenRequired) as refused:
            console.get("/api/hub")
        assert "refused" in str(refused.value) and hubauth.TOKEN_ENV in str(refused.value)
        with pytest.raises(tui_client.HubError) as tui_refused:
            await tui.hub_info()
        assert tui_refused.value.status == 401 and tui_refused.value.message == str(refused.value)
        assert (await proxy.post(base + "/mcp", json={})).status_code == 401
        assert await next_states(2) == ["lost", "rejected"]
    finally:
        await stream.aclose()
        await tui.aclose()
        await proxy.aclose()
        server.should_exit = True
        await asyncio.to_thread(thread.join, 30)


def test_a_stale_stored_token_does_not_hide_the_one_the_user_set(tmp_path, monkeypatch):
    """A sandbox killed before it removed its token file leaves a token nobody asks for: DREAMTEAM_HUB_TOKEN, set on
    purpose, goes right after it, and first once the hub took it. Another server's 401 is not retried."""
    hubauth.store_token(9320, "stale", tmp_path / "home")
    monkeypatch.setenv(hubauth.TOKEN_ENV, TOKEN)
    url = "http://127.0.0.1:9320"
    assert hubauth.client_tokens(url) == ["stale", TOKEN]
    seen: list[str | None] = []

    def hub(request, timeout=None):
        seen.append(request.get_header("Authorization"))
        if request.get_header("Authorization") != f"Bearer {TOKEN}":
            raise _refusal(request)
        return _Answer(b'{"pid": 1}')

    monkeypatch.setattr(hubauth, "_urlopen",hub)
    assert cli.hub_identity(url) == {"pid": 1}
    assert seen == ["Bearer stale", f"Bearer {TOKEN}"]
    assert hubauth.client_tokens(url) == [TOKEN, "stale"]   # the one the hub took goes first now
    seen.clear()
    console_module.Hub(url + "/s/001-x").get("/api/state")
    assert seen == [f"Bearer {TOKEN}"]
    monkeypatch.setenv(hubauth.TOKEN_ENV, "wrong too")   # neither fits: the text names both, and what to set
    with pytest.raises(hubauth.TokenRequired) as refused:
        cli.hub_identity(url)
    assert str(hubauth.tokens_dir()) in str(refused.value) and f"in {hubauth.TOKEN_ENV}" in str(refused.value)
    assert f"set {hubauth.TOKEN_ENV}" in str(refused.value)
    seen.clear()

    def other(request, timeout=None):
        seen.append(request.get_header("Authorization"))
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(hubauth, "_urlopen",other)
    assert cli.hub_identity(url) is None and len(seen) == 1


def test_a_redirect_never_takes_the_token_along(tmp_path):
    """urllib copies a request's headers to wherever a redirect points: the token goes as an unredirected one."""
    import http.server

    seen: list[tuple[str, str | None]] = []

    class Elsewhere(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(("elsewhere", self.headers.get("Authorization")))
            self.send_response(200)
            self.send_header("Content-Length", "10")
            self.end_headers()
            self.wfile.write(b'{"pid": 1}')

        def log_message(self, *args):
            pass

    class Redirecting(Elsewhere):
        def do_GET(self):
            seen.append(("hub", self.headers.get("Authorization")))
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{servers[1].server_port}/api/hub")
            self.send_header("Content-Length", "0")
            self.end_headers()

    servers = [http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler) for handler in (Redirecting, Elsewhere)]
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
    for thread in threads:
        thread.start()
    try:
        hubauth.store_token(servers[0].server_port, TOKEN, tmp_path / "home")
        assert cli.hub_identity(f"http://127.0.0.1:{servers[0].server_port}") == {"pid": 1}
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
    assert seen == [("hub", f"Bearer {TOKEN}"), ("elsewhere", None)]


@pytest.mark.parametrize("url, user_url, tokens", [
    ("http://127.0.0.1:9321/api/hub", None, ["stored", TOKEN]),
    ("ws://localhost:9321/s/x/ws", None, ["stored", TOKEN]),
    ("http://[::1]:9321", None, ["stored", TOKEN]),
    ("http://127.0.0.1:9322", None, [TOKEN]),                             # another port: never its stored token
    ("http://192.168.1.5:9321/api/hub", None, []),                        # a hub.json's address: nothing
    ("http://127.0.0.1.evil.example.com:9321", None, []),
    ("http://192.168.1.5:9321/s/x/api/state", "http://192.168.1.5:9321", [TOKEN]),   # typed: the user's token only
    ("ws://192.168.1.5:9321/s/x/ws", "http://192.168.1.5:9321/", [TOKEN]),
    ("http://192.168.1.5:9322", "http://192.168.1.5:9321", []),           # ...on that very scheme, host and port
    ("https://192.168.1.5:9321", "http://192.168.1.5:9321", []),
    ("http://192.168.1.6:9321", "http://192.168.1.5:9321", []),
    ("not a url", "not a url", []),
])
def test_where_a_token_may_go(tmp_path, monkeypatch, url, user_url, tokens):
    hubauth.store_token(9321, "stored", tmp_path / "home")
    monkeypatch.setenv(hubauth.TOKEN_ENV, TOKEN)
    assert hubauth.client_tokens(url, user_url=user_url) == tokens


def test_a_hub_address_the_user_typed_gets_the_users_token_and_a_hub_json_never_does(tmp_path, monkeypatch, capsys):
    """`dreamteam console --url http://192.168.1.5:8766` reaches a hub on another computer with DREAMTEAM_HUB_TOKEN;
    the same address in a hub.json (agent-writable: the mounted folder) gets no token, and the text says why."""
    lan = "http://192.168.1.5:8766"
    hubauth.store_token(8766, "stored-for-the-sandbox-of-8766", tmp_path / "home")
    monkeypatch.setenv(hubauth.TOKEN_ENV, TOKEN)
    seen: list[tuple[str, str | None]] = []

    def hub(request, timeout=None):
        seen.append((request.full_url, request.get_header("Authorization")))
        if request.get_header("Authorization") != f"Bearer {TOKEN}":
            raise _refusal(request)
        if request.full_url.endswith("/api/hub"):
            return _Answer(b'{"pid": 7, "mode": "launcher"}')
        return _Answer(json.dumps({"sessions": [{"id": "001-x", "title": "T", "status": "running"}]}).encode())

    consoles: list[tuple[str, dict]] = []
    monkeypatch.setattr(hubauth, "_urlopen",hub)
    monkeypatch.setattr(console_module, "run_console", lambda base, lang, **kw: consoles.append((base, kw)))
    assert cli.main(["console", "--url", lan]) == 0
    assert seen and all(sent == f"Bearer {TOKEN}" for _, sent in seen), seen
    [(base, options)] = consoles
    assert base == lan + "/s/001-x" and options["user_url"] == lan
    seen.clear()
    console_module.Hub(base, options["user_url"]).post("/api/act/board", {})
    assert seen == [(lan + "/s/001-x/api/act/board", f"Bearer {TOKEN}")]
    # the same address from a hub.json
    home = tmp_path / "home"
    cli._load_home(home)
    cli.hub_file(home).parent.mkdir(parents=True, exist_ok=True)
    cli.hub_file(home).write_text(json.dumps({"base_url": lan, "pid": 7, "project": "/swarm"}), encoding="utf-8")
    seen.clear()
    with pytest.raises(hubauth.TokenRequired) as refused:
        cli.running_hub(home)
    assert seen == [(lan + "/api/hub", None)]
    assert cli.main(["console", "--home", str(home)]) == 1
    assert all(sent is None for _, sent in seen[1:])
    # ...and no ready-made command with that address: it said `dreamteam console --url <the address in hub.json>`,
    # which the agents who wrote the file would have got the user's token from
    for text in (str(refused.value), capsys.readouterr().err):
        assert "loopback" in text and "hub.json" in text and "console --url" in text, text
        assert f"--url {lan}" not in text and "--url http" not in text, text


# ---------------------------------------------------------------------------------------------- refused up front
def _no_side_effects(monkeypatch) -> dict[str, list]:
    """What a command must not do before the token check: create a session, start a swarm, run a hub."""
    from dreamteam import sessions as sessions_module, swarm as swarm_module

    done: dict[str, list] = {"created": [], "started": []}

    async def create(self, *args, **kwargs):
        done["created"].append(args)
        raise AssertionError("a session was created")

    async def start(self, *args, **kwargs):
        done["started"].append(args)
        raise AssertionError("a swarm was started")

    monkeypatch.setattr(sessions_module.SessionManager, "create", create)
    monkeypatch.setattr(swarm_module.Swarm, "start", start)
    return done


def test_serve_and_run_refuse_a_non_loopback_address_before_they_start_anything(tmp_path, monkeypatch, capsys):
    """Without a token, `serve --host 0.0.0.0 --goal X` used to create and start the session (each retry another)
    and `run --host 0.0.0.0` to start the swarm and set its goal, before serve() refused."""
    done = _no_side_effects(monkeypatch)
    home = tmp_path / "home"
    port = str(_free_port())
    assert cli.main(["serve", "--home", str(home), "--host", "0.0.0.0", "--port", port, "--goal", "X",
                     "--no-browser", "--no-console"]) == 1
    err = capsys.readouterr().err
    assert "0.0.0.0" in err and hubauth.TOKEN_ENV in err and hubauth.NO_TOKEN_ENV in err
    assert not done["created"] and not list((home / "sessions").iterdir())
    assert not list((home / ".dreamteam").glob("launcher-*.pid")) and not cli.hub_file(home).exists()
    project = tmp_path / "project"
    assert cli.main(["init", str(project), "--lang", "en"]) == 0
    capsys.readouterr()
    assert cli.main(["run", str(project), "--host", "0.0.0.0", "--port", port, "--goal", "X", "--no-console"]) == 1
    err = capsys.readouterr().err
    assert "0.0.0.0" in err and hubauth.TOKEN_ENV in err and not done["started"]
    assert not cli.hub_file(project).exists()


def test_a_running_hub_of_the_home_is_found_before_a_token_is_asked_for(tmp_path, monkeypatch, capsys):
    """A second `serve` of a home whose hub runs on 0.0.0.0 (a desktop shortcut, a shell without DREAMTEAM_HUB_TOKEN)
    starts nothing, so it needs no token: it says the hub runs and opens that one in the browser, as before there
    were tokens. It said that the web server could not start for want of a token. `run` likewise. Without a running
    hub the token is asked for, before anything starts."""
    done = _no_side_effects(monkeypatch)
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", opened.append)
    running: dict[str, str | None] = {"url": "http://127.0.0.1:9340"}
    monkeypatch.setattr(cli, "running_hub", lambda folder: running["url"])
    home, project = tmp_path / "home", tmp_path / "project"
    assert cli.main(["init", str(project), "--lang", "en"]) == 0
    serve = ["serve", "--home", str(home), "--host", "0.0.0.0", "--port", str(_free_port()), "--goal", "X",
             "--no-console"]
    run = ["run", str(project), "--host", "0.0.0.0", "--port", str(_free_port()), "--goal", "X", "--no-console"]
    capsys.readouterr()
    for argv, browser in ((serve, ["http://127.0.0.1:9340/"]), ([*serve, "--no-browser"], []), (run, [])):
        assert cli.main(argv) == 1, argv
        err = capsys.readouterr().err
        assert "already running" in err and "cannot start" not in err, err
        assert opened == browser, argv
        opened.clear()
    running["url"] = None
    for argv in (serve, run):
        assert cli.main(argv) == 1, argv
        err = capsys.readouterr().err
        assert "cannot start on 0.0.0.0" in err and hubauth.TOKEN_ENV in err, err
    assert not done["created"] and not done["started"] and not opened
    assert not list((home / "sessions").iterdir()) and not cli.hub_file(home).exists()


def test_the_tui_says_why_its_launcher_cannot_serve_a_non_loopback_address(tmp_path, monkeypatch):
    """The TUI's in-process launcher on a configured address other than loopback without a token: the reason, before
    anything starts; never the silent fallback to 127.0.0.1, which is for a busy port."""
    import io

    from dreamteam import tui

    _no_side_effects(monkeypatch)
    home = tmp_path / "home"
    cli._load_home(home)
    port = _free_port()

    def factory(profile, agent):
        return ScriptedLLM(lambda system, messages, tools: "Nothing to do.", profile)

    async def attach():
        with pytest.raises(tui_client.HubError) as refused:
            await tui_client.connect(home, host="0.0.0.0", port=port, llm_factory=factory)
        return refused.value

    refused = asyncio.run(attach())
    assert "0.0.0.0" in refused.message and hubauth.TOKEN_ENV in refused.message
    assert not list((home / ".dreamteam").glob("launcher-*.pid")) and not cli.hub_file(home).exists()
    err = io.StringIO()   # `dreamteam -p`, like the TUI, prints it and ends
    assert tui.main_print(home, "hello", host="0.0.0.0", port=port, llm_factory=factory, err=err) == 1
    assert err.getvalue().startswith("dreamteam: ") and hubauth.TOKEN_ENV in err.getvalue()
    st.write_raw(home, {**st.read_raw(home), "server": {"host": "0.0.0.0", "port": port}})   # the settings' address
    assert "0.0.0.0" in asyncio.run(attach()).message


def test_the_consoles_poll_says_once_per_outage_that_the_hub_takes_no_token(monkeypatch, capsys):
    """The inbox poll used to swallow the hub's 401 (an HTTPError is a URLError): the console went quiet."""
    class Stop(threading.Event):
        polls = 0

        def wait(self, timeout=None):
            self.polls += 1
            if self.polls >= 4:
                self.set()
            return self.is_set()

    base = "http://127.0.0.1:9330"
    console = console_module.Console(base + "/s/001-x", "en", stop=Stop())
    answers: list = [hubauth.TokenRequired(base), hubauth.TokenRequired(base), {"messages": [], "notifications": []},
                     hubauth.TokenRequired(base)]

    def get(path, **params):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(console.hub, "get", get)
    console._poll()
    assert capsys.readouterr().out.count(f"set {hubauth.TOKEN_ENV}") == 2 and not answers


@contextlib.contextmanager
def _fake_hub(accept: str | None):
    """A DreamTeam hub as its clients see it, on 127.0.0.1: it answers a request with `Bearer <accept>` and asks for
    its token otherwise (None: whatever is sent). Yields its URL and what came in, (method and path, Authorization)."""
    seen: list[tuple[str, str | None]] = []

    class Hub(http.server.BaseHTTPRequestHandler):
        def _answer(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            sent = self.headers.get("Authorization")
            seen.append((f"{self.command} {self.path}", sent))
            if accept is not None and sent == f"Bearer {accept}":
                status, extra = 200, {}
                body = json.dumps({"pid": 1, "mode": "swarm", "messages": [], "notifications": []}).encode()
            else:
                status, extra, body = 401, {"WWW-Authenticate": 'Bearer realm="DreamTeam"'}, web.TOKEN_TEXT.encode()
            self.send_response(status)
            for name, value in {**extra, "Content-Type": "application/json", "Content-Length": str(len(body))}.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _answer

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Hub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", seen
    finally:
        server.shutdown()
        server.server_close()


def _console_of_the_hub(base: str, monkeypatch):
    """cli._start_console, as `serve` and `run` start it, with nobody typing: a coroutine function that runs it until
    `until()` holds or it ends, stops it and returns whether it still ran."""
    stop = threading.Event()

    def nobody_types(prompt=""):
        stop.wait(30)
        raise EOFError

    monkeypatch.setattr("builtins.input", nobody_types)

    async def run(until) -> bool:
        done = cli._start_console(asyncio.get_running_loop(), base + "/s/001-x", "en", stop)
        deadline = time.monotonic() + 15
        while not until() and not done.done() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        running = not done.done()
        stop.set()
        await asyncio.wait_for(done, 15)
        return running

    return run


def test_a_hub_that_refuses_the_consoles_token_ends_neither_the_console_nor_the_hub(monkeypatch, capsys):
    """`serve` and `run` stop when their console ends (the user's /quit): a TokenRequired out of the console's first
    read of the inbox ended it, and with it the hub, whose console has no token for its address."""
    with _fake_hub(accept=None) as (base, seen):
        run = _console_of_the_hub(base, monkeypatch)
        assert asyncio.run(run(lambda: len(seen) >= 2)), "the console ended"   # (its first read, then its poll)
    assert capsys.readouterr().out.count("asks for its token") == 1   # said once, not at every poll


def test_the_clients_in_a_hubs_own_process_send_its_token_to_its_own_address(tmp_path, monkeypatch, token):
    """`serve --host <a LAN address>` or the TUI with such a `server.host`, and DREAMTEAM_HUB_TOKEN: the hub asks its
    own console and its own TUI client (peers on that address, not on loopback) for the token. They sent none: their
    address looked like one read from a hub.json, which gets no token."""
    monkeypatch.setattr(hubauth, "LOOPBACK_NAMES", frozenset({"localhost", "::1"}))   # 127.0.0.1: the LAN address
    with _fake_hub(accept=TOKEN) as (base, seen):
        assert hubauth.client_tokens(base) == []   # (what a hub.json's address gets)
        run = _console_of_the_hub(base, monkeypatch)
        asyncio.run(run(lambda: seen))
    assert seen and all(sent == f"Bearer {TOKEN}" for _, sent in seen), seen
    # the TUI's launcher in this process, which asks every peer for its token (its own loopback too)
    home = tmp_path / "home"
    cli._load_home(home)
    headers: list = []

    def fake_connect(url, **kwargs):
        headers.append(kwargs.get("additional_headers"))
        raise OSError("refused")

    monkeypatch.setattr(tui_client, "ws_connect", fake_connect)

    def factory(profile, agent):
        return ScriptedLLM(lambda system, messages, tools: "Nothing to do.", profile)

    async def attach():
        conn = await tui_client.connect(home, host="127.0.0.1", port=_free_port(), llm_factory=factory)
        try:
            assert conn.owned and (await conn.client.sessions()) == []
            stream = conn.client.stream(None)
            assert (await stream.__anext__())["data"]["state"] == "lost"
            await stream.aclose()
            with create_pipe_input() as pipe:   # its /web
                TUI(conn, lang="en", input=pipe, output=DummyOutput(), printer=lambda r: None).open_url(
                    conn.base_url + "/")
            return conn.base_url
        finally:
            await conn.close()

    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from dreamteam.tui.app import TUI

    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", opened.append)
    own = asyncio.run(attach())
    assert headers == [{"Authorization": f"Bearer {TOKEN}"}]
    assert opened == [f"{own}/?token={TOKEN}"]


def test_a_hub_with_the_tokens_digest_alone_lets_its_own_console_and_tui_in(tmp_path, monkeypatch):
    """A hub started with DREAMTEAM_HUB_TOKEN_SHA256 alone never has the token, and asks its own loopback for it too:
    the console of `serve`/`run` and the TUI's launcher in its process get in with hubauth.own_token(), which lives
    in that process's memory alone. A browser never gets it, and the guard takes it only as a header."""
    monkeypatch.setenv(hubauth.DIGEST_ENV, DIGEST)
    assert hubauth.env_token() is None and hubauth.own_digest() is None
    own = hubauth.own_token()
    with _fake_hub(accept=own) as (base, seen):
        assert hubauth.client_tokens(base) == []   # (not before the console says it serves that hub)
        run = _console_of_the_hub(base, monkeypatch)
        asyncio.run(run(lambda: seen))
        assert hubauth.browser_url(base + "/") == base + "/" and hubauth.client_token(base) is None
    assert seen and all(sent == f"Bearer {own}" for _, sent in seen), seen

    async def guard() -> None:
        hub = web.GuardMiddleware(_ok_app, host="127.0.0.1")
        mine = {"authorization": f"Bearer {own}"}
        loopback = ("127.0.0.1", 5)
        assert (await _call(hub, client=loopback))[0] == 401
        assert (await _call(hub, client=loopback, headers=mine))[0] == 200
        assert (await _call(hub, client=loopback, kind="websocket", path="/ws", headers=mine))[0] == 200
        assert (await _call(hub, client=loopback, headers={"cookie": f"{COOKIE}={own}"}))[0] == 401   # never a cookie
        assert (await _call(hub, client=loopback, path="/", query=f"token={own}"))[0] == 401      # nor a board URL's

    asyncio.run(guard())
    # the TUI's launcher in this process: REST and the stream with the own token; anyone else on loopback: 401
    home = tmp_path / "home"
    cli._load_home(home)

    def factory(profile, agent):
        return ScriptedLLM(lambda system, messages, tools: "Nothing to do.", profile)

    async def attach() -> None:
        conn = await tui_client.connect(home, host="127.0.0.1", port=_free_port(), llm_factory=factory)
        try:
            assert conn.owned and (await conn.client.sessions()) == []
            stream = conn.client.stream(None)
            assert (await asyncio.wait_for(stream.__anext__(), 15))["data"]["state"] == "connected"
            await stream.aclose()
            assert (await asyncio.to_thread(_status, conn.base_url + "/api/sessions"))[0] == 401
            printed: list = []
            with create_pipe_input() as pipe:   # its /web: no token for the browser, so it says how to get in
                TUI(conn, lang="en", input=pipe, output=DummyOutput(), printer=printed.append).open_url(
                    conn.base_url + "/")
            assert opened == [conn.base_url + "/"]
            assert f"?token=<the token whose SHA-256 is in {hubauth.DIGEST_ENV}>" in " ".join(
                tui_render.to_plain(r, 300) for r in printed)
        finally:
            await conn.close()

    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from dreamteam.tui import render as tui_render
    from dreamteam.tui.app import TUI

    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", opened.append)
    asyncio.run(attach())


def test_serve_and_run_open_their_own_board_with_its_token(tmp_path, monkeypatch, capsys, token):
    """`serve --host <a LAN address>` (or `run --open`) with DREAMTEAM_HUB_TOKEN: the browser on this computer comes
    to the hub from that address, not from loopback, and the board it opened answered 401. It opens the hub's own
    address with `?token=` now (the hub makes it a cookie), as the TUI's /web does; what the screen shows has no
    token. A loopback hub without a token opens the plain URL."""
    from dreamteam import launcher

    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", opened.append)
    monkeypatch.setattr(cli, "_port_free", lambda host, port: True)   # (nothing is bound: no LAN address here)
    monkeypatch.delenv(web.PUBLIC_URL_ENV, raising=False)

    async def listening(owner, host, port, *, on_started=None, on_listening=None, **kwargs):   # serve_launcher, serve
        if on_listening is not None:
            on_listening()
        on_started()
        await asyncio.Event().wait()

    monkeypatch.setattr(launcher, "serve_launcher", listening)
    monkeypatch.setattr(web, "serve", listening)
    home = tmp_path / "home"
    cli._load_home(home)
    config = Config.from_dict(tmp_path / "project", {
        "swarm": {"name": "Run", "language": "en", "start_agents": 0, "max_cost_usd": 0}, "workspace": {"git": False},
        "skills": {"env_retention_days": 0}, "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}}})
    port = _free_port()

    async def board(serving) -> str:
        stop = asyncio.Event()
        task = asyncio.create_task(serving(stop))
        while not opened and not task.done():
            await asyncio.sleep(0.02)
        stop.set()
        assert await asyncio.wait_for(task, 60) == 0
        return opened.pop()

    def boards(host: str) -> tuple[str, str]:
        return (asyncio.run(board(lambda stop: cli._serve_home(home, host, port, console=False, open_browser=True,
                                                               stop=stop))),
                asyncio.run(board(lambda stop: cli._serve_swarm(config.project_dir, config, host, port, None, False,
                                                                True, stop=stop))))

    lan = f"http://192.168.1.5:{port}"
    assert boards("192.168.1.5") == (f"{lan}/?token={TOKEN}", f"{lan}/?token={TOKEN}")
    err = capsys.readouterr().err
    assert f"Sessions: {lan}/" in err and f"Board: {lan}" in err and TOKEN not in err
    monkeypatch.setattr(hubauth, "_taken", None)
    assert boards("127.0.0.1") == (f"http://127.0.0.1:{port}/", f"http://127.0.0.1:{port}")
    assert "SHA-256" not in capsys.readouterr().err   # (a hub without a token: nothing to say)
    # the digest alone: the browser has no token for the board, which answers 401, so they say how to get in
    monkeypatch.setenv(hubauth.DIGEST_ENV, DIGEST)
    assert boards("127.0.0.1") == (f"http://127.0.0.1:{port}/", f"http://127.0.0.1:{port}")
    link = f"http://127.0.0.1:{port}/?token=<the token whose SHA-256 is in {hubauth.DIGEST_ENV}>"
    assert capsys.readouterr().err.count(link) == 2


async def test_a_loopback_hub_is_reached_directly_never_through_the_environments_proxy(tmp_path, monkeypatch):
    """HTTP_PROXY without NO_PROXY for loopback (a corporate proxy): urllib (the console, `status`, running_hub) and
    the `dreamteam mcp` proxy's httpx2 sent a sandbox hub's requests, and its stored token, to that proxy."""
    proxied: list[tuple[str, str | None]] = []

    class Proxy(http.server.BaseHTTPRequestHandler):
        def _any(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            proxied.append((f"{self.command} {self.path}", self.headers.get("Authorization")))
            self.send_response(502)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_POST = _any

        def log_message(self, *args):
            pass

    proxy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    for name in ("NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    for name in ("HTTP_PROXY", "http_proxy"):
        monkeypatch.setenv(name, f"http://127.0.0.1:{proxy.server_port}")
    try:
        with _fake_hub(accept=TOKEN) as (base, seen):
            hubauth.store_token(int(base.rsplit(":", 1)[1]), TOKEN, tmp_path / "home")
            assert console_module.Hub(base).get("/api/hub")["pid"] == 1
            assert cli.hub_identity(base) is not None
            async with hub_mcp.proxy_http_client(base + "/mcp") as mcp_proxy:
                assert (await mcp_proxy.post(base + "/mcp", json={})).status_code == 200
            async with tui_client.HubClient(base) as tui:
                assert (await tui.hub_info())["pid"] == 1
    finally:
        proxy.shutdown()
        proxy.server_close()
    assert not proxied and len(seen) == 4 and all(sent == f"Bearer {TOKEN}" for _, sent in seen), (proxied, seen)


async def test_the_tui_says_why_the_stream_of_a_single_swarm_is_refused(tmp_path):
    """`dreamteam sandbox <project>` runs one swarm (the TUI follows it with sid None): when no token fits after the
    sandbox restarted, the TUI said only that the connection was lost; it asked the hub why for a session only."""
    import httpx2
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from dreamteam.tui import render
    from dreamteam.tui.app import TUI

    def refuse(request):
        return httpx2.Response(401, headers={"WWW-Authenticate": 'Bearer realm="DreamTeam"'}, text=web.TOKEN_TEXT)

    base = "http://127.0.0.1:9341"
    printed: list[str] = []
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(refuse)) as http:
        client = tui_client.HubClient(base, http=http)
        with create_pipe_input() as pipe:
            tui = TUI(tui_client.Connection(client, base, tmp_path, owned=False), lang="en", input=pipe,
                      output=DummyOutput(), printer=lambda r: printed.append(render.to_plain(r, 200)))
            tui.client.mode = "swarm"
            tui.attached, tui.sid = True, None
            await tui._on_connection("rejected")
            if tui._lost_handle is not None:
                tui._lost_handle.cancel()
        await client.aclose()
    assert any("asks for its token" in line and hubauth.TOKEN_ENV in line for line in printed), printed


# ---------------------------------------------------------------------------------------------- dreamteam sandbox
def _sandbox_project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "dreamteam.toml").write_text("", encoding="utf-8")
    return project


def test_the_sandbox_command_passes_the_tokens_digest_by_name_only(tmp_path, monkeypatch):
    project = _sandbox_project(tmp_path)
    monkeypatch.setenv(hubauth.TOKEN_ENV, TOKEN)
    monkeypatch.setenv(hubauth.DIGEST_ENV, DIGEST)
    config = Config.from_dict(project, {"shell": {"pass_env": [hubauth.TOKEN_ENV, hubauth.DIGEST_ENV]}})
    cmd = docker_module.sandbox_command(project, 9315, config, docker="docker")
    image = cmd.index(docker_module.IMAGE)
    assert cmd[cmd.index(hubauth.DIGEST_ENV) - 1] == "-e" and cmd.index(hubauth.DIGEST_ENV) < image
    assert cmd.count(hubauth.DIGEST_ENV) == 1 and not any(TOKEN in part or DIGEST in part for part in cmd)
    assert hubauth.TOKEN_ENV not in cmd   # never the token itself, only its digest
    assert not {hubauth.TOKEN_ENV, hubauth.DIGEST_ENV} & set(docker_module.env_passthrough(config))  # nor the host's
    assert cmd[cmd.index("--host") + 1] == "0.0.0.0"   # a published port never reaches the container's loopback


def test_compose_gives_the_container_the_tokens_digest_only():
    """docker-compose.yml passes DREAMTEAM_HUB_TOKEN_SHA256 by name, as `dreamteam sandbox` does: the container's
    environment never holds the token itself (the agents' commands run there); the host's clients keep the token."""
    compose = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text(encoding="utf-8")
    passed = {line.strip()[2:].strip('"').split("=", 1)[0] for line in compose.splitlines()
              if line.strip().startswith("- ")}
    assert hubauth.DIGEST_ENV in passed and hubauth.TOKEN_ENV not in passed
    assert "hashlib.sha256(os.environ['DREAMTEAM_HUB_TOKEN']" in compose   # how to compute it on the host


def test_run_sandbox_makes_a_token_per_container_and_prints_how_to_use_it(tmp_path, monkeypatch, capsys):
    project = _sandbox_project(tmp_path)
    calls: list[tuple[list[str], dict[str, str], str | None]] = []

    def fake_call(cmd, env=None):
        calls.append((cmd, env, hubauth.stored_token(9316)))   # stored while the container runs
        return 0

    monkeypatch.setattr(docker_module, "_docker", lambda: "docker")
    monkeypatch.setattr(docker_module, "image_exists", lambda image=docker_module.IMAGE: True)
    monkeypatch.setattr(docker_module, "image_dockerfile", lambda image=docker_module.IMAGE: docker_module.dockerfile_hash())
    monkeypatch.setattr(docker_module, "container_exists", lambda name: False)
    monkeypatch.setattr(docker_module.subprocess, "call", fake_call)
    monkeypatch.setattr(cli, "_stdin_is_terminal", lambda: False)
    monkeypatch.setenv(hubauth.TOKEN_ENV, "the-hosts-own")
    assert docker_module.run_sandbox(project, 9316) == 0
    assert docker_module.run_sandbox(project, 9316) == 0
    (cmd, env, stored), (_, env2, stored2) = calls
    token = stored
    assert token and len(token) >= 40 and token != stored2 and token != "the-hosts-own"
    assert env[hubauth.DIGEST_ENV] == hubauth.digest(token) and env2[hubauth.DIGEST_ENV] == hubauth.digest(stored2)
    assert hubauth.TOKEN_ENV not in env and not any(token in v for v in env.values())   # the docker CLI: the digest
    assert not any(token in part for part in cmd)
    assert hubauth.stored_token(9316) is None   # removed when the container ends
    err = capsys.readouterr().err
    assert f"http://127.0.0.1:9316/?token={token}" in err
    assert (f'claude mcp add --transport http dreamteam http://127.0.0.1:9316/mcp '
            f'--header "Authorization: Bearer {token}"') in err
    assert "claude mcp add dreamteam -- dreamteam mcp" in err and str(hubauth.tokens_dir()) in err
    # the store inside the mounted folder: nothing stored, the variable named instead
    monkeypatch.setenv(hubauth.TOKENS_DIR_ENV, str(project / "tokens"))
    calls.clear()
    assert docker_module.run_sandbox(project, 9316) == 0
    assert calls[0][2] is None and not (project / "tokens").exists()
    assert f"set {hubauth.TOKEN_ENV}" in capsys.readouterr().err
    # ...nor inside the host's skills library, which is mounted too (read-only, at /dreamteam/host-skills)
    library = docker_module.sandbox_host_library(Config.load(project))
    monkeypatch.setenv(hubauth.TOKENS_DIR_ENV, str(library / "hub-tokens"))
    calls.clear()
    assert docker_module.run_sandbox(project, 9316) == 0
    assert calls[0][2] is None and not (library / "hub-tokens").exists()
    assert f"set {hubauth.TOKEN_ENV}" in capsys.readouterr().err
    # a container of that port runs already: its stored token stays
    monkeypatch.setattr(docker_module, "container_exists", lambda name: name == "dreamteam-9316")
    calls.clear()
    assert docker_module.run_sandbox(project, 9316) == 1 and not calls


# ---------------------------------------------------------------------------------------------- real Docker (opt-in)
DOCKER_IMAGE = f"dreamteam-auth-test:pytest-{os.getpid()}"

NEIGHBOUR_CHECK = r"""
import json, sys, urllib.error, urllib.request
ip, token = sys.argv[1], sys.argv[2]

def status(headers):
    request = urllib.request.Request(f"http://{ip}:8765/api/hub", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code

print(json.dumps({"none": status({}), "wrong": status({"Authorization": "Bearer wrong"}),
                  "token": status({"Authorization": f"Bearer {token}"})}))
"""


def _docker_tests() -> bool:
    if os.environ.get("DREAMTEAM_DOCKER_TESTS") != "1" or not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _status(url: str, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str]]:
    parts = urllib.parse.urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)   # (no redirect is followed)
    try:
        conn.request("GET", parts.path + (f"?{parts.query}" if parts.query else ""), headers=headers or {})
        resp = conn.getresponse()
        resp.read()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}
    finally:
        conn.close()


@pytest.mark.skipif(not _docker_tests(), reason="opt-in: DREAMTEAM_DOCKER_TESTS=1 and a Docker daemon (builds an "
                                                "image under a tag of its own)")
def test_a_neighbour_container_cannot_use_the_sandbox_hub_without_its_token(tmp_path):
    """`dreamteam sandbox` for real, in an image under a test tag: a second container on the same bridge reaches the
    hub's port but gets 401 without the token; the host gets in with the token run_sandbox stored for it."""
    home = tmp_path / "home"
    cli._load_home(home)
    port = _free_port()
    name = docker_module.container_name(port)
    result: dict[str, int] = {}
    runner = threading.Thread(target=lambda: result.update(code=docker_module.run_sandbox(home, port,
                                                                                          image=DOCKER_IMAGE)))
    try:
        docker_module.build_image(DOCKER_IMAGE)
        runner.start()
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 180
        while True:
            token = hubauth.stored_token(port)
            try:
                if token and _status(base + "/api/hub", hubauth.auth_headers(base))[0] == 200:
                    break
            except OSError:
                pass
            assert time.monotonic() < deadline and runner.is_alive(), result
            time.sleep(0.5)
        assert _status(base + "/api/hub")[0] == 401                     # the host without the token
        status, headers = _status(f"{base}/?token={token}")
        assert status == 303 and headers["location"] == "/" and "httponly" in headers["set-cookie"].lower()
        assert cli.running_hub(home) == base                            # `dreamteam console/tui/status` find it
        ip = subprocess.run(["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                             name], capture_output=True, text=True, timeout=30).stdout.strip()
        assert ip, "the container has no bridge address"
        out = subprocess.run(["docker", "run", "--rm", "--entrypoint", "python", DOCKER_IMAGE, "-c", NEIGHBOUR_CHECK,
                              ip, token], capture_output=True, text=True, timeout=120)
        seen = json.loads(out.stdout.strip().splitlines()[-1])
        assert seen == {"none": 401, "wrong": 401, "token": 200}, (seen, out.stderr)
        # the environments of the container's processes hold the digest, never the token
        environ = subprocess.run(["docker", "exec", name, "sh", "-c", "cat /proc/[0-9]*/environ 2>/dev/null; true"],
                                 capture_output=True, timeout=60).stdout
        assert hubauth.digest(token).encode() in environ and token.encode() not in environ
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)
        if runner.ident is not None:
            runner.join(timeout=60)
        subprocess.run(["docker", "image", "rm", "-f", DOCKER_IMAGE], capture_output=True, timeout=120)
    assert hubauth.stored_token(port) is None
