"""CLI wiring of the terminal UI: which command `dreamteam ...` runs, and `dreamteam -p` end to end."""

from __future__ import annotations

import asyncio
import io
import json
import os
import socket
import sys
from pathlib import Path
from typing import Any

import pytest

from dreamteam import cli
from dreamteam import settings as st
from dreamteam import tui
from dreamteam.llm.scripted import ScriptedLLM

COMMANDS = ["tui", "chat", "serve", "sessions", "traces", "init", "run", "console", "mcp", "sandbox", "status"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.parametrize("argv, interactive, expected", [
    ([], True, ["tui"]),
    ([], False, ["serve"]),
    (["Сделай игру змейка"], True, ["tui", "Сделай игру змейка"]),
    (["Сделай игру змейка"], False, ["tui", "Сделай игру змейка"]),  # tui itself says it needs a terminal
    (["-p", "goal"], False, ["tui", "-p", "goal"]),
    (["-c"], True, ["tui", "-c"]),
    (["-r", "001-x"], True, ["tui", "-r", "001-x"]),
    (["--output-format=json", "-p", "x"], False, ["tui", "--output-format=json", "-p", "x"]),
    (["--port", "9999"], True, ["tui", "--port", "9999"]),
    (["--port", "9999"], False, ["serve", "--port", "9999"]),
    (["--home", "h", "--no-browser"], True, ["serve", "--home", "h", "--no-browser"]),
    (["--goal", "x"], True, ["serve", "--goal", "x"]),
    (["sesions"], True, ["sesions"]),  # a typo of a command is left to argparse, not sent as a goal
    (["serve"], True, ["serve"]),
    (["--version"], True, ["--version"]),
])
def test_default_command(monkeypatch, argv, interactive, expected):
    monkeypatch.setattr(cli, "_interactive", lambda: interactive)
    assert cli._default_command(argv, COMMANDS) == expected


def test_parser_knows_every_command():
    parser = cli.build_parser()
    names = [name for action in parser._subparsers._group_actions for name in action.choices]
    assert set(COMMANDS) <= set(names)


def test_tui_options_reach_main_tui(monkeypatch, tmp_path):
    seen: dict[str, Any] = {}
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(tui, "main_tui", lambda home, **kw: seen.update(home=home, **kw) or 0)
    assert cli.main(["--home", str(tmp_path), "Сделай змейку", "--model", "fast", "--agents", "2",
                     "--max-agents", "5", "--budget", "3.5", "--title", "Змейка", "--lang", "en"]) == 0
    assert seen["home"] == tmp_path.resolve()
    assert seen["initial_prompt"] == "Сделай змейку" and seen["lang"] == "en"
    assert seen["overrides"] == {"model": "fast", "agents": 2, "max_agents": 5, "budget": 3.5, "title": "Змейка"}
    assert seen["session"] is None and seen["continue_last"] is False and seen["verbosity"] == "normal"

    seen.clear()
    assert cli.main(["-r", "--home", str(tmp_path)]) == 0  # -r without an id opens the session picker
    assert seen["initial_prompt"] == "/sessions" and seen["session"] == ""   # "": the one picked is resumed
    seen.clear()
    assert cli.main(["chat", "-r", "004-kalk", "--home", str(tmp_path), "--verbose"]) == 0
    assert seen["session"] == "004-kalk" and seen["initial_prompt"] is None and seen["verbosity"] == "verbose"
    seen.clear()
    assert cli.main(["-c", "--home", str(tmp_path)]) == 0
    assert seen["continue_last"] is True


def test_team_option_reaches_the_new_session(monkeypatch, tmp_path, capsys):
    from dreamteam.tui.commands import session_overrides

    seen: dict[str, Any] = {}
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(tui, "main_tui", lambda home, **kw: seen.update(**kw) or 0)
    monkeypatch.setattr(tui, "main_print", lambda home, prompt, **kw: seen.update(**kw) or 0)
    monkeypatch.setattr(cli, "_piped_stdin", lambda: "")
    assert cli.main(["--home", str(tmp_path), "Snake", "--team", "architect, developer*2"]) == 0
    assert {k: v for k, v in seen["overrides"].items() if v is not None} == {"team": "architect, developer*2"}
    assert session_overrides(seen["overrides"]) == (
        {"swarm": {"starting_roles": ["architect", "developer", "developer"], "start_agents": 3}}, None)
    seen.clear()
    assert cli.main(["-p", "Snake", "--home", str(tmp_path), "--team", "tester", "--agents", "2"]) == 0
    assert session_overrides(seen["overrides"])[0] == {"swarm": {"starting_roles": ["tester"], "start_agents": 2}}
    seen.clear()
    assert cli.main(["-p", "Snake", "--home", str(tmp_path), "--team", "tester*0"]) == 2   # checked before anything
    assert seen == {} and "--team: 'tester*0': the count must be a whole number" in capsys.readouterr().err


def test_tui_needs_a_terminal(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    monkeypatch.setattr(tui, "main_tui", lambda home, **kw: pytest.fail("the TUI must not start"))
    assert cli.main(["tui", "--home", str(tmp_path)]) == 2
    assert "dreamteam -p" in capsys.readouterr().err


def test_print_options_and_piped_stdin(monkeypatch, tmp_path):
    seen: dict[str, Any] = {}
    monkeypatch.setattr(tui, "main_print", lambda home, prompt, **kw: seen.update(prompt=prompt, **kw) or 0)
    monkeypatch.setattr(cli, "_piped_stdin", lambda: "Traceback: ValueError")
    assert cli.main(["-p", "Почини ошибку", "--home", str(tmp_path), "--output-format", "stream-json",
                     "--timeout", "60", "--no-auto-reply", "--budget", "1"]) == 0
    assert seen["prompt"] == "Почини ошибку\n\nTraceback: ValueError"
    assert seen["output_format"] == "stream-json" and seen["timeout"] == 60 and seen["auto_reply"] is False
    assert seen["overrides"]["budget"] == 1 and seen["session"] is None and "lang" not in seen

    monkeypatch.setattr(cli, "_piped_stdin", lambda: "")
    monkeypatch.setattr(tui, "main_print", lambda *a, **kw: pytest.fail("nothing to run"))
    assert cli.main(["-p", "--home", str(tmp_path)]) == 2


def test_piped_stdin_ignores_terminals_and_unreadable_streams(monkeypatch):
    class Tty(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", Tty("typed"))
    assert cli._piped_stdin() == ""
    monkeypatch.setattr(sys, "stdin", io.StringIO("no fileno"))  # fileno() raises io.UnsupportedOperation
    assert cli._piped_stdin() == ""
    monkeypatch.setattr(sys, "stdin", None)
    assert cli._piped_stdin() == ""


def test_print_mode_end_to_end(monkeypatch, tmp_path, capsys):
    """`dreamteam -p GOAL --output-format json` creates a session, runs it to the goal and prints the result."""
    home = tmp_path / "home"
    home.mkdir()
    st.write_raw(home, {
        "swarm": {"name": "Home", "language": "en", "start_agents": 1, "max_agents": 2, "max_cost_usd": 1.0},
        "agents": {"stagger_seconds": 0},
        "server": {"port": free_port()},
        "workspace": {"git": False},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
    })

    def policy(system, messages, tools):
        seen = [m.get("content") or "" for m in messages if m["role"] in ("user", "tool")]
        if any("marked as achieved" in t or "already marked" in t for t in seen[-1:]):
            return {"tool_calls": [{"name": "wait", "arguments": {}}]}
        if any("snake" in t for t in seen):
            return {"tool_calls": [{"name": "goal_complete", "arguments": {"summary": "snake.py is ready."}}]}
        return {"tool_calls": [{"name": "wait", "arguments": {}}]}

    real = tui.main_print
    monkeypatch.setattr(tui, "main_print", lambda home, prompt, **kw: real(
        home, prompt, llm_factory=lambda profile, agent: ScriptedLLM(policy, profile), **kw))
    monkeypatch.setattr(cli, "_piped_stdin", lambda: "")
    code = cli.main(["-p", "Write a snake game", "--home", str(home), "--output-format", "json",
                     "--title", "Snake", "--agents", "1", "--timeout", "60"])
    out = capsys.readouterr().out
    assert code == 0, out
    result = json.loads(out.strip().splitlines()[-1])
    assert result["type"] == "result" and result["status"] == "done"
    assert result["summary"] == "snake.py is ready."
    sessions = list((home / "sessions").iterdir())
    assert len([p for p in sessions if not p.name.startswith(".")]) == 1
    meta = json.loads(next(p for p in sessions if not p.name.startswith(".")).joinpath("session.json")
                      .read_text(encoding="utf-8"))
    assert meta["title"] == "Snake"
    assert not (home / ".dreamteam" / "hub.json").exists()  # the in-process launcher removed its hub file


def test_workspace_option_reaches_the_session_creation(monkeypatch, tmp_path, capsys):
    """`--workspace DIR` (tui, -p, serve --goal): the folder of the session that run creates, as an absolute path
    (relative to the current directory); refused with -c/-r, for a file, and for serve without --goal."""
    home = str(tmp_path / "home")
    seen: dict[str, Any] = {}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(tui, "main_tui", lambda home, **kw: seen.update(kw) or 0)
    assert cli.main(["--home", home, "Сделай змейку", "--workspace", "proj"]) == 0
    assert seen["workspace"] == str(tmp_path / "proj") and seen["initial_prompt"] == "Сделай змейку"
    seen.clear()
    assert cli.main(["tui", "--home", home, "--workspace", "~/some-project"]) == 0
    assert seen["workspace"] == os.path.normpath(os.path.expanduser("~/some-project"))
    seen.clear()
    assert cli.main(["--home", home]) == 0 and seen["workspace"] is None

    monkeypatch.setattr(tui, "main_print", lambda home, prompt, **kw: seen.update(kw) or 0)
    monkeypatch.setattr(cli, "_piped_stdin", lambda: "")
    assert cli.main(["-p", "Почини тесты", "--home", home, "--workspace", str(tmp_path / "repo")]) == 0
    assert seen["workspace"] == str(tmp_path / "repo")

    monkeypatch.setattr(tui, "main_tui", lambda home, **kw: pytest.fail("must be refused"))
    monkeypatch.setattr(tui, "main_print", lambda *a, **kw: pytest.fail("must be refused"))
    capsys.readouterr()
    assert cli.main(["-c", "--home", home, "--workspace", "proj"]) == 2
    assert cli.main(["-p", "x", "-r", "001-a", "--home", home, "--workspace", "proj"]) == 2
    assert "/cd" in capsys.readouterr().err
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    assert cli.main(["--home", home, "--workspace", "notes.txt"]) == 2
    assert "is a file" in capsys.readouterr().err

    calls: dict[str, Any] = {}

    async def serve_home(home_dir, host, port, **kw):
        calls.update(home=home_dir, **kw)
        return 0

    monkeypatch.setattr(cli, "_serve_home", serve_home)
    monkeypatch.setattr(cli, "setup_logging", lambda *a, **kw: None)
    assert cli.main(["serve", "--home", home, "--workspace", "proj", "--no-browser"]) == 2
    assert "--workspace needs --goal" in capsys.readouterr().err and not calls
    assert cli.main(["serve", "--home", home, "--goal", "Сделай змейку", "--workspace", "proj", "--no-browser",
                     "--no-console"]) == 0
    assert calls["workspace"] == str(tmp_path / "proj") and calls["goal"] == "Сделай змейку"

    parser = cli.build_parser()
    subcommands = parser._subparsers._group_actions[0].choices
    assert "--workspace" in subcommands["tui"].format_help() and "--workspace" in subcommands["serve"].format_help()


def test_skill_option_reaches_the_session(monkeypatch, tmp_path, capsys):
    """`--skill REF|PATH|URL` (repeatable; tui, -p, serve --goal): a path (or a name that is a folder here) made
    absolute, a URL as it is, a reference checked before anything starts; with -c / -r it goes to the resumed
    session; not a TUI-only flag (no terminal: serve)."""
    from conftest import make_skill

    home = tmp_path / "home"
    library = Path(os.environ["DREAMTEAM_SKILLS_DIR"])
    make_skill(library / "pdf", "pdf")
    make_skill(Path(os.environ["CLAUDE_CONFIG_DIR"]) / "skills" / "review", "review")
    make_skill(tmp_path / "tidy", "tidy")
    seen: dict[str, Any] = {}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(tui, "main_tui", lambda home, **kw: seen.update(kw) or 0)
    url = "https://github.com/o/r/tree/main/skills/x"
    assert cli.main(["--home", str(home), "Сделай змейку", "--skill", "pdf", "--skill", "./tidy", "--skill", "tidy",
                     "--skill", "claude-user:review", "--skill", url, "--skill", "~/x.zip"]) == 0
    tidy = str(tmp_path / "tidy")
    assert seen["overrides"]["skills"] == ["pdf", tidy, tidy, "claude-user:review", url,
                                           os.path.normpath(os.path.expanduser("~/x.zip"))]
    seen.clear()
    assert cli.main(["-c", "--home", str(home), "--skill", "pdf"]) == 0   # attached to the resumed session
    assert seen["continue_last"] is True and seen["overrides"]["skills"] == ["pdf"]
    seen.clear()
    assert cli.main(["--home", str(home)]) == 0 and "skills" not in seen["overrides"]

    monkeypatch.setattr(tui, "main_tui", lambda home, **kw: pytest.fail("must be refused"))
    capsys.readouterr()
    assert cli.main(["--home", str(home), "Goal", "--skill", "nope"]) == 2
    assert "--skill nope: No skill 'nope' in the library or in ~/.claude/skills. Library: pdf." in capsys.readouterr().err
    assert cli.main(["--home", str(home), "Goal", "--skill", "claude-project:local"]) == 2   # no --workspace
    project = tmp_path / "project"
    make_skill(project / ".claude" / "skills" / "local", "local")
    monkeypatch.setattr(tui, "main_tui", lambda home, **kw: seen.update(kw) or 0)
    assert cli.main(["--home", str(home), "Goal", "--workspace", str(project), "--skill", "claude-project:local"]) == 0
    assert seen["overrides"]["skills"] == ["claude-project:local"]
    seen.clear()
    assert cli.main(["-r", "001-x", "--home", str(home), "--skill", "claude-project:local"]) == 0  # the hub resolves it
    assert seen["session"] == "001-x" and seen["overrides"]["skills"] == ["claude-project:local"]

    monkeypatch.setattr(tui, "main_print", lambda home, prompt, **kw: seen.update(kw) or 0)
    monkeypatch.setattr(cli, "_piped_stdin", lambda: "")
    seen.clear()
    assert cli.main(["-p", "Goal", "--home", str(home), "--skill", "pdf"]) == 0
    assert seen["overrides"]["skills"] == ["pdf"]
    # not a TUI flag: without a terminal `dreamteam --skill x --goal g` serves
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    assert cli._default_command(["--skill", "pdf", "--goal", "g"], COMMANDS)[0] == "serve"

    calls: dict[str, Any] = {}

    async def serve_home(home_dir, host, port, **kw):
        calls.update(kw)
        return 0

    monkeypatch.setattr(cli, "_serve_home", serve_home)
    monkeypatch.setattr(cli, "setup_logging", lambda *a, **kw: None)
    capsys.readouterr()
    assert cli.main(["serve", "--home", str(home), "--skill", "pdf", "--no-browser"]) == 2
    assert "--skill needs --goal" in capsys.readouterr().err and not calls
    assert cli.main(["serve", "--home", str(home), "--goal", "G", "--skill", "./tidy", "--no-browser",
                     "--no-console"]) == 0
    assert calls["skills"] == [tidy]
    assert cli.main(["serve", "--home", str(home), "--goal", "G", "--skill", "nope", "--no-browser"]) == 2


async def test_print_mode_passes_the_skills_to_the_hub():
    """`-p --skill`: the new session's body `skills`, or the start call of the resumed one (also when it runs)."""
    from dreamteam.tui import printmode

    calls: list[tuple] = []

    class Client:
        mode = "launcher"

        async def create_session(self, title, goal, **kw):
            calls.append(("create", kw.get("skills")))
            return {"id": "001-a", "title": "A", "language": "en"}

        async def sessions(self):
            return [{"id": "001-a", "title": "A", "status": "running"}]

        async def start_session(self, sid, skills=None):
            calls.append(("start", sid, skills))
            return {"id": sid, "title": "A", "status": "running"}

    class Conn:
        client = Client()

    run = printmode._PrintRun(Conn(), "Goal", session=None, continue_last=False, title=None, overrides=None,
                              output_format="text", verbose=False, timeout=None, auto_reply=True, lang="en",
                              out=io.StringIO(), err=io.StringIO(), skills=["pdf"])
    await run.prepare()
    run = printmode._PrintRun(Conn(), "Next", session="001-a", continue_last=False, title=None, overrides=None,
                              output_format="text", verbose=False, timeout=None, auto_reply=True, lang="en",
                              out=io.StringIO(), err=io.StringIO(), skills=["csv"])
    run.attach = lambda sid: asyncio.sleep(0)   # (the prompt's delivery is not this test's)
    await run.prepare()
    assert calls == [("create", ["pdf"]), ("start", "001-a", ["csv"])]

    seen: dict[str, Any] = {}

    async def fake_run_print(conn, prompt, **kw):
        seen.update(kw)
        return 0

    original = printmode.run_print
    printmode.run_print = fake_run_print
    try:
        await tui.run_print_mode(Conn(), "Goal", overrides={"model": "fast", "skills": ["pdf"]})
    finally:
        printmode.run_print = original
    assert seen["skills"] == ["pdf"] and seen["overrides"] == {"agents": {"default_model": "fast"}}


def test_sessions_rm_says_how_to_settle_waiting_proposals(monkeypatch, tmp_path, capsys):
    from dreamteam.sessions import SessionError

    home = tmp_path / "home"
    home.mkdir()
    st.write_raw(home, {"swarm": {"name": "Home"}})
    (home / "sessions").mkdir()
    deleted: list[tuple] = []

    class Manager:
        def __init__(self, home):
            pass

        def list(self):
            return [{"id": "001-a", "title": "A", "status": "stopped"}]

        async def delete(self, sid, *, skills=None, via="home"):
            deleted.append((sid, skills, via))
            if skills is None:
                raise SessionError("Session 001-a has skills waiting for your library decision: tidy.", 409)
            return None

    monkeypatch.setattr("dreamteam.sessions.SessionManager", Manager)
    monkeypatch.setattr(cli, "running_hub", lambda home: None)
    monkeypatch.setattr(cli, "_session_holders", lambda home: [])
    assert cli.main(["sessions", "rm", "001-a", "--yes", "--home", str(home)]) == 1
    assert "Pass --skills save (to the skills library first) or --skills discard." in capsys.readouterr().err
    assert cli.main(["sessions", "rm", "001-a", "--yes", "--skills", "save", "--home", str(home)]) == 0
    assert deleted == [("001-a", None, "cli"), ("001-a", "save", "cli")]


def test_print_mode_with_a_workspace_folder(monkeypatch, tmp_path, capsys):
    """`dreamteam -p GOAL --workspace DIR`: the new session works in DIR (created when missing)."""
    home = tmp_path / "home"
    home.mkdir()
    st.write_raw(home, {
        "swarm": {"name": "Home", "language": "en", "start_agents": 1, "max_agents": 2, "max_cost_usd": 1.0},
        "agents": {"stagger_seconds": 0},
        "server": {"port": free_port()},
        "workspace": {"git": False},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
    })

    def policy(system, messages, tools):
        seen = [m.get("content") or "" for m in messages if m["role"] in ("user", "tool")]
        if any("marked as achieved" in t or "already marked" in t for t in seen[-1:]):
            return {"tool_calls": [{"name": "wait", "arguments": {}}]}
        if any("snake" in t for t in seen):
            return {"tool_calls": [{"name": "goal_complete", "arguments": {"summary": "Done."}}]}
        return {"tool_calls": [{"name": "wait", "arguments": {}}]}

    real = tui.main_print
    monkeypatch.setattr(tui, "main_print", lambda home, prompt, **kw: real(
        home, prompt, llm_factory=lambda profile, agent: ScriptedLLM(policy, profile), **kw))
    monkeypatch.setattr(cli, "_piped_stdin", lambda: "")
    project = tmp_path / "projects" / "snake"
    code = cli.main(["-p", "Write a snake game", "--home", str(home), "--workspace", str(project), "--verbose",
                     "--timeout", "60"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert f"Session folder: {project}" in captured.err
    meta = json.loads(next((home / "sessions").glob("*/session.json")).read_text(encoding="utf-8"))
    assert meta["workspace"] == {"path": str(project), "adopted": False} and project.is_dir()
