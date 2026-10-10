"""CLI of the launcher: serve, sessions, traces, console/status against a launcher hub, sandbox."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from dreamteam import cli
from dreamteam import docker as docker_module
from dreamteam import settings as st
from dreamteam.config import Config
from dreamteam.sessions import SessionManager


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_home(tmp_path: Path, port: int) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    (home / "sessions").mkdir()
    st.write_raw(home, {
        "swarm": {"name": "Home", "language": "en", "start_agents": 0, "max_agents": 2, "max_cost_usd": 1.0},
        "server": {"port": port, "resume_sessions": True},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted"}},
    })
    return home


def _post(url: str, body: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def test_no_arguments_runs_serve_without_a_terminal(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    monkeypatch.setattr(cli, "cmd_serve", lambda args: seen.setdefault("args", args) and 0)
    assert cli.main([]) == 0
    assert seen["args"].command == "serve" and seen["args"].home is None
    seen.clear()
    cli.main(["--port", "9999"])
    assert seen["args"].port == 9999


def test_sessions_and_traces_without_hub(tmp_path, capsys):
    home = make_home(tmp_path, free_port())
    assert cli.main(["sessions", "--home", str(home)]) == 0
    assert "No sessions yet" in capsys.readouterr().out
    assert cli.main(["traces", "--home", str(home)]) == 1
    asyncio.run(SessionManager(home).create("Offline", "Plan it", start=False))
    assert cli.main(["status", "--home", str(home)]) == 0
    out = capsys.readouterr().out
    assert "001-offline [stopped]" in out and "Hub: not running" in out


def test_serve_home_hub_end_to_end(tmp_path, capsys, monkeypatch):
    port = free_port()
    home = make_home(tmp_path, port)
    monkeypatch.chdir(tmp_path)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(cli._serve_home(home, "127.0.0.1", port, console=False, open_browser=False,
                                                   goal="Write a haiku", title="Poetry", stop=stop))
        base = f"http://127.0.0.1:{port}"
        for _ in range(200):  # running_hub() calls the hub over HTTP: never from the loop thread
            if cli.hub_file(home).exists() and await asyncio.to_thread(cli.running_hub, home):
                break
            await asyncio.sleep(0.05)
        assert await asyncio.to_thread(cli.running_hub, home) == base
        sessions = await asyncio.to_thread(lambda: json.loads(urllib.request.urlopen(base + "/api/sessions").read()))
        assert sessions["sessions"][0]["id"] == "001-poetry" and sessions["sessions"][0]["status"] == "running"
        assert (await asyncio.to_thread(cli.main, ["sessions", "--home", str(home)])) == 0
        assert (await asyncio.to_thread(cli.main, ["traces", "--home", str(home)])) == 0
        state = await asyncio.to_thread(lambda: json.loads(urllib.request.urlopen(base + "/s/001-poetry/api/state").read()))
        assert state["swarm"]["goal"]["title"] == "Write a haiku"
        assert (await asyncio.to_thread(cli._pick_session, base, None))["id"] == "001-poetry"
        with pytest.raises(LookupError):
            await asyncio.to_thread(cli._pick_session, base, "nope")

        # the launcher MCP (/mcp) and a session's hub MCP (/s/{sid}/mcp) over streamable HTTP, mcp 2.x client
        import httpx2
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client

        async with httpx2.AsyncClient(timeout=30) as http, \
                Client(streamable_http_client(base + "/mcp", http_client=http)) as client:
            names = [t.name for t in (await client.list_tools()).tools]
            assert names[:4] == ["sessions_list", "session_create", "session_start", "session_stop"]
            res = await client.call_tool("board", {})  # the only running session
            assert not res.is_error and "Write a haiku" in res.content[0].text
            res = await client.call_tool("session_create", {"title": "Later", "goal": "Not now", "start": False})
            assert not res.is_error and f"{base}/s/002-later/" in res.content[0].text
        async with httpx2.AsyncClient(timeout=30) as http, \
                Client(streamable_http_client(base + "/s/001-poetry/mcp", http_client=http)) as client:
            names = [t.name for t in (await client.list_tools()).tools]
            assert "board" in names and "sessions_list" not in names
            res = await client.call_tool("board", {})
            assert not res.is_error and "Write a haiku" in res.content[0].text
        # `dreamteam mcp` while the launcher runs: a stdio proxy, which hands on the launcher's instructions (the
        # single-swarm ones said nothing about sessions)
        def through_proxy() -> str:
            proc = subprocess.Popen([sys.executable, "-m", "dreamteam", "mcp", "--home", str(home)], cwd=str(tmp_path),
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    env=_child_env())
            try:
                proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "t", "version": "1"}}}) + "\n").encode())
                proc.stdin.flush()
                return _read_line(proc)["result"]["instructions"]
            finally:
                proc.stdin.close()  # end of input: the proxy exits
                try:
                    proc.wait(30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(10)

        instructions = await asyncio.to_thread(through_proxy)
        assert "session_create" in instructions and "sessions_list" in instructions
        # the console refuses a stopped session (each of its commands would get 409) and a dead URL
        assert (await asyncio.to_thread(cli.main, ["console", "--home", str(home), "--session", "002-later"])) == 1
        assert (await asyncio.to_thread(cli.main, ["console", "--url", f"http://127.0.0.1:{free_port()}"])) == 1
        stop.set()
        assert await asyncio.wait_for(task, 30) == 0

    asyncio.run(scenario())
    out, err = capsys.readouterr()
    assert "Session 002-later is stopped" in err and "No DreamTeam hub answers" in err
    assert "001-poetry [running] Poetry" in out and "No traces." in out
    assert not cli.hub_file(home).exists()
    manager = SessionManager(home)
    assert manager.meta("001-poetry")["autostart"] is True  # resumed at the next start


def test_serve_refuses_a_busy_port(tmp_path, capsys):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        home = make_home(tmp_path, port)
        code = asyncio.run(cli._serve_home(home, "127.0.0.1", port, console=False, open_browser=False))
    assert code == 1 and "busy" in capsys.readouterr().err


def test_sandbox_serves_a_home(tmp_path):
    home = make_home(tmp_path, 8765)
    config = Config.load(home)
    cmd = docker_module.sandbox_command(home, 9300, config, goal="Go", docker="docker")
    assert cmd[cmd.index("dreamteam:latest") + 1] == "serve" and "--no-browser" in cmd and cmd[-2:] == ["--goal", "Go"]
    assert '"serve", "/swarm"' in docker_module.DOCKERFILE


# ---- home resolution: --home / DIR / the default home ---------------------------------------------------
def _project(tmp_path: Path, name: str) -> Path:
    assert cli.main(["init", str(tmp_path / name), "--lang", "en"]) == 0
    return tmp_path / name


def test_home_mode_resolution(tmp_path, monkeypatch):
    """`--home H` and the default home are launcher homes even before `serve` created sessions/ in them; a DIR
    is one only when it has sessions/ (else it is a single-swarm project of `run`)."""
    monkeypatch.setenv("DREAMTEAM_HOME", str(tmp_path / "default"))
    monkeypatch.chdir(tmp_path)
    parser = cli.build_parser()

    def mode(*argv):
        args = parser.parse_args(list(argv))
        path = cli._home_arg(args)
        return path, cli._home_mode(args, path)

    assert mode("status") == ((tmp_path / "default").resolve(), True)
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "dreamteam.toml").write_text("", encoding="utf-8")
    assert mode("mcp", "--home", str(bare)) == (bare.resolve(), True)
    project = _project(tmp_path, "proj")
    assert mode("mcp", str(project)) == (project.resolve(), False)
    (project / "sessions").mkdir()
    assert mode("mcp", str(project)) == (project.resolve(), True)
    monkeypatch.chdir(bare)  # a project (no sessions/) in the current directory
    assert mode("status") == (bare.resolve(), False)


def test_listing_commands_do_not_create_a_home(tmp_path, monkeypatch, capsys):
    missing = tmp_path / "not-yet"
    monkeypatch.setenv("DREAMTEAM_HOME", str(missing))
    monkeypatch.chdir(tmp_path)
    assert cli.main(["sessions"]) == 0
    assert cli.main(["status"]) == 0
    assert cli.main(["traces"]) == 1
    assert "No sessions yet" in capsys.readouterr().out
    assert not missing.exists()
    project = _project(tmp_path, "proj")  # `sessions DIR` on a `run` project must not turn it into a home
    assert cli.main(["sessions", str(project)]) == 0
    assert not (project / "sessions").exists() and not cli.is_home(project)


def test_traces_offline_reads_the_most_recent_session(tmp_path, capsys):
    home = make_home(tmp_path, free_port())

    async def prepare():
        manager = SessionManager(home)
        for title in ("Older", "Newer"):
            await manager.create(title, f"Goal {title}")
            await asyncio.sleep(0.05)
        await manager.create("Never", "Not started", start=False)
        await manager.shutdown()

    asyncio.run(prepare())
    capsys.readouterr()
    assert cli.main(["traces", "--home", str(home), "--json"]) == 0
    out, err = capsys.readouterr()
    assert "Session 002-newer" in err and json.loads(out) == []
    assert cli.main(["traces", "--home", str(home), "--session", "nope"]) == 1
    assert "Unknown session 'nope'" in capsys.readouterr().err
    assert cli.main(["traces", "--home", str(home), "--session", "003-never"]) == 0
    assert "No traces." in capsys.readouterr().out
    assert not (home / "sessions" / "003-never" / ".dreamteam" / "swarm.db").exists()


def test_sandbox_serves_a_home_without_sessions_dir(tmp_path, monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(docker_module, "run_sandbox",
                        lambda project, port, **kw: seen.update(project=project, **kw) or 0)
    bare = tmp_path / "bare"
    assert cli.main(["sandbox", "--home", str(bare), "--port", "9301"]) == 0
    assert seen["home"] is True and (bare / "sessions").is_dir()
    cmd = docker_module.sandbox_command(tmp_path / "x", 9301, Config.load(bare), home=True, docker="docker")
    assert cmd[cmd.index("dreamteam:latest") + 1] == "serve"
    project = _project(tmp_path, "proj")
    assert cli.main(["sandbox", str(project), "--port", "9301"]) == 0
    assert seen["home"] is False and not (project / "sessions").exists()
    # a hub already serving the home: a container would run the same sessions a second time
    seen.clear()
    monkeypatch.setattr(cli, "running_hub", lambda path: "http://127.0.0.1:9999")
    assert cli.main(["sandbox", "--home", str(bare)]) == 1
    assert not seen and "already running" in capsys.readouterr().err


def test_serve_reports_unusable_homes(tmp_path, capsys, monkeypatch):
    afile = tmp_path / "file.txt"
    afile.write_text("x", encoding="utf-8")
    assert cli.main(["serve", str(afile), "--no-browser", "--no-console"]) == 1
    assert "Cannot use" in capsys.readouterr().err
    home = make_home(tmp_path, free_port())

    async def broken_create(self, *args, **kwargs):
        raise RuntimeError("git is not installed")

    monkeypatch.setattr(SessionManager, "create", broken_create)
    code = asyncio.run(cli._serve_home(home, "127.0.0.1", free_port(), console=False, open_browser=False, goal="Go"))
    assert code == 1 and "git is not installed" in capsys.readouterr().err
    assert not cli.hub_file(home).exists()


def _child_env(**extra: str) -> dict[str, str]:
    env = dict(os.environ, PYTHONUTF8="1", **extra)
    for name in ("DREAMTEAM_PUBLIC_URL", "DREAMTEAM_ALLOWED_HOSTS"):
        env.pop(name, None)
    return env


def _read_line(proc: subprocess.Popen, timeout: float = 60) -> dict:
    box: dict[str, bytes] = {}
    reader = threading.Thread(target=lambda: box.setdefault("line", proc.stdout.readline()), daemon=True)
    reader.start()
    reader.join(timeout)
    assert box.get("line"), f"no output from the MCP server (exit={proc.poll()})"
    return json.loads(box["line"])


def test_stdio_launcher_creates_the_home_and_stops_on_signal(tmp_path):
    """`dreamteam mcp` before the first `serve`: it creates the default home and serves the launcher MCP (it
    failed: the home did not exist). Ctrl+Break / SIGTERM while the client keeps stdin open must end it (the
    SDK's stdin reader thread could not be cancelled: the process hung and no session was stopped)."""
    home = tmp_path / "fresh-home"
    group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32"
             else {"start_new_session": True})
    proc = subprocess.Popen([sys.executable, "-m", "dreamteam", "mcp", "--no-web"], cwd=str(tmp_path),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            env=_child_env(DREAMTEAM_HOME=str(home)), **group)
    try:
        def send(obj: dict) -> None:
            proc.stdin.write((json.dumps(obj) + "\n").encode())
            proc.stdin.flush()

        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
        assert "launcher" in _read_line(proc)["result"]["instructions"]
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "sessions_list", "arguments": {}}})
        assert "No sessions yet" in _read_line(proc)["result"]["content"][0]["text"]
        assert cli.is_home(home)
        if sys.platform == "win32":
            os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
        else:
            proc.send_signal(signal.SIGTERM)
        assert proc.wait(30) == 0
        assert "launcher stopped" in (home / ".dreamteam" / "logs" / "dreamteam.log").read_text(encoding="utf-8")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)
        proc.stdin.close()


def test_nul_stdin_is_no_terminal():
    """On Windows NUL claims to be a tty: `dreamteam serve < NUL` (a scheduled task, `start /b`) attached the
    console, which read EOF at once, and its end shut the launcher down right after it started."""
    script = "from dreamteam.cli import _stdin_is_terminal; print(_stdin_is_terminal())"
    for stdin in (subprocess.DEVNULL, subprocess.PIPE):
        out = subprocess.run([sys.executable, "-c", script], stdin=stdin, capture_output=True, text=True,
                             env=_child_env(), timeout=60)
        assert out.stdout.strip() == "False", out.stderr


def test_serve_subprocess_stops_on_signal_and_keeps_sessions_for_resume(tmp_path):
    """`dreamteam --home H` (options only = serve) with a running session, then Ctrl+Break / SIGTERM: exit 0,
    hub.json removed, the session stopped but still marked for the next start."""
    port = free_port()
    home = make_home(tmp_path, port)

    async def prepare():
        manager = SessionManager(home)
        await manager.create("Keep", "Keep going")
        await manager.shutdown()

    asyncio.run(prepare())
    group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32"
             else {"start_new_session": True})
    proc = subprocess.Popen([sys.executable, "-m", "dreamteam", "--home", str(home), "--no-browser"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env=_child_env(), **group)
    base = f"http://127.0.0.1:{port}"
    try:
        sessions = None
        for _ in range(300):
            try:
                with urllib.request.urlopen(base + "/api/sessions", timeout=2) as resp:
                    sessions = json.loads(resp.read())["sessions"]
                if sessions and sessions[0]["status"] == "running" and cli.running_hub(home) == base:
                    break
            except OSError:
                pass
            assert proc.poll() is None, "the launcher exited by itself"  # (NUL stdin used to end it at once)
            time.sleep(0.1)
        assert sessions and sessions[0]["status"] == "running"
        time.sleep(2)
        assert proc.poll() is None, "the launcher exited by itself"
        if sys.platform == "win32":
            os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
        else:
            proc.send_signal(signal.SIGTERM)
        assert proc.wait(30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)
    assert not cli.hub_file(home).exists()
    assert SessionManager(home).meta("001-keep")["autostart"] is True
    assert "launcher stopped" in (home / ".dreamteam" / "logs" / "dreamteam.log").read_text(encoding="utf-8")


def test_console_session_command(capsys):
    """`/session` (docs/SPEC.md §18.3) was missing: the console printed 'Unknown command'."""
    from dreamteam import console as console_module

    class Hub:
        def __init__(self, session):
            self.session = session

        def get(self, path, **params):
            assert path == "/api/state"
            return {"swarm": {"session": self.session}, "agents": []}

    con = console_module.Console("http://127.0.0.1:9", "en")
    assert "/session" in console_module.HELP["en"] and "/session" in console_module.HELP["ru"]
    con.hub = Hub({"id": "001-snake", "title": "Snake\x1b]0;evil\x07", "status": "running"})
    con.handle("/session")
    out = capsys.readouterr().out
    assert "Session 001-snake: Snake]0;evil [running]" in out and "--session ID" in out and "\x1b" not in out
    con.lang = "ru"
    con.handle("/session")
    assert "Сессия 001-snake" in capsys.readouterr().out
    con.hub = Hub(None)  # `dreamteam run`: no launcher session
    con.handle("/session")
    assert "отдельный рой" in capsys.readouterr().out
