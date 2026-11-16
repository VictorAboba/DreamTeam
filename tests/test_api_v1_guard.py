"""The external HTTP API v1's front door (docs/SPEC.md §14, §21.1, §21.2, §21.9): the guard's v1 branch (only the
Bearer header counts there; the hub token and own_token() are the owner, the API token the API caller; JSON refusals
with their codes), the API token opening no internal route, the dispatch of /api/v1 to ApiV1's own app (JSON 404 and
405, no slash redirect, no HEAD), GET /api/v1/options, the shared request helpers (the bounded JSON body, the unknown
fields), the error codes and the redaction of paths for the API caller, aclose() and what `serve` and `dreamteam
sandbox` print."""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from starlette.requests import Request
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conftest import make_skill
from dreamteam import api_v1, cli, copyfrom, hubauth, web
from dreamteam import docker as docker_module
from dreamteam import sessions as sessions_module
from dreamteam import settings as st
from dreamteam import skills as skills_module
from dreamteam.api_v1 import ApiError, ApiV1
from dreamteam.config import Config
from dreamteam.copyfrom import CopyError
from dreamteam.httpjson import JSONResponse
from dreamteam.launcher import SessionRouter, create_launcher_app
from dreamteam.sessions import SessionError, SessionManager
from dreamteam.skills import SkillError
from test_hub_auth import API_TOKEN, BASE, BEARER, COOKIE, NEIGHBOUR, PEER, TOKEN, WS, _manager, _scripted_swarm

API_BEARER = {"authorization": f"Bearer {API_TOKEN}"}
JSON = {"content-type": "application/json"}


@pytest.fixture
def hub_token(monkeypatch):
    """The hub's token, as main() takes it out of DREAMTEAM_HUB_TOKEN."""
    monkeypatch.setattr(hubauth, "_taken", TOKEN)
    return TOKEN


@pytest.fixture
def api_token(monkeypatch):
    """The API token, as main() takes it out of DREAMTEAM_API_TOKEN (set before the guard is made: it reads the
    digest once)."""
    monkeypatch.setattr(hubauth, "_api_taken", API_TOKEN)
    return API_TOKEN


class _Recorder:
    """An app that answers 200 "ok" and keeps every scope it was given (what the guard let through)."""

    def __init__(self) -> None:
        self.scopes: list[dict[str, Any]] = []

    async def __call__(self, scope, receive, send):
        self.scopes.append(dict(scope))
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})


async def _ask(app, *, path: str = "/api/v1/options", method: str = "GET", headers: dict[str, str] | None = None,
               query: str = "", kind: str = "http", client=NEIGHBOUR, host: str = "127.0.0.1:8765",
               server: str = "172.17.0.2", body: bytes = b"", scope_extra: dict[str, Any] | None = None,
               ) -> tuple[int, dict[str, str], bytes]:
    """(status, response headers, body) of one request made as the server would make it: the path exactly as given
    (no `..` or `//` resolved, as uvicorn passes it), 1008 for a WebSocket closed before it was accepted, 200 for one
    the app took."""
    sent_headers = [(b"host", host.encode()), *((k.encode(), v.encode()) for k, v in (headers or {}).items())]
    scope = {"type": kind, "method": method, "path": path, "raw_path": path.encode(), "headers": sent_headers,
             "query_string": query.encode(), "scheme": "ws" if kind == "websocket" else "http", "root_path": "",
             "server": (server, 8765), "client": client, "http_version": "1.1", **(scope_extra or {})}
    sent: list[dict] = []
    messages = [{"type": "http.request", "body": body, "more_body": False}] if kind == "http" else \
        [{"type": "websocket.connect"}]

    async def receive():
        if messages:
            return messages.pop(0)
        await asyncio.sleep(0.01)
        return {"type": "http.disconnect"} if kind == "http" else {"type": "websocket.disconnect", "code": 1000}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    if not sent:
        return 200, {}, b""
    if sent[0]["type"] == "websocket.close":
        return sent[0].get("code", 1000), {}, b""
    out = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in sent[0].get("headers", [])}
    return sent[0]["status"], out, b"".join(m.get("body", b"") for m in sent[1:])


def _json(body: bytes) -> dict[str, Any]:
    return json.loads(body.decode("utf-8"))


# ---------------------------------------------------------------------------------------------- the predicate
@pytest.mark.parametrize("path, taken", [
    ("/api/v1", True), ("/api/v1/", True), ("/api/v1/options", True), ("/api/v1/../api/settings", True),
    ("/api/v1x", False), ("/api/v10/options", False), ("//api/v1/options", False), ("/s/x/api/v1/options", False),
    ("/API/v1/options", False), ("/api/v2/options", False), ("/api/v", False), ("", False),
])
def test_one_predicate_says_which_paths_are_the_apis(path, taken):
    assert web.is_api_path(path) is taken


# ---------------------------------------------------------------------------------------------- the guard (ASGI)
async def test_on_v1_only_the_bearer_counts_and_says_who_calls(hub_token, api_token):
    """The hub token and own_token() are the owner, the API token the API caller (scope["dreamteam.api_caller"]); the
    hub token's cookie and a GET ?token= count for nothing on v1 (no 303), and nothing else gets in."""
    app = _Recorder()
    guard = web.GuardMiddleware(app, host="0.0.0.0", api=True)

    async def caller(headers: dict[str, str] | None = None, query: str = "") -> tuple[int, Any]:
        before = len(app.scopes)
        status, _, _ = await _ask(guard, headers=headers, query=query)
        return status, (app.scopes[-1].get(web.API_CALLER) if len(app.scopes) > before else None)

    assert await caller(API_BEARER) == (200, "api")
    assert await caller({"authorization": f"bearer  {API_TOKEN} "}) == (200, "api")
    assert await caller(BEARER) == (200, "owner")
    assert await caller({"authorization": f"Bearer {hubauth.own_token()}"}) == (200, "owner")
    for headers, query in (({}, ""), ({"authorization": "Bearer wrong"}, ""), ({"authorization": f"Basic {TOKEN}"}, ""),
                           ({"authorization": f"Bearer {hubauth.digest(API_TOKEN)}"}, ""),
                           ({"cookie": f"{COOKIE}={TOKEN}"}, ""), ({}, f"token={TOKEN}"), ({}, f"token={API_TOKEN}")):
        status, headers_out, body = await _ask(guard, headers=headers, query=query)
        assert status == 401, (headers, query)
        assert headers_out["www-authenticate"] == web.BEARER_REALM and headers_out["content-type"] == "application/json"
        assert _json(body) == {"error": web.API_TOKEN_TEXT, "code": "token_required"}
    assert len(app.scopes) == 4
    # a WebSocket on a v1 path: the same rules, closed before it is accepted
    assert (await _ask(guard, kind="websocket", headers={"cookie": f"{COOKIE}={TOKEN}"}))[0] == 1008
    assert (await _ask(guard, kind="websocket", headers=API_BEARER))[0] == 200


async def test_the_api_token_opens_no_other_path(hub_token, api_token):
    """Off v1 the guard never takes the API token: every internal route, the board, /mcp and the streams answer it
    as a request without a token (the plain 401, a closed WebSocket); paths that only look like v1 are no v1."""
    guard = web.GuardMiddleware(_Recorder(), host="0.0.0.0", api=True,
                                raw_posts={copyfrom.UPLOAD_PATH: "application/zip"})
    for method, path, headers in (("GET", "/api/settings", {}), ("PUT", "/api/settings", JSON),
                                  ("GET", "/api/sessions", {}), ("POST", "/api/sessions", JSON),
                                  ("POST", "/api/uploads", {"content-type": "application/zip"}),
                                  ("GET", "/api/skills", {}), ("GET", "/s/001-x/api/state", {}),
                                  ("POST", "/mcp", JSON), ("GET", "/", {}), ("GET", "//api/v1/options", {}),
                                  ("GET", "/s/x/api/v1/options", {}), ("GET", "/api/v1x", {})):
        status, out, body = await _ask(guard, method=method, path=path, headers={**API_BEARER, **headers})
        assert status == 401 and body.decode() == web.TOKEN_TEXT, (method, path)
        assert out["content-type"].startswith("text/plain")
        assert (await _ask(guard, method=method, path=path, headers={**BEARER, **headers}))[0] == 200, path
    for path in ("/ws", "/s/001-x/ws"):
        assert (await _ask(guard, kind="websocket", path=path, headers=API_BEARER))[0] == 1008
    # the same paths on a guard without the API's branch (`dreamteam run`, the session apps): never the API token
    plain = web.GuardMiddleware(_Recorder(), host="0.0.0.0")
    assert plain.api_digest is None
    status, out, _ = await _ask(plain, headers=API_BEARER)
    assert status == 401 and out["content-type"].startswith("text/plain")


async def test_a_hub_without_either_token_answers_api_disabled():
    """Neither an API token nor a hub token: every v1 request is 403 api_disabled, on loopback too, whatever it sends
    (own_token() included); the other paths ask nothing, as before."""
    for bind, client in (("127.0.0.1", ("127.0.0.1", 50000)), ("0.0.0.0", PEER)):
        guard = web.GuardMiddleware(_Recorder(), host=bind, api=True)
        assert guard.digest is None and guard.api_digest is None
        for headers in ({}, BEARER, {"authorization": f"Bearer {hubauth.own_token()}"}):
            status, out, body = await _ask(guard, headers=headers, client=client)
            assert status == 403 and _json(body) == {"error": web.API_DISABLED_TEXT, "code": "api_disabled"}
            assert "www-authenticate" not in out
        assert (await _ask(guard, path="/api/settings", client=client))[0] == 200


async def test_a_hub_token_alone_serves_v1_to_its_owner(hub_token):
    guard = web.GuardMiddleware(_Recorder(), host="0.0.0.0", api=True)
    assert guard.api_digest is None
    assert (await _ask(guard, headers=BEARER))[0] == 200
    assert (await _ask(guard))[0] == 401
    assert (await _ask(guard, headers=API_BEARER))[0] == 401   # (no API token on this hub)


async def test_an_api_token_alone_serves_v1_and_asks_nothing_elsewhere(api_token):
    """No hub token: v1 needs the API token (or own_token()), the internal routes ask nothing, as on any hub without
    a hub token (serve warns of this, web.api_token_warning)."""
    app = _Recorder()
    guard = web.GuardMiddleware(app, host="127.0.0.1", api=True)
    assert guard.digest is None and guard.api_digest == hubauth.digest(API_TOKEN)
    assert (await _ask(guard, headers=API_BEARER))[0] == 200 and app.scopes[-1][web.API_CALLER] == "api"
    assert (await _ask(guard, headers={"authorization": f"Bearer {hubauth.own_token()}"}))[0] == 200
    assert app.scopes[-1][web.API_CALLER] == "owner"
    assert (await _ask(guard))[0] == 401
    assert (await _ask(guard, path="/api/settings"))[0] == 200
    # the digest is read once, when the guard is made (a malformed digest variable gives another one at every call)
    assert web.GuardMiddleware(app, api=True, api_token=None).api_digest is None
    assert web.GuardMiddleware(app, api=True, api_token="other").api_digest == hubauth.digest("other")


async def test_the_guards_refusals_on_v1_are_json_with_their_codes(hub_token, api_token):
    """forbidden_host, forbidden_origin, token_required, unsupported_media_type as {"error", "code"}; the same refusals
    off v1 keep their plain texts. The Origin rule on v1 is the header's (any scheme of the host and port it was
    made to), never the cookie's."""
    guard = web.GuardMiddleware(_Recorder(), host="0.0.0.0", api=True)
    plain_text = "text/plain"
    cases = [   # (the request on v1 with the API token, its headers off v1 with the hub token, status, code, text)
        (dict(host="rebind.example.com:8765"), {}, 403, "forbidden_host", "Forbidden host"),
        (dict(host="10.1.2.3:8765"), None, 403, "forbidden_host", "Forbidden host"),
        (dict(headers={"origin": "http://127.0.0.1:5173"}), {}, 403, "forbidden_origin", "Forbidden origin"),
        (dict(headers={"origin": "null"}), {}, 403, "forbidden_origin", "Forbidden origin"),
        (dict(method="POST", headers={"content-type": plain_text}), {}, 415, "unsupported_media_type",
         web.JSON_TYPE_TEXT),
        (dict(method="POST"), {}, 415, "unsupported_media_type", web.JSON_TYPE_TEXT),
        (dict(), None, 401, "token_required", web.TOKEN_TEXT),
    ]
    for kwargs, off_v1, status, code, text in cases:
        bearer = {} if off_v1 is None else API_BEARER   # (None: no token at all, on and off v1)
        got, out, body = await _ask(guard, **{**kwargs, "headers": {**kwargs.get("headers", {}), **bearer}})
        assert (got, _json(body)["code"]) == (status, code), kwargs
        assert out["content-type"] == "application/json" and set(_json(body)) == {"error", "code"}
        hub = {} if off_v1 is None else BEARER
        plain, out, body = await _ask(guard, **{**kwargs, "path": "/api/state",
                                                "headers": {**kwargs.get("headers", {}), **hub}})
        assert plain == status and body.decode() == text and out["content-type"].startswith(plain_text), kwargs
    # an IP literal the connection did not reach: a valid Bearer counts as the token, the API token too
    assert (await _ask(guard, host="10.1.2.3:8765", headers=API_BEARER))[0] == 200
    assert (await _ask(guard, headers={**API_BEARER, "origin": "https://127.0.0.1:8765"}))[0] == 200
    assert (await _ask(guard, method="POST", headers={**API_BEARER, **JSON}))[0] == 200
    assert (await _ask(guard, method="PATCH", headers=API_BEARER))[0] == 200   # (PATCH: the handler checks the type)
    # on v1 exactly application/json (a charset aside), as the PATCH handler checks it; off v1 the rule of before
    for kind, on_v1 in (("Application/JSON; charset=utf-8", 200), ("application/jsonx", 415),
                        ("application/json-seq", 415)):
        got, _, body = await _ask(guard, method="POST", headers={**API_BEARER, "content-type": kind})
        assert got == on_v1, kind
        if got == 415:
            assert _json(body) == {"error": web.JSON_TYPE_TEXT, "code": "unsupported_media_type"}
        assert (await _ask(guard, method="POST", path="/api/state", headers={**BEARER, "content-type": kind}))[0] \
            == 200, kind


async def test_both_upload_types_pass_the_guard_on_v1_only(tmp_path, hub_token, api_token):
    """raw_posts maps a path to a set of types (a plain string is one): /api/v1/uploads takes application/octet-stream
    and application/zip and nothing else, JSON included (it stores any body it takes as a file: docs/SPEC.md §21.3);
    the board's /api/uploads application/zip, and JSON as before."""
    manager = _manager(tmp_path)
    app = create_launcher_app(manager, "0.0.0.0")
    await manager.shutdown()
    types = ("application/octet-stream", "application/zip")
    guard = web.GuardMiddleware(_Recorder(), host="0.0.0.0", api=True,
                                raw_posts={copyfrom.UPLOAD_PATH: "Application/Zip", copyfrom.API_UPLOAD_PATH: types})
    assert guard.raw_posts == app.raw_posts == {
        copyfrom.UPLOAD_PATH: frozenset({"application/zip"}),
        copyfrom.API_UPLOAD_PATH: frozenset({"application/octet-stream", "application/zip"})}

    async def upload(path: str, kind: str, headers: dict[str, str] = API_BEARER) -> int:
        return (await _ask(guard, method="POST", path=path, headers={**headers, "content-type": kind}))[0]

    for kind in ("application/octet-stream", "application/zip", "Application/Zip; x=1"):
        assert await upload(copyfrom.API_UPLOAD_PATH, kind) == 200, kind
    for kind in ("text/plain", "application/json", "application/json; charset=utf-8", ""):
        status, out, body = await _ask(guard, method="POST", path=copyfrom.API_UPLOAD_PATH,
                                       headers={**API_BEARER, "content-type": kind})
        assert status == 415 and out["content-type"] == "application/json", kind
        assert _json(body) == {"error": "Content-Type must be application/octet-stream or application/zip",
                               "code": "unsupported_media_type"}
    assert await upload(copyfrom.API_UPLOAD_PATH + "/", "application/zip") == 415   # (exact paths only)
    assert await upload(copyfrom.API_UPLOAD_PATH + "/", "application/json") == 200   # (no upload path: JSON)
    assert await upload(copyfrom.UPLOAD_PATH, "application/zip", BEARER) == 200
    assert await upload(copyfrom.UPLOAD_PATH, "application/json", BEARER) == 200   # (off v1: the rule of before)
    assert await upload(copyfrom.UPLOAD_PATH, "application/octet-stream", BEARER) == 415


async def test_no_request_brings_its_own_caller(hub_token, api_token):
    """Only the v1 branch says who calls: a scope that arrives with the key (another middleware, a test) loses it on
    every other path."""
    app = _Recorder()
    guard = web.GuardMiddleware(app, host="0.0.0.0", api=True)
    assert (await _ask(guard, path="/api/state", headers=BEARER, scope_extra={web.API_CALLER: "owner"}))[0] == 200
    assert web.API_CALLER not in app.scopes[-1]
    assert (await _ask(guard, headers=API_BEARER, scope_extra={web.API_CALLER: "owner"}))[0] == 200
    assert app.scopes[-1][web.API_CALLER] == "api"


# ---------------------------------------------------------------------------------------------- the launcher
def _expect_ws_refused(c: TestClient, path: str, headers: dict[str, str]) -> None:
    with pytest.raises(WebSocketDisconnect) as refused:
        with c.websocket_connect(WS + path, headers=headers) as ws:
            ws.receive_text()
    assert refused.value.code == 1008


def test_the_launcher_gives_the_api_token_v1_alone(tmp_path, hub_token, api_token):
    """A launcher with both tokens: the API token reads /api/v1/options and opens no internal route (the settings,
    the sessions, the uploads, the skills, a session's board and API, /mcp, both streams); the hub token opens
    both."""
    manager = _manager(tmp_path)
    with TestClient(create_launcher_app(manager, "0.0.0.0"), base_url=BASE, client=PEER) as c:
        try:
            made = c.post("/api/sessions", content=json.dumps({"title": "T", "goal": "G"}), headers={**JSON, **BEARER})
            assert made.status_code == 201, made.text
            sid = made.json()["session"]["id"]
            for method, path, kwargs in (
                    ("GET", "/api/settings", {}), ("PUT", "/api/settings", {"content": "{}", "headers": JSON}),
                    ("GET", "/api/sessions", {}), ("POST", "/api/sessions", {"content": "{}", "headers": JSON}),
                    ("POST", "/api/uploads?filename=a.zip", {"content": b"PK", "headers": {
                        "content-type": "application/zip"}}),
                    ("GET", "/api/skills", {}), ("GET", f"/s/{sid}/api/state", {}), ("GET", f"/s/{sid}/", {}),
                    ("POST", "/mcp", {"content": "{}", "headers": JSON}), ("GET", "/", {}), ("GET", "/api/hub", {})):
                headers = {**kwargs.pop("headers", {}), **API_BEARER}
                answer = c.request(method, path, headers=headers, **kwargs)
                assert answer.status_code == 401 and answer.text == web.TOKEN_TEXT, (method, path)
            _expect_ws_refused(c, "/ws", API_BEARER)
            _expect_ws_refused(c, f"/s/{sid}/ws", API_BEARER)
            assert c.get("/api/sessions", headers=BEARER).status_code == 200
            for headers in (API_BEARER, BEARER):
                answer = c.get("/api/v1/options", headers=headers)
                assert answer.status_code == 200 and answer.json()["api"] == 1
            assert c.get(f"/api/v1/options?token={TOKEN}", follow_redirects=False).status_code == 401   # no 303
            c.cookies.set(COOKIE, TOKEN)
            assert c.get("/api/sessions").status_code == 200   # (the cookie works off v1...)
            refused = c.get("/api/v1/options")                 # (...and not on v1)
            assert refused.status_code == 401 and refused.json()["code"] == "token_required"
        finally:
            c.portal.call(manager.shutdown)


async def test_paths_that_only_look_like_v1_reach_neither_v1_nor_the_internal_routes(tmp_path, hub_token, api_token):
    """`..` is never resolved: /api/v1/../api/settings is a v1 path, which v1 answers 404 (never the settings);
    //api/v1/options and /s/x/api/v1/options are no v1 paths, which the API token does not open."""
    manager = _manager(tmp_path)
    app = create_launcher_app(manager, "0.0.0.0")
    try:
        for path in ("/api/v1/../api/settings", "/api/v1/%2e%2e/api/settings", "/api/v1/./options"):
            status, out, body = await _ask(app, path=path, headers=API_BEARER)
            assert status == 404 and _json(body)["code"] == "not_found", path
        for path in ("//api/v1/options", "/s/x/api/v1/options", "/api/v1x"):
            status, out, body = await _ask(app, path=path, headers=API_BEARER)
            assert status == 401 and body.decode() == web.TOKEN_TEXT, path
        assert (await _ask(app, headers=API_BEARER))[0] == 200
    finally:
        await manager.shutdown()


def test_wrong_paths_and_methods_are_json(tmp_path, api_token):
    """ApiV1's own app: a JSON 404 for any path it lacks, a bare /api/v1 and a trailing slash included (never a 307);
    a JSON 405 with Allow for a method a route does not take, HEAD and OPTIONS included."""
    manager = _manager(tmp_path)
    with TestClient(create_launcher_app(manager, "127.0.0.1"), base_url=BASE) as c:
        try:
            for path in ("/api/v1", "/api/v1/", "/api/v1/options/", "/api/v1/nope", "/api/v1/sessions/x/y/z"):
                for method in ("GET", "POST", "DELETE"):
                    answer = c.request(method, path, headers={**API_BEARER, **JSON}, follow_redirects=False)
                    assert answer.status_code == 404, (method, path)
                    assert answer.json() == {"error": api_v1.NOT_FOUND_TEXT, "code": "not_found"}
            for method in ("POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
                answer = c.request(method, "/api/v1/options", headers={**API_BEARER, **JSON})
                assert answer.status_code == 405 and answer.headers["allow"] == "GET", method
                if method != "HEAD":
                    assert answer.json()["code"] == "method_not_allowed" and "use GET" in answer.json()["error"]
        finally:
            c.portal.call(manager.shutdown)


def test_options_say_what_a_client_may_choose_and_nothing_secret(tmp_path, api_token, monkeypatch):
    """GET /api/v1/options: the version, the languages, the teams, the visible roles, the models by name, provider
    and model, the default model, the library's skills (the host library's too in the sandbox; a name in both is the
    library's; never a broken one), the roots of [api] folders by name, and the limits. Never a key, a base_url, an
    env name or a path."""
    home = tmp_path / "home"
    home.mkdir()
    secret_key, secret_env, secret_url = "sk-secret-123456", "MY_SECRET_KEY_ENV", "https://llm.internal.example/v1"
    root = tmp_path / "srv" / "jobs"
    st.write_raw(home, {
        "swarm": {"name": "Home", "language": "en", "start_agents": 0},
        "workspace": {"copy_max_mb": 7, "copy_max_files": 70},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0},
                   "remote": {"provider": "openai", "model": "gpt-x", "base_url": secret_url, "api_key": secret_key},
                   "envkey": {"provider": "anthropic", "model": "claude-x", "api_key_env": secret_env}},
        "roles": [{"name": "Hidden one", "description": "Never offered.", "hidden": True},
                  {"name": "Writer", "description": "Writes the texts."}],
        "teams": [{"name": "Docs", "roles": ["writer", "tester"]}],
        "api": {"max_running": 2, "uploads_pending": 5, "archive_max_mb": 9,
                "folders": {"jobs": {"path": str(root), "in_place": True}, "inbox": {"path": str(tmp_path / "in")}}},
    })
    library = Path(os.environ["DREAMTEAM_SKILLS_DIR"])
    make_skill(library / "demo", "demo", "The library's demo.")
    (library / "broken").mkdir(parents=True)
    host = tmp_path / "host-skills"
    make_skill(host / "demo", "demo", "The host's demo.")
    make_skill(host / "hosted", "hosted", "Only on the host.")
    monkeypatch.setenv("DREAMTEAM_HOST_SKILLS_DIR", str(host))
    manager = SessionManager(home)
    with TestClient(create_launcher_app(manager, "127.0.0.1"), base_url=BASE) as c:
        try:
            answer = c.get("/api/v1/options", headers=API_BEARER)
            assert answer.status_code == 200, answer.text
            options = answer.json()
            assert list(options) == ["api", "version", "languages", "language", "teams", "roles", "models",
                                     "default_model", "skills", "folders", "limits"]
            assert options["api"] == 1 and options["version"] == api_v1.__version__
            assert options["languages"] == ["ru", "en"] and options["language"] == "en"
            assert options["teams"] == [{"name": "Docs", "roles": ["writer", "tester"]}]
            keys = [role["key"] for role in options["roles"]]
            assert "writer" in keys and "hidden one" not in keys and "generalist" in keys
            assert all(set(role) == {"key", "name", "description"} for role in options["roles"])
            assert options["models"] == [{"name": "default", "provider": "scripted", "model": "scripted"},
                                         {"name": "remote", "provider": "openai", "model": "gpt-x"},
                                         {"name": "envkey", "provider": "anthropic", "model": "claude-x"}]
            assert options["default_model"] == "default"
            assert options["skills"] == [{"name": "demo", "description": "The library's demo."},
                                         {"name": "hosted", "description": "Only on the host."}]
            assert options["folders"] == [{"name": "inbox", "in_place": False}, {"name": "jobs", "in_place": True}]
            assert options["limits"] == {"upload_max_mb": 7, "upload_max_files": 70, "files_per_session": 20,
                                         "uploads_pending": 5, "max_running": 2, "archive_max_mb": 9}
            text = answer.text
            for secret in (secret_key, secret_env, secret_url, "base_url", "api_key", "SKILLS_DIR", "DREAMTEAM_",
                           str(root), str(tmp_path), json.dumps(str(tmp_path))[1:-1], str(host)):
                assert secret not in text, secret
            assert c.get("/api/v1/options", headers=BEARER).status_code == 401   # (this hub has no hub token)
            refused = c.get("/api/v1/options?lang=en", headers=API_BEARER)   # (options read no query)
            assert refused.status_code == 400 and refused.json()["code"] == "unknown_field"
            assert "'lang'" in refused.json()["error"]
            # a hand edit is picked up at the next request
            raw = st.read_raw(home)
            raw["api"]["max_running"] = 10
            st.write_raw(home, raw)
            assert c.get("/api/v1/options", headers=API_BEARER).json()["limits"]["max_running"] == 10
        finally:
            c.portal.call(manager.shutdown)


async def test_dreamteam_run_never_takes_the_api_token(tmp_path, monkeypatch):
    """web.create_app (`dreamteam run`, a session's own app) has no v1 branch: with a hub token the API token is a
    request without a token anywhere; without one the hub asks nothing and has no /api/v1."""
    swarm = _scripted_swarm(tmp_path)
    await swarm.start()
    try:
        monkeypatch.setattr(hubauth, "_api_taken", API_TOKEN)
        monkeypatch.setattr(hubauth, "_taken", TOKEN)
        app = web.create_app(swarm, "0.0.0.0")
        for path in ("/api/v1/options", "/api/state", "/mcp"):
            status, out, body = await _ask(app, path=path, headers=API_BEARER)
            assert status == 401 and body.decode() == web.TOKEN_TEXT, path
        assert (await _ask(app, path="/api/v1/options", headers=BEARER))[0] == 404   # (no such route)
        monkeypatch.setattr(hubauth, "_taken", None)
        open_app = web.create_app(swarm, "127.0.0.1")
        status, out, body = await _ask(open_app, path="/api/v1/options", headers=API_BEARER)
        assert status == 404 and out["content-type"].startswith("text/plain")
    finally:
        await swarm.stop()
        swarm.close()


def test_the_lifespan_closes_the_api_before_the_sessions(tmp_path):
    manager = _manager(tmp_path)
    app = create_launcher_app(manager, "127.0.0.1")
    order: list[str] = []
    api_close, router_close = app.api.aclose, app.session_router.aclose

    async def closing_api():
        order.append("api")
        await api_close()

    async def closing_router():
        order.append("router")
        await router_close()

    app.api.aclose = closing_api
    app.session_router.aclose = closing_router
    assert isinstance(app.api, ApiV1) and isinstance(app.session_router, SessionRouter)
    with TestClient(app, base_url=BASE) as c:
        c.portal.call(manager.shutdown)
    assert order == ["api", "router"] and app.api.closing


# ---------------------------------------------------------------------------------------------- the helpers
def _request(body: bytes | list[bytes], headers: dict[str, str] | None = None, query: str = "") -> Request:
    chunks = list(body) if isinstance(body, list) else [body]
    read: list[int] = []

    async def receive():
        read.append(1)
        if chunks:
            chunk = chunks.pop(0)
            return {"type": "http.request", "body": chunk, "more_body": bool(chunks)}
        return {"type": "http.disconnect"}

    scope = {"type": "http", "method": "POST", "path": "/api/v1/x", "query_string": query.encode(),
             "headers": [(k.encode(), v.encode()) for k, v in (headers or {}).items()]}
    request = Request(scope, receive)
    request.read = read   # type: ignore[attr-defined]
    return request


async def test_a_json_body_is_bounded_and_must_be_an_object():
    assert await api_v1.read_json(_request(b"")) == {}
    assert await api_v1.read_json(_request(b"  \n")) == {}
    assert await api_v1.read_json(_request([b'{"a": ', b'[1, 2]}'])) == {"a": [1, 2]}
    assert await api_v1.read_json(_request(b'{"a": 1.5, "b": -2e3, "c": 1.7e308}')) == {"a": 1.5, "b": -2000.0,
                                                                                        "c": 1.7e308}
    # NaN and Infinity, also a number that overflows to it (httpjson could never write it back)
    for body in (b"{", b"[1]", b'"text"', b"null", b'{"a": NaN}', b'{"a": Infinity}', b'{"a": -Infinity}',
                 b'{"a": 1e999}', b'{"a": -1e400}', b'{"a": [1, 2.5e308]}', b"\xff\xfe{"):
        with pytest.raises(ApiError) as refused:
            await api_v1.read_json(_request(body))
        assert refused.value.code == "bad_request", body
    big = b'{"a": "' + b"x" * api_v1.JSON_MAX + b'"}'
    with pytest.raises(ApiError) as refused:   # streamed: refused once the limit is passed
        await api_v1.read_json(_request([big[:1000], big[1000:]]))
    assert refused.value.code == "too_large" and refused.value.response().status_code == 413
    told = _request(b"{}", {"content-length": str(api_v1.JSON_MAX + 1)})
    with pytest.raises(ApiError) as refused:   # the Content-Length says more: nothing is read
        await api_v1.read_json(told)
    assert refused.value.code == "too_large" and told.read == []
    exact = b'{"a": "' + b"x" * (api_v1.JSON_MAX - 9) + b'"}'
    assert len(exact) == api_v1.JSON_MAX and len((await api_v1.read_json(_request(exact)))["a"]) == api_v1.JSON_MAX - 9


def test_unknown_fields_of_a_body_or_a_query():
    api_v1.check_fields({"goal": 1, "title": 2}, ("goal", "title", "team"))
    with pytest.raises(ApiError) as refused:
        api_v1.check_fields({"goal": 1, "tittle": 2, "x" * 200: 3}, ("goal", "title"))
    assert refused.value.code == "unknown_field" and refused.value.response().status_code == 400
    text = refused.value.text
    assert text.startswith("Unknown fields in the body: 'tittle', 'xxx") and "Known: goal, title." in text
    assert "x" * (api_v1.FIELD_CHARS + 1) not in text
    assert api_v1.query_fields(_request(b"", query="after=3&limit=10"), ("after", "limit", "wait")) == {
        "after": "3", "limit": "10"}
    with pytest.raises(ApiError) as refused:
        api_v1.query_fields(_request(b"", query="after=3&after=4"), ("after",))
    assert refused.value.code == "bad_request" and "'after'" in refused.value.text
    with pytest.raises(ApiError) as refused:
        api_v1.query_fields(_request(b"", query="afer=3"), ("after",))
    assert refused.value.code == "unknown_field" and "in the query" in refused.value.text
    with pytest.raises(ValueError):
        ApiError("no_such_code", "x")


def test_every_429_the_api_can_meet_names_its_code():
    """A 429 without a code would read `bad_request` (error_code: another 4xx), which tells a client not to retry:
    every SessionError, CopyError or SkillError raised with 429 in the modules v1 calls carries its code."""
    found = 0
    for module in (sessions_module, copyfrom, skills_module):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name not in ("SessionError", "CopyError", "SkillError"):
                continue
            status = node.args[1] if len(node.args) > 1 else next(
                (keyword.value for keyword in node.keywords if keyword.arg == "status"), None)
            if isinstance(status, ast.Constant) and status.value == 429:
                found += 1
                assert any(keyword.arg == "code" for keyword in node.keywords), (module.__name__, node.lineno)
    assert found >= 1   # (copyfrom's too_many_uploads: the scan sees them)


def test_every_code_has_its_status_and_errors_take_theirs():
    """The codes of docs/SPEC.md §21.1 with their statuses; a manager's error by its own code when v1 knows it (a
    code decides the status: folder_not_ready is 409 also where the internal routes answer 400), an earlier version's
    session by its flag, anything else by its status."""
    assert copyfrom.UPLOAD_CALLERS == web.API_CALLERS == ("owner", "api")
    for code in ("token_required", "api_disabled", "forbidden_host", "forbidden_origin", "unsupported_media_type"):
        assert code in api_v1.CODES   # (the guard's)
    assert {status for status in api_v1.CODES.values()} == {400, 401, 403, 404, 405, 409, 413, 415, 429, 500}
    cases = [
        (SessionError("x", 400, code="folder_not_ready"), "folder_not_ready", 409),
        (SessionError("x", 409, code="folder_busy"), "folder_busy", 409),
        (SessionError("x", 409, code="not_a_v1_code"), "conflict", 409),
        (SessionError("x", 409, earlier_version=True), "earlier_version", 409),
        (SessionError("x", 404), "not_found", 404), (SessionError("x"), "bad_request", 400),
        (SessionError("x", 413), "too_large", 413), (SessionError("x", 500), "internal", 500),
        (SessionError("shutting down", 503), "internal", 500),
        (CopyError("x", 429, code="too_many_uploads"), "too_many_uploads", 429),
        (CopyError("x", 400, code="bad_name"), "bad_name", 400),
        (CopyError("x", 409, code="file_conflict"), "file_conflict", 409),
        (CopyError("x", 413), "too_large", 413), (SkillError("read-only", 403), "bad_request", 400),
        (ValueError("x"), "internal", 500), (RuntimeError("x"), "internal", 500),
    ]
    for exc, code, status in cases:
        assert api_v1.error_code(exc) == code, exc
        assert api_v1.CODES[code] == status


async def test_error_texts_name_no_path_of_the_hubs_computer_for_the_api_caller(tmp_path, caplog):
    """For the API caller an error text names a root of [api] folders as <root>/<path>, the home and the user's home
    by name, a path in another session's folder as OTHER_SESSION (its folder name is its title), any other absolute
    path by its last component; the owner gets the text as it is. A root that is not absolute on this system is never
    named (nor resolved)."""
    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / "srv" / "jobs"
    foreign = "/srv/elsewhere" if os.name == "nt" else "C:\\elsewhere"
    st.write_raw(home, {
        "swarm": {"name": "Home", "language": "en", "start_agents": 0},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
        "api": {"folders": {"jobs": {"path": str(root)}, "other": {"path": foreign}}},
    })
    manager = SessionManager(home)
    api = ApiV1(manager, "127.0.0.1")
    assert api_v1.root_pairs(manager.config)[0] == (root, "jobs")
    assert all(name == "jobs" for _, name in api_v1.root_pairs(manager.config))
    below = root / "acme" / "my report.txt"
    outside = tmp_path / "private" / "keys.txt"
    text = f"Cannot read {below}: no such file. Also {outside} and {home / 'sessions'}."
    error = SessionError(text, 404)
    answer = await api.error_response(error, "api")
    shown = json.loads(answer.body)
    assert answer.status_code == 404 and shown["code"] == "not_found"
    assert shown["error"] == (f"Cannot read jobs{os.sep}acme{os.sep}my report.txt: no such file. Also keys.txt and "
                              "sessions.")
    assert json.loads((await api.error_response(error, "owner")).body)["error"] == text
    assert api.redact(f"see {foreign}{os.sep}x") == "see x"   # (never "other/x": that root is not one here)
    # another session's folder (named by its title) is never named, nor what lies in it; the session's own is
    other = home / "sessions" / "003-acquisition-of-acme-corp"
    mine = home / "sessions" / "004-mine"
    told = f"Cannot use {other / 'workspace' / 'plan.md'}. Nor {other}, {other.as_posix()}/x or {home / 'skills'}."
    hidden = SessionManager.OTHER_SESSION
    assert api.redact(told) == f"Cannot use {hidden}. Nor {hidden}, {hidden} or home{os.sep}skills."
    told = f"{mine / 'workspace' / 'a.txt'} is not {other / 'workspace' / 'a.txt'}; see {home / 'sessions'}."
    assert api.redact(told, "004-mine") == f"004-mine{os.sep}workspace{os.sep}a.txt is not {hidden}; see sessions."
    assert "acquisition" not in json.loads((await api.error_response(SessionError(told, 409), "api")).body)["error"]
    caplog.set_level("INFO")
    await api.error_response(SessionError(f"Session is damaged: {outside}", 500), "api")
    damaged = [record for record in caplog.records if "damaged" in record.getMessage()]
    assert damaged and damaged[0].levelno == logging.ERROR and damaged[0].exc_info   # (internal: logged)
    answer = await api.error_response(SessionError("DreamTeam is shutting down.", 503), "api")
    assert answer.status_code == 500 and json.loads(answer.body)["code"] == "internal"
    closing = [record for record in caplog.records if "shutting down" in record.getMessage()]
    assert closing and closing[0].levelno == logging.INFO and not closing[0].exc_info   # (no fault: no traceback)


async def test_a_route_without_the_guards_caller_and_an_unexpected_error(tmp_path, caplog):
    """ApiV1.app reached without the guard's v1 branch answers 401; an unexpected error is 500 internal, logged, with
    no text of its own (it may name a path)."""
    manager = _manager(tmp_path)
    api = ApiV1(manager, "127.0.0.1")
    status, out, body = await _ask(api.app)
    assert status == 401 and _json(body)["code"] == "token_required" and out["www-authenticate"] == web.BEARER_REALM

    def broken():
        raise RuntimeError(f"cannot read {tmp_path / 'secret'}")

    manager.global_config = broken   # type: ignore[method-assign]
    caplog.set_level("ERROR")
    status, _, body = await _ask(api.app, scope_extra={web.API_CALLER: "api"})
    assert status == 500 and _json(body) == {
        "error": "Internal error (RuntimeError): the hub's log has the details.", "code": "internal"}
    assert any(record.exc_info and "cannot read" in str(record.exc_info[1]) for record in caplog.records)

    class Coded(ValueError):   # (an error of no class of CODED_ERRORS: its `code` and its text count for nothing)
        code = "unknown_team"

    async def refusing(request, caller):
        raise SessionError(f"No saved team 'x' (in {tmp_path / 'teams'}).", code="unknown_team")

    async def other(request, caller):
        raise Coded(f"No saved team 'x' (in {tmp_path}).")

    api.app.router.routes += [api._route("/api/v1/coded", {"GET": refusing}),
                              api._route("/api/v1/other", {"GET": other})]
    status, _, body = await _ask(api.app, path="/api/v1/coded", scope_extra={web.API_CALLER: "api"})
    assert status == 400 and _json(body) == {"error": "No saved team 'x' (in teams).", "code": "unknown_team"}
    status, _, body = await _ask(api.app, path="/api/v1/other", scope_extra={web.API_CALLER: "api"})
    assert status == 500 and _json(body) == {
        "error": "Internal error (Coded): the hub's log has the details.", "code": "internal"}
    await manager.shutdown()


async def test_a_path_has_one_route_and_its_405_names_every_method(tmp_path):
    """Two methods of one path are one route (ApiV1._route): a third method is 405 with an Allow naming both, sorted
    (Starlette answers a wrong method with the first partial match alone and joins a set); each method reaches its
    own handler."""
    api = ApiV1(_manager(tmp_path), "127.0.0.1")

    def answering(text: str):
        async def handler(request, caller):
            return JSONResponse({"by": text, "caller": caller})
        return handler

    api.app.router.routes.append(api._route("/api/v1/two", {"get": answering("get"), "DELETE": answering("delete"),
                                                             "PATCH": answering("patch")}))
    caller = {web.API_CALLER: "owner"}
    for method in ("GET", "DELETE", "PATCH"):
        status, _, body = await _ask(api.app, method=method, path="/api/v1/two", scope_extra=caller)
        assert status == 200 and _json(body) == {"by": method.lower(), "caller": "owner"}
    for method in ("POST", "PUT", "HEAD", "OPTIONS"):
        status, out, body = await _ask(api.app, method=method, path="/api/v1/two", scope_extra=caller)
        assert status == 405 and out["allow"] == "DELETE, GET, PATCH", method
        assert _json(body) == {"error": f"{method} is not allowed here: use DELETE, GET, PATCH.",
                               "code": "method_not_allowed"}
    await api.manager.shutdown()


# ---------------------------------------------------------------------------------------------- aclose
async def test_aclose_waits_for_the_starts_in_flight_and_never_cancels_them(tmp_path, monkeypatch):
    """docs/SPEC.md §21.9: a start cancelled inside swarm.start() would skip its cleanup, so aclose() waits for the
    background tasks, at most START_WAIT, and leaves the rest running (manager.shutdown() waits for the session
    locks); a cancel of aclose() itself cancels none of them either."""
    monkeypatch.setattr(api_v1, "START_WAIT", 0.3)
    api = ApiV1(_manager(tmp_path), "127.0.0.1")
    release = asyncio.Event()
    finished: list[str] = []

    async def start(name: str, event: asyncio.Event | None) -> None:
        if event is not None:
            await event.wait()
        await asyncio.sleep(0.01)
        finished.append(name)

    quick = api.spawn(start("quick", None), name="quick")
    slow = api.spawn(start("slow", release), name="slow")
    loop = asyncio.get_running_loop()
    began = loop.time()
    await api.aclose()
    assert 0.25 <= loop.time() - began < 1.0
    assert api.closing and quick.done() and not slow.done() and not slow.cancelled() and finished == ["quick"]
    closing = asyncio.create_task(api.aclose())
    await asyncio.sleep(0.05)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert not slow.done()
    release.set()
    await asyncio.wait_for(slow, 1.0)
    assert finished == ["quick", "slow"] and not api._tasks
    began = loop.time()
    await api.aclose()   # nothing in flight: at once
    assert loop.time() - began < 0.1


async def test_a_failed_background_task_is_logged(tmp_path, caplog):
    api = ApiV1(_manager(tmp_path), "127.0.0.1")
    caplog.set_level("ERROR")

    async def failing():
        raise RuntimeError("the start broke")

    task = api.spawn(failing(), name="start of 001-x")
    with pytest.raises(RuntimeError):
        await task
    await asyncio.sleep(0)
    assert any("start of 001-x" in record.getMessage() for record in caplog.records) and not api._tasks


# ---------------------------------------------------------------------------------------------- what is printed
async def test_serve_prints_the_apis_address_when_it_has_a_token(tmp_path, monkeypatch, capsys):
    from dreamteam import launcher

    async def fake_launcher(manager, host, port, on_started=None, on_listening=None):
        on_listening()
        on_started()
        await asyncio.Event().wait()

    monkeypatch.setattr(launcher, "serve_launcher", fake_launcher)
    monkeypatch.setattr(cli, "_port_free", lambda host, port: True)
    home = tmp_path / "home"
    (home / "sessions").mkdir(parents=True)
    st.write_raw(home, {"swarm": {"name": "Run", "language": "en", "start_agents": 0},
                        "skills": {"env_retention_days": 0}, "mcp": {"library_dir": str(tmp_path / "library")},
                        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0,
                                               "output_price": 0}}})
    stop = asyncio.Event()
    stop.set()
    assert await cli._serve_home(home, "127.0.0.1", 1, console=False, open_browser=False, stop=stop) == 0
    assert "API:" not in capsys.readouterr().err
    monkeypatch.setattr(hubauth, "_api_taken", API_TOKEN)
    assert await cli._serve_home(home, "127.0.0.1", 1, console=False, open_browser=False, stop=stop) == 0
    err = capsys.readouterr().err
    assert "  MCP:      http://127.0.0.1:1/mcp\n  API:      http://127.0.0.1:1/api/v1\n  Home:" in err
    assert API_TOKEN not in err and hubauth.digest(API_TOKEN) not in err


def test_the_sandbox_prints_the_apis_address_for_a_home_with_an_api_token(tmp_path, monkeypatch, capsys):
    lines = docker_module.access_lines(tmp_path, 9318, "the-token", None, True, api=True)
    assert lines[-2] == ("API:      http://127.0.0.1:9318/api/v1  "
                         f"(header Authorization: Bearer <your {hubauth.API_TOKEN_ENV}>)")
    assert not any("api/v1" in line for line in docker_module.access_lines(tmp_path, 9318, "t", None, True))
    project = tmp_path / "proj"
    project.mkdir()
    (project / "dreamteam.toml").write_text("", encoding="utf-8")
    (project / "sessions").mkdir()   # a home
    monkeypatch.setattr(docker_module, "_docker", lambda: "docker")
    monkeypatch.setattr(docker_module, "image_exists", lambda image=docker_module.IMAGE: True)
    same = docker_module.dockerfile_hash()
    monkeypatch.setattr(docker_module, "image_dockerfile", lambda image=docker_module.IMAGE: same)
    monkeypatch.setattr(docker_module, "container_exists", lambda name: False)
    monkeypatch.setattr(docker_module.subprocess, "call", lambda cmd, env=None: 0)
    monkeypatch.setattr(cli, "_stdin_is_terminal", lambda: False)
    monkeypatch.setattr(hubauth, "_api_taken", API_TOKEN)
    for home, shown in ((None, True), (False, False)):
        assert docker_module.run_sandbox(project, 9318, home=home) == 0
        err = capsys.readouterr().err
        assert ("http://127.0.0.1:9318/api/v1" in err) is shown, home
        assert API_TOKEN not in err and hubauth.digest(API_TOKEN) not in err
    monkeypatch.setattr(hubauth, "_api_taken", None)
    assert docker_module.run_sandbox(project, 9318) == 0
    assert "api/v1" not in capsys.readouterr().err


def test_the_api_imports_no_tui():
    """Hub code never imports dreamteam.tui (its package imports prompt_toolkit): api_v1 and the launcher that mounts
    it, in a fresh isolated interpreter with this run's sources first on its path."""
    src = str(Path(api_v1.__file__).resolve().parent.parent)
    code = (f"import sys\nsys.path.insert(0, {src!r})\nimport dreamteam.api_v1, dreamteam.launcher\n"
            f"assert dreamteam.__file__.startswith({src!r}), dreamteam.__file__\n"
            "bad = sorted(m for m in sys.modules if m.startswith(('prompt_toolkit', 'rich', 'dreamteam.tui')))\n"
            "assert not bad, bad\n")
    done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr


def test_a_config_for_the_options_is_the_global_one(tmp_path):
    """options_view reads the settings it is given alone (the launcher passes manager.global_config())."""
    config = Config.from_dict(tmp_path / "p", {"swarm": {"language": "ru"}})
    view = api_v1.options_view(config, [])
    assert view["language"] == "ru" and view["folders"] == [] and view["skills"] == []
    assert [role["key"] for role in view["roles"]][0] == "generalist"
