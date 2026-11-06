"""CLI of moving a home to another computer: `dreamteam export FOLDER` and `dreamteam import FOLDER` (issue #38).

The engines are tested in test_transfer.py; this file tests the commands: their parsing and exit codes, that an export
only reads the home (no launcher marker removed, no home created), and that an import shows its plan, asks or needs
--yes, is refused while another DreamTeam process runs the home, re-checks that before each session swap, and stops
cleanly on Ctrl+C."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest
from proc_guard import kill_tree, spawn

from dreamteam import cli, transfer
from dreamteam import settings as st
from dreamteam.db import Database
from dreamteam.sessions import SessionManager
from dreamteam.skills import Library
from dreamteam.treecopy import scan_tree


@pytest.fixture(autouse=True)
def _restore_logging():
    """cmd_import logs into the home it imports into (setup_logging): close that file again after each test."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    for handler in handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(level)


def _raw(name: str) -> dict[str, Any]:
    return {"swarm": {"name": name, "language": "en", "start_agents": 1, "max_agents": 2, "max_cost_usd": 1.0},
            "agents": {"stagger_seconds": 0},
            "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}}}


def _computer(monkeypatch, tmp_path: Path, name: str) -> Path:
    """A home of its own computer: its own skills and MCP libraries."""
    monkeypatch.setenv("DREAMTEAM_SKILLS_DIR", str(tmp_path / f"libs-{name}" / "skills"))
    monkeypatch.setenv("DREAMTEAM_MCP_LIBRARY_DIR", str(tmp_path / f"libs-{name}" / "mcp"))
    home = tmp_path / name
    (home / "sessions").mkdir(parents=True)
    st.write_raw(home, _raw(name))
    return home


def _create(home: Path, title: str) -> str:
    async def make() -> str:
        manager = SessionManager(home)
        try:
            return (await manager.create(title, "", start=False))["id"]
        finally:
            await manager.shutdown()

    return asyncio.run(make())


def _skill(tmp_path: Path, name: str) -> None:
    folder = tmp_path / "skill-src" / name
    (folder / "scripts").mkdir(parents=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Does {name} things\n---\nBody\n",
                                     encoding="utf-8")
    (folder / "scripts" / "run.py").write_text("print('run')\n", encoding="utf-8")
    Library(Path(os.environ["DREAMTEAM_SKILLS_DIR"])).add(folder, "folder", {})


def _state(home: Path) -> tuple[str | None, list[str]]:
    """Every file of the home byte for byte, and every path (folders and -wal/-shm files included)."""
    paths = sorted(p.relative_to(home).as_posix() for p in home.rglob("*"))
    return scan_tree(home, exclude_names=(), exclude_paths=()).digest, paths


def _markers(home: Path) -> list[str]:
    return sorted(p.name for p in (home / ".dreamteam").glob("launcher-*.pid"))


def _exported(monkeypatch, tmp_path: Path) -> tuple[Path, list[str]]:
    """Computer A with two sessions and a skill, exported to tmp_path/usb; then computer B's libraries are set."""
    home_a = _computer(monkeypatch, tmp_path, "a")
    ids = [_create(home_a, "Alpha"), _create(home_a, "Beta")]
    for sid in ids:   # (their history, as if they had run)
        db = Database(home_a / "sessions" / sid / ".dreamteam" / "swarm.db")
        try:
            db.execute("INSERT INTO events(ts, type, actor, data) VALUES (1.0, 'session.started', 'user', '{}')")
            db.execute("INSERT INTO history(agent, seq, msg) VALUES ('alice', 1, ?)", (json.dumps({"text": sid}),))
        finally:
            db.close()
    _skill(tmp_path, "csv-tool")
    assert cli.main(["export", str(tmp_path / "usb"), "--home", str(home_a)]) == 0
    return home_a, ids


def _commands() -> list[str]:
    parser = cli.build_parser()
    return [name for action in parser._subparsers._group_actions for name in action.choices]


# ---- parsing ----------------------------------------------------------------------------------------------------
def test_export_and_import_are_commands_and_their_folder_is_not_the_home(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    commands = _commands()
    assert {"export", "import"} <= set(commands)
    # `dreamteam export E:/dt` is the command, not the first message of a TUI session
    assert cli._default_command(["export", "E:/dt"], commands) == ["export", "E:/dt"]
    assert cli._default_command(["import", "E:/dt", "--yes"], commands) == ["import", "E:/dt", "--yes"]
    parser = cli.build_parser()
    args = parser.parse_args(["export", "E:/dt", "--include", "settings,skills", "--include", "mcp",
                              "--session", "001-a", "--session", "002-b", "--dry-run"])
    assert args.func is cli.cmd_export and args.folder == "E:/dt" and args.dir is None
    assert args.include == ["settings,skills", "mcp"] and args.session == ["001-a", "002-b"] and args.dry_run
    args = parser.parse_args(["import", "E:/dt", "-y", "--session", "0123abcd" * 4])
    assert args.func is cli.cmd_import and args.yes and not args.dry_run and args.include is None
    monkeypatch.chdir(tmp_path)   # (no dreamteam.toml here)
    monkeypatch.setenv(st.HOME_ENV, str(tmp_path / "the-home"))
    assert cli._home_arg(args) == (tmp_path / "the-home").resolve()   # the FOLDER is never taken for the home
    with pytest.raises(SystemExit):
        cli.main(["export", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "dreamteam export FOLDER [--include CATEGORIES]... [--session ID]... [--dry-run] [--home HOME]" in text
    assert "settings (with the model profiles and roles), skills, mcp" in text
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "export write the settings, skills, MCP servers and sessions" in text
    assert "import bring a folder written by `dreamteam export` into a home" in text
    with pytest.raises(SystemExit):   # no --force in stage 1
        cli.main(["import", "E:/dt", "--force"])


def test_an_unknown_include_or_session_is_a_usage_error(monkeypatch, tmp_path, capsys):
    home = _computer(monkeypatch, tmp_path, "a")
    _create(home, "Alpha")
    before = _state(home)
    dest = tmp_path / "usb"
    assert cli.main(["export", str(dest), "--include", "settings,traces,models", "--home", str(home)]) == 2
    err = capsys.readouterr().err
    assert "Unknown --include categories: models, traces (choose from settings, skills, mcp, sessions)." in err
    assert cli.main(["export", str(dest), "--session", "007-nope", "--home", str(home)]) == 2
    assert "Unknown session '007-nope'. Sessions: 001-alpha." in capsys.readouterr().err
    assert not dest.exists()
    assert cli.main(["import", str(dest), "--include", "secrets", "--home", str(home)]) == 2
    assert "Unknown --include category: secrets" in capsys.readouterr().err
    assert _state(home) == before


def test_a_folder_that_is_no_home_is_refused_and_not_made_one(monkeypatch, tmp_path, capsys):
    nothing = tmp_path / "nothing"
    assert cli.main(["export", str(tmp_path / "usb"), "--home", str(nothing)]) == 1
    assert "Nothing exported: " in capsys.readouterr().err and not nothing.exists()
    project = tmp_path / "project"   # a project of `dreamteam run`: dreamteam.toml without sessions/
    project.mkdir()
    st.write_raw(project, _raw("project"))
    assert cli.main(["export", str(tmp_path / "usb"), "--home", str(project)]) == 1
    assert "is not a DreamTeam home" in capsys.readouterr().err
    _exported(monkeypatch, tmp_path)
    assert cli.main(["import", str(tmp_path / "usb"), "--yes", "--home", str(project)]) == 1
    assert "Nothing imported: " in capsys.readouterr().err
    assert sorted(p.name for p in project.iterdir()) == ["dreamteam.toml"]
    stuff = tmp_path / "stuff"
    stuff.mkdir()
    (stuff / "notes.txt").write_text("mine\n", encoding="utf-8")
    assert cli.main(["import", str(tmp_path / "usb"), "--dry-run", "--home", str(stuff)]) == 1
    assert "not an empty folder" in capsys.readouterr().err and sorted(os.listdir(stuff)) == ["notes.txt"]


# ---- export -----------------------------------------------------------------------------------------------------
def test_export_of_settings_and_skills(monkeypatch, tmp_path, capsys):
    home = _computer(monkeypatch, tmp_path, "a")
    _create(home, "Alpha")
    _skill(tmp_path, "csv-tool")
    capsys.readouterr()
    before = _state(home)
    dest = tmp_path / "usb"
    assert cli.main(["export", str(dest), "--include", "settings,skills", "--home", str(home)]) == 0
    out = capsys.readouterr().out
    assert f"Exported {home} to {dest}:" in out
    assert "settings  dreamteam.toml: " in out and "skills    1 skill, " in out
    assert "sessions" not in out.split("Total")[0] and f"Import it there with: dreamteam import {dest}" in out
    assert sorted(os.listdir(dest)) == ["dreamteam-export.json", "settings", "skills"]
    assert json.loads((dest / "dreamteam-export.json").read_text(encoding="utf-8"))["include"] == ["settings",
                                                                                                    "skills"]
    assert _state(home) == before
    # a dry run writes nothing; a second export replaces the first one
    assert cli.main(["export", str(tmp_path / "dry"), "--dry-run", "--home", str(home)]) == 0
    assert "dry run: nothing is written" in capsys.readouterr().out and not (tmp_path / "dry").exists()
    assert cli.main(["export", str(dest), "--home", str(home)]) == 0
    assert "(the previous export there was replaced)" in capsys.readouterr().out
    assert (dest / "sessions" / "001-alpha" / "session.json").is_file()
    # a folder with other files is refused (exit 1), and stays as it was
    other = tmp_path / "other"
    other.mkdir()
    (other / "keep.txt").write_text("x", encoding="utf-8")
    assert cli.main(["export", str(other), "--home", str(home)]) == 1
    assert "is not empty and holds no DreamTeam export" in capsys.readouterr().err
    assert os.listdir(other) == ["keep.txt"] and _state(home) == before


def test_export_names_the_sessions_a_hub_runs_and_never_removes_launcher_markers(monkeypatch, tmp_path, capsys):
    home = _computer(monkeypatch, tmp_path, "a")
    one, two = _create(home, "Alpha"), _create(home, "Beta")
    running_hub, hub_json = cli.running_hub, cli._hub_json
    # a hub serves the home and runs `one`
    asked = []
    monkeypatch.setattr(cli, "running_hub", lambda path: "http://127.0.0.1:1")
    monkeypatch.setattr(cli, "_hub_json", lambda base, path, *a, **kw: asked.append((base, path)) or {
        "sessions": [{"id": one, "status": "running"}, {"id": two, "status": "stopped"}]})
    dest = tmp_path / "usb"
    assert cli.main(["export", str(dest), "--include", "sessions", "--home", str(home)]) == 0
    out = capsys.readouterr().out
    assert asked == [("http://127.0.0.1:1", "/api/sessions")]
    assert f"session {one} is running: it is exported as it was at this moment" in out and two not in out
    manifest = json.loads((dest / "dreamteam-export.json").read_text(encoding="utf-8"))
    assert {s["id"]: s["running_at_export"] for s in manifest["sessions"]} == {one: True, two: False}
    # a hub that does not list its sessions: a warning, the export goes on
    monkeypatch.setattr(cli, "_hub_json", lambda *a, **kw: (_ for _ in ()).throw(OSError("connection reset")))
    assert cli.main(["export", str(dest), "--include", "sessions", "--home", str(home)]) == 0
    assert "the hub http://127.0.0.1:1/ did not list its sessions (connection reset)" in capsys.readouterr().err
    monkeypatch.setattr(cli, "running_hub", running_hub)
    monkeypatch.setattr(cli, "_hub_json", hub_json)
    # another DreamTeam process without a hub: a warning; the marker of a process that is gone stays
    holder = spawn([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        marker = home / ".dreamteam" / f"launcher-{holder.pid}.pid"
        marker.parent.mkdir(exist_ok=True)
        marker.write_text(str(holder.pid), encoding="utf-8")
        assert cli.main(["export", str(dest), "--include", "settings", "--home", str(home)]) == 0
        err = capsys.readouterr().err
        assert f"another DreamTeam process may be running sessions of {home} (process {holder.pid}" in err
    finally:
        kill_tree(holder)
    assert cli.main(["export", str(dest), "--include", "settings", "--home", str(home)]) == 0
    assert capsys.readouterr().err == "" and marker.exists()


def test_export_failures_exit_1_and_ctrl_c_exits_130(monkeypatch, tmp_path, capsys):
    home = _computer(monkeypatch, tmp_path, "a")
    sid = _create(home, "Alpha")
    _create(home, "Beta")
    (home / "sessions" / sid / "session.json").write_text("{broken", encoding="utf-8")
    dest = tmp_path / "usb"
    assert cli.main(["export", str(dest), "--home", str(home)]) == 1   # a refused session: the rest is exported
    out = capsys.readouterr().out
    assert "Left out:" in out and f"session {sid}: " in out and (dest / "sessions" / "002-beta").is_dir()

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(transfer, "export_home", interrupted)
    assert cli.main(["export", str(tmp_path / "other"), "--home", str(home)]) == 130
    assert "Nothing exported." in capsys.readouterr().err

    def secret(*args, **kwargs):
        raise transfer.SecretFound(["sessions/002-beta/workspace/a.txt: the API key of model profile 'default'"])

    monkeypatch.setattr(transfer, "export_home", secret)
    assert cli.main(["export", str(tmp_path / "other"), "--home", str(home)]) == 1
    err = capsys.readouterr().err
    assert "Nothing exported: the export would contain a secret" in err and "workspace/a.txt" in err


def test_an_export_to_a_path_that_cannot_be_made_is_a_message(monkeypatch, tmp_path, capsys):
    home = _computer(monkeypatch, tmp_path, "a")
    (tmp_path / "afile").write_text("x", encoding="utf-8")
    assert cli.main(["export", str(tmp_path / "afile" / "sub" / "export"), "--home", str(home)]) == 1
    err = capsys.readouterr().err
    assert err.startswith("Nothing exported:") and "Traceback" not in err and "afile" in err


# ---- import -----------------------------------------------------------------------------------------------------
def test_import_dry_run_changes_nothing(monkeypatch, tmp_path, capsys):
    _home_a, (one, two) = _exported(monkeypatch, tmp_path)
    home_b = _computer(monkeypatch, tmp_path, "b")
    own = _create(home_b, "Only here")
    capsys.readouterr()
    before = _state(home_b)
    libraries = _state(tmp_path / "libs-b") if (tmp_path / "libs-b").exists() else None
    assert cli.main(["import", str(tmp_path / "usb"), "--dry-run", "--home", str(home_b)]) == 0
    out = capsys.readouterr().out
    assert f"Import {tmp_path / 'usb'} into {home_b}" in out
    assert f"sessions  {one}: add" in out and f"sessions  {two}: add" in out and "skills    csv-tool: add" in out
    assert own not in out and "Import?" not in out
    assert _state(home_b) == before
    assert (_state(tmp_path / "libs-b") if (tmp_path / "libs-b").exists() else None) == libraries
    # into a home that does not exist yet: the plan, and nothing is created
    new = tmp_path / "new-home"
    assert cli.main(["import", str(tmp_path / "usb"), "--dry-run", "--home", str(new)]) == 0
    out = capsys.readouterr().out
    assert f"into {new.resolve()}" in out and f"The import creates the DreamTeam home {new.resolve()}." in out
    assert f"sessions  {one}: add" in out and "dreamteam-import-" not in out and not new.exists()
    # an export folder that is incomplete: exit 1
    assert cli.main(["import", str(tmp_path), "--dry-run", "--home", str(home_b)]) == 1
    assert "holds no complete DreamTeam export" in capsys.readouterr().err


def test_import_without_a_terminal_needs_yes(monkeypatch, tmp_path, capsys):
    _home_a, (one, two) = _exported(monkeypatch, tmp_path)
    home_b = _computer(monkeypatch, tmp_path, "b")
    capsys.readouterr()
    before = _state(home_b)
    monkeypatch.setattr(cli, "_stdin_is_terminal", lambda: False)
    assert cli.main(["import", str(tmp_path / "usb"), "--home", str(home_b)]) == 1
    captured = capsys.readouterr()
    assert f"sessions  {one}: add" in captured.out
    assert "Nothing imported: pass --yes to import without a confirmation." in captured.err
    assert _state(home_b) == before
    assert cli.main(["import", str(tmp_path / "usb"), "--yes", "--home", str(home_b)]) == 0
    out = capsys.readouterr().out
    assert f"Imported {tmp_path / 'usb'} into {home_b}:" in out
    assert f"sessions  {one}: added" in out and f"sessions  {two}: added" in out and "csv-tool: added" in out
    assert "Imported sessions do not start by themselves" in out
    manager = SessionManager(home_b)
    assert manager.meta(one)["title"] == "Alpha" and manager.meta(one)["autostart"] is False
    assert Library(Path(os.environ["DREAMTEAM_SKILLS_DIR"])).get("csv-tool") is not None
    assert _markers(home_b) == []   # the import's own launcher marker is gone again
    assert not [p for p in (home_b / "sessions").iterdir() if p.name.startswith(".importing-")]
    # the same export again: nothing to change, no question (also without --yes)
    assert cli.main(["import", str(tmp_path / "usb"), "--home", str(home_b)]) == 0
    captured = capsys.readouterr()
    assert "Nothing to change here." in captured.out and captured.err == ""


def test_import_asks_in_a_terminal(monkeypatch, tmp_path, capsys):
    _home_a, (one, _two) = _exported(monkeypatch, tmp_path)
    home_b = _computer(monkeypatch, tmp_path, "b")
    capsys.readouterr()
    before = _state(home_b)
    monkeypatch.setattr(cli, "_stdin_is_terminal", lambda: True)
    prompts: list[str] = []

    def answer(*replies):
        it = iter(replies)

        def ask(prompt=""):
            prompts.append(prompt)
            reply = next(it)
            if isinstance(reply, BaseException):
                raise reply
            return reply

        monkeypatch.setattr("builtins.input", ask)

    answer(KeyboardInterrupt())   # Ctrl+C at the question
    assert cli.main(["import", str(tmp_path / "usb"), "--home", str(home_b)]) == 130
    assert "Nothing imported." in capsys.readouterr().out and _state(home_b) == before
    assert prompts == ["Import? [y/N] "]
    answer("n")
    assert cli.main(["import", str(tmp_path / "usb"), "--home", str(home_b)]) == 0
    assert "Nothing imported." in capsys.readouterr().out and _state(home_b) == before
    answer(EOFError())
    assert cli.main(["import", str(tmp_path / "usb"), "--home", str(home_b)]) == 0
    assert "Nothing imported." in capsys.readouterr().out and _state(home_b) == before
    answer("да")
    assert cli.main(["import", str(tmp_path / "usb"), "--home", str(home_b)]) == 0
    assert f"sessions  {one}: added" in capsys.readouterr().out
    assert (home_b / "sessions" / one / "session.json").is_file()


def test_import_creates_a_new_home_only_when_it_goes_ahead(monkeypatch, tmp_path, capsys):
    _home_a, (one, two) = _exported(monkeypatch, tmp_path)
    monkeypatch.setenv("DREAMTEAM_SKILLS_DIR", str(tmp_path / "libs-b" / "skills"))
    monkeypatch.setenv("DREAMTEAM_MCP_LIBRARY_DIR", str(tmp_path / "libs-b" / "mcp"))
    new = tmp_path / "fresh"
    monkeypatch.setattr(cli, "_stdin_is_terminal", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert cli.main(["import", str(tmp_path / "usb"), "--home", str(new)]) == 0
    assert not new.exists()
    empty = tmp_path / "empty"
    empty.mkdir()
    assert cli.main(["import", str(tmp_path / "usb"), "--yes", "--home", str(empty)]) == 0
    out = capsys.readouterr().out
    assert f"Imported {tmp_path / 'usb'} into {empty.resolve()}:" in out and f"sessions  {two}: added" in out
    assert cli.is_home(empty) and sorted(SessionManager(empty).ids()) == [one, two]
    assert "dreamteam-import-" not in out


def test_import_is_refused_while_another_process_runs_the_home(monkeypatch, tmp_path, capsys):
    _home_a, (one, _two) = _exported(monkeypatch, tmp_path)
    home_b = _computer(monkeypatch, tmp_path, "b")
    _create(home_b, "Only here")
    capsys.readouterr()
    holder = spawn([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        marker = home_b / ".dreamteam" / f"launcher-{holder.pid}.pid"
        marker.parent.mkdir(exist_ok=True)
        marker.write_text(str(holder.pid), encoding="utf-8")
        before = _state(home_b)
        assert cli.main(["import", str(tmp_path / "usb"), "--yes", "--home", str(home_b)]) == 1
        captured = capsys.readouterr()
        assert "Nothing imported: another DreamTeam process may be running the sessions of" in captured.err
        assert f"process {holder.pid}" in captured.err and captured.out == ""
        assert _state(home_b) == before
        # a dry run still shows the plan, says the import is refused, and changes nothing
        assert cli.main(["import", str(tmp_path / "usb"), "--dry-run", "--home", str(home_b)]) == 0
        captured = capsys.readouterr()
        assert f"sessions  {one}: add" in captured.out and "the import itself is refused" in captured.err
        assert _state(home_b) == before
    finally:
        kill_tree(holder)
    # a hub that answers
    monkeypatch.setattr(cli, "running_hub", lambda path: "http://127.0.0.1:1")
    assert cli.main(["import", str(tmp_path / "usb"), "--yes", "--home", str(home_b)]) == 1
    assert "the DreamTeam hub http://127.0.0.1:1/" in capsys.readouterr().err
    monkeypatch.setattr(cli, "running_hub", lambda path: None)
    # the process is gone: its marker is removed, and the import goes ahead
    assert cli.main(["import", str(tmp_path / "usb"), "--yes", "--home", str(home_b)]) == 0
    assert f"sessions  {one}: added" in capsys.readouterr().out and _markers(home_b) == []


def test_a_process_that_starts_during_the_import_stops_it_before_the_next_write(monkeypatch, tmp_path, capsys):
    _home_a, (one, two) = _exported(monkeypatch, tmp_path)
    home_b = _computer(monkeypatch, tmp_path, "b")
    capsys.readouterr()
    holders = cli._session_holders
    calls: list[list[str]] = []

    def appears(home: Path, *, clean: bool = True) -> list[str]:
        calls.append(_markers(home_b))
        # before the plan and after the question nothing runs; then a launcher starts before the first swap
        return holders(home, clean=clean) or (["process 4242 (a launcher)"] if len(calls) > 2 else [])

    monkeypatch.setattr(cli, "_session_holders", appears)
    assert cli.main(["import", str(tmp_path / "usb"), "--yes", "--home", str(home_b)]) == 1
    out = capsys.readouterr().out
    assert f"launcher-{os.getpid()}.pid" in calls[2]   # the import's own marker exists while it applies
    assert len(calls) == 3   # before the first session swap; nothing is written into the home after it
    why = "another DreamTeam process started meanwhile (process 4242 (a launcher)): stop it and import again"
    assert f"sessions  {one}: failed ({why})" in out and f"sessions  {two}: not done ({why})" in out
    assert "csv-tool: added" in out and "What is marked 'not done' was not imported" in out   # (skills come first)
    for sid in (one, two):
        assert not (home_b / "sessions" / sid).exists()
    assert _markers(home_b) == []


def test_ctrl_c_during_the_import_stops_after_the_current_item(monkeypatch, tmp_path, capsys):
    _home_a, (one, two) = _exported(monkeypatch, tmp_path)
    home_b = _computer(monkeypatch, tmp_path, "b")
    capsys.readouterr()
    blockers = cli._import_blockers
    calls = []

    def ctrl_c(home: Path, *, clean: bool = True) -> list[str]:
        calls.append(home)
        if len(calls) == 3:   # right before the first session swap
            raise KeyboardInterrupt
        return blockers(home, clean=clean)

    monkeypatch.setattr(cli, "_import_blockers", ctrl_c)
    assert cli.main(["import", str(tmp_path / "usb"), "--yes", "--home", str(home_b)]) == 130
    out = capsys.readouterr().out
    assert "stopped (Ctrl+C)" in out and "csv-tool: added" in out
    assert f"sessions  {one}: not done" in out and f"sessions  {two}: not done" in out
    assert "run the import again to bring it in" in out
    assert not (home_b / "sessions" / one).exists() and _markers(home_b) == []
    # the rest comes in with the next run
    monkeypatch.setattr(cli, "_import_blockers", blockers)
    assert cli.main(["import", str(tmp_path / "usb"), "--yes", "--home", str(home_b)]) == 0
    out = capsys.readouterr().out
    assert f"sessions  {one}: added" in out and "csv-tool: unchanged" in out

    async def broken_off(*args, **kwargs):   # a second Ctrl+C: the import itself is broken off
        raise KeyboardInterrupt

    monkeypatch.setattr(transfer, "apply_import", broken_off)
    home_c = _computer(monkeypatch, tmp_path, "c")
    assert cli.main(["import", str(tmp_path / "usb"), "--yes", "--home", str(home_c)]) == 130
    assert "Import interrupted (Ctrl+C)" in capsys.readouterr().err and _markers(home_c) == []


def test_import_exit_codes_for_refused_sessions_and_session_choice(monkeypatch, tmp_path, capsys):
    _home_a, (one, two) = _exported(monkeypatch, tmp_path)
    home_b = _computer(monkeypatch, tmp_path, "b")
    capsys.readouterr()
    # one session of a newer DreamTeam: refused, the others are imported, exit 1
    db = tmp_path / "usb" / "sessions" / two / ".dreamteam" / "swarm.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("ALTER TABLE events ADD COLUMN future TEXT")
        conn.commit()
    finally:
        conn.close()
    manifest_path = tmp_path / "usb" / "dreamteam-export.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in manifest["sessions"]:
        if entry["id"] == two:
            entry["db_sha256"] = transfer._sha256(db)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert cli.main(["import", str(tmp_path / "usb"), "--dry-run", "--home", str(home_b)]) == 1
    assert f"sessions  {two}: refused (" in capsys.readouterr().out
    assert cli.main(["import", str(tmp_path / "usb"), "--yes", "--home", str(home_b)]) == 1
    out = capsys.readouterr().out
    assert f"sessions  {one}: added" in out and f"sessions  {two}: refused (" in out
    # --session picks one session (and implies the category); an unknown one is a usage error
    home_c = _computer(monkeypatch, tmp_path, "c")
    assert cli.main(["import", str(tmp_path / "usb"), "--include", "settings", "--session", one, "--yes",
                     "--home", str(home_c)]) == 0
    out = capsys.readouterr().out
    assert f"sessions  {one}: added" in out and two not in out and "csv-tool" not in out
    assert cli.main(["import", str(tmp_path / "usb"), "--session", "009-nope", "--home", str(home_c)]) == 2
    assert "the export holds no session '009-nope'" in capsys.readouterr().err


def test_a_mistyped_session_is_a_usage_error_also_while_the_home_runs(monkeypatch, tmp_path, capsys):
    _home_a, ids = _exported(monkeypatch, tmp_path)
    home_b = _computer(monkeypatch, tmp_path, "b")
    monkeypatch.setattr(cli, "_import_blockers", lambda home, clean=True: ["process 1 (a marker)"])
    assert cli.main(["import", str(tmp_path / "usb"), "--session", "009-nope", "--yes", "--home", str(home_b)]) == 2
    assert "the export holds no session '009-nope'" in capsys.readouterr().err
    assert cli.main(["import", str(tmp_path / "usb"), "--session", ids[0], "--yes", "--home", str(home_b)]) == 1
    assert "another DreamTeam process may be running" in capsys.readouterr().err


def test_ctrl_c_while_the_hub_is_asked_exits_130(monkeypatch, tmp_path, capsys):
    home_a, _ids = _exported(monkeypatch, tmp_path)

    def interrupted(*args, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_running_for_export", interrupted)
    assert cli.main(["export", str(tmp_path / "usb2"), "--home", str(home_a)]) == 130
    assert "Nothing exported." in capsys.readouterr().err
    monkeypatch.setattr(cli, "_import_blockers", interrupted)
    assert cli.main(["import", str(tmp_path / "usb"), "--yes", "--home", str(home_a)]) == 130
    assert "Nothing imported." in capsys.readouterr().err


@pytest.mark.parametrize("word, command", [
    ("report", False), ("explore", False), ("important", False), ("airport", False), ("deport", False),
    ("expo", False), ("exprot", True), ("improt", True), ("sesions", True), ("Export", True), ("Status", True),
    ("map", False), ("fun", False), ("mvp", False), ("rum", False), ("rnu", True), ("mpc", True),
])
def test_one_word_goals_close_to_a_command_open_the_tui(monkeypatch, word, command):
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    assert cli._default_command([word], _commands()) == ([word] if command else ["tui", word])


# ---- review fixes, third round -----------------------------------------------------------------------------------
def test_an_import_into_a_new_home_keeps_no_backup_of_settings_it_never_had(monkeypatch, tmp_path, capsys):
    _exported(monkeypatch, tmp_path)
    _computer(monkeypatch, tmp_path, "b")   # (computer B's libraries)
    capsys.readouterr()
    new = tmp_path / "new-home"
    assert cli.main(["import", str(tmp_path / "usb"), "--yes", "--home", str(new)]) == 0
    out = capsys.readouterr().out
    assert "from before the import" not in out and not (new / "dreamteam.toml.bak").exists()
    # a plan that imports nothing creates no home, and does not say it would
    assert cli.main(["export", str(tmp_path / "xs"), "--include", "settings", "--home", str(tmp_path / "a")]) == 0
    out = capsys.readouterr().out
    assert "histories of the sessions" not in out and "env values of the MCP servers" not in out
    other = tmp_path / "n4"
    assert cli.main(["import", str(tmp_path / "xs"), "--yes", "--include", "skills", "--home", str(other)]) == 1
    assert "The import creates the DreamTeam home" not in capsys.readouterr().out and not other.exists()


@pytest.mark.skipif(os.name == "nt", reason="EPERM of signal 0 is POSIX")
def test_a_marker_whose_pid_is_another_users_process_now_is_stale(monkeypatch, tmp_path):
    home = _computer(monkeypatch, tmp_path, "a")
    marker = home / ".dreamteam" / "launcher-424242.pid"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("", encoding="utf-8")

    def denied(pid, sig):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "kill", denied)
    assert cli._session_holders(home, clean=False) == [] and marker.exists()
    assert cli._session_holders(home) == [] and not marker.exists()
