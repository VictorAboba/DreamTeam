"""CLI wiring of the terminal UI: which command `dreamteam ...` runs, and `dreamteam -p` end to end."""

from __future__ import annotations

import io
import json
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
    assert seen["initial_prompt"] == "/sessions" and seen["session"] is None
    seen.clear()
    assert cli.main(["chat", "-r", "004-kalk", "--home", str(tmp_path), "--verbose"]) == 0
    assert seen["session"] == "004-kalk" and seen["initial_prompt"] is None and seen["verbosity"] == "verbose"
    seen.clear()
    assert cli.main(["-c", "--home", str(tmp_path)]) == 0
    assert seen["continue_last"] is True


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
