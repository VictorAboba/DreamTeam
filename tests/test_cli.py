"""CLI: init, status and the stdio MCP server in a real subprocess."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import anyio
import pytest
from proc_guard import call

from dreamteam.cli import main
from dreamteam.config import Config


def _init(tmp_path: Path, *extra: str) -> Path:
    project = tmp_path / "swarm"
    assert main(["init", str(project), "--lang", "en", "--name", "CLI test", *extra]) == 0
    text = (project / "dreamteam.toml").read_text(encoding="utf-8")
    text = text.replace("start_agents = 3", "start_agents = 0").replace(
        'library_dir = "~/.dreamteam/library"', f'library_dir = "{(tmp_path / "lib").as_posix()}"')
    (project / "dreamteam.toml").write_text(text, encoding="utf-8")
    return project


def test_init_creates_loadable_project(tmp_path, capsys):
    project = _init(tmp_path, "--provider", "ollama", "--goal", 'Say "hi"')
    cfg = Config.load(project)
    assert cfg.swarm.name == "CLI test" and cfg.swarm.language == "en"
    assert cfg.swarm.goal == 'Say "hi"'
    assert cfg.model(None).provider == "openai" and "11434" in cfg.model(None).base_url
    assert (project / "workspace").is_dir()
    assert main(["init", str(project)]) == 1  # refuses to overwrite


def test_status_without_hub(tmp_path, capsys):
    project = _init(tmp_path)
    capsys.readouterr()
    assert call(main, ["status", str(project)], timeout=60, what="dreamteam status") == 0
    out = capsys.readouterr().out
    assert "CLI test" in out and "Hub: not running" in out


async def test_stdio_mcp_server_subprocess(tmp_path):
    from mcp import Client, StdioServerParameters, stdio_client

    project = _init(tmp_path)
    env = {k: v for k, v in os.environ.items()}
    env["PYTHONUTF8"] = "1"
    params = StdioServerParameters(command=sys.executable, args=["-m", "dreamteam", "mcp", str(project), "--no-web"],
                                   env=env, cwd=str(tmp_path))
    with open(tmp_path / "stderr.log", "w", encoding="utf-8") as errlog:
        try:
            # bounded: leaving the client (also when the bound cancels it) kills the server's whole process tree
            with anyio.fail_after(120):
                async with Client(stdio_client(params, errlog=errlog)) as client:
                    names = {t.name for t in (await client.list_tools()).tools}
                    assert {"set_goal", "board", "join_swarm"} <= names
                    res = await client.call_tool("set_goal", {"title": "Test goal over stdio"})
                    assert not res.is_error, res.content[0].text
                    res = await client.call_tool("board", {})
                    assert "Test goal over stdio" in res.content[0].text
        except TimeoutError:
            pytest.fail("the stdio MCP server did not answer within 120 s: its process tree was killed")


# ---- the work mode on the command line (docs/SPEC.md §22.8 "CLI") -------------------------------------------------
def test_mode_flags_are_neither_tui_nor_serve_only():
    from dreamteam import cli

    assert not {"--mode", "--goal-rule"} & (cli.TUI_FLAGS | cli.SERVE_FLAGS)
    parser = cli.build_parser()
    for command in ("tui", "serve", "run"):
        args = parser.parse_args([command])
        assert args.mode is None and args.goal_rule is None, command
        args = parser.parse_args([command, "--mode", "thread", "--goal-rule", "veto"])
        assert (args.mode, args.goal_rule) == ("thread", "veto"), command
    with pytest.raises(SystemExit):
        parser.parse_args(["tui", "--mode", "kanban"])
    sandbox = parser.parse_args(["sandbox"])
    assert not hasattr(sandbox, "mode") and not hasattr(sandbox, "goal_rule")


def test_print_mode_refuses_the_mode_and_the_rule_of_an_existing_session(tmp_path, capsys, monkeypatch):
    import dreamteam.tui as tui

    monkeypatch.setattr(tui, "main_print", lambda *a, **kw: pytest.fail("print mode must not start"))
    home = str(tmp_path / "home")
    for extra in (["-c"], ["-r", "001-x"]):
        assert main(["-p", "next", *extra, "--mode", "thread", "--home", home]) == 2
        assert ("dreamteam -p: --mode is chosen when a session is created; an existing session keeps its mode."
                in capsys.readouterr().err)
        assert main(["-p", "next", *extra, "--goal-rule", "veto", "--home", home]) == 2
        assert ("dreamteam -p: --goal-rule is a setting of a new session; change an existing one with /rule in "
                "dreamteam tui or in its ⚙ settings on the board." in capsys.readouterr().err)


def test_print_mode_and_the_tui_pass_the_mode_to_new_sessions(tmp_path, monkeypatch):
    import dreamteam.cli as cli
    import dreamteam.tui as tui
    from dreamteam.tui.commands import session_overrides

    seen: dict = {}
    monkeypatch.setattr(tui, "main_print", lambda home, prompt, **kw: seen.update(print=kw) or 0)
    monkeypatch.setattr(tui, "main_tui", lambda home, **kw: seen.update(tui=kw) or 0)
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    home = str(tmp_path / "home")
    assert main(["-p", "Build it", "--mode", "thread", "--goal-rule", "veto", "--home", home]) == 0
    overrides = seen["print"]["overrides"]
    assert overrides["mode"] == "thread" and overrides["goal_rule"] == "veto"
    assert session_overrides(overrides)[0]["swarm"] == {"mode": "thread", "goal_rule": "veto"}
    assert main(["-p", "Build it", "--home", home]) == 0
    assert "mode" not in seen["print"]["overrides"] and "goal_rule" not in seen["print"]["overrides"]
    # the TUI: the window's setting for its new sessions, also with -c
    assert main(["tui", "-c", "--mode", "thread", "--home", home]) == 0
    assert seen["tui"]["overrides"]["mode"] == "thread" and "goal_rule" not in seen["tui"]["overrides"]


def test_serve_takes_the_mode_with_goal_only(tmp_path, capsys, monkeypatch):
    import dreamteam.cli as cli

    seen: dict = {}
    monkeypatch.setattr(cli, "setup_logging", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "_serve_home", lambda *a, **kw: seen.update(kw))
    monkeypatch.setattr(cli, "run_loop", lambda main: 0)
    home = str(tmp_path / "home")
    assert main(["serve", "--home", home, "--mode", "thread", "--no-browser"]) == 2
    assert ("--mode needs --goal: it is a setting of the session that --goal creates (an existing session keeps its "
            "mode).") in capsys.readouterr().err
    assert main(["serve", "--home", home, "--goal-rule", "veto", "--no-browser"]) == 2
    assert ("--goal-rule needs --goal: it is a setting of the session that --goal creates (an existing session "
            "changes it in its ⚙ settings or with /rule).") in capsys.readouterr().err
    assert seen == {}
    assert main(["serve", "--home", home, "--goal", "Build it", "--mode", "thread", "--goal-rule", "veto",
                 "--no-browser", "--no-console"]) == 0
    assert seen["mode"] == "thread" and seen["goal_rule"] == "veto"


async def test_serve_with_goal_creates_the_session_in_its_mode(tmp_path, monkeypatch):
    """_serve_home's create gets the mode and the rule as swarm overrides (stopped at once by the stop event)."""
    import asyncio

    import dreamteam.cli as cli
    from dreamteam import settings as st
    from dreamteam.sessions import SessionManager

    home = tmp_path / "home"
    home.mkdir()
    st.write_raw(home, {"swarm": {"name": "Home", "language": "en", "start_agents": 0},
                        "mcp": {"library_dir": str(tmp_path / "library")},
                        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0,
                                               "output_price": 0}}})
    made: list[dict] = []
    real_create = SessionManager.create

    async def create(self, *args, **kwargs):
        made.append(kwargs.get("overrides"))
        return await real_create(self, *args, **kwargs)

    async def start(self, sid):   # nothing runs: the overrides are what this test is about
        raise RuntimeError("not started in this test")

    monkeypatch.setattr(SessionManager, "create", create)
    monkeypatch.setattr(SessionManager, "start", start)
    monkeypatch.setattr(cli, "_port_free", lambda host, port: True)
    code = await cli._serve_home(home, "127.0.0.1", 1, console=False, open_browser=False, goal="Build it",
                                 stop=asyncio.Event(), mode="thread", goal_rule="veto")
    assert code == 1 and made == [{"swarm": {"mode": "thread", "goal_rule": "veto"}}]
    manager = SessionManager(home)
    (sid,) = manager.ids()
    assert manager.summary(sid)["mode"] == "thread" and manager.summary(sid)["goal_rule"] == "veto"


def test_run_fixes_the_projects_mode_once(tmp_path, capsys):
    from dreamteam.cli import _project_mode
    from dreamteam.db import Database
    from dreamteam.llm.scripted import ScriptedLLM
    from dreamteam.swarm import Swarm

    project = _init(tmp_path)
    db_file = Config.load(project).data_dir / "swarm.db"
    assert _project_mode(db_file) is None                     # no database yet: it takes the flag
    Database(db_file).close()
    assert _project_mode(db_file) is None                     # empty: it takes the flag too

    def open_swarm() -> str:
        sw = Swarm(config=Config.load(project), llm_factory=lambda p, a: ScriptedLLM(lambda *x: "ok", p))
        try:
            return sw.mode
        finally:
            sw.close()

    assert open_swarm() == "board" and _project_mode(db_file) == "board"
    toml = project / "dreamteam.toml"
    toml.write_text(toml.read_text(encoding="utf-8").replace('mode = "board"', 'mode = "thread"', 1),
                    encoding="utf-8")
    assert Config.load(project).swarm.mode == "thread"
    assert open_swarm() == "board"                            # kv wins over dreamteam.toml
    assert main(["run", str(project), "--mode", "thread"]) == 2
    assert ("this project's swarm.db is a board swarm: the mode is chosen when it is created"
            in capsys.readouterr().err)
    # a database with work but no kv (made before the thread mode) is a board one
    other = tmp_path / "old.db"
    old = Database(other)
    old.execute("INSERT INTO events(ts, type, actor, data) VALUES (1, 'x', NULL, '{}')")
    old.close()
    assert _project_mode(other) == "board"
    thread = Database(tmp_path / "thread.db")
    thread.set("swarm_mode", "thread")
    thread.close()
    assert _project_mode(tmp_path / "thread.db") == "thread"
