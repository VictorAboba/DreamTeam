"""Outward-facing surfaces: web guard (Host/Origin/Content-Type), REST, hub MCP (in-process, HTTP, stdio,
stdio->HTTP proxy), hub.json lifecycle, graceful shutdown, the terminal console and the Docker sandbox."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import anyio
import pytest
from proc_guard import kill_tree, read_line, read_rest, spawn

from dreamteam import cli, console as console_module, docker as docker_module, hub_mcp, swarm_skills, web
from dreamteam.cli import main as cli_main
from dreamteam.config import Config
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.models import USER
from dreamteam.swarm import Swarm

WINDOWS = sys.platform == "win32"
REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------------------------- helpers
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(method: str, url: str, body: Any = None, headers: dict[str, str] | None = None,
         timeout: float = 10) -> tuple[int, Any]:
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(raw)
            except ValueError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


JSON = {"Content-Type": "application/json"}


def scripted_config(tmp_path: Path, name: str = "project", **swarm: Any) -> Config:
    raw = {
        "swarm": {"name": "Surface swarm", "language": "en", "start_agents": 0, "max_agents": 6, "max_cost_usd": 0,
                  **swarm},
        "agents": {"stagger_seconds": 0},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
    }
    return Config.from_dict(tmp_path / name, raw)


def scripted_swarm(tmp_path: Path, **swarm: Any) -> Swarm:
    def factory(profile, agent):
        return ScriptedLLM(lambda system, messages, tools: {"tool_calls": [{"name": "wait", "arguments": {}}]}, profile)

    return Swarm(config=scripted_config(tmp_path, **swarm), llm_factory=factory)


async def wait_http(url: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, _ = await asyncio.to_thread(http, "GET", url, None, None, 2)
            if status == 200:
                return
        except OSError:
            pass
        await asyncio.sleep(0.05)
    raise AssertionError(f"{url} did not come up")


def wait_http_sync(url: str, timeout: float = 30, proc: subprocess.Popen | None = None) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            raise AssertionError(f"process exited with {proc.returncode}")
        try:
            if http("GET", url, timeout=2)[0] == 200:
                return
        except OSError:
            pass
        time.sleep(0.1)
    raise AssertionError(f"{url} did not come up")


@contextlib.asynccontextmanager
async def running_server(swarm: Swarm, app_host: str = "127.0.0.1"):
    """Serve create_app(swarm, app_host) on 127.0.0.1:<free port> (app_host decides the guard policy)."""
    import uvicorn

    port = free_port()
    app = web.create_app(swarm, app_host)
    server = web.HubServer(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False,
                                          log_config=None, lifespan="on", timeout_graceful_shutdown=1))
    task = asyncio.create_task(server.serve())
    base = f"http://127.0.0.1:{port}"
    try:
        await wait_http(base + "/api/hub")
        yield base, port
    finally:
        server.should_exit = True
        await asyncio.wait({task}, timeout=10)
        if not task.done():
            task.cancel()


def make_project(tmp_path: Path, name: str = "swarm", port: int | None = None) -> Path:
    project = tmp_path / name
    assert cli_main(["init", str(project), "--lang", "en", "--name", name]) == 0
    path = project / "dreamteam.toml"
    text = path.read_text(encoding="utf-8")
    text = text.replace("start_agents = 3", "start_agents = 0").replace(
        'library_dir = "~/.dreamteam/library"', f'library_dir = "{(tmp_path / "lib").as_posix()}"')
    if port is not None:
        assert "port = 8765" in text
        text = text.replace("port = 8765", f"port = {port}")
    path.write_text(text, encoding="utf-8")
    return project


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env.pop("DREAMTEAM_PUBLIC_URL", None)
    env.pop("DREAMTEAM_ALLOWED_HOSTS", None)
    return env


def group_kwargs() -> dict[str, Any]:
    return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS else {"start_new_session": True}


def interrupt(proc: subprocess.Popen) -> None:
    """Ctrl+Break on Windows (what a console user can send to a process group), SIGTERM elsewhere."""
    if WINDOWS:
        os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
    else:
        proc.send_signal(signal.SIGTERM)


@contextlib.contextmanager
def hub_process(project: Path, port: int, log: Path):
    with open(log, "w", encoding="utf-8") as err:
        proc = spawn([sys.executable, "-m", "dreamteam", "run", str(project), "--port", str(port), "--no-console"],
                     stdin=subprocess.DEVNULL, stdout=err, stderr=err, env=child_env(), **group_kwargs())
        try:
            wait_http_sync(f"http://127.0.0.1:{port}/api/hub", proc=proc)
            yield proc
        finally:
            kill_tree(proc)  # the venv launcher and the hub it runs (proc.kill() ended only the launcher)


# ---------------------------------------------------------------------------------------------- guard (unit)
async def _asgi(guard: web.GuardMiddleware, *, host: str | None, origin: str | None = None, method: str = "GET",
                path: str = "/api/state", kind: str = "http", content_type: str | None = None) -> int:
    headers = []
    if host is not None:
        headers.append((b"host", host.encode("latin-1")))
    if origin is not None:
        headers.append((b"origin", origin.encode("latin-1")))
    if content_type is not None:
        headers.append((b"content-type", content_type.encode()))
    scope = {"type": kind, "method": method, "path": path, "headers": headers, "scheme": "ws" if kind == "websocket" else "http",
             "query_string": b"", "root_path": ""}
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await guard(scope, receive, send)
    if not sent:
        return 200
    if sent[0]["type"] == "websocket.close":
        return 1008
    return sent[0]["status"]


async def _ok_app(scope, receive, send):
    pass


@pytest.mark.parametrize("host, origin, expected", [
    ("127.0.0.1:8765", None, 200),
    ("localhost", None, 200),
    ("LOCALHOST:8765", None, 200),
    ("[::1]:8765", None, 200),
    ("evil.example.com", None, 403),
    ("localhost.evil.example.com:8765", None, 403),
    ("evil.example.com@localhost:8765", None, 403),
    ("192.168.1.20:8765", None, 403),       # loopback bind: only loopback names
    (None, None, 403),
    ("127.0.0.1:8765", "http://127.0.0.1:8765", 200),
    ("127.0.0.1:8765", "http://localhost:3000", 200),   # another local page: allowed like the MCP SDK does
    ("127.0.0.1:8765", "http://evil.example.com", 403),
    ("127.0.0.1:8765", "null", 403),
    ("127.0.0.1:8765", "http://127.0.0.1:8765.evil.example.com", 403),   # used to pass (split on ':')
    ("127.0.0.1:8765", "http://localhost@evil.example.com", 403),
])
async def test_guard_loopback_bind(host, origin, expected):
    guard = web.GuardMiddleware(_ok_app, host="127.0.0.1")
    assert await _asgi(guard, host=host, origin=origin) == expected


@pytest.mark.parametrize("host, origin, expected", [
    ("127.0.0.1:8765", None, 200),            # docker -p 127.0.0.1:8765:8765, browser on the host
    ("localhost:8765", None, 200),
    ("192.168.1.20:8765", None, 200),         # LAN by IP: an IP literal cannot be DNS-rebound
    ("[fe80::1]:8765", None, 200),
    ("evil.example.com:8765", None, 403),     # DNS rebinding: was accepted for 0.0.0.0 binds
    ("127.0.0.1:8765", "http://evil.example.com", 403),   # cross-site: was accepted for 0.0.0.0 binds
    ("127.0.0.1:8765", "http://1.2.3.4:8765", 403),       # an IP origin of someone else's page
    ("192.168.1.20:8765", "http://192.168.1.20:8765", 200),   # same origin
    ("192.168.1.20:8765", "http://192.168.1.20:9999", 403),
    ("192.168.1.20", "http://192.168.1.20", 200),         # default port
])
async def test_guard_wildcard_bind(host, origin, expected):
    guard = web.GuardMiddleware(_ok_app, host="0.0.0.0")
    assert await _asgi(guard, host=host, origin=origin) == expected
    assert await _asgi(guard, host=host, origin=origin, kind="websocket", path="/ws") == (1008 if expected == 403 else 200)


async def test_guard_allowed_hosts_env_and_content_type(monkeypatch):
    monkeypatch.setenv(web.ALLOWED_HOSTS_ENV, "swarm.example.com, Board.Example.org:443")
    assert web.env_allowed_hosts() == {"swarm.example.com", "board.example.org"}
    for bind in ("127.0.0.1", "0.0.0.0"):
        guard = web.GuardMiddleware(_ok_app, host=bind, extra_hosts=web.env_allowed_hosts())
        assert await _asgi(guard, host="swarm.example.com") == 200
        assert await _asgi(guard, host="swarm.example.com", origin="https://swarm.example.com") == 200
        assert await _asgi(guard, host="other.example.com") == 403
        # JSON-only POSTs: a cross-site <form> can only send urlencoded / multipart / text/plain
        assert await _asgi(guard, host="127.0.0.1", method="POST", path="/api/act/set_goal",
                           content_type="text/plain") == 415
        assert await _asgi(guard, host="127.0.0.1", method="POST", path="/api/act/set_goal",
                           content_type="application/json; charset=utf-8") == 200
    settings = hub_mcp.security_settings("127.0.0.1", {"swarm.example.com"})
    assert "swarm.example.com:*" in settings.allowed_hosts and "https://swarm.example.com" in settings.allowed_origins
    assert hub_mcp.security_settings("0.0.0.0") is None   # GuardMiddleware covers wildcard binds


def test_display_and_public_urls(monkeypatch):
    monkeypatch.delenv(web.PUBLIC_URL_ENV, raising=False)
    assert web.display_host("0.0.0.0") == "127.0.0.1"
    assert web.display_host("::") == "[::1]"
    assert web.display_host("::1") == "[::1]"
    assert web.public_base("::1", 8765) == "http://[::1]:8765"
    monkeypatch.setenv(web.PUBLIC_URL_ENV, "http://127.0.0.1:9000/")
    assert web.public_base("0.0.0.0", 8765) == "http://127.0.0.1:9000"


# ---------------------------------------------------------------------------------------------- web (integration)
async def test_wildcard_bind_rejects_rebinding_over_http_ws_and_mcp(tmp_path):
    import websockets

    swarm = scripted_swarm(tmp_path)
    await swarm.start()
    try:
        async with running_server(swarm, app_host="0.0.0.0") as (base, port):
            status, _ = await asyncio.to_thread(http, "GET", base + "/api/state")
            assert status == 200
            for path in ("/api/state", "/api/files?path=.", "/"):
                status, _ = await asyncio.to_thread(http, "GET", base + path, None, {"Host": f"rebind.example.com:{port}"})
                assert status == 403, path
            status, _ = await asyncio.to_thread(
                http, "POST", base + "/api/act/set_goal", {"title": "pwned"},
                {**JSON, "Host": f"rebind.example.com:{port}"})
            assert status == 403 and swarm.goal() is None
            status, _ = await asyncio.to_thread(
                http, "POST", base + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                {**JSON, "Accept": "application/json, text/event-stream", "Host": f"rebind.example.com:{port}"})
            assert status == 403
            with pytest.raises(websockets.exceptions.InvalidStatus):
                async with websockets.connect(f"ws://127.0.0.1:{port}/ws", origin="http://evil.example.com"):
                    pass
            async with websockets.connect(f"ws://127.0.0.1:{port}/ws", origin=f"http://127.0.0.1:{port}") as ws:
                await asyncio.to_thread(http, "POST", base + "/api/act/set_goal", {"title": "Legit"}, JSON)
                got = json.loads(await asyncio.wait_for(ws.recv(), 5))
                assert got["type"] in ("goal.set", "task.created", "notification")
    finally:
        await swarm.stop()
        swarm.close()


async def test_rest_files_identity_and_public_url(tmp_path, monkeypatch):
    monkeypatch.setenv(web.PUBLIC_URL_ENV, "http://127.0.0.1:9999")
    swarm = scripted_swarm(tmp_path)
    await swarm.start()
    port = free_port()
    server = asyncio.create_task(web.serve(swarm, "127.0.0.1", port))
    base = f"http://127.0.0.1:{port}"
    try:
        await wait_http(base + "/api/state")
        status, ident = await asyncio.to_thread(http, "GET", base + "/api/hub")
        assert status == 200 and ident["pid"] == os.getpid()
        assert ident["project"] == str(swarm.config.project_dir) and ident["name"] == "Surface swarm"
        status, state = await asyncio.to_thread(http, "GET", base + "/api/state")
        assert state["swarm"]["mcp_url"] == "http://127.0.0.1:9999/mcp"   # Docker port mapping honoured

        await swarm.workspace.write(USER, "docs/readme.md", "hello")
        (swarm.config.project_dir / "secret.txt").write_text("top secret", encoding="utf-8")
        status, res = await asyncio.to_thread(http, "GET", base + "/api/files?path=docs/readme.md")
        assert status == 200 and res["content"] == "hello"
        status, res = await asyncio.to_thread(http, "GET", base + "/api/files?path=docs")
        assert res["type"] == "dir" and [e["path"] for e in res["entries"]] == ["docs/readme.md"]
        outside = ["../secret.txt", "..%2Fsecret.txt", "docs/../../secret.txt", "..\\secret.txt",
                   urllib.parse.quote(str(swarm.config.project_dir / "secret.txt")), "../.dreamteam/swarm.db",
                   "docs/%2e%2e/%2e%2e/secret.txt",
                   ".git/config", "%5C%5Cexample.invalid%5Cshare%5Cx"]
        for path in outside:
            status, res = await asyncio.to_thread(http, "GET", base + "/api/files?path=" + path)
            assert status == 404 and "top secret" not in json.dumps(res), path

        # malformed bodies get a clean answer, not a 500
        status, res = await asyncio.to_thread(http, "POST", base + "/api/read", [1, 2], JSON)
        assert status == 200 and res["ok"]
        status, res = await asyncio.to_thread(http, "POST", base + "/api/act/board", [1], JSON)
        assert status == 400
        # user-only actions over REST, members-only ones refused
        status, res = await asyncio.to_thread(http, "POST", base + "/api/act/write_file", {"path": "x", "content": "y"}, JSON)
        assert res["ok"] is False and "not available" in res["result"]
    finally:
        server.cancel()
        await asyncio.gather(server, return_exceptions=True)
        await swarm.stop()
        swarm.close()
    assert swarm.hub_url is None


async def test_serve_reports_busy_port_as_oserror(tmp_path):
    """uvicorn calls sys.exit() when it cannot bind; that SystemExit used to escape the event loop."""
    swarm = scripted_swarm(tmp_path)
    await swarm.start()
    try:
        with socket.socket() as blocker:
            blocker.bind(("127.0.0.1", 0))
            blocker.listen()
            port = blocker.getsockname()[1]
            started: list[bool] = []
            with pytest.raises(OSError, match="in use"):
                await asyncio.wait_for(web.serve(swarm, "127.0.0.1", port, on_started=lambda: started.append(True)), 15)
            assert not started
    finally:
        await swarm.stop()
        swarm.close()


async def test_serve_cancel_closes_the_port(tmp_path):
    swarm = scripted_swarm(tmp_path)
    await swarm.start()
    port = free_port()
    started = asyncio.Event()
    server = asyncio.create_task(web.serve(swarm, "127.0.0.1", port, on_started=started.set))
    try:
        await asyncio.wait_for(started.wait(), 15)
        assert not cli._port_free("127.0.0.1", port)
        server.cancel()
        await asyncio.gather(server, return_exceptions=True)
        assert cli._port_free("127.0.0.1", port)   # graceful: the listening socket is closed
    finally:
        if not server.done():
            server.cancel()
        await swarm.stop()
        swarm.close()


# ---------------------------------------------------------------------------------------------- hub MCP
async def test_hub_mcp_identity_and_audiences(tmp_path):
    from mcp import Client

    swarm = scripted_swarm(tmp_path, start_agents=1)
    await swarm.start()
    try:
        internal = swarm.members()[0]["name"]
        async with running_server(swarm) as (base, _), Client(base + "/mcp") as client:
            names = {t.name for t in (await client.list_tools()).tools}
            assert {"set_goal", "swarm_control", "mcp_approve", "join_swarm", "task_claim", "shell"} <= names
            assert not names & {"wait", "update_notes"}

            async def call(tool: str, **args: Any) -> tuple[bool, str]:
                res = await client.call_tool(tool, args)
                return res.is_error, res.content[0].text

            assert (await call("wait"))[0] and (await call("update_notes", content="x"))[0]
            err, text = await call("join_swarm", role="Reviewer", name="Ext Member")
            assert not err, text
            # internal agents cannot be impersonated, by full name, first name or odd spacing/case
            first = internal.split()[0]
            for alias in (internal, internal.upper(), "  " + internal.replace(" ", "   ") + " ", first):
                err, text = await call("board", as_agent=alias)
                assert err and "join_swarm" in text, alias
            err, text = await call("board", as_agent="Nobody Here")
            assert err and "Unknown member" in text
            # user-only tools are refused to members, members-only tools to the user
            for tool, args in (("set_goal", {"title": "x"}), ("swarm_control", {"action": "pause"}),
                               ("mcp_approve", {"name": "x", "approve": True})):
                err, text = await call(tool, as_agent="Ext Member", **args)
                assert err and "not available" in text, tool
            assert not swarm.paused and swarm.goal() is None
            for tool, args in (("task_claim", {"task_id": 1}), ("shell", {"command": "echo hi"}),
                               ("write_file", {"path": "a.txt", "content": "x"})):
                err, text = await call(tool, **args)
                assert err and "not available" in text, tool
            assert not (swarm.workspace.root / "a.txt").exists()
            err, text = await call("write_file", as_agent="ext member", path="a.txt", content="x")
            assert not err, text
            # a retired member's name is dead
            await swarm.retire(USER, "Ext Member")
            err, text = await call("board", as_agent="Ext Member")
            assert err
    finally:
        await swarm.stop()
        swarm.close()


def test_proxy_read_timeout_outlasts_wait_for_events():
    from dreamteam.tools import REGISTRY, tools_for

    tools_for("user")
    assert "wait_for_events" in REGISTRY
    assert hub_mcp.PROXY_READ_TIMEOUT > 600   # wait_for_events accepts timeout up to 600 s


def _read_json_line(proc: subprocess.Popen, timeout: float = 30) -> dict:
    line = read_line(proc, timeout, "the MCP server")  # kills the tree and fails when none comes
    assert line, f"no output from the MCP server (exit={proc.poll()})"
    return json.loads(line)   # anything else on stdout breaks the protocol


def test_stdio_mcp_with_web_board_lifecycle(tmp_path):
    """`dreamteam mcp DIR` (in-process swarm + web board): stdout carries only JSON-RPC, hub.json exists while
    the board runs and names this process, closing stdin stops everything and removes hub.json."""
    port = free_port()
    project = make_project(tmp_path, "stdio", port=port)
    # (stderr is not read: a pipe of it could fill up and block the server)
    proc = spawn([sys.executable, "-m", "dreamteam", "mcp", str(project)], stdin=subprocess.PIPE,
                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=child_env())
    try:
        def send(obj: dict) -> None:
            proc.stdin.write((json.dumps(obj) + "\n").encode())
            proc.stdin.flush()

        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
        assert _read_json_line(proc)["id"] == 1
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "set_goal", "arguments": {"title": "Over stdio"}}})
        reply = _read_json_line(proc)
        assert reply["id"] == 2 and not reply["result"]["isError"]
        info = json.loads((project / ".dreamteam" / "hub.json").read_text(encoding="utf-8"))
        assert info["base_url"] == f"http://127.0.0.1:{port}" and info["project"] == str(project)
        # (on Windows a venv's python.exe is a launcher: the hub runs in its child process)
        assert cli.hub_identity(info["base_url"])["pid"] == info["pid"] != os.getpid()
        assert cli.running_hub(project) == info["base_url"]
        status, state = http("GET", info["base_url"] + "/api/state")
        assert state["swarm"]["goal"]["title"] == "Over stdio"
        proc.stdin.close()
        assert proc.wait(30) == 0
        rest = read_rest(proc, 30, "the MCP server")  # a child left holding stdout fails, not hangs
        assert all(json.loads(line) for line in rest.splitlines() if line.strip())
        assert not (project / ".dreamteam" / "hub.json").exists()
        assert "swarm stopped" in (project / ".dreamteam" / "logs" / "dreamteam.log").read_text(encoding="utf-8")
    finally:
        kill_tree(proc)


def test_stdio_mcp_stops_on_signal_while_client_keeps_stdin_open(tmp_path):
    """Ctrl+Break / SIGTERM while the MCP client still holds stdin: the server stops the swarm, removes
    hub.json and exits (the SDK's stdin worker thread could not be cancelled, so it hung until stdin closed)."""
    port = free_port()
    project = make_project(tmp_path, "sig", port=port)
    errlog = open(tmp_path / "stderr.log", "w+", encoding="utf-8")  # a file: a pipe read after the exit may hang
    proc = spawn([sys.executable, "-m", "dreamteam", "mcp", str(project)], stdin=subprocess.PIPE,
                 stdout=subprocess.PIPE, stderr=errlog, env=child_env(), **group_kwargs())
    try:
        proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}) + "\n").encode())
        proc.stdin.flush()
        assert _read_json_line(proc)["id"] == 1
        assert (project / ".dreamteam" / "hub.json").exists()
        interrupt(proc)
        assert proc.wait(30) == 0, (tmp_path / "stderr.log").read_text(encoding="utf-8", errors="replace")[-2000:]
        assert not (project / ".dreamteam" / "hub.json").exists()
        assert "swarm stopped" in (project / ".dreamteam" / "logs" / "dreamteam.log").read_text(encoding="utf-8")
    finally:
        kill_tree(proc)
        proc.stdin.close()
        errlog.close()


async def test_stdio_mcp_survives_web_port_race(tmp_path):
    """The board port gets taken between the free-port check and the bind: MCP over stdio keeps working
    (the web server's sys.exit() used to kill the whole MCP server)."""
    from mcp import Client, StdioServerParameters, stdio_client

    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        project = make_project(tmp_path, "race", port=port)
        script = ("import sys, dreamteam.cli as c; c._port_free = lambda h, p: True; "
                  "sys.exit(c.main(['mcp', sys.argv[1]]))")
        params = StdioServerParameters(command=sys.executable, args=["-c", script, str(project)], env=child_env(),
                                       cwd=str(tmp_path))
        with open(tmp_path / "stderr.log", "w", encoding="utf-8") as errlog:
            try:
                with anyio.fail_after(120):  # leaving the client kills the server's whole process tree
                    async with Client(stdio_client(params, errlog=errlog)) as client:
                        res = await client.call_tool("set_goal", {"title": "Still alive"})
                        assert not res.is_error, res.content[0].text
                        res = await client.call_tool("swarm_status", {})
                        assert "Still alive" in res.content[0].text
            except TimeoutError:
                pytest.fail("the stdio MCP server did not answer within 120 s: its process tree was killed")
        assert not (project / ".dreamteam" / "hub.json").exists()
        log = (project / ".dreamteam" / "logs" / "dreamteam.log").read_text(encoding="utf-8")
        assert "web board not started" in log


def test_run_proxy_hub_json_and_graceful_stop(tmp_path):
    """`dreamteam run` + `dreamteam mcp` as a stdio->HTTP proxy, hub.json identity checks, and Ctrl+Break /
    SIGTERM: the hub used to die from uvicorn re-raising the signal (exit 3 on Windows) before swarm.stop(),
    leaving hub.json and the agents' MCP server processes behind."""
    port = free_port()
    project = make_project(tmp_path, "hub", port=port)
    other = make_project(tmp_path, "other")
    with hub_process(project, port, tmp_path / "hub.log") as proc:
        base = f"http://127.0.0.1:{port}"
        info = json.loads((project / ".dreamteam" / "hub.json").read_text(encoding="utf-8"))
        hub_pid = info["pid"]
        assert cli.hub_identity(base)["pid"] == hub_pid and info["project"] == str(project)
        assert cli.running_hub(project) == base
        assert cli_main(["run", str(project), "--port", str(free_port()), "--no-console"]) == 1   # already running

        # hub.json of another project pointing at this hub, with a live but unrelated pid (pid reuse)
        hub_json = other / ".dreamteam" / "hub.json"
        hub_json.parent.mkdir(parents=True, exist_ok=True)
        hub_json.write_text(json.dumps({"base_url": base, "pid": os.getpid(), "project": str(other)}), encoding="utf-8")
        assert cli.running_hub(other) is None
        hub_json.write_text(json.dumps({"base_url": base, "pid": os.getpid()}), encoding="utf-8")   # legacy format
        assert cli.running_hub(other) is None
        # a hub in Docker writes its own view (/swarm, its pid); the identity check alone decides
        hub_json.write_text(json.dumps({"base_url": base, "pid": hub_pid, "project": "/swarm"}), encoding="utf-8")
        assert cli.running_hub(other) is None
        hub_json.write_text(json.dumps({"base_url": base, "pid": hub_pid, "project": str(project)}), encoding="utf-8")
        assert cli.running_hub(other) == base

        # the stdio proxy forwards to the running hub
        async def through_proxy() -> str:
            from mcp import Client, StdioServerParameters, stdio_client

            params = StdioServerParameters(command=sys.executable, args=["-m", "dreamteam", "mcp", str(project)],
                                           env=child_env(), cwd=str(tmp_path))
            with open(tmp_path / "proxy.log", "w", encoding="utf-8") as errlog, anyio.fail_after(120):
                async with Client(stdio_client(params, errlog=errlog)) as client:
                    res = await client.call_tool("set_goal", {"title": "Via proxy"})
                    assert not res.is_error, res.content[0].text
                    res = await client.call_tool("join_swarm", {"role": "Tester", "name": "Proxy Member"})
                    assert not res.is_error, res.content[0].text
                    res = await client.call_tool("wait_for_events", {"timeout": 1, "as_agent": "Proxy Member"})
                    return res.content[0].text

        asyncio.run(through_proxy())
        status, state = http("GET", base + "/api/state")
        assert state["swarm"]["goal"]["title"] == "Via proxy"
        assert "Proxy Member" in [a["name"] for a in state["agents"]]

        interrupt(proc)
        assert proc.wait(30) == 0
    assert not (project / ".dreamteam" / "hub.json").exists()
    assert "swarm stopped" in (project / ".dreamteam" / "logs" / "dreamteam.log").read_text(encoding="utf-8")
    assert cli.running_hub(project) is None


def test_running_hub_tolerates_broken_and_stale_files(tmp_path):
    project = tmp_path / "p"
    path = project / ".dreamteam" / "hub.json"
    path.parent.mkdir(parents=True)
    assert cli.running_hub(project) is None
    for content in ("", "{", "[]", "null", "42", '{"pid": 1}', '{"base_url": 5, "pid": 1}',
                    '{"base_url": "http://127.0.0.1:1", "pid": "abc"}', '{"base_url": "http://127.0.0.1:1", "pid": null}'):
        path.write_text(content, encoding="utf-8")
        assert cli.running_hub(project) is None, content   # used to raise on lists / bad pids
    # a crashed hub: its pid is gone
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(60)
    path.write_text(json.dumps({"base_url": "http://127.0.0.1:9", "pid": dead.pid, "project": str(project)}),
                    encoding="utf-8")
    assert cli.running_hub(project) is None
    # removing only our own file
    path.write_text(json.dumps({"base_url": "http://127.0.0.1:9", "pid": os.getpid() + 1}), encoding="utf-8")
    cli._remove_hub_file(project)
    assert path.exists()
    path.write_text(json.dumps({"base_url": "http://127.0.0.1:9", "pid": os.getpid()}), encoding="utf-8")
    cli._remove_hub_file(project)
    assert not path.exists()


@pytest.fixture
def restore_logging():
    """`dreamteam run` reconfigures the root logger (file + stderr handlers); give pytest its own back."""
    import logging

    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for h in list(root.handlers):
        if h not in handlers:
            root.removeHandler(h)
            h.close()
    for h in handlers:
        if h not in root.handlers:
            root.addHandler(h)
    root.setLevel(level)


def test_run_refuses_busy_port_cleanly(tmp_path, capsys, restore_logging):
    project = make_project(tmp_path, "busy")
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        capsys.readouterr()
        assert cli_main(["run", str(project), "--port", str(port), "--no-console"]) == 1
    assert "busy" in capsys.readouterr().err
    assert not (project / ".dreamteam" / "hub.json").exists()


def test_mcp_on_a_non_project_dir_leaves_no_trace(tmp_path, capsys):
    target = tmp_path / "not-a-project"
    target.mkdir()
    assert cli_main(["mcp", str(target)]) == 1
    assert "dreamteam init" in capsys.readouterr().err
    assert not (target / ".dreamteam").exists()


def test_port_free_ipv6():
    assert cli._port_free("[::1]", free_port()) in (True, False)   # must not raise for IPv6 hosts
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        assert not cli._port_free("localhost", s.getsockname()[1])


# ---------------------------------------------------------------------------------------------- graceful shutdown
def _run_in_thread(coro_factory, timeout: float) -> tuple[threading.Thread, dict[str, Any]]:
    out: dict[str, Any] = {}

    def target():
        try:
            out["result"] = asyncio.run(coro_factory(out))
        except BaseException as exc:  # noqa: BLE001 - report everything to the test
            out["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)
    return thread, out


def test_serve_swarm_exits_although_console_blocks_in_input(tmp_path, monkeypatch):
    """The console thread sits in input(); after a stop request the process must still finish (with
    asyncio.to_thread, asyncio.run waited for that thread forever) and swarm.stop() must run."""
    config = scripted_config(tmp_path)
    project = config.project_dir
    port = free_port()
    never = threading.Event()
    calls: list[str] = []

    def blocking_console(base, lang, stop=None, local=None):
        calls.append(base)
        never.wait()   # like input() with nobody typing

    monkeypatch.setattr(console_module, "run_console", blocking_console)
    stopped: list[bool] = []
    original_stop = Swarm.stop

    async def recording_stop(self):
        stopped.append(True)
        await original_stop(self)

    monkeypatch.setattr(Swarm, "stop", recording_stop)

    async def scenario(out):
        stop = asyncio.Event()
        task = asyncio.create_task(cli._serve_swarm(project, config, "127.0.0.1", port, "Goal from CLI", True, False,
                                                    stop=stop))
        await wait_http(f"http://127.0.0.1:{port}/api/state")
        out["hub_json"] = json.loads((project / ".dreamteam" / "hub.json").read_text(encoding="utf-8"))
        stop.set()
        return await task

    try:
        thread, out = _run_in_thread(scenario, 40)
        assert not thread.is_alive(), "the hub did not exit while the console thread was blocked"
        assert out.get("result") == 0, out
        assert calls == [f"http://127.0.0.1:{port}"]
        assert out["hub_json"]["pid"] == os.getpid()
        assert stopped == [True]
        assert not (project / ".dreamteam" / "hub.json").exists()
        assert cli.hub_identity(f"http://127.0.0.1:{port}", timeout=5) is None
    finally:
        never.set()


def test_serve_swarm_web_failure_after_port_check(tmp_path, monkeypatch, capsys):
    config = scripted_config(tmp_path)
    project = config.project_dir
    stopped: list[bool] = []
    original_stop = Swarm.stop

    async def recording_stop(self):
        stopped.append(True)
        await original_stop(self)

    monkeypatch.setattr(Swarm, "stop", recording_stop)
    monkeypatch.setattr(cli, "_port_free", lambda host, port: True)
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]

        async def scenario(out):
            return await cli._serve_swarm(project, config, "127.0.0.1", port, None, False, False)

        thread, out = _run_in_thread(scenario, 40)
    assert not thread.is_alive()
    assert out.get("result") == 1, out
    assert "could not start" in capsys.readouterr().err
    assert stopped == [True]
    assert not (project / ".dreamteam" / "hub.json").exists()


# ---------------------------------------------------------------------------------------------- console
MEMBERS = ["Анна Смирнова", "Анна Смирнова-Петрова", "John Smith", "John Smithson", "Grace Hopper", "Grace Kelly"]


@pytest.mark.parametrize("line, name, text", [
    ("@Анна Смирнова привет", "Анна Смирнова", "привет"),
    ("@анна   СМИРНОВА:  как дела?", "Анна Смирнова", "как дела?"),
    ("@Анна Смирнова, привет", "Анна Смирнова", "привет"),
    ("@Анна Смирнова-Петрова привет", "Анна Смирнова-Петрова", "привет"),
    ("@Анна Смирновааа привет", None, None),          # used to match "Анна Смирнова" with text "аа привет"
    ("@John Smith hi", "John Smith", "hi"),
    ("@John Smithson hi", "John Smithson", "hi"),
    ("@John Smithsonian hi", None, None),
    ("@Hopper hi", None, None),
    ("@Grace hi", None, None),                        # ambiguous first name
    ("@john: hi", None, None),                        # ambiguous too
    ("Grace Hopper: hi there", "Grace Hopper", "hi there"),
])
def test_match_addressee(line, name, text):
    got_name, got_text = console_module.match_addressee(line, MEMBERS)
    assert got_name == name
    if name:
        assert got_text == text


def test_match_addressee_unique_first_name():
    assert console_module.match_addressee("@Анна привет", ["Анна Смирнова", "Иван Петров"]) == ("Анна Смирнова", "привет")
    assert console_module.match_addressee("@иван: ок", ["Анна Смирнова", "Иван Петров"]) == ("Иван Петров", "ок")


class StubHub:
    def __init__(self, members: list[str]):
        self.members = members
        self.acts: list[tuple[str, dict]] = []

    def get(self, path: str, **params: Any) -> Any:
        assert path == "/api/state"
        return {"agents": [{"name": n, "status": "idle"} for n in self.members]}

    def act(self, tool: str, **args: Any) -> str:
        self.acts.append((tool, args))
        return "ok"


@pytest.mark.parametrize("line", ["/show abc", "/comment x hello", "/budget lots", "/max many", '/approve "broken',
                                  "/show #", "/comment"])
def test_console_bad_input_does_not_crash(line, capsys):
    con = console_module.Console("http://127.0.0.1:9", "en")
    con.hub = StubHub(MEMBERS)
    assert con.handle(line) is True   # used to raise ValueError and kill the console
    out = capsys.readouterr().out
    assert "parse" in out or "Unknown" in out or "ok" in out


def test_console_commands_reach_the_hub(capsys):
    con = console_module.Console("http://127.0.0.1:9", "ru")
    hub = StubHub(MEMBERS)
    con.hub = hub
    con.handle("@grace hopper  Привет!")
    con.handle("/msg Анна Смирнова-Петрова: отчёт готов?")
    con.handle("/msg Анна Смирнова как дела")
    con.handle("/chat Анна Смирнова")
    con.handle("обычный текст")
    con.handle("/chat")
    con.handle("/all всем привет")
    con.handle("/budget $7.5")
    con.handle("/reject csv_tools 'не нужен'")
    assert hub.acts == [
        ("send_message", {"to": ["Grace Hopper"], "text": "Привет!"}),
        ("send_message", {"to": ["Анна Смирнова-Петрова"], "text": "отчёт готов?"}),
        ("send_message", {"to": ["Анна Смирнова"], "text": "как дела"}),
        ("send_message", {"to": ["Анна Смирнова"], "text": "обычный текст"}),
        ("send_message", {"to": ["all"], "text": "всем привет"}),
        ("swarm_control", {"max_cost_usd": 7.5}),
        ("mcp_approve", {"name": "csv_tools", "approve": False, "comment": "не нужен"}),
    ]


def test_console_settles_reviews_and_hires_from_the_catalog(capsys):
    con = console_module.Console("http://127.0.0.1:9", "ru")
    hub = StubHub(MEMBERS)
    con.hub = hub
    con.handle("/approve 5 ok")
    con.handle("/reject #5 'нет тестов'")
    con.handle("/reject 5")                       # a return needs a reason: no call
    assert "/reject N <что исправить>" in capsys.readouterr().out
    con.handle("/approve calc")                   # a name is still an MCP server
    # the reason is free text: an apostrophe or quotes inside it are kept, as in the TUI
    con.handle("/reject 12 don't use global state")
    con.handle("/approve 12 it's fine, the \"total\"  line too")
    con.handle("/hire Тестировщик")               # a catalog role: instructions "" (the parameter stays required)
    con.handle("/hire QA | гоняй тесты")
    assert hub.acts == [
        ("task_review", {"task_id": 5, "approve": True, "comment": "ok"}),
        ("task_review", {"task_id": 5, "approve": False, "comment": "нет тестов"}),
        ("mcp_approve", {"name": "calc", "approve": True, "comment": ""}),
        ("task_review", {"task_id": 12, "approve": False, "comment": "don't use global state"}),
        ("task_review", {"task_id": 12, "approve": True, "comment": "it's fine, the \"total\"  line too"}),
        ("hire_agent", {"role": "Тестировщик", "instructions": ""}),
        ("hire_agent", {"role": "QA", "instructions": "гоняй тесты"}),
    ]
    assert "/approve имя|N [комментарий]  /reject имя|N [что исправить]" in console_module.HELP["ru"]
    assert "/hire Role [| instructions] — hire an agent (a catalog role or a new one)" in console_module.HELP["en"]


def test_console_lists_shows_and_stops_background_jobs(capsys):
    con = console_module.Console("http://127.0.0.1:9", "en")
    hub = StubHub(MEMBERS)
    con.hub = hub
    for line in ("/jobs", "/jobs j2", "/jobs stop j1", "/jobs stop j1 %3", "/jobs стоп 4", "/jobs kill j5"):
        con.handle(line)
    con.handle("/jobs stop")                      # no job: the usage, no call
    assert hub.acts == [
        ("jobs", {}), ("jobs", {"job_id": "j2"}), ("job_stop", {"job_id": "j1"}), ("job_stop", {"job_id": "j1"}),
        ("job_stop", {"job_id": "%3"}), ("job_stop", {"job_id": "4"}), ("job_stop", {"job_id": "j5"}),
    ]
    assert "/jobs stop jN" in capsys.readouterr().out
    for lang, text in (("ru", "фоновые процессы агентов: список, вывод одного, остановка"),
                       ("en", "agents' background jobs: list, one job's output, stop")):
        assert f"  /jobs [jN | stop jN]     — {text}" in console_module.HELP[lang].splitlines()


class StubSkillsHub(StubHub):
    """A hub whose GETs answer the skills routes (docs/SPEC.md §14) and record what was asked."""

    def __init__(self) -> None:
        super().__init__(MEMBERS)
        self.gets: list[tuple[str, dict]] = []
        self.posts: list[tuple[str, dict]] = []

    def get(self, path: str, **params: Any) -> Any:
        self.gets.append((path, params))
        if path == "/api/skills":
            return {"library_dir": "/lib", "attached": [{"name": "pdf", "from": "library"},
                                                         {"name": "tidy", "from": "agent"}],
                    "library": [{"name": "pdf", "description": "Reads PDFs."},
                                {"name": "csv", "description": "CSV.", "changed_outside": True}],
                    "claude": [{"name": "review", "origin": "claude-user", "description": "Reviews."}], "host": []}
        if path.endswith("/deps-log"):
            return {"path": "/envs/pdf-py-1/install.log", "log": "Installed pypdf\x1b[2J"}
        if path.startswith("/api/skills/"):
            return {"name": path.rsplit("/", 1)[1], "description": "Reads PDFs.", "skill_md": "\n".join(
                f"line {i}" for i in range(60)), "files_list": [{"path": "SKILL.md", "size": 9}],
                    "deps": {"python": {"files": ["requirements.txt"], "packages": ["pypdf"]}}}
        return super().get(path, **params)

    def post(self, path: str, body: dict) -> Any:
        self.posts.append((path, body))
        return {"removed": ["a-py-1"], "kept": [{"dirname": "b-js-2", "why": "in use"}]}

    def act(self, tool: str, **args: Any) -> str:
        return super().act(tool, **{k: v for k, v in args.items() if v is not None})   # as console.Hub.act sends


def test_console_skills_commands(tmp_path, monkeypatch, capsys):
    con = console_module.Console("http://127.0.0.1:9/s/001-a", "en", local=True)
    hub = StubSkillsHub()
    con.hub = hub
    con.launcher = hub
    monkeypatch.chdir(tmp_path)
    for line in ("/skills", "/skills add pdf", "/skills add claude-user:review --replace", "/skills add ./mine",
                 "/skills add https://github.com/o/r --pick a,b", "/skills update pdf", "/skills rm pdf",
                 "/skills approve tidy replace", "/skills reject tidy 'not general enough'",
                 "/skills reinstall pdf node", "/skills reinstall pdf"):
        assert con.handle(line) is True
    assert hub.acts == [
        ("skills", {}), ("skill_attach", {"name": "pdf"}),
        ("skill_attach", {"name": "claude-user:review", "replace": True}),
        ("skill_attach", {"source": str(tmp_path / "mine")}),
        ("skill_attach", {"source": "https://github.com/o/r", "pick": ["a", "b"]}),
        ("skill_attach", {"name": "library:pdf", "replace": True}), ("skill_detach", {"name": "pdf"}),
        ("skill_approve", {"name": "tidy", "approve": True, "replace": True}),
        ("skill_approve", {"name": "tidy", "approve": False, "comment": "not general enough"}),
        ("skill_reinstall", {"name": "pdf", "ecosystem": "node"}), ("skill_reinstall", {"name": "pdf"}),
    ]
    capsys.readouterr()
    con.handle("/skills update tidy")      # a member's proposal has no source of its own
    con.handle("/skills reject")           # a name is required, a reason is not
    con.handle("/skills zap")
    out = capsys.readouterr().out
    assert "its owner proposes it again" in out and "/skills reject NAME [reason]" in out and "clean-envs]" in out
    assert len(hub.acts) == 11
    con.handle("/skills reject tidy")
    assert hub.acts[-1] == ("skill_approve", {"name": "tidy", "approve": False, "comment": ""})
    capsys.readouterr()
    con.handle("/skills show pdf")
    out = capsys.readouterr().out
    assert hub.gets[-1] == ("/api/skills/pdf", {"from": "session"})   # attached: the session's copy
    assert "pdf [session]: Reads PDFs." in out and "python: requirements.txt: pypdf" in out
    assert "line 39" in out and "line 40" not in out
    con.handle("/skills show claude-user:review")
    assert hub.gets[-1] == ("/api/skills/review", {"from": "claude-user"})
    con.handle("/skills log pdf python")
    out = capsys.readouterr().out
    assert hub.gets[-1] == ("/api/skills/pdf/deps-log", {"ecosystem": "python"})
    assert "/envs/pdf-py-1/install.log" in out and "Installed pypdf" in out and "\x1b" not in out
    con.handle("/skills lib")
    out = capsys.readouterr().out
    assert "  pdf [library, attached]: Reads PDFs." in out and "csv [library, changed outside DreamTeam]" in out
    assert "claude-user:review [claude-user]: Reviews." in out
    con.handle("/skills clean-envs")
    assert hub.posts == [("/api/skill-envs/cleanup", {})] and "Removed 1 environment(s); kept 1 (in use)." in \
        capsys.readouterr().out
    hub.post = lambda path, body: {"removed": [], "kept": []}   # nothing kept: nothing said about it
    con.handle("/skills clean-envs")
    assert capsys.readouterr().out.strip().endswith("Removed 0 environment(s).")
    # a single swarm has no launcher; a remote hub needs absolute paths
    single = console_module.Console("http://10.0.0.5:9", "ru")
    single.hub = StubSkillsHub()
    single.handle("/skills clean-envs")
    single.handle("/skills add ./mine")
    out = capsys.readouterr().out
    assert "лаунчер" in out and "абсолютный путь" in out and single.hub.acts == []
    for lang in ("ru", "en"):
        assert ("  /skills [lib | add X | update X | rm X | show X | approve X | reject X [reason] | reinstall X | "
                "log X | clean-envs]") in console_module.HELP[lang].splitlines()


async def test_console_skills_against_a_standalone_hub(tmp_path, capsys):
    """`dreamteam run`'s own app serves the skills routes: the console attaches, shows and lists through them."""
    from conftest import make_skill

    swarm = scripted_swarm(tmp_path)
    await swarm.start()
    try:
        folder = make_skill(tmp_path / "src" / "notes", "notes", "Writes notes.")
        async with running_server(swarm) as (base, _):
            con = console_module.Console(base, "en")
            out = await asyncio.to_thread(con._dispatch, f"/skills add {folder}")
            assert 'Skill "notes" attached (1 file); members are told.' in out, out
            assert swarm.skills.get("notes").frm == "library"
            out = await asyncio.to_thread(con._dispatch, "/skills show notes")
            assert out.startswith("notes [session]: Writes notes.") and "Steps." in out
            out = await asyncio.to_thread(con._dispatch, "/skills lib")
            assert "  notes [library, attached]: Writes notes." in out
            out = await asyncio.to_thread(con._dispatch, "/skills log notes")
            assert "no packages installed by DreamTeam" in out   # the hub's 404, in words
            assert "launcher" in await asyncio.to_thread(con._dispatch, "/skills clean-envs")
    finally:
        await swarm.stop()
        swarm.close()


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
async def test_launcher_mcp_forwards_calls_as_its_client(tmp_path):
    """The launcher MCP server forwards a client's calls to a session as that client (hub_mcp.serving_client): what
    a client that joined as a member sends as the user is no news to its own member, while another client's is."""
    from mcp import Client

    from dreamteam import settings as st
    from dreamteam.launcher_mcp import build_launcher_server
    from dreamteam.sessions import SessionManager

    home = tmp_path / "home"
    home.mkdir()
    st.write_raw(home, {
        "swarm": {"name": "Home", "language": "en", "start_agents": 1, "max_agents": 3, "max_cost_usd": 0},
        "agents": {"stagger_seconds": 0, "idle_nudge_seconds": 30},
        "workspace": {"git": False},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
    })
    manager = SessionManager(home, llm_factory=lambda profile, agent: ScriptedLLM(
        lambda *a: {"tool_calls": [{"name": "wait", "arguments": {}}]}, profile))

    async def call(client, tool: str, args: dict) -> str:
        res = await client.call_tool(tool, args)
        assert not res.is_error, res.content[0].text
        return res.content[0].text

    try:
        async with Client(build_launcher_server(manager)) as one, Client(build_launcher_server(manager)) as two:
            await call(one, "session_create", {"title": "Clients", "goal": "Talk", "start_agents": 1})
            sid = manager.ids()[0]
            swarm = manager.swarms[sid]
            await call(one, "join_swarm", {"session": sid, "role": "Researcher", "name": "Ann Lee"})
            await call(one, "send_message", {"session": sid, "to": ["all"], "text": "status, please"})  # as the user
            assert not swarm.has_pending("Ann Lee")
            await call(two, "send_message", {"session": sid, "to": ["all"], "text": "from the other client"})
            assert swarm.has_pending("Ann Lee")
    finally:
        await manager.shutdown()


def test_session_line_counts_the_running_jobs():
    from dreamteam.launcher_mcp import session_line

    summary = {"id": "001-calc", "title": "Calc", "status": "running", "tasks": {"done": 1, "total": 3},
               "agents": {"total": 2, "working": 1}, "spent_usd": 0.5, "max_cost_usd": 5, "jobs": {"running": 2}}
    assert session_line(summary).startswith(
        "001-calc [running] Calc | tasks 1/3 done | agents 2 (1 working) | jobs 2 running | $0.50 of $5.00")
    assert "jobs" not in session_line({**summary, "jobs": {"running": 0}})
    assert "jobs" not in session_line({**summary, "status": "stopped"})       # (a stale count of a stopped one)
    assert "jobs" not in session_line({k: v for k, v in summary.items() if k != "jobs"})  # an older hub


def test_console_strips_terminal_escapes(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    con = console_module.Console("http://127.0.0.1:9", "en")
    con._show_message({"ts": time.time(), "sender": "Eve\x1b[2J", "recipients": ["user"],
                       "text": "hi\x1b]52;c;cm0gLXJmIH4=\x07 there\r\nnext\tline\x9b31m"})
    out = buf.getvalue()
    assert "\x1b" not in out and "\x07" not in out and "\x9b" not in out
    assert "Eve[2J" in out and "next\tline" in out
    assert console_module.clean("a\x1b[31mb\rc") == "a[31mbc"


async def test_console_against_a_real_hub(tmp_path):
    swarm = scripted_swarm(tmp_path, language="ru")
    await swarm.start()
    try:
        await swarm.hire(USER, "Аналитик", name="Анна Смирнова", kind="external")
        async with running_server(swarm) as (base, _):
            con = console_module.Console(base, "ru")
            await asyncio.to_thread(con.handle, "@анна смирнова   Привет, как дела?")
            msgs = swarm.mail.conversation(USER, "Анна Смирнова")
            assert [m["text"] for m in msgs] == ["Привет, как дела?"]
            await asyncio.to_thread(con.handle, "/goal Сделать CSV-конвертер | с тестами")
            assert swarm.goal()["title"] == "Сделать CSV-конвертер"
            # the user's `jobs` / `job_stop` through the act API (no job runs here)
            assert await asyncio.to_thread(con._jobs, "") == "No background jobs are running."
            assert "There is no job j4. No jobs are running." in await asyncio.to_thread(con._jobs, "stop j4")
            swarm.mail.send("Анна Смирнова", [USER], "Готово")
            data = await asyncio.to_thread(con.hub.get, "/api/inbox")
            assert data["messages"][-1]["text"] == "Готово"
    finally:
        await swarm.stop()
        swarm.close()


# ---------------------------------------------------------------------------------------------- docker
def test_docker_build_context(tmp_path):
    ctx = docker_module.build_context(tmp_path / "ctx")
    assert (ctx / "Dockerfile").read_text(encoding="utf-8") == (REPO / "Dockerfile").read_text(encoding="utf-8")
    assert docker_module._is_dreamteam_pyproject(ctx / "pyproject.toml")
    assert (ctx / "README.md").exists()
    pkg = ctx / "src" / "dreamteam"
    for rel in ("__init__.py", "cli.py", "hub_mcp.py", "static/index.html", "static/app.js", "tools/__init__.py",
                "llm/__init__.py"):
        assert (pkg / rel).exists(), rel
    assert not list(ctx.rglob("__pycache__")) and not list(ctx.rglob("*.pyc"))
    # the container port is pinned: a project with `port = 9000` must still answer on the published 8765
    assert '"--port", "8765"' in docker_module.DOCKERFILE
    compose = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    assert '"--port", "8765"' in compose and "DREAMTEAM_PUBLIC_URL=http://127.0.0.1:${PORT:-8765}" in compose
    other = tmp_path / "pyproject.toml"
    other.write_text('[project]\nname = "something-else"\n', encoding="utf-8")
    assert not docker_module._is_dreamteam_pyproject(other)


def test_docker_build_context_without_repo_pyproject(tmp_path, monkeypatch):
    """Installed from a wheel there is no pyproject.toml next to the package: the generated one must
    declare the same dependencies and package layout."""
    import tomllib

    monkeypatch.setattr(docker_module, "_is_dreamteam_pyproject", lambda path: False)
    ctx = docker_module.build_context(tmp_path / "ctx")
    generated = tomllib.loads((ctx / "pyproject.toml").read_text(encoding="utf-8"))
    repo = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert generated["project"]["dependencies"] == repo["project"]["dependencies"]
    assert generated["project"]["scripts"] == repo["project"]["scripts"]
    assert generated["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["src/dreamteam"]


def test_sandbox_command(tmp_path, monkeypatch):
    project = make_project(tmp_path, "sandboxed", port=9100)
    config = Config.load(project)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-value")
    cmd = docker_module.sandbox_command(project, 9200, config, goal="Build it", docker="docker")
    assert cmd[:2] == ["docker", "run"]
    assert "--init" in cmd                                       # a reaper as PID 1: agents' orphans never linger
    assert "127.0.0.1:9200:8765" in cmd                          # only on the host's loopback
    assert "DREAMTEAM_PUBLIC_URL=http://127.0.0.1:9200" in cmd
    assert cmd[cmd.index("--port") + 1] == "8765" and cmd[-2:] == ["--goal", "Build it"]
    assert "ANTHROPIC_API_KEY" in cmd and not any("sk-secret-value" in part for part in cmd)
    assert f"{project.resolve()}:/swarm" in cmd


def test_sandbox_command_gives_each_home_a_volume_of_skill_environments(tmp_path, monkeypatch):
    """docs/SPEC.md §19: the sandbox's skill environments live in a named volume per home (a Linux file system,
    kept across --rm containers); the host's environments are never mounted."""
    project = make_project(tmp_path, "sandboxed", port=9100)
    other = make_project(tmp_path, "other", port=9101)
    monkeypatch.setenv("DREAMTEAM_SKILL_ENVS_DIR", str(tmp_path / "host-envs"))
    config = Config.load(project)
    cmd = docker_module.sandbox_command(project, 9200, config, docker="docker")
    image = cmd.index(docker_module.IMAGE)
    mounts = [cmd[i + 1] for i in range(image) if cmd[i] == "-v"]
    volume = docker_module.envs_volume(project)
    assert re.fullmatch(r"dreamteam-skill-envs-[0-9a-f]{10}", volume)
    assert f"{volume}:/dreamteam/skill-envs" in mounts
    assert volume == docker_module.envs_volume(project) != docker_module.envs_volume(other)
    assert "DREAMTEAM_SKILL_ENVS_DIR=/dreamteam/skill-envs" in cmd[:image]
    assert "UV_CACHE_DIR=/dreamteam/skill-envs/.cache/uv" in cmd[:image]
    assert not any("host-envs" in m for m in mounts)
    passing = Config.from_dict(project, {"shell": {"pass_env": ["DREAMTEAM_SKILL_ENVS_DIR", "UV_CACHE_DIR"]}})
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv"))
    assert not set(docker_module.env_passthrough(passing)) & {"DREAMTEAM_SKILL_ENVS_DIR", "UV_CACHE_DIR"}


def test_sandbox_command_mounts_the_host_skills_library_read_only(tmp_path, monkeypatch):
    """docs/SPEC.md §19: agents have `shell` in the hub's container, so the host library is read-only there, the
    sandbox gets its own writable library in the mounted home, and ~/.claude is never mounted."""
    project = make_project(tmp_path, "sandboxed", port=9100)
    host_library = tmp_path / "host-skills"
    monkeypatch.setenv("DREAMTEAM_SKILLS_DIR", str(host_library))
    config = Config.load(project)
    cmd = docker_module.sandbox_command(project, 9200, config, docker="docker")
    image = cmd.index(docker_module.IMAGE)
    mounts = [cmd[i + 1] for i in range(image) if cmd[i] == "-v"]
    assert f"{host_library.resolve()}:/dreamteam/host-skills:ro" in mounts
    assert "DREAMTEAM_HOST_SKILLS_DIR=/dreamteam/host-skills" in cmd[:image]
    assert "DREAMTEAM_SKILLS_DIR=/swarm/.dreamteam/skills-library" in cmd[:image]
    assert not any(".claude" in m for m in mounts) and "CLAUDE_CONFIG_DIR" not in " ".join(cmd)
    assert not host_library.exists()  # a pure argv builder: run_sandbox creates the folder
    # never the mounted project's folder of a session's own copies
    data_skills = f"/swarm/{swarm_skills.skills_dir(config.data_dir).relative_to(project).as_posix()}"
    assert docker_module.SANDBOX_SKILLS_DIR.rstrip("/") != data_skills
    # the library's variables never reach the container from the host environment by name, not even through
    # [shell] pass_env (a later `-e NAME` would win over the explicit values)
    monkeypatch.setenv("DREAMTEAM_HOST_SKILLS_DIR", "/elsewhere")
    passing = Config.from_dict(project, {"shell": {"pass_env": ["DREAMTEAM_SKILLS_DIR", "DREAMTEAM_HOST_SKILLS_DIR",
                                                                "ANTHROPIC_API_KEY"]}})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert "DREAMTEAM_SKILLS_DIR" in os.environ and "DREAMTEAM_HOST_SKILLS_DIR" in os.environ
    names = docker_module.env_passthrough(passing)
    assert "ANTHROPIC_API_KEY" in names and not set(names) & {"DREAMTEAM_SKILLS_DIR", "DREAMTEAM_HOST_SKILLS_DIR"}
    cmd = docker_module.sandbox_command(project, 9200, passing, docker="docker")
    assert [p for p in cmd if p.startswith("DREAMTEAM_SKILLS_DIR")] == ["DREAMTEAM_SKILLS_DIR=" +
                                                                        docker_module.SANDBOX_SKILLS_DIR]


def test_sandbox_warns_about_a_library_inside_the_mounted_folder(tmp_path, monkeypatch):
    project = make_project(tmp_path, "sandboxed", port=9100)
    monkeypatch.setenv("DREAMTEAM_SKILLS_DIR", str(tmp_path / "outside-skills"))
    assert docker_module.library_inside_warning(project, Config.load(project)) == []
    monkeypatch.setenv("DREAMTEAM_SKILLS_DIR", str(project / "lib" / "skills"))
    warning = docker_module.library_inside_warning(project, Config.load(project))
    assert len(warning) == 1 and "writable" in warning[0]


def _docker_ready() -> bool:
    exe = shutil.which("docker")
    if not exe:
        return False
    try:
        return subprocess.run([exe, "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.mark.skipif(not _docker_ready(), reason="Docker daemon not available")
def test_docker_image_builds_and_runs():
    docker_module.build_image()
    out = subprocess.run(["docker", "run", "--rm", docker_module.IMAGE, "--version"], capture_output=True, text=True,
                         timeout=300)
    assert out.returncode == 0 and re.search(r"dreamteam \d", out.stdout + out.stderr)
