"""CLI of the launcher: serve, sessions, traces, console/status against a launcher hub, sandbox."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from proc_guard import kill_tree, read_line, run as run_bounded, run_async, spawn

from dreamteam import cli
from dreamteam import console as console_module
from dreamteam import docker as docker_module
from dreamteam import sessions as sessions_module
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


def test_sessions_rm_without_a_hub(tmp_path, capsys, monkeypatch):
    home = make_home(tmp_path, free_port())
    manager = SessionManager(home)
    for title in ("One", "Two", "Three"):
        asyncio.run(manager.create(title, "", start=False))
    sessions = home / "sessions"
    capsys.readouterr()
    # without a terminal to confirm in, nothing is deleted unless --yes
    monkeypatch.setattr(cli, "_stdin_is_terminal", lambda: False)
    assert cli.main(["sessions", "rm", "001-one", "--home", str(home)]) == 1
    assert "pass --yes" in capsys.readouterr().err and (sessions / "001-one").is_dir()
    # an unknown id: nothing is deleted
    assert cli.main(["sessions", "rm", "001-one", "nope", "--home", str(home), "--yes"]) == 1
    err = capsys.readouterr().err
    assert "Unknown session: nope" in err and "003-three" in err and (sessions / "001-one").is_dir()
    # in a terminal it asks: "n" keeps the sessions, "y" deletes them (a repeated id once)
    monkeypatch.setattr(cli, "_stdin_is_terminal", lambda: True)
    answers = iter(["n", "y"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert cli.main(["sessions", "rm", "001-one", "002-two", "--home", str(home)]) == 0
    out = capsys.readouterr().out
    assert "001-one [stopped] One" in out and "Nothing deleted" in out and (sessions / "002-two").is_dir()
    assert cli.main(["sessions", "delete", "001-one", "002-two", "001-one", "--home", str(home)]) == 0
    out = capsys.readouterr().out
    assert out.count("Deleted session 001-one.") == 1 and "Deleted session 002-two." in out
    assert [s["id"] for s in SessionManager(home).list()] == ["003-three"]
    assert sorted(p.name for p in sessions.iterdir()) == ["003-three"]  # no renamed-away leftovers
    # listing still takes a DIR; `rm` without ids prints the usage; a folder that is not a home is refused
    assert cli.main(["sessions", str(home)]) == 0 and "003-three [stopped]" in capsys.readouterr().out
    assert cli.main(["sessions", "rm", "--home", str(home)]) == 2
    assert "sessions rm ID" in capsys.readouterr().err
    assert cli.main(["sessions", "rm", "x", "--yes", "--home", str(tmp_path / "nothing")]) == 1
    assert "not a DreamTeam home" in capsys.readouterr().err and not (tmp_path / "nothing").exists()
    with pytest.raises(SystemExit):
        cli.main(["sessions", "rm", "003-three", "--force"])


def test_sessions_rm_refusals_without_a_hub(tmp_path, capsys, monkeypatch):
    from dreamteam.sessions import SessionError

    home = make_home(tmp_path, free_port())
    for title in ("One", "Two"):
        asyncio.run(SessionManager(home).create(title, "", start=False))
    sessions = home / "sessions"
    capsys.readouterr()
    # another DreamTeam process runs this home's sessions without a hub that answers (`dreamteam mcp --no-web`):
    # nothing is deleted behind its back; the marker of a process that is gone is removed
    holder = spawn([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        marker = home / ".dreamteam" / f"launcher-{holder.pid}.pid"
        marker.parent.mkdir(exist_ok=True)
        marker.write_text(str(holder.pid), encoding="utf-8")
        assert cli.main(["sessions", "rm", "001-one", "--yes", "--home", str(home)]) == 1
        err = capsys.readouterr().err
        assert "another DreamTeam process" in err and f"process {holder.pid}" in err and (sessions / "001-one").is_dir()
    finally:
        kill_tree(holder)
    dead = home / ".dreamteam" / f"launcher-{holder.pid}.pid"
    assert dead.exists()
    # a refusal of the home is printed once (not "Could not delete session X: Could not delete session X: ...")
    async def busy(self, sid, **kwargs):
        raise SessionError(f"Could not delete session {sid}: its files are in use (WinError 32).", 409)

    monkeypatch.setattr(SessionManager, "delete", busy)
    assert cli.main(["sessions", "rm", "001-one", "--yes", "--home", str(home)]) == 1
    err = capsys.readouterr().err
    assert err.count("Could not delete session 001-one") == 1 and "files are in use" in err
    assert not dead.exists()  # the stale marker went away on the way
    monkeypatch.undo()
    # Ctrl+C at the question deletes nothing
    monkeypatch.setattr(cli, "_stdin_is_terminal", lambda: True)

    def interrupt(prompt=""):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", interrupt)
    assert cli.main(["sessions", "rm", "001-one", "--home", str(home)]) == 130
    assert "Nothing deleted" in capsys.readouterr().out and (sessions / "001-one").is_dir()
    # the main session of a project home is refused before the question
    listed = SessionManager.list
    monkeypatch.setattr(SessionManager, "list",
                        lambda self: [{**s, "legacy": s["id"] == "002-two"} for s in listed(self)])
    assert cli.main(["sessions", "rm", "001-one", "002-two", "--home", str(home)]) == 1
    err = capsys.readouterr().err
    assert "002-two is the project's own session" in err and (sessions / "001-one").is_dir()


def test_serve_home_hub_end_to_end(tmp_path, capsys, monkeypatch):
    port = free_port()
    home = make_home(tmp_path, port)
    monkeypatch.chdir(tmp_path)

    async def steps(base: str) -> None:
        for _ in range(200):  # running_hub() calls the hub over HTTP: never from the loop thread
            if cli.hub_file(home).exists() and await asyncio.to_thread(cli.running_hub, home):
                break
            await asyncio.sleep(0.05)
        assert await asyncio.to_thread(cli.running_hub, home) == base
        sessions = await asyncio.to_thread(_get_json, base + "/api/sessions")
        assert sessions["sessions"][0]["id"] == "001-poetry" and sessions["sessions"][0]["status"] == "running"
        assert (await asyncio.to_thread(cli.main, ["sessions", "--home", str(home)])) == 0
        assert (await asyncio.to_thread(cli.main, ["traces", "--home", str(home)])) == 0
        state = await asyncio.to_thread(_get_json, base + "/s/001-poetry/api/state")
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
            proc = spawn([sys.executable, "-m", "dreamteam", "mcp", "--home", str(home)], cwd=str(tmp_path),
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
                    kill_tree(proc)

        instructions = await asyncio.to_thread(through_proxy)
        assert "session_create" in instructions and "sessions_list" in instructions
        # the console refuses a stopped session (each of its commands would get 409) and a dead URL
        assert (await asyncio.to_thread(cli.main, ["console", "--home", str(home), "--session", "002-later"])) == 1
        assert (await asyncio.to_thread(cli.main, ["console", "--url", f"http://127.0.0.1:{free_port()}"])) == 1
        # ...but not a stopped session whose folder is gone: /cd is what fixes it, and the console can do that; the
        # hub found through the home's hub.json is on this computer (relative /cd paths), whatever its address
        usb = tmp_path / "usb" / "work"
        created = await asyncio.to_thread(_post, base + "/api/sessions", {"title": "Usb", "goal": "Later",
                                                                         "start": False, "workspace": str(usb)})
        usb_sid = created["session"]["id"]
        sessions_module._rmtree(tmp_path / "usb")  # (git objects are read-only on Windows)
        consoles: list[tuple] = []
        monkeypatch.setattr(console_module, "run_console",
                            lambda url, lang, stop=None, local=None: consoles.append((url, local)))
        assert (await asyncio.to_thread(cli.main, ["console", "--home", str(home), "--session", usb_sid])) == 0
        assert consoles == [(f"{base}/s/{usb_sid}", True)]
        await asyncio.to_thread(lambda: urllib.request.urlopen(urllib.request.Request(
            f"{base}/api/sessions/{usb_sid}?confirm={usb_sid}", method="DELETE"), timeout=20).close())
        # `sessions rm` deletes through the running hub, which stops a running session first
        await asyncio.to_thread(_post, base + "/api/sessions/002-later/start", {})
        assert (await asyncio.to_thread(cli.main, ["sessions", "rm", "--yes", "002-later", "--home", str(home)])) == 0
        listed = await asyncio.to_thread(_get_json, base + "/api/sessions")
        assert [s["id"] for s in listed["sessions"]] == ["001-poetry"] and not (home / "sessions" / "002-later").exists()

    async def scenario() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(cli._serve_home(home, "127.0.0.1", port, console=False, open_browser=False,
                                                   goal="Write a haiku", title="Poetry", stop=stop))
        try:
            await steps(f"http://127.0.0.1:{port}")
        except BaseException:
            stop.set()  # the hub stops also when a step failed or the bound below ran out...
            with contextlib.suppress(Exception):
                await asyncio.wait_for(task, 30)
            raise  # ...and the step's failure is what the test reports
        stop.set()
        try:
            code = await asyncio.wait_for(task, 30)
        except TimeoutError:
            pytest.fail("the hub did not stop within 30 s")
        assert code == 0

    run_async(scenario, 180, "the launcher scenario")
    out, err = capsys.readouterr()
    assert "Session 002-later is stopped" in err and "No DreamTeam hub answers" in err
    assert "cannot start: its folder" in err and "/cd <folder>" in err
    assert "001-poetry [running] Poetry" in out and "No traces." in out
    assert "Deleted session 002-later." in out
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


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=20) as resp:
        return json.loads(resp.read())


def _read_line(proc: subprocess.Popen, timeout: float = 60) -> dict:
    line = read_line(proc, timeout, "the MCP server")  # kills the tree and fails when none comes
    assert line, f"no output from the MCP server (exit={proc.poll()})"
    return json.loads(line)


def test_stdio_launcher_creates_the_home_and_stops_on_signal(tmp_path):
    """`dreamteam mcp` before the first `serve`: it creates the default home and serves the launcher MCP (it
    failed: the home did not exist). Ctrl+Break / SIGTERM while the client keeps stdin open must end it (the
    SDK's stdin reader thread could not be cancelled: the process hung and no session was stopped)."""
    home = tmp_path / "fresh-home"
    group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32"
             else {"start_new_session": True})
    proc = spawn([sys.executable, "-m", "dreamteam", "mcp", "--no-web"], cwd=str(tmp_path),
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
        kill_tree(proc)
        proc.stdin.close()


def test_nul_stdin_is_no_terminal():
    """On Windows NUL claims to be a tty: `dreamteam serve < NUL` (a scheduled task, `start /b`) attached the
    console, which read EOF at once, and its end shut the launcher down right after it started."""
    script = "from dreamteam.cli import _stdin_is_terminal; print(_stdin_is_terminal())"
    for stdin in (subprocess.DEVNULL, subprocess.PIPE):
        out = run_bounded([sys.executable, "-c", script], 60, stdin=stdin, capture_output=True, text=True,
                          env=_child_env())
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
    proc = spawn([sys.executable, "-m", "dreamteam", "--home", str(home), "--no-browser"],
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
        kill_tree(proc)
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


# ---------------------------------------------------------------------------------- goal split, --team
def test_team_option_is_a_tui_option_checked_before_anything_starts(tmp_path, monkeypatch, capsys):
    from dreamteam import tui

    commands = [name for action in cli.build_parser()._subparsers._group_actions for name in action.choices]
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    assert cli._default_command(["--team", "tester"], commands) == ["tui", "--team", "tester"]
    seen: dict = {}
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(tui, "main_tui", lambda home, **kw: seen.update(kw) or 0)
    monkeypatch.setattr(tui, "main_print", lambda home, prompt, **kw: seen.update(kw) or 0)
    assert cli.main(["--home", str(tmp_path), "Сделай змейку", "--team", "architect, developer*2"]) == 0
    assert seen["overrides"]["team"] == "architect, developer*2"  # the TUI turns it into swarm.starting_roles
    seen.clear()
    assert cli.main(["--home", str(tmp_path), "Сделай змейку"]) == 0
    assert "team" not in seen["overrides"]
    seen.clear()
    assert cli.main(["-p", "Сделай змейку", "--home", str(tmp_path), "--team", "tester*2"]) == 0
    assert seen["overrides"]["team"] == "tester*2"
    seen.clear()
    capsys.readouterr()
    for argv, why in ((["--team", "developer*0"], "'developer*0': the count must be a whole number from 1 to 50"),
                      (["--team", " , "], "The team is empty"),
                      (["-p", "x", "--team", "x*51"], "'x*51': the count must be a whole number from 1 to 50")):
        assert cli.main(["--home", str(tmp_path), *argv]) == 2
        assert capsys.readouterr().err.strip() == f"dreamteam: --team: {why}"
    assert not seen  # neither the TUI nor print mode ran


def test_help_explains_the_goal_split_and_the_team():
    choices = next(iter(cli.build_parser()._subparsers._group_actions)).choices
    tui_help = " ".join(choices["tui"].format_help().split())
    assert "its first line becomes the title, the rest the description" in tui_help
    assert '--team TEAM starting team of a new session: roles in order, ROLE*N for several' in tui_help
    assert "with this goal (first line: the title)" in " ".join(choices["serve"].format_help().split())


async def test_run_keeps_a_multi_line_goal_across_restarts(tmp_path, monkeypatch):
    """`dreamteam run --goal` sets the goal unless the same one is active: the stored title is the goal's first
    line, so a multi-line --goal must not reset the goal (and plan it again) at every restart."""
    from dreamteam import web as web_module
    from dreamteam.db import Database

    async def no_server(swarm, host, port, on_started=None):  # the web server is not what this is about
        await asyncio.Event().wait()

    monkeypatch.setattr(web_module, "serve", no_server)
    monkeypatch.setattr(cli, "_port_free", lambda host, port: True)
    config = Config.from_dict(tmp_path / "proj", {
        "swarm": {"name": "Run", "language": "en", "start_agents": 0, "max_agents": 2, "max_cost_usd": 0},
        "agents": {"stagger_seconds": 0}, "workspace": {"git": False}, "mcp": {"library_dir": str(tmp_path / "lib")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
    })

    async def run(goal: str) -> dict:
        stop = asyncio.Event()
        stop.set()  # stops right after the start, where the goal is set or kept
        assert await cli._serve_swarm(config.project_dir, config, "127.0.0.1", 1, goal, False, False, stop=stop) == 0
        db = Database(config.data_dir / "swarm.db")
        try:
            return db.get("goal")
        finally:
            db.close()

    goal = "Snake game\nPython, pygame\n\n- tests"
    first = await run(goal)
    assert (first["title"], first["description"]) == ("Snake game", "Python, pygame\n\n- tests")
    assert await run(goal) == first  # kept: same updated_at
    changed = await run("Tetris\nPython")
    assert changed["title"] == "Tetris" and changed["updated_at"] >= first["updated_at"]
