"""Sessions of the HTTP API v1 at the manager level (docs/SPEC.md §17.2, §21.4, §21.7, §21.9): the "via" and "api"
meta a create writes, what every start does with it (the auto-reply hold, `started_at` and `base_commit`,
`start_error`, `finished_at`), `control`, the uid lookups, the start mark, the lists of a stopped session, the v1 codes
of the folder refusals, the copy of v1 uploads and what an import drops."""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import subprocess
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest

from dreamteam import prompts
from dreamteam import settings as st
from dreamteam import swarm as swarm_module
from dreamteam.copyfrom import CopyError, CopyLimits
from dreamteam.db import Database
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.models import USER
from dreamteam.result import build_result
from dreamteam.sessions import (API_GIVEN, API_LATER, FOLDER_BUSY, FOLDER_NOT_READY, MOVE_JOURNAL, ImportedWorkspace,
                                SessionError, SessionManager, _error_code, _Plan, api_meta, read_lists, read_stats)
from dreamteam.swarm import Swarm
from dreamteam.tools import frozen

NO_GIT = {"workspace": {"git": False}}
AUTO = {"answers": "auto", "stop_when_done": True, "client_ref": "job-1", "request_digest": "d" * 64}
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    st.write_raw(home, {
        "swarm": {"name": "Home", "language": "en", "start_agents": 1, "max_agents": 3, "max_cost_usd": 0},
        "agents": {"stagger_seconds": 0, "idle_nudge_seconds": 60},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
    })
    return home


def _manager(home: Path) -> SessionManager:
    def factory(profile, agent):
        return ScriptedLLM(lambda system, messages, tools: "Nothing to do right now.", profile)

    return SessionManager(home, llm_factory=factory)


def stored(manager: SessionManager, sid: str) -> dict[str, Any]:
    """The session's session.json as it is on disk."""
    return json.loads((manager.dir_of(sid) / "session.json").read_text(encoding="utf-8"))


def db_path(manager: SessionManager, sid: str) -> Path:
    return manager.dir_of(sid) / ".dreamteam" / "swarm.db"


def git(folder: Path, *args: str) -> str:
    config = ["-c", "user.name=Owner", "-c", "user.email=owner@example.com", "-c", "commit.gpgsign=false"]
    return subprocess.run(["git", "-C", str(folder), *config, *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def repository(folder: Path, commits: int) -> list[str]:
    """`folder` as a clean repository of the user's with `commits` commits; their ids, oldest first."""
    folder.mkdir(parents=True)
    git(folder, "init", "-q")
    made = []
    for n in range(commits):
        (folder / f"file{n}.txt").write_text(f"{n}\n", encoding="utf-8")
        git(folder, "add", "-A")
        git(folder, "commit", "-q", "--no-verify", "-m", f"commit {n}")
        made.append(git(folder, "rev-parse", "HEAD"))
    return made


# the folders above the session's own that a text for a reader outside the hub never names: a directory and a user
# name with spaces in them (docs/SPEC.md §21.1 "Paths in answers")
SPACED = ("rv5 pkvolo07", "John Smith")


def assert_no_folder_above(text: str, tmp_path: Path, *names: str) -> None:
    for part in (*SPACED, "rv5", "pkvolo07", "Smith", tmp_path.name, tmp_path.parent.name, *names):
        assert part not in text, (part, text)
    assert str(tmp_path) not in text and tmp_path.as_posix() not in text


def journal_errors(path: Path) -> list[tuple[str, Any]]:
    """(reason, error) of every session.stopped the session's journal holds, oldest first."""
    db = Database(path)
    try:
        rows = db.all("SELECT data FROM events WHERE type = 'session.stopped' ORDER BY id")
    finally:
        db.close()
    return [(data["reason"], data.get("error")) for data in (json.loads(row["data"]) for row in rows)]


async def _chunks(*parts: bytes):
    for part in parts:
        yield part


def _zip(files: dict[str, bytes]) -> bytes:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return data.getvalue()


def make_earlier(manager: SessionManager, sid: str) -> None:
    """The database of an earlier version in place of the session's (a revision-4 tool-set record)."""
    path = db_path(manager, sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = Database(path)
    try:
        db.execute("INSERT INTO agents(name, kind, role, model, status) VALUES ('Anna', 'llm', 'Tester', 'default', "
                   "'idle')")
        frozen.write(db, "Anna", frozen.record(4, "en", frozen.snapshot(4, "en")))
    finally:
        db.close()


# ---- what a create writes ----------------------------------------------------------------------------------------
async def test_the_create_writes_via_and_api_before_the_first_start(tmp_path, monkeypatch):
    manager = _manager(_home(tmp_path))
    seen: dict[str, dict[str, Any]] = {}
    real_start = Swarm.start

    async def watching_start(self, *args, **kwargs):
        seen[self.session_id] = stored(manager, self.session_id)   # what session.json says as the swarm starts
        await real_start(self, *args, **kwargs)

    monkeypatch.setattr(Swarm, "start", watching_start)
    try:
        started = await manager.create("Calc", "Build a calculator", overrides=NO_GIT, api=AUTO)   # start=True
        sid = started["id"]
        assert seen[sid]["via"] == "api"
        assert seen[sid]["api"] == {**AUTO, "folder": None, **dict.fromkeys(API_LATER)}
        assert started["via"] == "api" and started["answers"] == "auto" and started["status"] == "running"
        later = await manager.create("Later", "Wait", overrides=NO_GIT, start=False, api={"answers": "manual"})
        assert stored(manager, later["id"])["api"] == {**API_GIVEN, "answers": "manual", **dict.fromkeys(API_LATER)}
        assert later["via"] == "api" and later["answers"] == "manual"
        board = await manager.create("Board", "Wait", overrides=NO_GIT, start=False)
        assert "via" not in stored(manager, board["id"]) and "api" not in stored(manager, board["id"])
        assert "via" not in board and "answers" not in board
        assert api_meta(stored(manager, board["id"])) is None
        before = manager.ids()
        for wrong in ({"answers": "sometimes"}, {"colour": "red"}, {"stop_when_done": "yes"}, {"client_ref": 5},
                      {"folder": "jobs/acme"}, ["auto"]):
            with pytest.raises(SessionError) as exc:
                await manager.create("Wrong", "Wait", overrides=NO_GIT, start=False, api=wrong)
            assert exc.value.status == 400
        assert manager.ids() == before   # nothing was created
    finally:
        await manager.shutdown()


async def test_validated_overrides_is_creates_own_check(tmp_path):
    manager = _manager(_home(tmp_path))
    assert manager.validated_overrides({"swarm": {"max_agents": 5, "goal": "dropped"}, "mcp": {}}) == \
        {"swarm": {"max_agents": 5}}
    for wrong in ({"models": {}}, {"swarm": {"max_agents": "many"}}, "swarm"):
        with pytest.raises(SessionError) as exc:
            manager.validated_overrides(wrong)
        assert exc.value.status == 400


# ---- what every start does with it -----------------------------------------------------------------------------
@needs_git
async def test_the_first_start_records_started_at_and_base_commit_and_later_starts_keep_them(tmp_path):
    manager = _manager(_home(tmp_path))
    try:
        sid = (await manager.create("Calc", "Build a calculator", api=AUTO))["id"]   # git on
        board = (await manager.create("Board", "Build a calculator"))["id"]
        api = stored(manager, sid)["api"]
        workspace = manager.default_workspace(sid)
        first_commit = git(workspace, "rev-list", "--max-parents=0", "HEAD")
        assert api["base_commit"] == first_commit   # the initial commit of the files, no agent's
        assert isinstance(api["started_at"], float) and api["finished_at"] is None and api["start_error"] is None
        assert manager.swarms[board].workspace.head_at_init is None   # recorded for a v1 session's first start only
        await manager.stop(sid)
        (workspace / "notes.md").write_text("a note\n", encoding="utf-8")
        git(workspace, "add", "notes.md")
        git(workspace, "commit", "-q", "--no-verify", "-m", "a note")
        assert git(workspace, "rev-parse", "HEAD") != first_commit
        await manager.start(sid)
        again = stored(manager, sid)["api"]
        assert again["base_commit"] == first_commit and again["started_at"] == api["started_at"]
        assert again["finished_at"] is None
    finally:
        await manager.shutdown()


async def test_base_commit_is_null_with_git_off(tmp_path):
    manager = _manager(_home(tmp_path))
    try:
        sid = (await manager.create("Calc", "Build a calculator", overrides=NO_GIT, api=AUTO))["id"]
        api = stored(manager, sid)["api"]
        assert api["base_commit"] is None and isinstance(api["started_at"], float)
    finally:
        await manager.shutdown()


async def test_a_refused_start_records_start_error_and_a_later_start_clears_it(tmp_path):
    manager = _manager(_home(tmp_path))
    gone = tmp_path.joinpath(*SPACED, "jobs", "acme site")   # an in-place folder whose path has spaces in it
    try:
        sid = (await manager.create("Calc", "Build a calculator", overrides=NO_GIT, start=False, api=AUTO))["id"]
        meta = manager.meta(sid)
        meta["workspace"] = {"path": str(gone), "adopted": True}
        manager._write_meta(sid, meta)
        with pytest.raises(SessionError) as exc:
            await manager.start(sid)
        assert exc.value.status == 409 and exc.value.code == FOLDER_NOT_READY
        api = stored(manager, sid)["api"]
        assert api["start_error"]["code"] == FOLDER_NOT_READY
        text = api["start_error"]["text"]
        # the folder by its name, whole; nothing above it (an unquoted path is not cut at its first space)
        assert text.startswith(f"The workspace folder of session {sid} does not exist: acme site. Bring it back")
        assert_no_folder_above(text, tmp_path, "jobs")
        assert isinstance(api["finished_at"], float) and api["started_at"] is None
        assert manager.is_final(sid) and not gone.exists()
        gone.mkdir(parents=True)
        await manager.start(sid)
        api = stored(manager, sid)["api"]
        assert api["start_error"] is None and api["finished_at"] is None and isinstance(api["started_at"], float)
        # a workspace move that did not finish: refused before the start, with the same code
        await manager.stop(sid)
        (manager.dir_of(sid) / ".dreamteam" / MOVE_JOURNAL).write_text("{}", encoding="utf-8")
        with pytest.raises(SessionError) as exc:
            await manager.start(sid)
        assert exc.value.code == FOLDER_NOT_READY
        assert stored(manager, sid)["api"]["start_error"]["code"] == FOLDER_NOT_READY
    finally:
        await manager.shutdown()


async def test_a_failing_swarm_start_records_start_error_before_session_stopped(tmp_path, monkeypatch):
    above = tmp_path.joinpath(*SPACED)   # the home lies in folders with spaces in their names
    above.mkdir(parents=True)
    manager = _manager(_home(above))
    failure: dict[str, Exception] = {}

    async def failing_start(self, *args, **kwargs):
        raise failure["error"]

    try:
        sid = (await manager.create("Calc", "Build a calculator", overrides=NO_GIT, start=False, api=AUTO))["id"]
        board = (await manager.create("Board", "Build a calculator", overrides=NO_GIT, start=False))["id"]
        recorded: list[tuple[str, Any]] = []
        real_journal = manager._journal_stopped

        def spy(sid_: str, swarm: Any, reason: str, **data: Any) -> None:
            recorded.append((reason, api_meta(manager.meta(sid_)) and manager.meta(sid_)["api"]["start_error"]))
            real_journal(sid_, swarm, reason, **data)

        monkeypatch.setattr(manager, "_journal_stopped", spy)
        monkeypatch.setattr(Swarm, "start", failing_start)
        failure["error"] = RuntimeError("the MCP server would not start")
        with pytest.raises(RuntimeError):
            await manager.start(sid)
        failed = {"code": "internal", "text": "RuntimeError: the MCP server would not start"}
        assert recorded == [("failed", failed)]   # recorded before the journal says the run ended
        api = stored(manager, sid)["api"]
        assert api["start_error"] == failed and isinstance(api["finished_at"], float)
        assert api["started_at"] is None and manager.is_final(sid)
        # an error that names the session's folders unquoted, spaces and all: each by its name, what lies below them
        # as a relative path, nothing above them, in start_error and in the journal alike
        workspace, home = manager.default_workspace(sid), manager.home
        failure["error"] = RuntimeError(f"cannot run {workspace / 'mcp' / 'my tool'}: exit 1; see "
                                        f"{(home / 'logs dir' / 'x.log').as_posix()} and {manager.dir_of(sid)}.")
        with pytest.raises(RuntimeError):
            await manager.start(sid)
        text = stored(manager, sid)["api"]["start_error"]["text"]
        below = os.path.join("workspace", "mcp", "my tool")
        assert text == f"RuntimeError: cannot run {below}: exit 1; see home/logs dir/x.log and {sid}."
        assert_no_folder_above(text, tmp_path)
        assert journal_errors(db_path(manager, sid))[-1] == ("failed", text)
        with pytest.raises(RuntimeError):
            await manager.start(board)
        assert "api" not in stored(manager, board)   # a session of the board gets no v1 block
        reason, error = journal_errors(db_path(manager, board))[-1]
        assert reason == "failed" and "home/logs dir/x.log" in error
        assert_no_folder_above(error, tmp_path)
    finally:
        await manager.shutdown()


async def test_a_session_that_cannot_be_loaded_records_start_error_but_never_an_earlier_one(tmp_path):
    manager = _manager(_home(tmp_path))
    try:
        sid = (await manager.create("Calc", "Build a calculator", overrides=NO_GIT, start=False, api=AUTO))["id"]
        meta = manager.meta(sid)
        meta["overrides"] = {"swarm": {"max_agents": "many"}}   # settings this home refuses now
        manager._write_meta(sid, meta)
        with pytest.raises(SessionError) as exc:
            await manager.start(sid)
        assert exc.value.status == 400
        assert stored(manager, sid)["api"]["start_error"]["code"] == "bad_request"
        old = (await manager.create("Old", "Build a calculator", overrides=NO_GIT, start=False, api=AUTO))["id"]
        make_earlier(manager, old)
        before = stored(manager, old)
        with pytest.raises(SessionError) as exc:
            await manager.start(old)
        assert exc.value.status == 409 and exc.value.extra.get("earlier_version")
        assert stored(manager, old) == before   # never written to
    finally:
        await manager.shutdown()


async def test_finished_at_is_written_when_a_running_session_stops_and_cleared_when_it_starts(tmp_path):
    manager = _manager(_home(tmp_path))
    try:
        sid = (await manager.create("Calc", "Build a calculator", overrides=NO_GIT, api=AUTO))["id"]
        started_at = stored(manager, sid)["api"]["started_at"]
        await manager.stop(sid)
        stopped_at = stored(manager, sid)["api"]["finished_at"]
        assert isinstance(stopped_at, float) and stopped_at >= started_at
        await manager.stop(sid)   # not running: nothing changes
        assert stored(manager, sid)["api"]["finished_at"] == stopped_at
        await manager.start(sid)
        assert stored(manager, sid)["api"]["finished_at"] is None
    finally:
        await manager.shutdown()   # the hub stops: the run ends too
    api = stored(manager, sid)["api"]
    assert isinstance(api["finished_at"], float) and api["started_at"] == started_at


async def test_a_start_cancelled_by_the_hub_stopping_records_finished_at_and_no_start_error(tmp_path, monkeypatch):
    manager = _manager(_home(tmp_path))

    async def cancelled_start(self, *args, **kwargs):   # Ctrl+C while resume() starts the session
        raise asyncio.CancelledError()

    try:
        sid = (await manager.create("Calc", "Build a calculator", overrides=NO_GIT, start=False, api=AUTO))["id"]
        monkeypatch.setattr(Swarm, "start", cancelled_start)
        with pytest.raises(asyncio.CancelledError):
            await manager.start(sid)
        api = stored(manager, sid)["api"]
        assert isinstance(api["finished_at"], float) and api["start_error"] is None and api["started_at"] is None
        assert journal_errors(db_path(manager, sid)) == [("shutdown", None)]
    finally:
        await manager.shutdown()


@needs_git
async def test_base_commit_of_an_adopted_repository_is_its_head_not_its_first_commit(tmp_path):
    manager = _manager(_home(tmp_path))
    project = tmp_path / "project"
    first, second = repository(project, 2)
    try:
        sid = (await manager.create("Calc", "Build a calculator", workspace=str(project), api=AUTO))["id"]
        assert manager.workspace_of(sid)["adopted"] and sid in manager.running
        api = stored(manager, sid)["api"]
        assert api["base_commit"] == git(project, "rev-parse", "HEAD") != first
        assert git(project, "merge-base", "--is-ancestor", second, api["base_commit"]) == ""   # (exit 0)
    finally:
        await manager.shutdown()


@needs_git
async def test_an_adopted_repository_changed_before_the_start_records_folder_not_ready(tmp_path):
    manager = _manager(_home(tmp_path))
    project = tmp_path.joinpath(*SPACED, "my project")
    repository(project, 1)
    try:
        sid = (await manager.create("Calc", "Build a calculator", start=False, workspace=str(project), api=AUTO))["id"]
        (project / "notes.txt").write_text("the owner's own work, not committed\n", encoding="utf-8")
        with pytest.raises(SessionError) as exc:
            await manager.start(sid)
        assert exc.value.status == 409 and exc.value.code == FOLDER_NOT_READY
        api = stored(manager, sid)["api"]
        assert api["start_error"]["code"] == FOLDER_NOT_READY and isinstance(api["finished_at"], float)
        text = api["start_error"]["text"]
        assert text.startswith(f"The git repository in my project (the folder session {sid} works in) has uncommitted")
        assert_no_folder_above(text, tmp_path)
        assert sid not in manager.running and manager.is_final(sid)
    finally:
        await manager.shutdown()


async def test_answers_auto_holds_the_lease_before_the_start_and_plans_the_classic_way(tmp_path, monkeypatch):
    manager = _manager(_home(tmp_path))
    held: dict[str, bool] = {}
    real_start = Swarm.start

    async def watching_start(self, *args, **kwargs):
        held[self.session_id] = self.auto_reply_active
        await real_start(self, *args, **kwargs)

    monkeypatch.setattr(Swarm, "start", watching_start)
    clarify = {"swarm": {"clarify": True}, **NO_GIT}
    try:
        auto = (await manager.create("Auto", "Build a calculator", overrides=clarify, api={"answers": "auto"}))["id"]
        manual = (await manager.create("Manual", "Build a calculator", overrides=clarify,
                                       api={"answers": "manual"}))["id"]
        assert held == {auto: True, manual: False}

        def planning_tags(sid: str) -> list[str]:
            return manager.swarms[sid].board.list(tag="planning")[0].tags

        assert prompts.CLARIFY_TAG not in planning_tags(auto)   # no question first: the hub answers for the user
        assert prompts.CLARIFY_TAG in planning_tags(manual)
        monkeypatch.setattr(swarm_module, "_monotonic", lambda: time.monotonic() + 10 ** 6)   # past any grace
        assert manager.swarms[auto].auto_reply_active and not manager.swarms[auto].clarifying
        assert not manager.swarms[manual].auto_reply_active and manager.swarms[manual].clarifying
    finally:
        await manager.shutdown()


# ---- control -----------------------------------------------------------------------------------------------------
async def test_control_of_a_running_session(tmp_path):
    manager = _manager(_home(tmp_path))
    try:
        sid = (await manager.create("Calc", "Build a calculator", overrides=NO_GIT, api=AUTO))["id"]
        swarm = manager.swarms[sid]
        summary = await manager.control(sid, max_cost_usd=2.5, max_agents=4)
        assert summary["max_cost_usd"] == 2.5 and summary["max_agents"] == 4
        assert swarm.limits() == {"max_cost_usd": 2.5, "max_agents": 4}
        events = [r["type"] for r in swarm.db.all("SELECT type FROM events ORDER BY id")]
        assert "swarm.limits" in events
        assert (await manager.control(sid, wrap_up=True))["wrap_up"] is True and swarm.wrapping_up
        assert (await manager.control(sid, wrap_up=False))["wrap_up"] is False and not swarm.wrapping_up
        assert (await manager.control(sid, max_agents=5.0))["max_agents"] == 5   # a whole number given as a float
        for wrong in ({"max_agents": 0}, {"max_cost_usd": -1}, {"max_cost_usd": "5"}, {"max_agents": True},
                      {"max_agents": 2.5}, {"wrap_up": "yes"}, {"max_cost_usd": float("nan")}):
            with pytest.raises(SessionError) as exc:
                await manager.control(sid, **wrong)
            assert exc.value.status == 400, wrong
        assert swarm.limits() == {"max_cost_usd": 2.5, "max_agents": 5}   # nothing written by a refusal
        assert (await manager.control(sid))["max_agents"] == 5   # nothing given: nothing changes
        assert sid in manager.running and manager.swarms[sid] is swarm
    finally:
        await manager.shutdown()


async def test_control_of_a_stopped_session_writes_its_database_and_unloads_it_again(tmp_path):
    manager = _manager(_home(tmp_path))
    try:
        sid = (await manager.create("Calc", "Build a calculator", overrides=NO_GIT, api=AUTO))["id"]
        await manager.stop(sid)
        db = Database(db_path(manager, sid))   # a budget pause the run left behind
        try:
            db.set("paused", {"paused": True, "reason": "budget"})
        finally:
            db.close()
        summary = await manager.control(sid, max_cost_usd=7.0)
        assert summary["max_cost_usd"] == 7.0 and summary["paused"] is False   # the budget covers what was spent
        assert sid not in manager.swarms and sid not in manager.running   # unloaded again
        await manager.load(sid)   # viewed (the board): left loaded
        await manager.control(sid, max_agents=2)
        assert sid in manager.swarms and sid not in manager.running
        await manager.start(sid)
        assert manager.swarms[sid].limits() == {"max_cost_usd": 7.0, "max_agents": 2}
    finally:
        await manager.shutdown()


async def test_control_of_a_session_that_never_started(tmp_path):
    manager = _manager(_home(tmp_path))
    try:
        sid = (await manager.create("Calc", "Build a calculator", overrides=NO_GIT, start=False, api=AUTO))["id"]
        with pytest.raises(SessionError) as exc:
            await manager.control(sid, wrap_up=True)   # the first start's set_goal would end it
        assert exc.value.status == 409 and exc.value.code == "conflict"
        assert not db_path(manager, sid).exists()   # refused before a database was made
        summary = await manager.control(sid, max_cost_usd=3.0, max_agents=2)
        assert summary["max_cost_usd"] == 3.0 and summary["max_agents"] == 2
        assert db_path(manager, sid).exists() and sid not in manager.swarms
        await manager.start(sid)
        swarm = manager.swarms[sid]
        assert swarm.limits() == {"max_cost_usd": 3.0, "max_agents": 2} and swarm.goal()
        await manager.control(sid, wrap_up=True)
        assert swarm.wrapping_up
        old = (await manager.create("Old", "Wait", overrides=NO_GIT, start=False, api=AUTO))["id"]
        make_earlier(manager, old)
        size = db_path(manager, old).stat().st_size
        with pytest.raises(SessionError) as exc:
            await manager.control(old, max_agents=2)
        assert exc.value.status == 409 and exc.value.extra.get("earlier_version")
        assert db_path(manager, old).stat().st_size == size and old not in manager.swarms
    finally:
        await manager.shutdown()


# ---- the uid lookups ---------------------------------------------------------------------------------------------
async def test_sid_of_uid_after_a_delete_is_none_while_the_folder_number_is_reused(tmp_path):
    manager = _manager(_home(tmp_path))
    try:
        first = await manager.create("Job", "Do it", overrides=NO_GIT, start=False, api=AUTO)
        assert manager.sid_of_uid(first["uid"]) == first["id"]
        await manager.delete(first["id"])
        second = await manager.create("Job", "Do it", overrides=NO_GIT, start=False, api=AUTO)
        assert second["id"] == first["id"]   # the folder number comes back...
        assert manager.sid_of_uid(first["uid"]) is None   # ...the id of the deleted job never reaches the new one
        assert manager.sid_of_uid(second["uid"]) == second["id"]
        for wrong in (first["uid"].upper(), "x" * 32, second["uid"][:31], None, 5):
            assert manager.sid_of_uid(wrong) is None
        # a folder copied by hand shares the uid: neither session is reached by it
        shutil.copytree(manager.dir_of(second["id"]), manager.sessions_dir / "007-copy")
        assert manager.sid_of_uid(second["uid"]) is None
        shutil.rmtree(manager.sessions_dir / "007-copy")
        assert manager.sid_of_uid(second["uid"]) == second["id"]
        assert manager.sid_of_uid(manager.summary(second["id"])["uid"]) == second["id"]
    finally:
        await manager.shutdown()


async def test_the_uid_lookups_find_a_session_json_written_after_its_folder(tmp_path, monkeypatch):
    """Another process makes a session's folder (the sessions folder's mtime changes) and writes its session.json
    later (no mtime of the sessions folder changes): an index read in between still finds it."""
    manager = _manager(_home(tmp_path))
    monkeypatch.setattr(manager, "_sessions_stamp", lambda: 1)   # no change of the sessions folder is ever seen

    def late(sid: str, uid: str) -> None:
        meta = {"title": "Late", "uid": uid, "created_at": time.time(), "overrides": NO_GIT, "autostart": False,
                "via": "api", "api": {**API_GIVEN, **dict.fromkeys(API_LATER)}}
        (manager.sessions_dir / sid / "session.json").write_text(json.dumps(meta), encoding="utf-8")

    try:
        known = await manager.create("Known", "Wait", overrides=NO_GIT, start=False, api=AUTO)
        for sid in ("050-late", "051-late"):
            (manager.sessions_dir / sid).mkdir()
        assert manager.sid_of_uid("f" * 32) is None and manager.api_sids() == [known["id"]]   # the index is read
        late("050-late", "e" * 32)
        assert manager.sid_of_uid("e" * 32) == "050-late"
        late("051-late", "d" * 32)
        assert manager.api_sids() == [known["id"], "050-late", "051-late"]
        assert manager.sid_of_uid("d" * 32) == "051-late" and manager.sid_of_uid(known["uid"]) == known["id"]
    finally:
        await manager.shutdown()


async def test_api_sids_names_the_v1_sessions_only(tmp_path):
    manager = _manager(_home(tmp_path))
    try:
        auto = (await manager.create("A", "Wait", overrides=NO_GIT, start=False, api=AUTO))["id"]
        await manager.create("B", "Wait", overrides=NO_GIT, start=False)
        manual = (await manager.create("C", "Wait", overrides=NO_GIT, start=False, api={"answers": "manual"}))["id"]
        assert manager.api_sids() == sorted([auto, manual])
        await manager.delete(auto, skills="discard")
        assert manager.api_sids() == [manual]
    finally:
        await manager.shutdown()


# ---- the start mark ----------------------------------------------------------------------------------------------
async def test_a_create_start_is_starting_until_it_ends(tmp_path, monkeypatch):
    manager = _manager(_home(tmp_path))
    seen: list[tuple[bool, bool]] = []
    told: list[tuple[str, bool]] = []
    real_start = Swarm.start

    async def watching_start(self, *args, **kwargs):
        seen.append((self.session_id in manager.starting, manager.is_final(self.session_id)))
        await real_start(self, *args, **kwargs)

    monkeypatch.setattr(Swarm, "start", watching_start)
    manager.listen(lambda e: told.append((e["change"], e["session"] in manager.starting)) if e["session"] else None)
    try:
        sid = (await manager.create("Calc", "Build a calculator", overrides=NO_GIT))["id"]
        assert seen == [(True, False)] and ("created", True) in told
        assert sid not in manager.starting and sid in manager.running and not manager.is_final(sid)
    finally:
        await manager.shutdown()


async def test_a_stop_that_holds_the_lock_while_a_marked_start_waits_is_never_undone(tmp_path, monkeypatch):
    manager = _manager(_home(tmp_path))
    stopping, release = asyncio.Event(), asyncio.Event()
    real_stop = Swarm.stop

    async def slow_stop(self, *args, **kwargs):   # the agents take their time to stop
        stopping.set()
        await release.wait()
        await real_stop(self, *args, **kwargs)

    try:
        sid = (await manager.create("Calc", "Build a calculator", overrides=NO_GIT, api=AUTO))["id"]
        monkeypatch.setattr(Swarm, "stop", slow_stop)
        stop = asyncio.ensure_future(manager.stop(sid))
        await asyncio.wait_for(stopping.wait(), 1)   # the stop holds the session's lock now
        with manager.watch(sid) as changed:
            mark = manager.mark_starting(sid)   # a v1 `continue` starts it again meanwhile
            assert changed.is_set() and sid in manager.starting
        start = asyncio.ensure_future(manager.start(sid, marked=mark))
        await asyncio.sleep(0.05)
        assert not start.done() and not stop.done()
        release.set()
        await asyncio.wait_for(stop, 1)
        assert await asyncio.wait_for(start, 1) is None   # the stop took the mark: nothing started
        assert sid not in manager.running and sid not in manager.starting and manager.is_final(sid)
        assert stored(manager, sid)["autostart"] is False
    finally:
        release.set()
        await manager.shutdown()


async def test_a_stop_or_a_delete_takes_every_mark_away_and_a_start_ends_its_own_only(tmp_path, monkeypatch):
    manager = _manager(_home(tmp_path))
    try:
        sid = (await manager.create("Calc", "Build a calculator", overrides=NO_GIT, start=False, api=AUTO))["id"]
        mark = manager.mark_starting(sid)
        await manager.stop(sid)
        assert await manager.start(sid, marked=mark) is None
        assert sid not in manager.running and manager.is_final(sid)
        first, second = manager.mark_starting(sid), manager.mark_starting(sid)   # two starts on their way
        assert await manager.start(sid, marked=first) is manager.swarms[sid] and sid in manager.running
        assert sid in manager.starting and not manager.is_final(sid)   # the second one's mark stays
        manager.unmark_starting(sid, first)   # (gone already: nothing)
        assert sid in manager.starting
        assert await manager.start(sid, marked=second) is manager.swarms[sid]   # running: nothing more to do
        assert sid not in manager.starting
        await manager.stop(sid)

        async def failing_start(self, *args, **kwargs):
            raise RuntimeError("no")

        monkeypatch.setattr(Swarm, "start", failing_start)
        mark = manager.mark_starting(sid)
        with pytest.raises(RuntimeError):
            await manager.start(sid, marked=mark)
        assert sid not in manager.starting and manager.is_final(sid)
        assert await manager.start(sid, marked=mark) is None   # its mark is gone: nothing to start
        monkeypatch.undo()
        # a delete takes the marks away; the session that gets the folder name keeps its own
        stale = manager.mark_starting(sid)
        await manager.delete(sid)
        assert sid not in manager.starting
        fresh = object()
        again = await manager.create("Calc", "Build a calculator", overrides=NO_GIT, start=False, api=AUTO, mark=fresh)
        assert again["id"] == sid and sid in manager.starting
        assert await manager.start(sid, marked=stale) is None   # the deleted session's start
        assert sid in manager.starting and sid not in manager.running   # did not take the new one's mark
        assert await manager.start(sid, marked=fresh) is manager.swarms[sid] and sid not in manager.starting
    finally:
        await manager.shutdown()


async def test_a_create_with_a_mark_leaves_the_session_starting_for_its_caller(tmp_path, monkeypatch):
    manager = _manager(_home(tmp_path))
    told: list[tuple[str, bool]] = []
    manager.listen(lambda e: told.append((e["change"], e["session"] in manager.starting)) if e["session"] else None)
    try:
        mark = object()
        sid = (await manager.create("Calc", "Build a calculator", overrides=NO_GIT, start=False, api=AUTO,
                                    mark=mark))["id"]
        assert told == [("created", True)] and sid in manager.starting and not manager.is_final(sid)
        assert sid not in manager.running
        assert await manager.start(sid, marked=mark) is manager.swarms[sid]   # the caller's background start
        assert sid not in manager.starting and sid in manager.running

        def broken_summary(sid_: str) -> dict[str, Any]:
            raise SessionError("cannot read it", 500)

        monkeypatch.setattr(manager, "summary", broken_summary)
        with pytest.raises(SessionError):   # a create that fails after the mark takes it away
            await manager.create("Other", "Wait", overrides=NO_GIT, start=False, api=AUTO, mark=object())
        assert manager.starting == set()
    finally:
        await manager.shutdown()


# ---- a stopped session's lists, a member's last message ----------------------------------------------------------
async def test_read_lists_gives_a_stopped_sessions_tasks_and_members_as_the_snapshot_does(tmp_path):
    manager = _manager(_home(tmp_path))
    try:
        sid = (await manager.create("Calc", "Build a calculator", overrides=NO_GIT, api=AUTO))["id"]
        await manager.stop(sid)
        swarm = await manager.load(sid)   # viewed, as the board shows it: its board changes, nobody works
        board = swarm.board
        low = board.create(USER, "Low", priority=0)
        critical = board.create(USER, "Critical", priority=3)
        normal = board.create(USER, "Normal")
        done = board.create(USER, "Done", priority=3)
        board.update(USER, done.id, status="done")
        cancelled = board.create(USER, "Cancelled", priority=2)
        board.update(USER, cancelled.id, status="cancelled")
        hired = await swarm.hire(USER, "Tester")
        await swarm.retire(USER, hired)
        path = db_path(manager, sid)
        tasks, agents = read_lists(path)
        snapshot = swarm.snapshot()
        ids = [t["id"] for t in tasks]
        # by priority, then id; closed ones too
        assert ids.index(critical.id) < ids.index(done.id) < ids.index(cancelled.id) < ids.index(normal.id) < \
            ids.index(low.id)
        assert {t["status"] for t in tasks} >= {"done", "cancelled", "todo"}
        assert [a["name"] for a in agents][-1] == hired and agents[-1]["status"] == "retired"   # hiring order
        assert [t["id"] for t in tasks] == [t["id"] for t in snapshot["tasks"]]
        keys = ("id", "title", "status", "assignee", "parent_id", "crew", "tags", "result")
        assert [{k: t[k] for k in keys} for t in tasks] == [{k: t[k] for k in keys} for t in snapshot["tasks"]]
        assert [(a["name"], a["status"]) for a in agents] == [(a["name"], a["status"]) for a in snapshot["agents"]]
        common = {"status": "stopped", "session": "u", "goal": None, "summary": "", "jobs": [], "cost_usd": 0.0,
                  "duration_s": None}
        assert build_result(tasks=tasks, agents=agents, **common) == \
            build_result(tasks=snapshot["tasks"], agents=snapshot["agents"], **common)
        factory = swarm.db.conn.row_factory
        with swarm.db._lock:   # a loaded session's own connection
            assert read_lists(path, swarm.db.conn) == (tasks, agents)
        assert swarm.db.conn.row_factory is factory   # (its rows are left as they were)
        assert read_lists(tmp_path / "missing.db") == ([], [])
    finally:
        await manager.shutdown()


async def test_last_message_id_names_the_newest_message_of_a_sender_to_a_recipient(tmp_path):
    manager = _manager(_home(tmp_path))
    try:
        sid = (await manager.create("Calc", "Build a calculator", overrides=NO_GIT, api=AUTO))["id"]
        swarm = manager.swarms[sid]
        member = swarm.members()[0]["name"]
        assert swarm.last_message_id(member, USER) is None
        first = swarm.mail.send(member, [USER], "Which language?")
        second = swarm.mail.send(member, [USER], "And which framework?")
        assert swarm.last_message_id(member, USER) == second["id"] != first["id"]
        assert swarm.last_message_id(USER, member) is None
        reply = swarm.mail.send(USER, [member], "Python, no framework.")
        assert swarm.last_message_id(USER, member) == reply["id"]
        assert swarm.last_message_id(member, USER, since=time.time() + 60) is None
    finally:
        await manager.shutdown()


# ---- the v1 codes ------------------------------------------------------------------------------------------------
async def test_the_folder_refusals_carry_their_v1_codes(tmp_path):
    manager = _manager(_home(tmp_path))
    try:
        await manager.create("A", "Wait", overrides=NO_GIT, start=False, workspace=str(tmp_path / "shared"))
        with pytest.raises(SessionError) as exc:   # a folder inside another session's folder
            await manager.create("B", "Wait", overrides=NO_GIT, start=False, workspace=str(tmp_path / "shared" / "in"))
        assert exc.value.status == 409 and exc.value.code == FOLDER_BUSY
        reserved = (tmp_path / "reserved").resolve()
        manager._creating_in.add(reserved)   # a create in that folder is in progress
        try:
            with pytest.raises(SessionError) as exc:
                await manager.create("C", "Wait", overrides=NO_GIT, start=False, workspace=str(reserved / "x"))
            assert exc.value.status == 409 and exc.value.code == FOLDER_BUSY
        finally:
            manager._creating_in.discard(reserved)
        plan = _Plan(target=tmp_path / "t", old=tmp_path / "o", mode="moved", default=False, adopted=False,
                     git="none")
        busy = SessionError("taken meanwhile", 409, code=FOLDER_BUSY)
        for creating in (True, False):   # a change that failed on such a refusal keeps its code
            error = manager._move_error("001-a", plan, busy, [], creating=creating)
            assert error.status == 409 and error.code == FOLDER_BUSY
        assert manager._move_error("001-a", plan, busy, ["x (belongs in y)"], creating=True).code is None
    finally:
        await manager.shutdown()


@needs_git
async def test_a_repository_not_ready_for_the_agents_is_folder_not_ready(tmp_path):
    manager = _manager(_home(tmp_path))
    try:
        dirty = tmp_path / "dirty"
        dirty.mkdir()
        git(dirty, "init", "-q")
        (dirty / "a.txt").write_text("not committed\n", encoding="utf-8")
        with pytest.raises(SessionError) as exc:
            await manager.create("A", "Wait", start=False, workspace=str(dirty))
        assert exc.value.status == 409 and exc.value.code == FOLDER_NOT_READY
        inner = tmp_path / "repo" / "sub"
        inner.mkdir(parents=True)
        git(tmp_path / "repo", "init", "-q")
        (inner / "b.txt").write_text("b\n", encoding="utf-8")
        with pytest.raises(SessionError) as exc:   # inside a repository, not its own folder
            await manager.create("B", "Wait", start=False, workspace=str(inner))
        assert exc.value.status == 400 and exc.value.code == FOLDER_NOT_READY
    finally:
        await manager.shutdown()


def test_a_start_errors_code():
    assert _error_code(SessionError("busy", 409, code=FOLDER_BUSY)) == FOLDER_BUSY
    assert _error_code(SessionError("taken", 409)) == "conflict"
    assert _error_code(SessionError("gone", 404)) == "not_found"
    assert _error_code(SessionError("bad", 400)) == "bad_request"
    assert _error_code(SessionError("big", 413)) == "too_large"
    assert _error_code(SessionError("old", 409, earlier_version=True)) == "earlier_version"
    assert _error_code(SessionError("broken", 500)) == "internal"
    assert _error_code(CopyError("clash", 409, code="file_conflict")) == "file_conflict"
    assert _error_code(RuntimeError("no")) == "internal"
    assert SessionError("x", 409, code=FOLDER_BUSY).extra == {}   # the code never goes into the internal answers


# ---- v1 uploads copied by a create -------------------------------------------------------------------------------
async def test_create_copies_the_v1_uploads_it_is_given(tmp_path):
    manager = _manager(_home(tmp_path))
    uploads = manager.api_uploads
    limits = CopyLimits.from_config(manager.config)

    async def upload(name: str, data: bytes, unpack: bool = False) -> str:
        got = await uploads.receive(_chunks(data), filename=name, length=str(len(data)), limits=limits, kind="file",
                                    unpack=unpack, caller="api")
        return got["upload"]

    try:
        brief = await upload("brief.md", b"# Brief\n")
        data = await upload("data.zip", _zip({"a.csv": b"1,2\n", "notes/b.txt": b"b\n", ".gitignore": b"*\n"}),
                            unpack=True)
        sources = uploads.claim_many([brief, data], caller="api")
        created = False
        try:
            summary = await manager.create("Job", "Read the brief", overrides=NO_GIT, start=False, api=AUTO,
                                           sources=sources)
            created = True
        finally:
            uploads.settle(sources, used=created)
        workspace = manager.default_workspace(summary["id"])
        assert (workspace / "brief.md").read_bytes() == b"# Brief\n"
        assert (workspace / "a.csv").read_bytes() == b"1,2\n" and (workspace / "notes" / "b.txt").exists()
        assert not (workspace / ".gitignore").exists()   # a name the session owns, left out at the top
        assert summary["copy_result"]["kind"] == "sources" and summary["copy_result"]["files"] == 3
        assert summary["copied_from"]["files"] == 3 and stored(manager, summary["id"])["via"] == "api"
        with pytest.raises(CopyError):   # used up
            uploads.peek_many([brief], caller="api")
        # two uploads with one name: 409 file_conflict, nothing created, both tokens usable again
        one, two = await upload("report.md", b"one\n"), await upload("report.md", b"two\n")
        sources = uploads.claim_many([one, two], caller="api")
        before = manager.ids()
        try:
            with pytest.raises(SessionError) as exc:
                await manager.create("Clash", "Read", overrides=NO_GIT, start=False, sources=sources)
        finally:
            uploads.settle(sources, used=False)
        assert exc.value.status == 409 and exc.value.code == "file_conflict" and manager.ids() == before
        assert [p["token"] for p in uploads.peek_many([one, two], caller="api")] == [one, two]
        with pytest.raises(SessionError) as exc:   # not with the board's ways of copying
            await manager.create("Both", "Read", overrides=NO_GIT, start=False, sources=[],
                                 copy_from=str(tmp_path / "x"))
        assert exc.value.status == 400
    finally:
        await manager.shutdown()


# ---- import ------------------------------------------------------------------------------------------------------
async def test_an_import_drops_what_belongs_to_the_other_computer(tmp_path):
    manager = _manager(_home(tmp_path))
    try:
        folder = {"root": "jobs", "path": "acme/site", "mode": "in_place"}
        api = {**AUTO, "folder": folder}
        sid = (await manager.create("Job", "Do it", overrides=NO_GIT, start=False, api=api))["id"]
        meta = manager.meta(sid)
        meta["api"].update(start_error={"code": "internal", "text": "x"}, error="not delivered", started_at=5.0,
                           finished_at=6.0, base_commit="a" * 40)
        kept = {"answers": "auto", "stop_when_done": True, "base_commit": "a" * 40, "started_at": 5.0,
                "finished_at": 6.0}
        dropped = dict.fromkeys(("client_ref", "request_digest", "folder", "start_error", "error"))
        data, _model = manager._import_meta("017-job", meta, ImportedWorkspace("own"))
        assert data["via"] == "api" and data["autostart"] is False
        assert data["api"] == {**kept, **dropped}
        assert meta["api"]["client_ref"] == "job-1" and meta["api"]["folder"] == folder   # the caller's copy is kept
        await manager.import_settings(sid, meta)   # written through an import of the same session
        assert stored(manager, sid)["api"] == {**kept, **dropped} and stored(manager, sid)["via"] == "api"
        assert manager.summary(sid)["via"] == "api" and sid in manager.api_sids()
        plain = {k: v for k, v in manager.meta(sid).items() if k not in ("via", "api")}
        data, _model = manager._import_meta("018-job", plain, ImportedWorkspace("own"))
        assert "via" not in data and "api" not in data
    finally:
        await manager.shutdown()


def test_read_stats_of_a_v1_session_is_unchanged(tmp_path):
    """The figures of read_stats do not depend on how the session was made (via and answers are in the summary)."""
    assert "via" not in read_stats(tmp_path / "missing.db") and "answers" not in read_stats(tmp_path / "missing.db")
