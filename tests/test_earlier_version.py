"""Sessions made by an earlier version of DreamTeam (docs/SPEC.md §17.2 "Sessions of an earlier version"): they are
listed and marked, deleted and exported, and every other surface refuses them with their message and writes nothing
(the session manager, the hub's REST, board page, WebSocket and `/mcp`, the launcher MCP, the CLI, `-p`, import).

The three old kinds: a revision-4 tool-set record, a history of an agent without a record, and a history with a
record that is not `frozen.valid`. Every refusal leaves the names, sizes and modification times of the session's
folder as they were (no -wal or -shm next to its database, no logs folder)."""

from __future__ import annotations

import asyncio
import contextlib
import html
import io
import json
import logging
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from dreamteam import __version__, cli
from dreamteam import console as console_module
from dreamteam import settings as st
from dreamteam import swarm as swarm_module
from dreamteam.config import Config
from dreamteam.db import Database
from dreamteam.launcher import create_launcher_app
from dreamteam.launcher_mcp import call_launcher_tool, session_line
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.sessions import (EARLIER_VERSION_TEXT, LEGACY_ID, MAIN_EARLIER_VERSION_TEXT, SessionError,
                                SessionManager, earlier_version_text, read_stats)
from dreamteam.tools import frozen
from dreamteam.tui.client import connect
from dreamteam.tui.printmode import run_print

KINDS = ("record", "history", "invalid")
MARK = " | made by an earlier version: cannot be opened"
HUB = "http://127.0.0.1:9"   # never asked: the CLI tests answer for the hub


def call(name: str, **arguments: Any) -> dict:
    return {"tool_calls": [{"name": name, "arguments": arguments}]}


def raw(tmp_path: Path) -> dict[str, Any]:
    return {
        "swarm": {"name": "Home", "language": "en", "start_agents": 1, "max_agents": 3, "max_cost_usd": 2.0},
        "agents": {"stagger_seconds": 0, "idle_nudge_seconds": 30},
        "workspace": {"git": False},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {
            "default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0},
            "fast": {"provider": "openai", "model": "m", "base_url": "http://127.0.0.1:9/v1",
                     "api_key_env": "DT_TEST_KEY_ENV"},
        },
    }


def make_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / "sessions").mkdir(parents=True)
    st.write_raw(home, raw(tmp_path))
    return home


def manager_of(home: Path, policy=None) -> SessionManager:
    policy = policy or (lambda system, messages, tools: call("wait"))
    return SessionManager(home, llm_factory=lambda profile, agent: ScriptedLLM(policy, profile))


def write_earlier(path: Path, kind: str) -> None:
    """A database an earlier version left: a member Anna on the profile `fast`, and one of the three old kinds."""
    path.parent.mkdir(parents=True, exist_ok=True)
    db = Database(path)
    try:
        db.execute("INSERT INTO agents(name, kind, role, model, status) VALUES ('Anna', 'llm', 'Tester', 'fast', "
                   "'idle')")
        db.execute("INSERT INTO events(ts, type, actor, data) VALUES (1.0, 'session.started', 'user', '{}')")
        if kind == "record":   # a revision-4 record (its history does not matter)
            frozen.write(db, "Anna", frozen.record(4, "en", frozen.snapshot(4, "en")))
        else:
            db.execute("INSERT INTO history(agent, seq, msg) VALUES ('Anna', 1, ?)",
                       (json.dumps({"role": "user", "content": "hello"}),))
            if kind == "invalid":   # a record, but not a usable one
                db.set(frozen.kv_key("Anna"), {"rev": "5", "tools": []})
    finally:
        db.close()


def snapshot(folder: Path) -> dict[str, tuple[bool, int, int]]:
    """Every path under `folder` with (is a folder, size, mtime): a write, a new file or a removed one shows."""
    found = {}
    for path in sorted(folder.rglob("*")):
        info = path.stat()
        found[path.relative_to(folder).as_posix()] = (path.is_dir(), 0 if path.is_dir() else info.st_size,
                                                      info.st_mtime_ns)
    return found


def untouched(old: SimpleNamespace) -> bool:
    """The folder is as it was (so no logs folder appeared either), with no -wal or -shm next to the database."""
    data = snapshot(old.folder)
    return data == old.before and not any(name.endswith(("-wal", "-shm")) for name in data)


async def make_old(tmp_path: Path, kind: str, policy=None) -> SimpleNamespace:
    home = make_home(tmp_path)
    manager = manager_of(home, policy)
    sid = (await manager.create("Old", "Do it", start=False))["id"]
    write_earlier(manager.dir_of(sid) / ".dreamteam" / "swarm.db", kind)
    folder = manager.dir_of(sid)
    assert not (folder / ".dreamteam" / "logs").exists()   # (so `untouched` also means: no logs folder)
    return SimpleNamespace(home=home, manager=manager, sid=sid, folder=folder, kind=kind, before=snapshot(folder),
                           text=EARLIER_VERSION_TEXT.format(sid=sid, version=__version__))


@pytest.fixture(params=KINDS)
async def old(tmp_path, request):
    made = await make_old(tmp_path, request.param)
    yield made
    await made.manager.shutdown()


def refused(exc: SessionError, text: str) -> bool:
    return exc.status == 409 and str(exc) == text and exc.extra == {"earlier_version": True}


# ---- the session manager --------------------------------------------------------------------------------------
async def test_it_is_listed_and_marked(old):
    manager, sid = old.manager, old.sid
    summary = manager.summary(sid)
    assert summary["earlier_version"] is True and summary["status"] == "stopped" and not summary["loaded"]
    assert [s["earlier_version"] for s in manager.list()] == [True]
    assert session_line(summary).splitlines()[0].endswith(MARK)
    assert read_stats(old.folder / ".dreamteam" / "swarm.db")["earlier_version"] is True
    text, error = await call_launcher_tool(manager, "sessions_list", {})
    assert not error and text.splitlines()[1].endswith(MARK)
    assert manager.earlier_version(sid) and manager.earlier_version_text(sid) == old.text
    assert untouched(old) and sid not in manager.swarms


async def test_every_other_operation_is_refused_and_writes_nothing(old, tmp_path):
    manager, sid = old.manager, old.sid
    target = tmp_path / "elsewhere"
    operations = {
        "load": lambda: manager.load(sid),
        "start": lambda: manager.start(sid),
        "start with skills": lambda: manager.start(sid, skills=["csv-tool"]),
        "stop": lambda: manager.stop(sid),
        "set_workspace": lambda: manager.set_workspace(sid, str(target)),
        "set_workspace default": lambda: manager.set_workspace(sid, None),
        "session_skills": lambda: manager.session_skills(sid),
        "session_skills show": lambda: manager.session_skills(sid, show="csv-tool"),
        "decide_skill": lambda: manager.decide_skill(sid, "csv-tool", True),
    }
    for name, operation in operations.items():
        with pytest.raises(SessionError) as exc:
            await operation()
        assert refused(exc.value, old.text), name
    for kwargs in ({"title": "New title"}, {"settings": {"swarm": {"max_agents": 2}}}, {"overrides": {}}):
        with pytest.raises(SessionError) as exc:
            manager.update(sid, **kwargs)
        assert refused(exc.value, old.text), kwargs
    assert untouched(old) and sid not in manager.swarms and sid not in manager.running
    assert not target.exists() and manager.meta(sid)["title"] == "Old"


async def test_the_swarm_refuses_it_also_when_the_manager_check_is_passed(old, monkeypatch):
    """Swarm.__init__ makes the check itself: `_load_locked` turns its EarlierVersionError into the 409."""
    manager = old.manager
    monkeypatch.setattr(manager, "earlier_version", lambda sid: False)
    with pytest.raises(SessionError) as exc:
        await manager.load(old.sid)
    assert refused(exc.value, old.text)
    assert untouched(old) and old.sid not in manager.swarms


async def test_resume_skips_it_with_one_warning_and_keeps_its_mark(tmp_path, caplog):
    made = await make_old(tmp_path, "history")
    manager, sid = made.manager, made.sid
    path = made.folder / "session.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**meta, "autostart": True}), encoding="utf-8")
    made.before = snapshot(made.folder)
    with caplog.at_level(logging.INFO, logger="dreamteam.sessions"):
        assert await manager.resume() == []
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING and sid in r.getMessage()]
    assert [r.getMessage() for r in warnings] == [
        f"session {sid} was made by an earlier version of DreamTeam: not resumed"]
    assert manager.meta(sid)["autostart"] is True and untouched(made) and sid not in manager.running
    await manager.shutdown()


async def test_default_session_skips_it_and_a_profile_rename_leaves_its_database_alone(old):
    manager = old.manager
    assert manager.default_session() is None
    text, error = await call_launcher_tool(manager, "board", {})
    assert error and "no single running session" in text and MARK in text
    fresh = (await manager.create("Fresh", "", start=False))["id"]
    assert manager.default_session() == fresh
    view = manager.settings_view()
    view["models"]["cheap"] = dict(view["models"].pop("fast"), renamed_from="fast")
    await manager.update_settings({"models": view["models"]})
    assert "cheap" in st.read_raw(old.home)["models"]
    assert untouched(old)
    conn = sqlite3.connect(old.folder / ".dreamteam" / "swarm.db")
    try:
        assert conn.execute("SELECT model FROM agents WHERE name = 'Anna'").fetchone() == ("fast",)
    finally:
        conn.close()


async def test_it_can_be_deleted(old):
    assert await old.manager.delete(old.sid) is None
    assert not old.manager.exists(old.sid) and not old.folder.exists()


async def test_main_is_refused_with_its_own_message_and_cannot_be_deleted(tmp_path, caplog):
    home = make_home(tmp_path)
    write_earlier(home / ".dreamteam" / "swarm.db", "record")
    (home / ".dreamteam" / "session.json").write_text(json.dumps({"autostart": True}), encoding="utf-8")
    manager = manager_of(home)
    data = home / ".dreamteam"
    before = snapshot(data)
    text = MAIN_EARLIER_VERSION_TEXT.format(home=home, version=__version__)
    assert text == earlier_version_text(LEGACY_ID, home) == manager.earlier_version_text(LEGACY_ID)
    assert f"remove {home}/.dreamteam/swarm.db (with its -wal and -shm files) by hand" in text
    assert manager.summary(LEGACY_ID)["earlier_version"] is True
    for operation in (lambda: manager.load(LEGACY_ID), lambda: manager.start(LEGACY_ID),
                      lambda: manager.delete(LEGACY_ID), lambda: manager.set_workspace(LEGACY_ID, None),
                      lambda: manager.stop(LEGACY_ID)):
        with pytest.raises(SessionError) as exc:
            await operation()
        assert refused(exc.value, text)
    with pytest.raises(SessionError) as exc:
        manager.update(LEGACY_ID, title="Main")
    assert refused(exc.value, text)
    with caplog.at_level(logging.WARNING, logger="dreamteam.sessions"):
        assert await manager.resume() == []
    assert "session main was made by an earlier version of DreamTeam: not resumed" in caplog.text
    assert snapshot(data) == before and manager.exists(LEGACY_ID)
    await manager.shutdown()


# ---- the hub: REST, the board page, WebSocket, /mcp ----------------------------------------------------------
def _json(c: TestClient, method: str, path: str, body: dict | None = None):
    return c.request(method, path, content=json.dumps(body or {}), headers={"content-type": "application/json"})


@pytest.mark.parametrize("kind", KINDS)
def test_the_hub_lists_it_refuses_it_with_409_and_deletes_it(tmp_path, kind):
    made = asyncio.run(make_old(tmp_path, kind))
    manager, sid, text = made.manager, made.sid, made.text
    with TestClient(create_launcher_app(manager), base_url="http://127.0.0.1:8765") as c:
        listed = c.get("/api/sessions").json()["sessions"]
        assert [s["earlier_version"] for s in listed] == [True]
        item = c.get(f"/api/sessions/{sid}")
        assert item.status_code == 200 and item.json()["session"]["earlier_version"] is True
        for method, path, body in (("POST", f"/api/sessions/{sid}/start", {}),
                                   ("POST", f"/api/sessions/{sid}/stop", {}),
                                   ("PATCH", f"/api/sessions/{sid}", {"title": "Renamed"}),
                                   ("POST", f"/api/sessions/{sid}/workspace", {"default": True}),
                                   ("GET", f"/api/sessions/{sid}/skills", None),
                                   ("POST", f"/api/sessions/{sid}/skills/csv-tool/decide", {"approve": True}),
                                   ("GET", f"/s/{sid}/api/state", None),
                                   ("POST", f"/s/{sid}/api/act/board", {}),
                                   ("POST", f"/s/{sid}/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"}),
                                   ("GET", f"/s/{sid}/mcp", None)):
            answer = _json(c, method, path, body) if body is not None else c.request(method, path)
            assert answer.status_code == 409, path
            assert answer.json() == {"error": text, "earlier_version": True}, path
        page = c.get(f"/s/{sid}/")
        assert page.status_code == 409 and "text/html" in page.headers["content-type"]
        assert html.escape(text) in page.text
        with c.websocket_connect(f"ws://127.0.0.1:8765/s/{sid}/ws") as ws:
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_text()
        assert closed.value.code == 1011 and closed.value.reason == "made by an earlier version"
        assert untouched(made) and sid not in manager.swarms
        gone = c.delete(f"/api/sessions/{sid}?confirm={sid}")
        assert gone.status_code == 200 and not made.folder.exists()
        c.portal.call(manager.shutdown)


def test_a_refusal_by_the_load_closes_the_websocket_with_the_reason(tmp_path, monkeypatch):
    """Past the router's own check (here switched off), the load's 409 gives the same answers."""
    made = asyncio.run(make_old(tmp_path, "record"))
    manager, sid = made.manager, made.sid
    monkeypatch.setattr(manager, "earlier_version", lambda sid: False)
    with TestClient(create_launcher_app(manager), base_url="http://127.0.0.1:8765") as c:
        answer = c.get(f"/s/{sid}/api/state")
        assert answer.status_code == 409 and answer.json() == {"error": made.text, "earlier_version": True}
        with c.websocket_connect(f"ws://127.0.0.1:8765/s/{sid}/ws") as ws:
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_text()
        assert closed.value.code == 1011 and closed.value.reason == "made by an earlier version"
        assert untouched(made) and sid not in manager.swarms
        c.portal.call(manager.shutdown)


async def test_the_launcher_mcp_answers_with_its_message(old, tmp_path):
    manager, sid = old.manager, old.sid
    for name, args in (("session_start", {"session": sid}), ("session_stop", {"session": sid}),
                       ("session_cd", {"session": sid, "path": str(tmp_path / "elsewhere")}),
                       ("session_cd", {"session": sid, "path": "default"}),
                       ("board", {"session": sid}), ("task_create", {"session": sid, "title": "Nope"})):
        text, error = await call_launcher_tool(manager, name, dict(args))
        assert error and text == f"Error: {old.text}", name
    assert untouched(old) and sid not in manager.swarms


# ---- the CLI ----------------------------------------------------------------------------------------------------
def _fake_hub(monkeypatch, manager: SessionManager, version: str = "9.9.9") -> None:
    """The CLI talks to a launcher hub of `manager` (version `version`) without a server."""
    monkeypatch.setattr(cli, "running_hub", lambda project: HUB)
    monkeypatch.setattr(cli, "hub_identity", lambda base, timeout=3.0, **kw: {
        "mode": "launcher", "home": str(manager.home), "version": version})

    def hub_json(base, path, timeout=15.0, **kw):
        assert path == "/api/sessions", path   # nothing else may be asked about such a session
        return {"sessions": manager.list()}

    monkeypatch.setattr(cli, "_hub_json", hub_json)
    monkeypatch.setattr(console_module, "run_console", lambda *a, **kw: pytest.fail("the console started"))


@pytest.mark.parametrize("kind", KINDS)
def test_the_cli_lists_it_and_refuses_it_without_a_hub(tmp_path, capsys, kind):
    made = asyncio.run(make_old(tmp_path, kind))
    home, sid = made.home, made.sid
    assert cli.main(["sessions", "--home", str(home)]) == 0
    assert capsys.readouterr().out.splitlines()[0].endswith(MARK)
    assert cli.main(["status", "--home", str(home)]) == 0
    assert MARK in capsys.readouterr().out
    assert cli.main(["traces", "--home", str(home), "--session", sid]) == 1
    assert capsys.readouterr().err.strip() == made.text
    # the default pick is a session this version opens: here there is none
    assert cli.main(["traces", "--home", str(home)]) == 1
    assert capsys.readouterr().err.strip() == "No session to read traces from."
    assert untouched(made)
    fresh = asyncio.run(manager_of(home).create("Fresh", "", start=False))["id"]
    assert cli.main(["traces", "--home", str(home)]) == 0
    out = capsys.readouterr()
    assert out.err.strip() == f"Session {fresh}" and out.out.strip() == "No traces."
    assert cli.main(["sessions", "rm", sid, "--yes", "--home", str(home)]) == 0
    assert not made.folder.exists()


def test_the_cli_refuses_it_through_a_hub(tmp_path, capsys, monkeypatch):
    made = asyncio.run(make_old(tmp_path, "record"))
    home, sid = made.home, made.sid
    _fake_hub(monkeypatch, made.manager)
    text = EARLIER_VERSION_TEXT.format(sid=sid, version="9.9.9")   # the hub's version
    assert cli.main(["console", "--home", str(home), "--session", sid]) == 1
    assert capsys.readouterr().err.strip() == text
    assert cli.main(["traces", "--home", str(home), "--session", sid]) == 1
    assert capsys.readouterr().err.strip() == text
    # without --session it is never picked
    assert cli.main(["console", "--home", str(home)]) == 1
    assert capsys.readouterr().err.startswith("No running session.")
    assert cli.main(["traces", "--home", str(home)]) == 1
    assert capsys.readouterr().err.startswith("No running session.")
    assert untouched(made)


def test_sessions_rm_main_names_the_file_to_remove(tmp_path, capsys):
    home = make_home(tmp_path)
    write_earlier(home / ".dreamteam" / "swarm.db", "history")
    before = snapshot(home / ".dreamteam")
    assert cli.main(["sessions", "rm", "main", "--yes", "--home", str(home)]) == 1
    err = capsys.readouterr().err.strip()
    assert err == "Nothing deleted: " + MAIN_EARLIER_VERSION_TEXT.format(home=home, version=__version__)
    assert snapshot(home / ".dreamteam") == before


@pytest.mark.parametrize("kind", KINDS)
def test_a_project_made_by_an_earlier_version_is_refused_by_every_command(tmp_path, capsys, kind):
    project = tmp_path / "project"
    assert cli.main(["init", str(project), "--lang", "en", "--name", "Old project"]) == 0
    config = Config.load(project)
    write_earlier(config.data_dir / "swarm.db", kind)
    before = snapshot(config.data_dir)
    text = swarm_module.PROJECT_EARLIER_VERSION_TEXT.format(dir=config.project_dir, version=__version__)
    capsys.readouterr()
    for argv in (["run", str(project), "--no-console"], ["mcp", str(project)], ["status", str(project)],
                 ["traces", "--home", str(project)]):
        assert cli.main(argv) == 1, argv
        out = capsys.readouterr()
        assert out.err.strip() == text and out.out == "", argv
        assert snapshot(config.data_dir) == before and not (config.data_dir / "logs").exists(), argv
    with pytest.raises(swarm_module.EarlierVersionError):
        swarm_module.Swarm(project)
    assert snapshot(config.data_dir) == before


# ---- `dreamteam -p` ---------------------------------------------------------------------------------------------
def finisher(system, messages, tools):
    seen = [m.get("content") or "" for m in messages if m["role"] in ("user", "tool")]
    if any("marked as achieved" in t or "already marked" in t for t in seen[-1:]):
        return call("wait")
    if any("haiku" in t for t in seen):
        return call("goal_complete", summary="Written.")
    return call("wait")


async def _print(conn, prompt: str, **kwargs: Any) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = await run_print(conn, prompt, out=out, err=err, timeout=30, lang="en", **kwargs)
    return code, out.getvalue(), err.getvalue()


@pytest.mark.parametrize("kind", KINDS)
async def test_print_mode_never_opens_it(tmp_path, kind):
    made = await make_old(tmp_path, kind)
    await made.manager.shutdown()
    conn = await connect(made.home, web=False, resume=False,
                         llm_factory=lambda profile, agent: ScriptedLLM(finisher, profile))
    try:
        code, out, err = await _print(conn, "A haiku", session=made.sid)
        assert code == 1 and out == "" and err.strip() == made.text
        code, out, err = await _print(conn, "A haiku", continue_last=True)
        assert code == 1 and out == "" and err.strip() == continue_text(made.sid)
        assert untouched(made) and len(await conn.client.sessions()) == 1
        fresh = (await conn.client.create_session("Fresh", "", start=False))["id"]
        code, out, err = await _print(conn, "Write a haiku", continue_last=True, output_format="json")
        assert code == 0, err
        assert json.loads(out)["session"] == fresh
        assert untouched(made)
    finally:
        await conn.close()


def continue_text(sid: str, lang: str = "en") -> str:
    if lang == "ru":
        return (f"Последняя сессия, {sid}, создана более ранней версией DreamTeam, и эта версия ({__version__}) не может "
                "её открыть. Начните новую сессию: запустите без -c.")
    return (f"The most recent session, {sid}, was made by an earlier version of DreamTeam and cannot be opened by this "
            f"one ({__version__}). Start a new session: run without -c.")


async def make_old_latest(tmp_path: Path) -> tuple[SimpleNamespace, str]:
    """An openable session, then a session of an earlier version that was active after it (the most recent one)."""
    made = await make_old(tmp_path, "record")
    older = (await made.manager.create("Older", "", start=False))["id"]
    db = Database(made.folder / ".dreamteam" / "swarm.db")
    try:
        db.execute("INSERT INTO events(ts, type, actor, data) VALUES (?, 'session.started', 'user', '{}')",
                   (time.time() + 3600,))
    finally:
        db.close()
    made.before = snapshot(made.folder)
    await made.manager.shutdown()
    return made, older


@pytest.mark.parametrize("lang", ["en", "ru"])
async def test_continue_says_the_most_recent_session_is_of_an_earlier_version(tmp_path, lang):
    """-p -c never passes over the most recent session for an older one the user did not ask for."""
    made, older = await make_old_latest(tmp_path)
    conn = await connect(made.home, web=False, resume=False,
                         llm_factory=lambda profile, agent: ScriptedLLM(finisher, profile))
    try:
        out, err = io.StringIO(), io.StringIO()
        code = await run_print(conn, "A haiku", out=out, err=err, timeout=30, lang=lang, continue_last=True)
        assert code == 1 and out.getvalue() == "" and err.getvalue().strip() == continue_text(made.sid, lang)
        assert untouched(made)
        summary = next(s for s in await conn.client.sessions() if s["id"] == older)
        assert summary["status"] != "running"   # not continued in its place
    finally:
        await conn.close()


async def test_the_tui_continue_says_the_most_recent_session_is_of_an_earlier_version(tmp_path):
    from test_tui_app import Harness

    made, older = await make_old_latest(tmp_path)
    conn = await connect(made.home, web=False, resume=False,
                         llm_factory=lambda profile, agent: ScriptedLLM(finisher, profile))
    with contextlib.ExitStack() as stack:
        h = Harness(conn, stack, width=200, continue_last=True)
        try:
            await h.see(f"The most recent session, {made.sid}, was made by an earlier version of DreamTeam and cannot "
                        f"be opened by this one ({__version__}): start a new session")
            assert not h.tui.attached and h.tui.sid is None
            assert untouched(made)
            summary = next(s for s in await conn.client.sessions() if s["id"] == older)
            assert summary["status"] != "running"
        finally:
            await h.close()
    await conn.close()


@pytest.mark.parametrize("how", ["continue", "resume", "unknown"])
async def test_a_tui_prompt_for_a_session_that_does_not_open_creates_nothing(tmp_path, how):
    """`dreamteam -c "go on"` after an upgrade: the text was meant for the old session, never a new swarm's goal."""
    from test_tui_app import Harness

    made, older = await make_old_latest(tmp_path)
    conn = await connect(made.home, web=False, resume=False,
                         llm_factory=lambda profile, agent: ScriptedLLM(finisher, profile))
    kwargs: dict[str, Any] = {"continue": {"continue_last": True}, "resume": {"session": made.sid},
                              "unknown": {"session": "999-nope"}}[how]
    with contextlib.ExitStack() as stack:
        h = Harness(conn, stack, width=200, initial_prompt="go on please", **kwargs)
        try:
            await h.until(lambda: h.tui.ready.is_set() and h.tui.buffer.text == "go on please", what="the prompt kept")
            await asyncio.sleep(0.3)
            assert not h.tui.attached and h.tui.sid is None
            assert len(await conn.client.sessions()) == 2
            assert untouched(made)
            if how == "resume":   # its own message, not "no such session"
                assert made.text in " ".join(h.text.split()) and "No session" not in h.text   # (wrapped at 200)
        finally:
            await h.close()
    await conn.close()


def wait_policy(system, messages, tools):
    return call("wait")


@pytest.mark.parametrize("lang", ["en", "ru"])
async def test_the_tui_resume_id_says_its_message(tmp_path, lang):
    """`dreamteam -r ID` of a session made by an earlier version: its message in the UI language under the welcome
    box (no session line in it), no start, nothing attached, nothing written."""
    from test_tui_app import Harness

    from dreamteam.tui import i18n

    made = await make_old(tmp_path, "record")
    await made.manager.shutdown()
    conn = await connect(made.home, web=False, resume=False,
                         llm_factory=lambda profile, agent: ScriptedLLM(wait_policy, profile))
    twin = i18n.t(lang, "earlier_session", sid=made.sid, version=__version__)
    with contextlib.ExitStack() as stack:
        h = Harness(conn, stack, width=400, session=made.sid, lang=lang)
        try:
            await h.see(twin)
            await h.until(lambda: h.tui.ready.is_set(), what="the start")
            await asyncio.sleep(0.2)
            text = h.text
            assert text.index(i18n.t(lang, "welcome")) < text.index(twin) and text.count(twin) == 1
            assert i18n.t(lang, "unknown_session", id=made.sid) not in text
            assert i18n.t(lang, "session_starting", title="Old") not in text
            assert f"{i18n.t(lang, 'w_session')}: Old" not in text
            assert not h.tui.attached and h.tui.sid is None
            assert untouched(made)
        finally:
            await h.close()
    await conn.close()


async def test_the_tui_shows_mains_own_message_and_never_deletes_it(tmp_path):
    """The legacy `main` made by an earlier version: `-r main`, Enter and Del in the picker, and `/delete main` show its
    own message, which names the file to remove by hand; nothing is written or deleted."""
    from test_tui_app import DELETE_KEY, Harness

    from dreamteam.tui.dialogs import SessionPicker

    home = make_home(tmp_path)
    write_earlier(home / ".dreamteam" / "swarm.db", "record")
    data = home / ".dreamteam"

    def swarm_files() -> dict:   # (the launcher's own files live in this folder too)
        return {name: value for name, value in snapshot(data).items() if name.startswith("swarm.db")}

    before = swarm_files()
    conn = await connect(home, web=False, resume=False,
                         llm_factory=lambda profile, agent: ScriptedLLM(wait_policy, profile))
    summary = next(s for s in await conn.client.sessions() if s["id"] == LEGACY_ID)
    assert summary["earlier_version"] is True and summary["legacy"] is True
    twin = MAIN_EARLIER_VERSION_TEXT.format(home=summary["dir"], version=__version__)
    assert f"remove {summary['dir']}/.dreamteam/swarm.db" in twin
    with contextlib.ExitStack() as stack:
        h = Harness(conn, stack, width=1000, session=LEGACY_ID)   # (a long path: not wrapped)
        try:
            await h.see(twin)
            assert not h.tui.attached
            await h.run(f"/delete {LEGACY_ID}", twin)
            assert h.text.count(twin) == 2
            h.type("/sessions\r")
            await h.until(lambda: isinstance(h.tui.dialog, SessionPicker), what="the picker")
            assert h.tui.dialog.select.selected.value == LEGACY_ID
            assert "earlier version" in h.tui.dialog.select.selected.hint
            h.type(DELETE_KEY)   # no confirmation: its message, and the picker again
            await h.until(lambda: h.text.count(twin) == 3, what="the message on Del")
            await h.until(lambda: isinstance(h.tui.dialog, SessionPicker), what="the picker again")
            h.type("\r")
            await h.until(lambda: h.text.count(twin) == 4 and h.tui.dialog is None, what="the message on Enter")
            assert not h.tui.attached
        finally:
            await h.close()
    assert swarm_files() == before and list(before) == ["swarm.db"]
    assert LEGACY_ID in [s["id"] for s in await conn.client.sessions()]
    await conn.close()


async def test_the_tui_picker_marks_it_shows_its_message_and_deletes_it(tmp_path):
    """`dreamteam -r --skill X` and the picker: the status "earlier version"; Enter prints the message and attaches
    nothing (the resume attempt of --skill ends: a session /new creates gets X); `/resume ID` the same; Del deletes it as
    any session."""
    from conftest import make_skill
    from test_tui_app import DELETE_KEY, DOWN, Harness

    from dreamteam.tui.dialogs import ConfirmDialog, SessionPicker

    made, older = await make_old_latest(tmp_path)
    pdf = make_skill(tmp_path / "src" / "pdf", "pdf")
    conn = await connect(made.home, web=False, resume=False,
                         llm_factory=lambda profile, agent: ScriptedLLM(wait_policy, profile))

    async def select(h: Harness, sid: str) -> None:
        await h.until(lambda: isinstance(h.tui.dialog, SessionPicker), what="the picker")
        for _ in range(len(h.tui.dialog.select.options)):
            if h.tui.dialog.select.selected.value == sid:
                return
            index = h.tui.dialog.select.index
            h.type(DOWN)
            await h.until(lambda: h.tui.dialog.select.index != index)
        raise AssertionError(f"{sid} is not in the picker")

    with contextlib.ExitStack() as stack:
        h = Harness(conn, stack, width=400, session="", initial_prompt="/sessions", overrides={"skills": [str(pdf)]})
        try:
            await select(h, made.sid)
            assert "earlier version" in h.tui.dialog.select.selected.hint
            assert "earlier version" not in next(o.hint for o in h.tui.dialog.select.options if o.value == older)
            h.type("\r")
            await h.see(made.text)
            await h.until(lambda: h.tui.dialog is None, what="the picker closed")
            assert not h.tui.attached and untouched(made)
            assert h.tui._resume_skills is False and h.tui.skills_override == [str(pdf)]
            await h.run(f"/resume {made.sid}", made.text)
            assert h.text.count(made.text) == 2 and not h.tui.attached and untouched(made)
            h.type("/sessions\r")
            await select(h, made.sid)
            h.type(DELETE_KEY)
            await h.until(lambda: isinstance(h.tui.dialog, ConfirmDialog), what="the confirmation")
            h.type("1")   # "Yes"
            await h.see(f'Session "Old" ({made.sid}) deleted', 20)
            h.type("\x1b")
            await h.until(lambda: h.tui.dialog is None, what="the picker closed")
            assert made.sid not in [s["id"] for s in await conn.client.sessions()] and not made.folder.exists()
        finally:
            await h.close()
    await conn.close()


@pytest.mark.parametrize("how", ["resume", "continue", "stale", "no_sessions"])
async def test_a_refused_session_leaves_the_skill_option_to_a_new_session(tmp_path, monkeypatch, how):
    """`--skill X` with `-r ID` of a session made by an earlier version, `-c` whose most recent session is one, or a
    start the hub answers 409 `earlier_version` (a list gone stale): the resume attempt ends (no later resumed
    session gets X), and the session /new creates gets X, as after `-c` with no sessions (docs/SPEC.md §18.3
    `--skill`)."""
    from conftest import make_skill
    from test_tui_app import Harness

    from dreamteam.tui.client import HubError

    older = None
    if how == "no_sessions":
        home, made = make_home(tmp_path), None
    else:
        made, older = await make_old_latest(tmp_path)
        home = made.home
    pdf = make_skill(tmp_path / "src" / "pdf", "pdf")
    conn = await connect(home, web=False, resume=False,
                         llm_factory=lambda profile, agent: ScriptedLLM(wait_policy, profile))
    if how == "stale":   # the list says `older` opens; an import replaced it meanwhile
        async def refused(sid, **kwargs):
            raise HubError(409, made.text, {"error": made.text, "earlier_version": True})

        monkeypatch.setattr(conn.client, "start_session", refused)
    kwargs: dict[str, Any] = {"resume": {"session": made.sid if made else None}, "continue": {"continue_last": True},
                              "stale": {"session": older}, "no_sessions": {"continue_last": True}}[how]
    with contextlib.ExitStack() as stack:
        h = Harness(conn, stack, width=400, overrides={"skills": [str(pdf)]}, **kwargs)
        try:
            await h.until(lambda: h.tui.ready.is_set(), what="the start")
            await asyncio.sleep(0.2)
            assert not h.tui.attached and h.tui.skills_override == [str(pdf)]
            assert h.tui._resume_skills is (how == "no_sessions")
            if how == "stale":   # the message of the session asked for, in place of the hub's text
                assert f"Session {older} was made by an earlier version" in " ".join(h.text.split())
            h.type("/new Write a haiku\r")
            await h.until(lambda: h.tui.attached and h.tui.sid, 30, "the new session")
            summary = next(s for s in await conn.client.sessions() if s["id"] == h.tui.sid)
            assert summary["skills"] == ["pdf"] and h.tui.skills_override == []
            if made is not None:
                assert untouched(made)
        finally:
            await h.close()
    await conn.close()


@pytest.mark.parametrize("how", ["continue", "resume"])
async def test_a_tui_prompt_for_a_session_is_kept_when_the_session_list_fails(tmp_path, monkeypatch, how):
    """`dreamteam -r ID "go on"` while GET /sessions fails: the text was meant for that session, never a new goal."""
    from test_tui_app import Harness

    from dreamteam.tui.client import HubError

    made, older = await make_old_latest(tmp_path)
    conn = await connect(made.home, web=False, resume=False,
                         llm_factory=lambda profile, agent: ScriptedLLM(finisher, profile))
    sessions, calls = conn.client.sessions, []

    async def failing(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise HubError(500, "one session folder is broken")
        return await sessions(*args, **kwargs)

    monkeypatch.setattr(conn.client, "sessions", failing)
    kwargs: dict[str, Any] = {"continue": {"continue_last": True}, "resume": {"session": older}}[how]
    with contextlib.ExitStack() as stack:
        h = Harness(conn, stack, width=200, initial_prompt="go on please", **kwargs)
        try:
            await h.until(lambda: h.tui.ready.is_set() and h.tui.buffer.text == "go on please", what="the prompt kept")
            await asyncio.sleep(0.3)
            assert not h.tui.attached and h.tui.sid is None
            assert len(await conn.client.sessions()) == 2
        finally:
            await h.close()
    await conn.close()


# ---- import, a session that ran, and what still opens -----------------------------------------------------------
def test_it_is_exported_and_dreamteam_import_refuses_it_before_writing(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("DREAMTEAM_SKILLS_DIR", str(tmp_path / "libs" / "skills"))
    monkeypatch.setenv("DREAMTEAM_MCP_LIBRARY_DIR", str(tmp_path / "libs" / "mcp"))
    made = asyncio.run(make_old(tmp_path, "record"))
    assert cli.main(["export", str(tmp_path / "usb"), "--home", str(made.home)]) == 0
    # the export says that the same version will refuse it on the other computer
    assert (f"session {made.sid} was made by an earlier version of DreamTeam: this version and the same one on the "
            "other computer refuse it") in capsys.readouterr().out
    assert untouched(made)
    assert (tmp_path / "usb" / "sessions" / made.sid / ".dreamteam" / "swarm.db").is_file()
    other = tmp_path / "other"
    (other / "sessions").mkdir(parents=True)
    st.write_raw(other, raw(tmp_path))
    capsys.readouterr()
    assert cli.main(["import", str(tmp_path / "usb"), "--include", "sessions", "--yes", "--home", str(other)]) == 1
    out = capsys.readouterr().out
    assert (f"sessions  {made.sid}: refused (it was made by an earlier version of DreamTeam and cannot be opened by "
            f"this one ({__version__}); import it with the version that made it)") in out
    assert list((other / "sessions").iterdir()) == []


async def test_a_session_that_ran_and_was_rewritten_to_revision_4_is_refused(tmp_path):
    home = make_home(tmp_path)
    manager = manager_of(home)
    sid = (await manager.create("Ran", "Do it", start=True))["id"]
    swarm = manager.swarms[sid]
    for _ in range(200):
        names = [m["name"] for m in swarm.members() if m["kind"] == "llm"]
        if names and frozen.read(swarm.db, names[0]):
            break
        await asyncio.sleep(0.05)
    assert frozen.read(swarm.db, names[0])["rev"] == frozen.TOOLSET_REVISION
    await manager.stop(sid)
    assert manager.summary(sid)["earlier_version"] is False
    path = manager.dir_of(sid) / ".dreamteam" / "swarm.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute("UPDATE kv SET value = ? WHERE key = ?",
                     (json.dumps(frozen.record(4, "en", frozen.snapshot(4, "en"))), frozen.kv_key(names[0])))
        conn.commit()
    finally:
        conn.close()
    made = SimpleNamespace(folder=manager.dir_of(sid), before=snapshot(manager.dir_of(sid)))
    assert manager.summary(sid)["earlier_version"] is True
    text = EARLIER_VERSION_TEXT.format(sid=sid, version=__version__)
    for operation in (lambda: manager.load(sid), lambda: manager.start(sid)):
        with pytest.raises(SessionError) as exc:
            await operation()
        assert refused(exc.value, text)
    assert untouched(made)
    await manager.shutdown()


async def test_sessions_without_anything_frozen_open_as_usual(tmp_path):
    """No database file (never started), and a database with neither a history nor a record (an external member and
    an LLM member hired and never started): opened as usual, and the LLM member gets the current revision at its
    first start."""
    home = make_home(tmp_path)
    manager = manager_of(home)
    never = (await manager.create("Never", "Do it", start=False))["id"]
    bare = (await manager.create("Bare", "Do it", start=False))["id"]
    db = Database(manager.dir_of(bare) / ".dreamteam" / "swarm.db")
    try:
        db.execute("INSERT INTO agents(name, kind, role, status) VALUES ('Client', 'external', 'Helper', 'idle')")
        db.execute("INSERT INTO agents(name, kind, role, model, status) VALUES ('Anna', 'llm', 'Tester', 'default', "
                   "'idle')")
    finally:
        db.close()
    assert [manager.summary(s)["earlier_version"] for s in (never, bare)] == [False, False]
    assert (await manager.load(never)) is manager.swarms[never]
    swarm = await manager.start(bare)
    for _ in range(200):
        if frozen.read(swarm.db, "Anna"):
            break
        await asyncio.sleep(0.05)
    assert frozen.read(swarm.db, "Anna")["rev"] == frozen.TOOLSET_REVISION
    await manager.shutdown()
