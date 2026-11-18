"""The external HTTP API v1's sessions (docs/SPEC.md §21.2-§21.4, §21.7): uploads, create (the body's checks, the
team, model and limits through sessionopts, the skills, the uploads with every token given back after a refusal, a
5xx or a cancel, client_ref, max_running, the start in the background), the one resolver of `{id}` and the scope of
the API token, the summary and its states, PATCH, continue (each path, and its order against stops), stop and DELETE.
Every request goes through the launcher's app and its guard (create_launcher_app), on the test's own loop: no port,
ScriptedLLM only."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import sqlite3
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Callable

import pytest

from conftest import make_skill
from dreamteam import api_v1, copyfrom, instruct, roles, sessionopts, web
from dreamteam import sessions as sessions_module
from dreamteam import settings as st
from dreamteam.api_v1 import ApiV1, CreateRequest, ApiError, request_digest, state_of
from dreamteam.config import Config
from dreamteam.copyfrom import CopyError
from dreamteam.db import reading_file
from dreamteam.instruct import ContinueStep
from dreamteam.launcher import create_launcher_app
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.mailbox import MAX_TEXT
from dreamteam.models import USER, Status
from dreamteam.sessionopts import OptionError, overrides_from
from dreamteam.sessions import FOLDER_BUSY, SessionError, SessionManager, session_uid
from dreamteam.swarm import Swarm
from test_api_sessions import AUTO, make_earlier
from test_api_v1_guard import _ask, api_token, hub_token  # noqa: F401 - fixtures
from test_hub_auth import API_TOKEN, TOKEN

LOCAL = ("127.0.0.1", 50000)
UID = "0123456789abcdef0123456789abcdef"


def _home(tmp_path: Path, **sections: Any) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    raw = {
        "swarm": {"name": "Home", "language": "en", "start_agents": 1, "max_agents": 3, "max_cost_usd": 0},
        "agents": {"stagger_seconds": 0, "idle_nudge_seconds": 60},
        "workspace": {"git": False},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0},
                   "second": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
        "teams": [{"name": "Docs", "roles": ["writer", "tester"]}],
        "api": {"max_running": 0},   # (several creates in a row; test_max_running_... sets its own)
    }
    for name, values in sections.items():
        raw[name] = {**raw.get(name, {}), **values}
    st.write_raw(home, raw)
    return home


def _manager(home: Path) -> SessionManager:
    def factory(profile, agent):
        return ScriptedLLM(lambda system, messages, tools: "Nothing to do right now.", profile)

    return SessionManager(home, llm_factory=factory)


class Hub:
    """The launcher's app of a manager, called on the test's loop as the server would call it."""

    def __init__(self, manager: SessionManager, host: str = "127.0.0.1"):
        self.manager = manager
        self.app = create_launcher_app(manager, host)
        self.api: ApiV1 = self.app.api

    async def call(self, method: str, path: str, *, body: Any = None, raw: bytes | None = None, query: str = "",
                   token: str = API_TOKEN, content_type: str | None = None) -> tuple[int, Any]:
        headers = {"authorization": f"Bearer {token}"} if token else {}
        data = raw if raw is not None else (b"" if body is None else json.dumps(body).encode("utf-8"))
        kind = "application/json" if content_type is None else content_type   # ("": no Content-Type at all)
        if method in ("POST", "PATCH") and kind:
            headers["content-type"] = kind
        status, _, answer = await _ask(self.app, path=path, method=method, headers=headers, query=query, body=data,
                                       client=LOCAL)
        return status, (json.loads(answer.decode("utf-8")) if answer else None)

    async def create(self, body: dict[str, Any], **kwargs: Any) -> tuple[int, Any]:
        return await self.call("POST", "/api/v1/sessions", body=body, **kwargs)

    async def upload(self, name: str, data: bytes, *, unpack: bool = False, token: str = API_TOKEN,
                     content_type: str = "application/octet-stream") -> tuple[int, Any]:
        query = f"filename={name}" + ("&unpack=true" if unpack else "")
        return await self.call("POST", "/api/v1/uploads", raw=data, query=query, token=token,
                               content_type=content_type)

    async def settled(self) -> None:
        """Every background start has ended."""
        while self.api._tasks:
            await asyncio.gather(*list(self.api._tasks), return_exceptions=True)

    async def close(self) -> None:
        await self.api.aclose()
        await self.manager.shutdown()


@pytest.fixture
def no_start(monkeypatch):
    """manager.start takes nothing but the start mark: the session never runs (its state stays `created`)."""
    def install(manager: SessionManager, hold: asyncio.Event | None = None) -> list[str]:
        started: list[str] = []

        async def start(sid: str, *, marked: object | None = None, **kwargs: Any) -> None:
            try:
                if hold is not None:
                    await hold.wait()
                started.append(sid)
            finally:
                if marked is not None:
                    manager.unmark_starting(sid, marked)

        monkeypatch.setattr(manager, "start", start)
        return started
    return install


def _zip(files: dict[str, bytes]) -> bytes:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return data.getvalue()


def _no_path(answer: Any, *paths: Path) -> None:
    text = json.dumps(answer, ensure_ascii=False)
    for path in paths:
        for spelling in (str(path), path.as_posix(), json.dumps(str(path))[1:-1]):
            assert spelling not in text, spelling


async def _until(condition: Callable[[], bool], what: str = "the condition", seconds: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while not condition():
        if loop.time() > deadline:
            raise AssertionError(f"never happened: {what}")
        await asyncio.sleep(0.01)


def _from_user(swarm: Swarm) -> list[tuple[list[str], str, Any]]:
    """The user's messages of a loaded session: (recipients, text, reply_to), oldest first."""
    return [(json.loads(row["recipients"]), row["text"], row["reply_to"])
            for row in swarm.db.all("SELECT recipients, text, reply_to FROM messages WHERE sender = ? ORDER BY id",
                                    (USER,))]


def _running(hub: Hub, sid: str) -> bool:
    return sid in hub.manager.running and hub.manager.swarms.get(sid) is not None


MANUAL = {"answers": "manual", "stop_when_done": False}   # (a session that keeps running, nobody answering for it)


# ---------------------------------------------------------------------------------------------- create and start
async def test_a_create_with_a_goal_only_starts_the_session_in_the_background(tmp_path, api_token):
    hub = Hub(_manager(_home(tmp_path)))
    try:
        body = {"goal": "Write a haiku\nAbout spring, in English."}
        status, answer = await hub.create(body)
        assert status == 202, answer
        session = answer["session"]
        assert list(session) == ["id", "name", "title", "state", "final", "error", "goal", "language", "model",
                                 "cost_usd", "max_cost_usd", "max_agents", "created_at", "started_at", "finished_at",
                                 "agents", "tasks", "requests", "answers", "stop_when_done", "client_ref", "via",
                                 "folder", "board_url", "earlier_version"]
        sid, uid = session["name"], session["id"]
        assert len(uid) == 32 and session_uid(hub.manager.meta(sid)) == uid
        assert session["state"] == "starting" and session["final"] is False and session["error"] is None
        assert session["title"] == "Write a haiku" and session["via"] == "api" and session["answers"] == "auto"
        assert session["goal"] == {"title": "Write a haiku", "description": "About spring, in English.",
                                   "status": "pending", "summary": ""}
        assert session["stop_when_done"] is True and session["client_ref"] is None and session["folder"] is None
        assert session["started_at"] is None and session["earlier_version"] is False and session["model"] == "default"
        _no_path(answer, tmp_path)
        stored = hub.manager.meta(sid)["api"]
        assert stored["request_digest"] == request_digest(body) and stored["client_ref"] is None
        await hub.settled()
        status, answer = await hub.call("GET", f"/api/v1/sessions/{uid}")
        assert status == 200 and answer["session"]["state"] == "running", answer
        session = answer["session"]
        assert session["goal"]["status"] == "active" and isinstance(session["started_at"], float)
        assert session["finished_at"] is None and session["agents"] == 1 and not session["final"]
        status, answer = await hub.call("POST", f"/api/v1/sessions/{uid}/stop")
        assert status == 200 and answer["session"]["state"] == "stopped" and answer["session"]["final"] is True
        assert isinstance(answer["session"]["finished_at"], float) and sid not in hub.manager.running
    finally:
        await hub.close()


async def test_a_failed_start_is_failed_with_its_error(tmp_path, api_token, monkeypatch, caplog):
    async def failing(self, *args, **kwargs):
        raise RuntimeError("no model answers")

    monkeypatch.setattr(Swarm, "start", failing)
    hub = Hub(_manager(_home(tmp_path)))
    try:
        with caplog.at_level(logging.INFO):
            status, answer = await hub.create({"goal": "Do it"})
            assert status == 202
            await hub.settled()
        status, answer = await hub.call("GET", f"/api/v1/sessions/{answer['session']['id']}")
        session = answer["session"]
        assert session["state"] == "failed" and session["final"] is True
        assert session["error"] == "RuntimeError: no model answers" and isinstance(session["finished_at"], float)
        # the manager logged it with its traceback; the background task, at information only
        mine = [record for record in caplog.records if record.name == api_v1.__name__]
        assert [record.levelno for record in mine] == [logging.INFO], mine
        assert any(record.name == sessions_module.__name__ and record.levelno == logging.ERROR
                   for record in caplog.records)
    finally:
        await hub.close()


@pytest.mark.parametrize("body, code", [
    ({}, "bad_request"),
    ({"goal": "  \n "}, "bad_request"),
    ({"goal": 5}, "bad_request"),
    ({"goal": "G", "colour": "red"}, "unknown_field"),
    ({"goal": "G", "folder": {"root": "jobs"}}, "unknown_folder"),   # (no root listed: the server folders, §21.10)
    ({"goal": "G", "folder": {"root": "jobs", "colour": "red"}}, "unknown_field"),
    ({"goal": "G", "folder": "jobs"}, "bad_request"),
    ({"goal": "G", "title": "two\nlines"}, "bad_request"),
    ({"goal": "G", "title": "x" * 201}, "bad_request"),
    ({"goal": "G", "title": "   "}, "bad_request"),
    ({"goal": "G", "title": "a\x1b[31mb"}, "bad_request"),   # (the title reaches the owner's terminal)
    ({"goal": "G", "title": "a\x00b"}, "bad_request"),
    ({"goal": "G", "title": "a\tb"}, "bad_request"),
    ({"goal": "G", "title": "a‮b"}, "bad_request"),       # a bidirectional override
    ({"goal": "Fix \x1b[2Jit\nmore"}, "bad_request"),           # the goal's first line is the title
    ({"goal": "G", "description": ["x"]}, "bad_request"),
    ({"goal": "G", "answers": "sometimes"}, "bad_request"),
    ({"goal": "G", "clarify": True}, "bad_request"),
    ({"goal": "G", "clarify": True, "answers": "auto"}, "bad_request"),
    ({"goal": "G", "clarify": "yes", "answers": "manual"}, "bad_request"),
    ({"goal": "G", "stop_when_done": "no"}, "bad_request"),
    ({"goal": "G", "client_ref": ""}, "bad_request"),
    ({"goal": "G", "client_ref": "x" * 129}, "bad_request"),
    ({"goal": "G", "client_ref": "job\n1"}, "bad_request"),
    ({"goal": "G", "files": "token"}, "bad_request"),
    ({"goal": "G", "files": ["a" * 24] * 21}, "bad_request"),
    ({"goal": "G", "files": ["not a token"]}, "bad_request"),
    ({"goal": "G", "skills": "demo"}, "bad_request"),
    ({"goal": "G", "skills": ["nope"]}, "unknown_skill"),
    ({"goal": "G", "skills": ["Not A Name"]}, "unknown_skill"),
    ({"goal": "G", "start_agents": 0}, "bad_request"),
    ({"goal": "G", "start_agents": 4}, "bad_request"),       # over the settings' max_agents (3)
    ({"goal": "G", "start_agents": 2, "max_agents": 1}, "bad_request"),
    ({"goal": "G", "max_agents": 0}, "bad_request"),
    ({"goal": "G", "start_agents": "2"}, "bad_request"),
    ({"goal": "G", "start_agents": True}, "bad_request"),
    ({"goal": "G", "team": "Docs", "start_agents": 2}, "bad_request"),
    ({"goal": "G", "team": "Nope"}, "unknown_team"),
    ({"goal": "G", "team": ["developer", "astronaut"]}, "unknown_role"),
    ({"goal": "G", "team": ["docs"]}, "unknown_role"),        # a saved team's name inside a list
    ({"goal": "G", "team": []}, "bad_request"),
    ({"goal": "G", "team": ["developer"] * 4}, "bad_request"),   # the team's size over max_agents
    ({"goal": "G", "team": 3}, "bad_request"),
    ({"goal": "G", "model": "nope"}, "unknown_model"),
    ({"goal": "G", "language": "de"}, "bad_request"),
    ({"goal": "G", "network": "maybe"}, "bad_request"),
    ({"goal": "G", "max_cost_usd": -1}, "bad_request"),
    ({"goal": "G", "max_cost_usd": "5"}, "bad_request"),
    ({"goal": "G", "max_cost_usd": 10**400}, "bad_request"),   # (no float holds it: never a 500)
])
async def test_a_create_refused_by_its_body_makes_nothing(tmp_path, api_token, body, code):
    hub = Hub(_manager(_home(tmp_path)))
    try:
        status, answer = await hub.create(body)
        assert status == api_v1.CODES[code] and answer["code"] == code, answer
        assert hub.manager.ids() == [] and not hub.api._refs and hub.api._reserved == 0
    finally:
        await hub.close()


async def test_refusals_name_what_a_client_may_choose(tmp_path, api_token):
    hub = Hub(_manager(_home(tmp_path)))
    try:
        assert "Saved teams: Docs." in (await hub.create({"goal": "G", "team": "Nope"}))[1]["error"]
        assert "Profiles: default, second." in (await hub.create({"goal": "G", "model": "nope"}))[1]["error"]
        refused = (await hub.create({"goal": "G", "team": ["developer", "astronaut", "astronaut"]}))[1]["error"]
        assert refused.startswith("Unknown role: astronaut.")
        assert "'docs' is a saved team" in (await hub.create({"goal": "G", "team": ["docs"]}))[1]["error"]
        assert "'colour'" in (await hub.create({"goal": "G", "colour": 1}))[1]["error"]
    finally:
        await hub.close()


async def test_a_title_and_a_description_combine_with_the_goal(tmp_path, api_token, no_start):
    hub = Hub(_manager(_home(tmp_path)))
    no_start(hub.manager)
    try:
        cases = [
            ({"goal": "Build X\nIn Go."}, "Build X", {"title": "Build X", "description": "In Go."}),
            ({"goal": "Build X\nIn Go.", "title": "Acme job"}, "Acme job",
             {"title": "Acme job", "description": "Build X\nIn Go."}),
            ({"goal": "Build  X\nIn Go.", "title": "Build X ", "description": "Tests too."}, "Build X",
             {"title": "Build X", "description": "In Go.\n\nTests too."}),
            ({"goal": "Build X", "description": "Tests too."}, "Build X",
             {"title": "Build X", "description": "Tests too."}),
            # the joiners of ordinary text pass (an emoji sequence); a tab in the goal's first line is a space there,
            # and a control character below it stays the agents' text
            ({"goal": "Ship\tthe 👩‍💻 site\nRaw \x1b[1m output."}, "Ship the 👩‍💻 site",
             {"title": "Ship the 👩‍💻 site", "description": "Raw \x1b[1m output."}),
        ]
        for body, title, goal in cases:
            status, answer = await hub.create(body)
            assert status == 202, answer
            meta = hub.manager.meta(answer["session"]["name"])
            assert meta["title"] == title and answer["session"]["title"] == title
            assert {key: meta["pending_goal"][key] for key in ("title", "description")} == goal, body
            assert answer["session"]["goal"]["title"] == goal["title"]
    finally:
        await hub.close()


async def test_the_team_model_and_limits_become_the_sessions_overrides(tmp_path, api_token, no_start):
    hub = Hub(_manager(_home(tmp_path)))
    no_start(hub.manager)
    try:
        status, answer = await hub.create({"goal": "G", "team": ["Architect", " developer "], "model": "second",
                                           "max_cost_usd": 2, "language": "ru", "network": "off", "max_agents": 3,
                                           "clarify": False})
        assert status == 202, answer
        overrides = hub.manager.meta(answer["session"]["name"])["overrides"]
        assert overrides == {"swarm": {"max_agents": 3, "starting_roles": ["Architect", "developer"],
                                       "start_agents": 2, "max_cost_usd": 2.0, "language": "ru", "network": "off",
                                       "clarify": False},
                             "agents": {"default_model": "second"}}
        assert answer["session"]["model"] == "second" and answer["session"]["language"] == "ru"
        assert answer["session"]["max_cost_usd"] == 2.0
        status, answer = await hub.create({"goal": "G", "team": "docs", "answers": "manual", "clarify": True,
                                           "stop_when_done": False})
        assert status == 202, answer
        meta = hub.manager.meta(answer["session"]["name"])
        assert meta["overrides"] == {"swarm": {"starting_roles": ["writer", "tester"], "start_agents": 2,
                                               "clarify": True}}
        assert meta["api"]["answers"] == "manual" and meta["api"]["stop_when_done"] is False
        assert answer["session"]["answers"] == "manual" and answer["session"]["stop_when_done"] is False
    finally:
        await hub.close()


async def test_skills_come_from_the_library_or_the_host_library_only(tmp_path, api_token, no_start, monkeypatch):
    library = Path(os.environ["DREAMTEAM_SKILLS_DIR"])
    make_skill(library / "demo", "demo", "The library's demo.")
    host = tmp_path / "host-skills"
    make_skill(host / "hosted", "hosted", "Only on the host.")
    monkeypatch.setenv("DREAMTEAM_HOST_SKILLS_DIR", str(host))
    claude = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "skills"
    make_skill(claude / "mine", "mine", "A Claude Code skill of the user's.")
    hub = Hub(_manager(_home(tmp_path, skills={"max_attached": 2})))
    no_start(hub.manager)
    try:
        status, answer = await hub.create({"goal": "G", "skills": ["demo", "hosted", "demo"]})
        assert status == 202, answer
        attached = {skill.name for skill in hub.manager._attached_skills(answer["session"]["name"])}
        assert attached == {"demo", "hosted"}
        assert (await hub.create({"goal": "G", "skills": ["mine"]}))[1]["code"] == "unknown_skill"   # never Claude's
        assert (await hub.create({"goal": "G", "skills": [str(library / "demo")]}))[1]["code"] == "unknown_skill"
        refused = await hub.create({"goal": "G", "skills": ["demo", "hosted", "x"]})
        assert refused[1]["code"] == "bad_request" and "max_attached" in refused[1]["error"]
    finally:
        await hub.close()


# ---------------------------------------------------------------------------------------------- uploads
async def test_uploads_keep_any_file_under_a_checked_name(tmp_path, api_token, hub_token):
    hub = Hub(_manager(_home(tmp_path)))
    try:
        status, answer = await hub.upload("brief.md", b"# Brief\n")
        assert status == 201 and set(answer) == {"upload", "filename", "bytes", "unpack", "expires_in"}
        assert answer["filename"] == "brief.md" and answer["bytes"] == 8 and answer["unpack"] is False
        status, answer = await hub.upload("data.zip", _zip({"a.csv": b"1\n"}), unpack=True,
                                          content_type="application/zip")
        assert status == 201 and answer["unpack"] is True
        for name in (".git", ".gitignore", "a%2Fb", "CON", "x%E2%80%AEtxt.exe", ".scratch", "GIT~1", "mcp"):
            status, answer = await hub.upload(name, b"x")
            assert status == 400 and answer["code"] == "bad_name", name
        status, answer = await hub.call("POST", "/api/v1/uploads", raw=b"x", query="filename=a.txt&unpack=yes",
                                        content_type="application/octet-stream")
        assert status == 400 and answer["code"] == "bad_request"
        status, answer = await hub.upload("a.txt", b"not a zip", unpack=True)
        assert status == 400 and answer["code"] == "bad_request"
        status, answer = await hub.call("POST", "/api/v1/uploads", raw=b"x", query="filename=a.txt&mode=x",
                                        content_type="application/octet-stream")
        assert status == 400 and answer["code"] == "unknown_field"
        status, answer = await hub.call("POST", "/api/v1/uploads", raw=b"x", query="filename=a.txt&filename=b.txt",
                                        content_type="application/octet-stream")
        assert status == 400 and answer["code"] == "bad_request"
        for kind in ("application/json", "text/plain"):
            status, answer = await hub.upload("a.txt", b"{}", content_type=kind)
            assert status == 415 and answer["code"] == "unsupported_media_type", kind
        status, answer = await hub.upload("brief.md", b"x", token=TOKEN)   # the owner uploads too
        assert status == 201
        stored = json.loads((hub.manager.api_uploads.root / f"{answer['upload']}.json").read_text(encoding="utf-8"))
        assert stored["caller"] == "owner"
    finally:
        await hub.close()


async def test_the_uploads_cap_and_size_limit(tmp_path, api_token):
    hub = Hub(_manager(_home(tmp_path, workspace={"git": False, "copy_max_mb": 1}, api={"uploads_pending": 2})))
    try:
        status, answer = await hub.upload("big.bin", b"x" * (1024 * 1024 + 1))
        assert status == 413 and answer["code"] == "too_large"
        assert (await hub.upload("a.txt", b"a"))[0] == 201 and (await hub.upload("b.txt", b"b"))[0] == 201
        status, answer = await hub.upload("c.txt", b"c")
        assert status == 429 and answer["code"] == "too_many_uploads"
    finally:
        await hub.close()


async def test_files_land_in_the_session_folder_and_their_tokens_are_used_up(tmp_path, api_token, no_start):
    hub = Hub(_manager(_home(tmp_path)))
    no_start(hub.manager)
    try:
        brief = (await hub.upload("brief.md", b"# Brief\n"))[1]["upload"]
        data = (await hub.upload("data.zip", _zip({"a.csv": b"1,2\n", "notes/b.txt": b"b\n", ".gitignore": b"*\n"}),
                                 unpack=True, content_type="application/zip"))[1]["upload"]
        status, answer = await hub.create({"goal": "Read the brief", "files": [brief, data]})
        assert status == 202, answer
        workspace = hub.manager.default_workspace(answer["session"]["name"])
        assert (workspace / "brief.md").read_bytes() == b"# Brief\n" and (workspace / "a.csv").exists()
        assert (workspace / "notes" / "b.txt").exists() and not (workspace / ".gitignore").exists()
        _no_path(answer, tmp_path)
        status, answer = await hub.create({"goal": "Again", "files": [brief]})
        assert status == 404 and answer["code"] == "not_found"   # used up
        assert not copyfrom._IN_USE
    finally:
        await hub.close()


async def test_a_refused_create_leaves_every_token_usable(tmp_path, api_token, hub_token, no_start, monkeypatch):
    hub = Hub(_manager(_home(tmp_path, workspace={"git": False, "copy_max_mb": 1})))
    no_start(hub.manager)
    uploads = hub.manager.api_uploads

    def usable(*tokens: str, caller: str = "api") -> bool:
        return [meta["token"] for meta in uploads.peek_many(list(tokens), caller=caller)] == list(tokens)

    try:
        one = (await hub.upload("report.md", b"one\n"))[1]["upload"]
        two = (await hub.upload("report.md", b"two\n"))[1]["upload"]
        status, answer = await hub.create({"goal": "G", "files": [one, two]})   # a name both have
        assert status == 409 and answer["code"] == "file_conflict" and usable(one, two)
        status, answer = await hub.create({"goal": "G", "files": [one, "A" * 24]})   # an unknown token
        assert status == 404 and answer["code"] == "not_found" and usable(one)
        status, answer = await hub.create({"goal": "G", "files": [one, one]})
        assert status == 400 and usable(one)
        big = [(await hub.upload(f"part{n}.bin", b"x" * 600_000))[1]["upload"] for n in range(2)]
        status, answer = await hub.create({"goal": "G", "files": big})   # the sum is over copy_max_mb
        assert status == 413 and answer["code"] == "too_large" and usable(*big)
        owners = (await hub.upload("theirs.md", b"x", token=TOKEN))[1]["upload"]
        status, answer = await hub.create({"goal": "G", "files": [owners]})   # the other caller's
        assert status == 404 and answer["code"] == "not_found" and usable(owners, caller="owner")
        status, answer = await hub.create({"goal": "G", "files": [one]}, token=TOKEN)
        assert status == 404 and usable(one)

        def broken(*args, **kwargs):
            raise CopyError(f"Cannot copy into {tmp_path / 'staging'}: the disk is full.", 500)

        monkeypatch.setattr(sessions_module, "copy_sources", broken)
        status, answer = await hub.create({"goal": "G", "files": [one]})
        assert status == 500 and answer["code"] == "internal" and usable(one)
        assert str(tmp_path) not in answer["error"]   # (redacted for the API caller)
        assert hub.manager.ids() == [] and not copyfrom._IN_USE
    finally:
        await hub.close()


async def test_a_cancelled_create_gives_its_uploads_back(tmp_path, api_token, no_start, monkeypatch):
    hub = Hub(_manager(_home(tmp_path)))
    no_start(hub.manager)
    copying = threading.Event()

    def slow(sources, staging, limits, check=None):
        copying.set()
        while True:   # until the create is cancelled (its check raises CopyCancelled)
            check()
            threading.Event().wait(0.01)

    monkeypatch.setattr(sessions_module, "copy_sources", slow)
    try:
        token = (await hub.upload("brief.md", b"x"))[1]["upload"]
        request = asyncio.create_task(hub.create({"goal": "G", "files": [token]}))
        while not copying.is_set():
            await asyncio.sleep(0.01)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert [m["token"] for m in hub.manager.api_uploads.peek_many([token], caller="api")] == [token]
        assert not copyfrom._IN_USE and hub.manager.ids() == [] and hub.api._reserved == 0 and not hub.api._refs
    finally:
        await hub.close()


# ---------------------------------------------------------------------------------------------- client_ref
async def test_client_ref_makes_one_session_per_job(tmp_path, api_token, no_start):
    hub = Hub(_manager(_home(tmp_path)))
    no_start(hub.manager)
    try:
        body = {"goal": "Translate the brief", "client_ref": "job-7"}
        status, first = await hub.create(body)
        assert status == 202 and first["session"]["client_ref"] == "job-7"
        status, again = await hub.create(dict(reversed(list(body.items()))))   # (the same body: keys in any order)
        assert status == 200 and again["session"]["id"] == first["session"]["id"]
        status, other = await hub.create({**body, "goal": "Another job"})
        assert status == 409 and other["code"] == "conflict"
        assert len(hub.manager.ids()) == 1
        # a field that is null is not given, and 5.0 is 5: the same request spelled another way is a repeat
        status, again = await hub.create({**body, "model": None, "skills": None})
        assert status == 200 and again["session"]["id"] == first["session"]["id"]
        priced = {"goal": "Price it", "max_cost_usd": 5, "client_ref": "job-9"}
        status, made = await hub.create(priced)
        assert status == 202
        status, again = await hub.create({**priced, "max_cost_usd": 5.0})
        assert status == 200 and again["session"]["id"] == made["session"]["id"]
        assert request_digest({"goal": "G", "title": None}) == request_digest({"goal": "G"})
        assert request_digest({"goal": "G", "max_cost_usd": 1.5}) != request_digest({"goal": "G", "max_cost_usd": 1})
        assert (await hub.create({"goal": "Translate the brief", "client_ref": "job-8"}))[0] == 202
        # the manager's index finds them (no session.json read per session at each create)
        assert hub.manager.sid_of_client_ref("job-7") == first["session"]["name"]
        assert hub.manager.sid_of_client_ref("job-0") is None and hub.manager.sid_of_client_ref(7) is None
        uid = first["session"]["id"]
        assert (await hub.call("DELETE", f"/api/v1/sessions/{uid}", query=f"confirm={uid}"))[0] == 200
        assert hub.manager.sid_of_client_ref("job-7") is None
        status, fresh = await hub.create(body)   # the job is new again once its session is gone
        assert status == 202 and fresh["session"]["id"] != uid
    finally:
        await hub.close()


async def test_a_retry_during_a_slow_create_waits_for_it(tmp_path, api_token, no_start, monkeypatch):
    """A retry with the same client_ref and body waits for the create in flight: a repeat (200) when it made the
    session, the create itself when it failed; another body is 409 at once."""
    hub = Hub(_manager(_home(tmp_path)))
    no_start(hub.manager)
    real = sessions_module.copy_sources
    gates = [threading.Event(), threading.Event()]
    entered: list[int] = []

    def slow(sources, staging, limits, check=None):
        n = len(entered)
        entered.append(n)
        if n < len(gates):
            while not gates[n].wait(0.01):
                check()
            if n == 0:
                raise CopyError("The disk is full.", 500)
        return real(sources, staging, limits, check)

    monkeypatch.setattr(sessions_module, "copy_sources", slow)

    async def until(condition) -> None:
        for _ in range(500):
            if condition():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("never happened")

    try:
        token = (await hub.upload("brief.md", b"x"))[1]["upload"]
        body = {"goal": "G", "files": [token], "client_ref": "job-1"}
        first = asyncio.create_task(hub.create(body))
        await until(lambda: entered == [0])
        retry = asyncio.create_task(hub.create(body))
        status, answer = await hub.create({**body, "goal": "H"})
        assert status == 409 and answer["code"] == "conflict"   # at once, while the first is in flight
        await asyncio.sleep(0.05)
        assert not retry.done() and entered == [0]   # (it waits: it has not even checked the token)
        gates[0].set()   # the first fails: the retry goes on with the token given back
        status, answer = await first
        assert status == 500
        await until(lambda: entered == [0, 1])
        third = asyncio.create_task(hub.create(body))
        await asyncio.sleep(0.05)
        assert not third.done()
        gates[1].set()
        status, made = await retry
        assert status == 202, made
        status, repeat = await third
        assert status == 200 and repeat["session"]["id"] == made["session"]["id"]
        assert len(hub.manager.ids()) == 1 and not hub.api._refs and not copyfrom._IN_USE
    finally:
        for gate in gates:
            gate.set()
        await hub.close()


# ---------------------------------------------------------------------------------------------- max_running
async def test_max_running_counts_the_starting_and_running_sessions_of_v1(tmp_path, api_token, no_start):
    hub = Hub(_manager(_home(tmp_path, api={"max_running": 1})))
    hold = asyncio.Event()
    started = no_start(hub.manager, hold)
    try:
        assert (await hub.create({"goal": "One"}))[0] == 202   # starting until `hold`
        status, answer = await hub.create({"goal": "Two"})
        assert status == 429 and answer["code"] == "too_many_sessions" and len(hub.manager.ids()) == 1
        board = (await hub.manager.create("Board", "Mine", start=False))["id"]
        hub.manager.mark_starting(board)   # a session of the board never counts
        hold.set()
        await hub.settled()
        assert len(started) == 1
        assert (await hub.create({"goal": "Two"}))[0] == 202
        await hub.settled()
        sid = hub.manager.api_sids()[0]
        hub.manager.running.add(sid)   # (as a running session counts)
        assert (await hub.create({"goal": "Three"}))[1]["code"] == "too_many_sessions"
        hub.manager.running.discard(sid)
        raw = st.read_raw(hub.manager.home)
        raw["api"].update(max_running=0, uploads_pending=31)   # no limit, picked up at the next request
        st.write_raw(hub.manager.home, raw)
        hub.manager.running.add(sid)
        hold.clear()
        for n in range(3):
            assert (await hub.create({"goal": f"Many {n}"}))[0] == 202
        assert hub.api.running_count() == 4
        hub.manager.running.discard(sid)
        hold.set()
    finally:
        hold.set()
        await hub.close()


# ---------------------------------------------------------------------------------------------- the resolver and scope
async def test_the_api_token_sees_only_the_sessions_of_v1(tmp_path, api_token, hub_token, no_start):
    hub = Hub(_manager(_home(tmp_path)))
    no_start(hub.manager)
    try:
        board = (await hub.manager.create("Board", "The owner's own", start=False))["id"]
        board_uid = session_uid(hub.manager.meta(board))
        status, made = await hub.create({"goal": "Through the API"})
        mine = made["session"]["id"]
        status, answer = await hub.call("GET", "/api/v1/sessions")
        assert status == 200 and [s["id"] for s in answer["sessions"]] == [mine]
        for method, path, body in (("GET", f"/api/v1/sessions/{board_uid}", None),
                                   ("PATCH", f"/api/v1/sessions/{board_uid}", {"max_agents": 2}),
                                   ("POST", f"/api/v1/sessions/{board_uid}/continue", {"text": "Go on."}),
                                   ("POST", f"/api/v1/sessions/{board_uid}/stop", {}),
                                   ("DELETE", f"/api/v1/sessions/{board_uid}", None)):
            query = f"confirm={board_uid}" if method == "DELETE" else ""
            status, answer = await hub.call(method, path, body=body, query=query)
            assert status == 404 and answer == {"error": api_v1.NO_SESSION_TEXT, "code": "not_found"}, path
        assert hub.manager.exists(board)
        status, answer = await hub.call("GET", "/api/v1/sessions", token=TOKEN)
        assert status == 200 and [s["id"] for s in answer["sessions"]] == [mine, board_uid]   # newest first
        status, answer = await hub.call("GET", f"/api/v1/sessions/{board_uid}", token=TOKEN)
        session = answer["session"]
        assert status == 200 and session["via"] is None and session["answers"] is None
        assert session["state"] == "created" and session["client_ref"] is None and session["started_at"] is None
        for wrong in ("nope", mine.upper(), UID, board):
            assert (await hub.call("GET", f"/api/v1/sessions/{wrong}", token=TOKEN))[0] == 404, wrong
    finally:
        await hub.close()


async def test_a_deleted_sessions_id_never_reaches_the_one_that_took_its_number(tmp_path, api_token, no_start):
    hub = Hub(_manager(_home(tmp_path)))
    no_start(hub.manager)
    try:
        first = (await hub.create({"goal": "First job"}))[1]["session"]
        path = f"/api/v1/sessions/{first['id']}"
        for query in ("", f"confirm={first['name']}", "confirm=x"):
            status, answer = await hub.call("DELETE", path, query=query)
            assert status == 400 and answer["code"] == "bad_request", query
        status, answer = await hub.call("DELETE", path, query=f"confirm={first['id']}&skills=save")
        assert status == 400 and answer["code"] == "unknown_field"
        status, answer = await hub.call("DELETE", path, query=f"confirm={first['id']}")
        assert status == 200 and answer == {"ok": True} and hub.manager.ids() == []
        second = (await hub.create({"goal": "Second job"}))[1]["session"]
        assert second["name"].split("-")[0] == first["name"].split("-")[0]   # the folder number came back
        assert (await hub.call("GET", path))[0] == 404
        assert (await hub.call("DELETE", path, query=f"confirm={first['id']}"))[0] == 404
        assert (await hub.call("GET", f"/api/v1/sessions/{second['id']}"))[0] == 200
    finally:
        await hub.close()


async def test_a_session_of_an_earlier_version_is_listed_stopped_and_refused(tmp_path, api_token):
    hub = Hub(_manager(_home(tmp_path)))
    try:
        sid = (await hub.manager.create("Old", "Do it", start=False, api=AUTO))["id"]
        make_earlier(hub.manager, sid)
        uid = session_uid(hub.manager.meta(sid))
        status, answer = await hub.call("GET", "/api/v1/sessions")
        (session,) = answer["sessions"]
        assert session["id"] == uid and session["earlier_version"] is True
        assert session["state"] == "stopped" and session["final"] is True
        status, answer = await hub.call("POST", f"/api/v1/sessions/{uid}/stop")
        assert status == 409 and answer["code"] == "earlier_version"
        for method, path, body in (("PATCH", f"/api/v1/sessions/{uid}", {"max_agents": 2}),
                                   ("POST", f"/api/v1/sessions/{uid}/continue", {"text": "Go on."})):
            status, answer = await hub.call(method, path, body=body)
            assert status == 409 and answer["code"] == "earlier_version", method
        assert sid not in hub.manager.starting and sid not in hub.manager.swarms
        status, answer = await hub.call("DELETE", f"/api/v1/sessions/{uid}", query=f"confirm={uid}")
        assert status == 200 and hub.manager.ids() == []
    finally:
        await hub.close()


async def test_the_list_filters_by_state_and_takes_a_limit(tmp_path, api_token, no_start):
    hub = Hub(_manager(_home(tmp_path)))
    no_start(hub.manager)
    try:
        made = [(await hub.create({"goal": f"Job {n}"}))[1]["session"]["id"] for n in range(3)]
        await hub.settled()   # (no longer `starting`)
        failed = hub.manager.api_sids()[0]
        hub.manager._api_write(failed, start_error={"code": "internal", "text": "broke"})
        status, answer = await hub.call("GET", "/api/v1/sessions", query="limit=2")
        assert [s["id"] for s in answer["sessions"]] == made[::-1][:2]
        status, answer = await hub.call("GET", "/api/v1/sessions", query="state=failed")
        assert [(s["name"], s["error"]) for s in answer["sessions"]] == [(failed, "broke")]
        status, answer = await hub.call("GET", "/api/v1/sessions", query="state=created&limit=1")
        assert [s["state"] for s in answer["sessions"]] == ["created"] and answer["sessions"][0]["id"] == made[2]
        for query, code in (("state=nope", "bad_request"), ("limit=0", "bad_request"), ("limit=201", "bad_request"),
                            ("limit=x", "bad_request"), ("limit=1&limit=2", "bad_request"),
                            ("order=asc", "unknown_field")):
            status, answer = await hub.call("GET", "/api/v1/sessions", query=query)
            assert status == 400 and answer["code"] == code, query
        assert (await hub.call("GET", f"/api/v1/sessions/{made[0]}", query="x=1"))[1]["code"] == "unknown_field"
        status, answer = await hub.call("POST", f"/api/v1/sessions/{made[0]}/stop", body={"now": True})
        assert status == 400 and answer["code"] == "unknown_field"
    finally:
        await hub.close()


# ---------------------------------------------------------------------------------------------- summary and state
def test_the_state_is_the_first_that_holds():
    base = dict(starting=False, earlier=False, failed=False, never_started=False, done=False, budget=False,
                running=False, paused=False)
    asked: list[bool] = []

    def blocking(value: bool):
        def ask() -> bool:
            asked.append(value)
            return value
        return ask

    def state(**changes: Any) -> str:
        return state_of(**{**base, **changes}, blocking=blocking(False))

    assert state() == "stopped" and state(starting=True, failed=True, earlier=True) == "starting"
    assert state(earlier=True, failed=True) == "stopped" and state(failed=True, never_started=True) == "failed"
    assert state(never_started=True, done=True) == "created" and state(done=True, budget=True) == "done"
    assert state(budget=True, running=True) == "budget" and state(done=True, running=True) == "done"
    assert state(running=True) == "running" and state(running=True, paused=True) == "paused"
    assert asked == [False, False]   # (the requests are read only for a running session)
    assert state_of(**{**base, "running": True, "paused": True}, blocking=blocking(True)) == "waiting"
    assert state(paused=True) == "stopped"


async def test_the_summary_follows_running_sessions_into_each_state(tmp_path, api_token, monkeypatch):
    """paused, waiting, budget and done as GET /api/v1/sessions/{id} and its list's `?state=` show them, on running
    sessions (ScriptedLLM) that stop_when_done leaves running, then budget and done again once stopped, and final.
    A request that cannot be read blocks nothing; a session whose summary fails is left out of the list."""
    hub = Hub(_manager(_home(tmp_path)))

    async def shown(uid: str) -> dict[str, Any]:
        status, answer = await hub.call("GET", f"/api/v1/sessions/{uid}")
        assert status == 200, answer
        session = answer["session"]
        status, listed = await hub.call("GET", "/api/v1/sessions", query=f"state={session['state']}")
        assert status == 200 and uid in [s["id"] for s in listed["sessions"]], session["state"]
        return session

    try:
        body = {"answers": "manual", "stop_when_done": False}
        one = (await hub.create({"goal": "Write a haiku", **body}))[1]["session"]
        two = (await hub.create({"goal": "Write a limerick", **body}))[1]["session"]
        await hub.settled()
        swarm = hub.manager.swarms[one["name"]]
        assert (await shown(one["id"]))["state"] == "running"
        swarm.pause(USER, "user")
        assert (await shown(one["id"]))["state"] == "paused"
        swarm.resume(USER)
        member = swarm.members()[0]["name"]   # a task handed in for the user's review: a request that blocks
        task = swarm.board.create(USER, "Write the haiku", assignee=member, reviewer=USER)
        if swarm.board.get(task.id).status != Status.IN_PROGRESS:
            swarm.board.claim(member, task.id)
        swarm.board.complete(member, task.id, "Written.")
        session = await shown(one["id"])
        assert session["state"] == "waiting" and session["requests"] == 1
        swarm.pause(USER, "user")   # (waiting comes before paused)
        assert (await shown(one["id"]))["state"] == "waiting"
        real = api_v1.requests_of

        def unreadable(swarm: Swarm) -> list[dict[str, Any]]:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(api_v1, "requests_of", unreadable)
        assert (await shown(one["id"]))["state"] == "paused"
        monkeypatch.setattr(api_v1, "requests_of", real)
        swarm.resume(USER)
        swarm.board.review(USER, task.id, True)
        assert (await shown(one["id"]))["state"] == "running"
        swarm.pause(USER, "budget")
        assert (await shown(one["id"]))["state"] == "budget"
        status, answer = await hub.call("POST", f"/api/v1/sessions/{one['id']}/stop")
        assert status == 200 and answer["session"]["state"] == "budget" and answer["session"]["final"] is True
        assert (await shown(one["id"]))["state"] == "budget"
        hub.manager.swarms[two["name"]].complete_goal(USER, "The limerick is written.")
        session = await shown(two["id"])
        assert session["state"] == "done" and session["goal"]["status"] == "done" and not session["final"]
        assert session["goal"]["summary"] == "The limerick is written."
        status, answer = await hub.call("POST", f"/api/v1/sessions/{two['id']}/stop")
        assert status == 200 and answer["session"]["state"] == "done" and answer["session"]["final"] is True
        assert (await shown(two["id"]))["state"] == "done"
        summary = ApiV1.summary

        def broken(self: ApiV1, sid: str) -> dict[str, Any]:
            if sid == one["name"]:
                raise RuntimeError("a damaged database")
            return summary(self, sid)

        monkeypatch.setattr(ApiV1, "summary", broken)
        status, answer = await hub.call("GET", "/api/v1/sessions")
        assert status == 200 and [s["id"] for s in answer["sessions"]] == [two["id"]]
    finally:
        await hub.close()


async def test_the_board_url_only_where_the_hub_knows_its_address(tmp_path, monkeypatch):
    manager = _manager(_home(tmp_path))
    try:
        sid = (await manager.create("Job", "G", start=False, api=AUTO))["id"]
        loopback, wide = ApiV1(manager, "127.0.0.1"), ApiV1(manager, "0.0.0.0")
        assert loopback.summary(sid)["board_url"] is None   # (not serving: no hub_url)
        manager.hub_url = "http://0.0.0.0:8765"
        assert wide.summary(sid)["board_url"] is None
        manager.hub_url = "http://127.0.0.1:8765"
        assert loopback.summary(sid)["board_url"] == f"http://127.0.0.1:8765/s/{sid}/"
        monkeypatch.setenv(web.PUBLIC_URL_ENV, "https://hub.example.org")
        manager.hub_url = "https://hub.example.org"
        assert wide.summary(sid)["board_url"] == f"https://hub.example.org/s/{sid}/"
    finally:
        await manager.shutdown()


# ---------------------------------------------------------------------------------------------- PATCH
async def test_patch_changes_the_limits_and_wrap_up_of_a_running_session(tmp_path, api_token):
    hub = Hub(_manager(_home(tmp_path)))
    try:
        made = (await hub.create({"goal": "Write a haiku", **MANUAL}))[1]["session"]
        await hub.settled()
        path, swarm = f"/api/v1/sessions/{made['id']}", hub.manager.swarms[made["name"]]
        status, answer = await hub.call("PATCH", path, body={"max_cost_usd": 2.5, "max_agents": 2, "wrap_up": True})
        assert status == 200, answer
        assert answer["session"]["max_cost_usd"] == 2.5 and answer["session"]["max_agents"] == 2
        assert answer["session"]["state"] == "running"
        assert swarm.limits() == {"max_cost_usd": 2.5, "max_agents": 2} and swarm.wrapping_up
        events = [row["type"] for row in swarm.db.all("SELECT type FROM events ORDER BY id")]
        assert "swarm.limits" in events and "swarm.wrap_up" in events
        # null is not given; a charset may follow the type
        status, answer = await hub.call("PATCH", path, body={"max_agents": None, "max_cost_usd": None,
                                                             "wrap_up": False},
                                        content_type="application/json; charset=utf-8")
        assert status == 200 and answer["session"]["max_agents"] == 2 and not swarm.wrapping_up
        assert (await hub.call("PATCH", path))[0] == 200   # (an empty body changes nothing)
        for body, code in (({"max_agents": 0}, "bad_request"), ({"max_cost_usd": -1}, "bad_request"),
                           ({"max_cost_usd": "5"}, "bad_request"), ({"max_agents": 2.5}, "bad_request"),
                           ({"max_agents": True}, "bad_request"), ({"wrap_up": "yes"}, "bad_request"),
                           ({"max_agents": 3, "colour": "red"}, "unknown_field")):
            status, answer = await hub.call("PATCH", path, body=body)
            assert status == 400 and answer["code"] == code, body
        assert swarm.limits() == {"max_cost_usd": 2.5, "max_agents": 2}   # nothing written by a refusal
        assert (await hub.call("PATCH", path, body={}, query="now=1"))[1]["code"] == "unknown_field"
        for kind in ("text/plain", "", "application/jsonx"):   # (the guard's 415 covers POST only)
            status, answer = await hub.call("PATCH", path, body={"max_agents": 3}, content_type=kind)
            assert status == 415 and answer["code"] == "unsupported_media_type", kind
        status, answer = await hub.call("PATCH", f"/api/v1/sessions/{UID}", body={"max_agents": 3})
        assert status == 404 and answer["code"] == "not_found"
        assert swarm.limits()["max_agents"] == 2
    finally:
        await hub.close()


async def test_patch_of_a_stopped_session_writes_what_its_next_start_reads(tmp_path, api_token):
    """A session that never started: wrap_up is 409 (its first start's goal would end it), the limits are written
    into its database, which is unloaded again; `continue` starts it, and the limits hold."""
    hub = Hub(_manager(_home(tmp_path)))
    try:
        sid = (await hub.manager.create("Haiku", "Write a haiku", start=False, api=MANUAL))["id"]
        path = f"/api/v1/sessions/{session_uid(hub.manager.meta(sid))}"
        status, answer = await hub.call("PATCH", path, body={"wrap_up": True})
        assert status == 409 and answer["code"] == "conflict"
        status, answer = await hub.call("PATCH", path, body={"max_cost_usd": 3, "max_agents": 2})
        assert status == 200 and answer["session"]["state"] == "created", answer
        assert answer["session"]["max_cost_usd"] == 3.0 and answer["session"]["max_agents"] == 2
        assert sid not in hub.manager.swarms   # (unloaded again: on Windows an open database blocks a delete)
        status, answer = await hub.call("POST", f"{path}/continue", body={"text": "Start with the season."})
        assert status == 200 and answer["session"]["state"] == "starting", answer
        await hub.settled()
        swarm = hub.manager.swarms[sid]
        assert sid in hub.manager.running and swarm.limits() == {"max_cost_usd": 3.0, "max_agents": 2}
        member = swarm.members()[0]["name"]
        assert _from_user(swarm)[-1] == ([member], "Start with the season.", None)   # (to the coordinator)
        assert (await hub.call("GET", path))[1]["session"]["state"] == "running"
    finally:
        await hub.close()


# ---------------------------------------------------------------------------------------------- continue
@pytest.mark.parametrize("body, query, code", [
    ({}, "", "bad_request"),
    ({"text": " \n "}, "", "bad_request"),
    ({"text": 5}, "", "bad_request"),
    ({"text": None}, "", "bad_request"),
    ({"text": "x" * (MAX_TEXT + 1)}, "", "bad_request"),
    ({"text": "Go on \ud800"}, "", "bad_request"),   # (an unpaired surrogate: no message could hold it)
    ({"text": "Go on.", "to": ["all"]}, "", "unknown_field"),
    ({"text": "Go on."}, "now=1", "unknown_field"),
])
async def test_a_continue_refused_by_its_body_changes_nothing(tmp_path, api_token, body, query, code):
    hub = Hub(_manager(_home(tmp_path)))
    try:
        sid = (await hub.manager.create("Haiku", "Write a haiku", start=False, api=MANUAL))["id"]
        path = f"/api/v1/sessions/{session_uid(hub.manager.meta(sid))}/continue"
        status, answer = await hub.call("POST", path, body=body, query=query)
        assert status == 400 and answer["code"] == code, answer
        assert sid not in hub.manager.starting and not hub.api._tasks and sid not in hub.manager.swarms
    finally:
        await hub.close()


async def test_continue_gives_a_running_session_its_text_as_p_r_does(tmp_path, api_token, monkeypatch):
    """A message to the coordinator; the answer to a pending question (a message to the asker that ends the
    pause); a session paused by the user, or by a budget that covers what was spent, resumed; a spent budget and what
    the swarm refuses are 409 with nothing given."""
    hub = Hub(_manager(_home(tmp_path)))
    try:
        made = (await hub.create({"goal": "Write a haiku", **MANUAL}))[1]["session"]
        await hub.settled()
        path, swarm = f"/api/v1/sessions/{made['id']}/continue", hub.manager.swarms[made["name"]]
        member = swarm.members()[0]["name"]
        status, answer = await hub.call("POST", path, body={"text": "Use the word 'cherry'."})
        assert status == 200 and answer["session"]["state"] == "running", answer
        assert _from_user(swarm)[-1] == ([member], "Use the word 'cherry'.", None)
        asked = swarm.mail.send(member, [USER], "Which season?")
        swarm.pause(member, "question", asker=member, message_id=asked["id"], since=time.time())
        assert (await hub.call("GET", f"/api/v1/sessions/{made['id']}"))[1]["session"]["state"] == "waiting"
        status, answer = await hub.call("POST", path, body={"text": "Spring."})
        assert status == 200 and _from_user(swarm)[-1] == ([member], "Spring.", asked["id"])
        assert not swarm.paused and answer["session"]["state"] == "running"
        for reason in ("paused by the user", "budget"):   # (no budget set: it covers what was spent)
            swarm.pause(USER, reason)
            status, answer = await hub.call("POST", path, body={"text": f"Go on ({reason})."})
            assert status == 200 and not swarm.paused and answer["session"]["state"] == "running", reason
            assert _from_user(swarm)[-1] == ([member], f"Go on ({reason}).", None)   # (given, then resumed)
        sent = len(_from_user(swarm))
        swarm.set_limits(USER, max_cost_usd=1.0)
        swarm.db.execute("UPDATE agents SET cost_usd = 2.0")
        status, answer = await hub.call("POST", path, body={"text": "More."})
        assert status == 409 and answer["code"] == "budget_exhausted" and "$2.00 of $1.00" in answer["error"]
        swarm.set_limits(USER, max_cost_usd=0)
        monkeypatch.setattr(api_v1, "continue_step", lambda state: ContinueStep("message", to="Nobody Here"))
        status, answer = await hub.call("POST", path, body={"text": "More."})
        assert status == 409 and answer["code"] == "conflict" and "Nobody Here" in answer["error"]
        assert len(_from_user(swarm)) == sent and _running(hub, made["name"])
    finally:
        await hub.close()


async def test_continue_reopens_the_goal_of_a_session_stop_when_done_stopped(tmp_path, api_token):
    """A done job (stop_when_done) is started again by `continue`, its goal reopened with the text (the comment
    without the whitespace around it) under the same lock hold as the start, so the job keeps running: the completion
    that stopped it was the last run's."""
    hub = Hub(_manager(_home(tmp_path)))
    try:
        made = (await hub.create({"goal": "Write a limerick", "answers": "manual"}))[1]["session"]
        await hub.settled()
        sid, path = made["name"], f"/api/v1/sessions/{made['id']}"
        hub.manager.swarms[sid].complete_goal(USER, "The limerick is written.")
        await _until(lambda: hub.manager.is_final(sid), "the stop of the done job")
        assert (await hub.call("GET", path))[1]["session"]["state"] == "done"
        status, answer = await hub.call("POST", f"{path}/continue", body={"text": "\n Make it rhyme better.\n"})
        assert status == 200 and answer["session"]["state"] == "starting" and not answer["session"]["final"]
        await hub.settled()
        swarm = hub.manager.swarms[sid]
        assert swarm.goal()["status"] == "active"
        (row,) = swarm.db.all("SELECT data FROM events WHERE type = 'goal.reopened'")
        assert json.loads(row["data"])["comment"] == "Make it rhyme better."
        await asyncio.sleep(0.05)
        while hub.manager._stopping:   # (none should be scheduled: the new run's watcher saw no completion)
            await asyncio.gather(*list(hub.manager._stopping), return_exceptions=True)
        status, answer = await hub.call("GET", path)
        assert _running(hub, sid) and answer["session"]["state"] == "running", answer
        assert answer["session"]["error"] is None and answer["session"]["finished_at"] is None
    finally:
        await hub.close()


async def test_continue_of_a_done_job_the_hub_resumes_keeps_it_running(tmp_path, api_token):
    """The hub stopped between a job's completion and its stop, so resume() starts it and its watcher stops it at
    once, but a `continue` came meanwhile and waits for the lock behind resume(): its start (a no-op after resume's)
    reopens the goal before that stop takes the lock, so the stop finds the job going on and the text is not lost."""
    hub = Hub(_manager(_home(tmp_path)))
    try:
        made = (await hub.create({"goal": "Write a limerick", "answers": "manual"}))[1]["session"]
        await hub.settled()
        sid, path = made["name"], f"/api/v1/sessions/{made['id']}"
        swarm = hub.manager.swarms[sid]
        swarm.board.create(USER, "Polish the rhymes")   # (an open task: resume() does not pass over the job)
        swarm.complete_goal(USER, "The limerick is written.")
        await _until(lambda: hub.manager.is_final(sid), "the stop of the done job")
        meta = hub.manager.meta(sid)
        meta["autostart"] = True   # (as a hub stopped before that stop leaves it)
        hub.manager._write_meta(sid, meta)
        assert hub.manager.plan_resume() == [sid]
        lock = hub.manager._lock(sid)
        await lock.acquire()   # (so that resume() takes the lock first and the continue's start second)
        try:
            resuming = asyncio.create_task(hub.manager.resume())
            await asyncio.sleep(0.01)
            status, answer = await hub.call("POST", f"{path}/continue", body={"text": "Add a second verse."})
            assert status == 200 and answer["session"]["state"] == "starting", answer
            await asyncio.sleep(0.01)
        finally:
            lock.release()
        assert await resuming == [sid]
        await hub.settled()
        while hub.manager._stopping:
            await asyncio.gather(*list(hub.manager._stopping), return_exceptions=True)
        swarm = hub.manager.swarms[sid]
        assert _running(hub, sid) and swarm.goal()["status"] == "active"
        assert hub.manager.meta(sid)["api"]["error"] is None
    finally:
        await hub.close()


async def test_a_stop_sent_after_a_continue_comes_after_its_text(tmp_path, api_token, monkeypatch):
    """A stop that waits for the lock while a `continue` starts the session takes it after the text was given (the
    start's `then`, the same lock hold): the requests keep their order, and the text is in the journal when the
    session stops."""
    gate = asyncio.Event()
    real = Swarm.start

    async def gated(self, *args, **kwargs):
        await gate.wait()
        await real(self, *args, **kwargs)

    hub = Hub(_manager(_home(tmp_path)))
    try:
        sid = (await hub.manager.create("Haiku", "Write a haiku", start=False, api=MANUAL))["id"]
        path = f"/api/v1/sessions/{session_uid(hub.manager.meta(sid))}"
        monkeypatch.setattr(Swarm, "start", gated)
        status, answer = await hub.call("POST", f"{path}/continue", body={"text": "Mind the syllables."})
        assert status == 200 and answer["session"]["state"] == "starting"
        await _until(lambda: hub.manager._lock(sid).locked(), "the continue's start")
        stopping = asyncio.create_task(hub.call("POST", f"{path}/stop"))
        await asyncio.sleep(0.02)
        assert not stopping.done()   # (it waits for the lock)
        gate.set()
        status, answer = await stopping
        assert status == 200 and answer["session"]["state"] == "stopped" and answer["session"]["error"] is None
        await hub.settled()
        with reading_file(hub.manager.dir_of(sid) / ".dreamteam" / "swarm.db") as (conn, _immutable):
            texts = [row[0] for row in conn.execute("SELECT text FROM messages WHERE sender = ?", (USER,))]
        assert texts == ["Mind the syllables."]
    finally:
        gate.set()
        await hub.close()


@pytest.mark.parametrize("moment", ["unloading", "queued", "starting"])
async def test_a_continue_sent_after_a_stop_starts_the_session_again(tmp_path, api_token, monkeypatch, moment):
    """Stop, then continue: the session runs again with the text, whenever the stop takes the session's lock (a stop
    takes away the start marks placed before it was asked, never a later continue's): while the stop unloads a
    running session (out of `running` already), while it waits for the lock of a stopped one, and while it waits
    behind a create's start."""
    gate, asked = asyncio.Event(), asyncio.Event()
    hub = Hub(_manager(_home(tmp_path)))
    real_manager_stop = hub.manager.stop

    async def stop(*args: Any, **kwargs: Any) -> None:   # (asked: the stop has read the marks before it)
        asked.set()
        await real_manager_stop(*args, **kwargs)

    monkeypatch.setattr(hub.manager, "stop", stop)
    try:
        if moment == "unloading":
            made = (await hub.create({"goal": "Write a haiku", **MANUAL}))[1]["session"]
            await hub.settled()
            sid, path = made["name"], f"/api/v1/sessions/{made['id']}"
            real_stop = Swarm.stop

            async def slow_stop(self, *args: Any, **kwargs: Any) -> None:
                await gate.wait()
                await real_stop(self, *args, **kwargs)

            monkeypatch.setattr(Swarm, "stop", slow_stop)
            stopping = asyncio.create_task(hub.call("POST", f"{path}/stop"))
            await _until(lambda: sid not in hub.manager.running, "the stop's unload")
        elif moment == "queued":
            sid = (await hub.manager.create("Haiku", "Write a haiku", start=False, api=MANUAL))["id"]
            path = f"/api/v1/sessions/{session_uid(hub.manager.meta(sid))}"

            async def hold() -> None:   # (another operation that holds the session's lock)
                async with hub.manager._lock(sid):
                    await gate.wait()

            holder = asyncio.create_task(hold())
            await _until(lambda: hub.manager._lock(sid).locked(), "the lock held")
            stopping = asyncio.create_task(hub.call("POST", f"{path}/stop"))
        else:
            real_start = Swarm.start

            async def gated(self, *args: Any, **kwargs: Any) -> None:
                await gate.wait()
                await real_start(self, *args, **kwargs)

            monkeypatch.setattr(Swarm, "start", gated)
            made = (await hub.create({"goal": "Write a haiku", **MANUAL}))[1]["session"]
            sid, path = made["name"], f"/api/v1/sessions/{made['id']}"
            await _until(lambda: hub.manager._lock(sid).locked(), "the create's start")
            stopping = asyncio.create_task(hub.call("POST", f"{path}/stop"))
        await asyncio.wait_for(asked.wait(), 5)
        await asyncio.sleep(0.01)
        assert not stopping.done()   # (it waits: for Swarm.stop, or for the lock)
        status, answer = await hub.call("POST", f"{path}/continue", body={"text": "Now the second verse."})
        assert status == 200 and answer["session"]["state"] == "starting", answer
        gate.set()
        assert (await stopping)[0] == 200
        if moment == "queued":
            await holder
        await hub.settled()
        session = (await hub.call("GET", path))[1]["session"]
        assert session["state"] == "running" and session["error"] is None, session
        swarm = hub.manager.swarms[sid]
        assert _from_user(swarm)[-1][1] == "Now the second verse."
        runs = [row["type"] for row in swarm.db.all("SELECT type FROM events WHERE type LIKE 'session.%' ORDER BY id")]
        assert runs[-1] == "session.started" and (moment == "queued" or runs[-2] == "session.stopped"), runs
    finally:
        gate.set()
        await hub.close()


async def test_a_continue_never_reaches_the_session_that_took_its_folder_name(tmp_path, api_token, monkeypatch):
    """A delete gives the folder number to the next session: a `continue` whose start had not taken the lock when its
    session was deleted gives its text to no one, and writes nothing into the session that has the folder name now
    (its mark went with the delete, so its start starts nothing; `not_running` is recorded for the same uid only)."""
    hub = Hub(_manager(_home(tmp_path)))
    gate = asyncio.Event()
    try:
        sid = (await hub.manager.create("Haiku", "Write a haiku", start=False, api=MANUAL))["id"]
        uid = session_uid(hub.manager.meta(sid))
        real = hub.manager.start

        async def gated(*args: Any, **kwargs: Any) -> Any:   # (the continue's start, before it takes the lock)
            await gate.wait()
            return await real(*args, **kwargs)

        monkeypatch.setattr(hub.manager, "start", gated)
        status, answer = await hub.call("POST", f"/api/v1/sessions/{uid}/continue", body={"text": "TEXT FOR X"})
        assert status == 200 and answer["session"]["state"] == "starting", answer
        assert (await hub.call("DELETE", f"/api/v1/sessions/{uid}", query=f"confirm={uid}"))[0] == 200
        assert (await hub.manager.create("Haiku", "Another job", start=False, api=MANUAL))["id"] == sid
        assert session_uid(hub.manager.meta(sid)) != uid   # (the same folder name, another session)
        gate.set()
        await hub.settled()
        assert hub.manager.meta(sid)["api"].get("error") is None and sid not in hub.manager.starting
        swarm = await real(sid)
        assert all(text != "TEXT FOR X" for _, text, _ in _from_user(swarm))
    finally:
        gate.set()
        await hub.close()


async def test_continue_after_a_budget_stop_needs_a_higher_budget_first(tmp_path, api_token):
    hub = Hub(_manager(_home(tmp_path)))
    try:
        made = (await hub.create({"goal": "Write a haiku", "max_cost_usd": 1, "answers": "manual"}))[1]["session"]
        await hub.settled()
        sid, path = made["name"], f"/api/v1/sessions/{made['id']}"
        swarm = hub.manager.swarms[sid]
        swarm.db.execute("UPDATE agents SET cost_usd = 2.0")
        swarm.pause(USER, "budget")
        await _until(lambda: hub.manager.is_final(sid), "the stop of the budget-paused job")
        assert (await hub.call("GET", path))[1]["session"]["state"] == "budget"
        status, answer = await hub.call("POST", f"{path}/continue", body={"text": "Finish it."})
        assert status == 409 and answer["code"] == "budget_exhausted", answer
        assert "PATCH /api/v1/sessions/{id}" in answer["error"]
        assert sid not in hub.manager.starting and not hub.api._tasks
        status, answer = await hub.call("PATCH", path, body={"max_cost_usd": 10})
        assert status == 200 and answer["session"]["state"] == "stopped", answer   # (the pause is lifted)
        status, answer = await hub.call("POST", f"{path}/continue", body={"text": "Finish it."})
        assert status == 200 and answer["session"]["state"] == "starting"
        await hub.settled()
        swarm = hub.manager.swarms[sid]
        assert _running(hub, sid) and not swarm.paused and swarm.limits()["max_cost_usd"] == 10.0
        assert _from_user(swarm)[-1][1] == "Finish it."
    finally:
        await hub.close()


async def test_continue_sets_the_goal_of_a_session_without_one(tmp_path, api_token, hub_token):
    hub = Hub(_manager(_home(tmp_path)))
    try:
        board = (await hub.manager.create("Board", start=False))["id"]   # the owner's, no goal
        path = f"/api/v1/sessions/{session_uid(hub.manager.meta(board))}/continue"
        assert (await hub.call("POST", path, body={"text": "Draw a cat"}))[0] == 404   # (not the API token's)
        status, answer = await hub.call("POST", path, body={"text": "Draw a cat\nIn ASCII."}, token=TOKEN)
        assert status == 200 and answer["session"]["state"] == "starting", answer
        await hub.settled()
        goal = hub.manager.swarms[board].goal()
        assert (goal["title"], goal["description"]) == ("Draw a cat", "In ASCII.")
    finally:
        await hub.close()


@pytest.mark.parametrize("fails", [False, True])
async def test_continue_while_a_create_starts_the_session_comes_after_that_start(tmp_path, api_token, monkeypatch,
                                                                                  fails):
    """A session already `starting` holds its place in max_running: `continue` is not checked again and counts once.
    Each has a start of its own, queued on the session's lock behind the create's in the order the requests came: a
    no-op once that start has the session running, so the texts are given in order; when that start fails, each
    tries again (as a `continue` of a failed session does), the text given only when one succeeds, else the summary
    shows the start's error."""
    gate = asyncio.Event()
    real = Swarm.start
    calls: list[str] = []

    async def gated(self, *args, **kwargs):
        await gate.wait()
        calls.append("start")
        if fails:
            raise RuntimeError("no model answers")
        await real(self, *args, **kwargs)

    monkeypatch.setattr(Swarm, "start", gated)
    hub = Hub(_manager(_home(tmp_path, api={"max_running": 1})))
    try:
        made = (await hub.create({"goal": "Write a haiku", **MANUAL}))[1]["session"]
        sid, path = made["name"], f"/api/v1/sessions/{made['id']}"
        for text in ("Short lines.", "And a title."):
            status, answer = await hub.call("POST", f"{path}/continue", body={"text": text})
            assert status == 200 and answer["session"]["state"] == "starting", answer
        assert hub.api.running_count() == 1 and len(hub.manager._marks[sid]) == 3   # (the create's and one each)
        assert (await hub.create({"goal": "Another"}))[1]["code"] == "too_many_sessions"
        gate.set()
        await hub.settled()
        session = (await hub.call("GET", path))[1]["session"]
        assert sid not in hub.manager.starting and hub.api.running_count() == (0 if fails else 1)
        if not fails:
            swarm = hub.manager.swarms[sid]
            member = swarm.members()[0]["name"]
            assert _from_user(swarm) == [([member], "Short lines.", None), ([member], "And a title.", None)]
            assert session["state"] == "running" and session["error"] is None and len(calls) == 1
        else:
            assert session["state"] == "failed" and session["error"] == "RuntimeError: no model answers"
            assert hub.manager.meta(sid)["api"]["error"] is None and len(calls) == 3
            with reading_file(hub.manager.dir_of(sid) / ".dreamteam" / "swarm.db") as (conn, _immutable):
                assert conn.execute("SELECT COUNT(*) FROM messages WHERE sender = ?", (USER,)).fetchone()[0] == 0
    finally:
        gate.set()
        await hub.close()


async def test_continue_of_a_stopped_session_counts_against_max_running(tmp_path, api_token, hub_token, no_start):
    hub = Hub(_manager(_home(tmp_path, api={"max_running": 1})))
    hold = asyncio.Event()
    no_start(hub.manager, hold)
    try:
        assert (await hub.create({"goal": "One"}))[0] == 202   # starting until `hold`
        stopped = (await hub.manager.create("Two", "Write a haiku", start=False, api=MANUAL))["id"]
        path = f"/api/v1/sessions/{session_uid(hub.manager.meta(stopped))}/continue"
        for token in (API_TOKEN, TOKEN):   # (a session made through v1 counts, whoever starts it)
            status, answer = await hub.call("POST", path, body={"text": "Go."}, token=token)
            assert status == 429 and answer["code"] == "too_many_sessions", token
        assert stopped not in hub.manager.starting
        board = (await hub.manager.create("Board", "Mine", start=False))["id"]   # the owner's: never counted
        status, answer = await hub.call("POST", f"/api/v1/sessions/{session_uid(hub.manager.meta(board))}/continue",
                                        body={"text": "Go."}, token=TOKEN)
        assert status == 200 and answer["session"]["state"] == "starting"
        hub.manager.resume_pending.add(stopped)   # about to be resumed: it counts already, so it is not refused
        status, answer = await hub.call("POST", path, body={"text": "Go."})
        assert status == 200 and answer["session"]["state"] == "starting", answer
        hub.manager.resume_pending.discard(stopped)
        hold.set()
        await hub.settled()
        status, answer = await hub.call("POST", path, body={"text": "Go."})
        assert status == 200   # (One no longer starts: no_start never runs it)
        await hub.settled()
        # no_start starts nothing (None, as a start whose mark a stop took): each text is recorded as not given
        assert hub.manager.meta(stopped)["api"]["error"] == {"code": "not_running", "text": api_v1.NOT_DELIVERED_TEXT}
        assert not hub.manager.starting and hub.api.running_count() == 0
    finally:
        hold.set()
        await hub.close()


async def test_a_text_that_cannot_be_given_after_the_start_is_the_sessions_error(tmp_path, api_token, monkeypatch):
    """In the background a refused delivery is `api.error` {code, text}, the summary's `error`, while the session
    runs; the next delivery that succeeds clears it. A stop sent after a `continue` that takes the session's lock
    before that continue's start leaves `not_running`: the start starts nothing."""
    hub = Hub(_manager(_home(tmp_path)))
    try:
        sid = (await hub.manager.create("Haiku", "Write a haiku", start=False, api=MANUAL))["id"]
        path = f"/api/v1/sessions/{session_uid(hub.manager.meta(sid))}"
        monkeypatch.setattr(api_v1, "continue_step", lambda state: ContinueStep("message", to="Nobody Here"))
        status, answer = await hub.call("POST", f"{path}/continue", body={"text": "Go on."})
        assert status == 200 and answer["session"]["state"] == "starting"
        await hub.settled()
        session = (await hub.call("GET", path))[1]["session"]
        assert session["state"] == "running" and "Unknown member 'Nobody Here'" in session["error"], session
        assert hub.manager.meta(sid)["api"]["error"]["code"] == "conflict"
        monkeypatch.setattr(api_v1, "continue_step", instruct.continue_step)
        status, answer = await hub.call("POST", f"{path}/continue", body={"text": "Go on."})
        assert status == 200 and answer["session"]["error"] is None and hub.manager.meta(sid)["api"]["error"] is None
        await hub.call("POST", f"{path}/stop")
        gate, real = asyncio.Event(), hub.manager.start

        async def gated(*args: Any, **kwargs: Any) -> Any:   # (the continue's start, before it takes the lock)
            await gate.wait()
            return await real(*args, **kwargs)

        monkeypatch.setattr(hub.manager, "start", gated)
        status, answer = await hub.call("POST", f"{path}/continue", body={"text": "Again."})
        assert status == 200 and answer["session"]["state"] == "starting"
        status, answer = await hub.call("POST", f"{path}/stop")   # sent after it, first to take the lock
        assert status == 200 and answer["session"]["state"] == "stopped" and sid not in hub.manager.starting
        gate.set()
        await hub.settled()
        session = (await hub.call("GET", path))[1]["session"]
        assert session["state"] == "stopped" and session["error"] == api_v1.NOT_DELIVERED_TEXT, session
        assert hub.manager.meta(sid)["api"]["error"]["code"] == "not_running"
        with reading_file(hub.manager.dir_of(sid) / ".dreamteam" / "swarm.db") as (conn, _immutable):
            texts = [row[0] for row in conn.execute("SELECT text FROM messages WHERE sender = ?", (USER,))]
        assert "Again." not in texts
    finally:
        await hub.close()


async def test_a_failed_session_is_continued_and_a_hub_that_stops_starts_nothing(tmp_path, api_token):
    hub = Hub(_manager(_home(tmp_path)))
    try:
        sid = (await hub.manager.create("Haiku", "Write a haiku", start=False, api=MANUAL))["id"]
        path = f"/api/v1/sessions/{session_uid(hub.manager.meta(sid))}"
        hub.manager._api_write(sid, start_error={"code": "internal", "text": api_v1.SHUTTING_DOWN_TEXT},
                               finished_at=time.time())   # (as a create the hub's stop left unstarted)
        assert (await hub.call("GET", path))[1]["session"]["state"] == "failed"
        hub.api.closing = True
        status, answer = await hub.call("POST", f"{path}/continue", body={"text": "Go on."})
        assert status == 500 and answer["code"] == "internal" and "shutting down" in answer["error"]
        assert sid not in hub.manager.starting and not hub.api._tasks
        hub.api.closing = False
        status, answer = await hub.call("POST", f"{path}/continue", body={"text": "Go on."})
        assert status == 200 and answer["session"]["state"] == "starting"
        await hub.settled()
        session = (await hub.call("GET", path))[1]["session"]
        assert session["state"] == "running" and session["error"] is None and session["finished_at"] is None
        assert hub.manager.meta(sid)["api"]["start_error"] is None
    finally:
        await hub.close()


# ---------------------------------------------------------------------------------------------- errors
async def test_folder_busy_is_v1s_own_text_naming_the_session_to_who_sees_it(tmp_path):
    manager = _manager(_home(tmp_path))
    api = ApiV1(manager, "127.0.0.1")
    try:
        mine = (await manager.create("Acquisition of Acme Corp", "G", start=False, api=AUTO))["id"]
        board = (await manager.create("Secret merger plan", "G", start=False))["id"]
        for holder, caller, named in ((mine, "api", mine), (board, "api", None), (board, "owner", board),
                                      (None, "api", None), ("999-gone", "owner", None)):
            text = f"{tmp_path} is the folder of session {holder} ({tmp_path}); two sessions cannot share files."
            answer = await api.error_response(SessionError(text, 409, code=FOLDER_BUSY, holder=holder), caller)
            shown = json.loads(answer.body)
            expected = {"error": api_v1.FOLDER_BUSY_TEXT, "code": "folder_busy"}
            if named is not None:
                expected["session"] = session_uid(manager.meta(named))
            assert answer.status_code == 409 and shown == expected, (holder, caller)
        assert SessionError("x", 409, code=FOLDER_BUSY, holder=mine).extra == {}   # (never in the internal answers)
    finally:
        await manager.shutdown()


async def test_a_create_while_the_hub_stops(tmp_path, api_token):
    """No create starts while aclose() runs (503: `internal`, logged as information), and one that passed that
    check finds nothing spawned once aclose() ended: its session is `failed` with the reason, never `starting`, and a
    repeat of its client_ref says why."""
    hub = Hub(_manager(_home(tmp_path)))
    try:
        hub.api._closed = True   # (a create that passed the check as aclose() ended)
        body = {"goal": "Late", "client_ref": "late-1"}
        status, answer = await hub.create(body)
        assert status == 500 and answer["code"] == "internal" and "shutting down" in answer["error"]
        (sid,) = hub.manager.ids()
        assert sid not in hub.manager.starting and hub.manager.is_final(sid)
        session = hub.api.summary(sid)
        assert session["state"] == "failed" and session["error"] == api_v1.SHUTTING_DOWN_TEXT
        assert isinstance(session["finished_at"], float) and session["started_at"] is None
        assert hub.manager.meta(sid)["api"]["start_error"] == {"code": "internal", "text": api_v1.SHUTTING_DOWN_TEXT}
        status, answer = await hub.create(body)
        assert status == 200 and answer["session"]["name"] == sid and answer["session"]["state"] == "failed"
        hub.api.closing = True
        status, answer = await hub.create({"goal": "Later"})
        assert status == 500 and "shutting down" in answer["error"] and hub.manager.ids() == [sid]
    finally:
        await hub.close()


async def test_route_names_in_error_texts_survive_the_redaction(tmp_path, api_token, hub_token):
    """The API caller's error texts lose the hub's paths, never the routes they name (GET /api/v1/options...)."""
    hub = Hub(_manager(_home(tmp_path)))
    try:
        for token in (API_TOKEN, TOKEN):
            status, answer = await hub.create({"goal": "G", "team": ["astronaut"]}, token=token)
            assert answer == {"error": "Unknown role: astronaut. GET /api/v1/options lists the roles.",
                              "code": "unknown_role"}, token
            status, answer = await hub.create({"goal": "G", "files": ["not a token"]}, token=token)
            assert status == 400 and answer["error"] == "'files' must hold tokens from POST /api/v1/uploads.", token
        secret = tmp_path / "elsewhere" / "private" / "notes.txt"   # (no folder the redaction names by its name)
        text = (f"Cannot read {secret} (see GET /api/v1/sessions/{{id}}/files, or /api/v1/options). "
                f"Not {secret.parent}/api/v1/x.")
        shown = hub.api.redact(text)
        assert shown == "Cannot read notes.txt (see GET /api/v1/sessions/{id}/files, or /api/v1/options). Not x.", \
            shown
        assert hub.api.redact("APIROUTE0APIROUTE GET /api/v1/options") == "APIROUTE0APIROUTE GET /api/v1/options"
    finally:
        await hub.close()


def test_option_errors_are_answered_with_their_codes():
    assert OptionError in api_v1.CODED_ERRORS
    assert api_v1.error_code(OptionError("unknown_model", "x")) == "unknown_model"
    assert api_v1.error_code(OptionError("bad_request", "x")) == "bad_request"
    with pytest.raises(ApiError) as exc:
        CreateRequest.parse({"goal": "G", "title": "a b"})
    assert exc.value.code == "bad_request"


# ---------------------------------------------------------------------------------------------- sessionopts
def _config(tmp_path: Path) -> Config:
    return Config.from_dict(tmp_path, {
        "swarm": {"start_agents": 3, "max_agents": 5},
        "models": {"default": {"provider": "scripted", "model": "scripted"}},
        "roles": [{"name": "Translator", "description": "Translates."}],
        "teams": [{"name": "Docs", "roles": ["writer", "tester"]}, {"name": "Broken", "roles": ["ghost"]}],
    })


def test_overrides_for_the_launcher_mcp_are_as_they_were(tmp_path):
    config = _config(tmp_path)
    assert overrides_from({}, config, strict=False) == {}
    assert overrides_from({"start_agents": "2", "max_agents": 4, "team": ["developer", "astronaut"],
                           "max_cost_usd": "1.5", "language": "en", "network": "off", "clarify": False,
                           "model": "anything"}, config, strict=False) == {
        "swarm": {"start_agents": 2, "max_agents": 4, "starting_roles": ["developer", "astronaut"],
                  "max_cost_usd": 1.5, "language": "en", "network": "off", "clarify": False},
        "agents": {"default_model": "anything"}}
    assert overrides_from({"team_name": "docs"}, config, strict=False) == {
        "swarm": {"starting_roles": ["writer", "tester"], "start_agents": 2}}
    for args, text in (({"start_agents": "x"}, "Expected an integer, got 'x'."),
                       ({"team_name": "Docs", "start_agents": 2}, "team_name sets the roles"),
                       ({"team": ["Docs"]}, "'Docs' is a saved team, not a role: pass it as team_name."),
                       ({"team_name": "nope"}, "No saved team 'nope'. Saved teams: Docs, Broken."),
                       ({"team_name": "Broken"}, "uses roles that are not in the role catalog: ghost"),
                       ({"team": "Docs"}, "team must be a list of role names"),
                       ({"clarify": "yes"}, "clarify must be true or false.")):
        with pytest.raises(OptionError) as exc:
            overrides_from(args, config, strict=False)
        assert text in str(exc.value), args
    with pytest.raises(ValueError):   # (as before: the launcher MCP answers it as an error)
        overrides_from({"max_cost_usd": "lots"}, config, strict=False)


def test_strict_overrides_for_v1(tmp_path):
    config = _config(tmp_path)
    assert overrides_from({"team": ["translator", "Разработчик"], "model": None, "language": None}, config,
                          strict=True) == {"swarm": {"starting_roles": ["translator", "Разработчик"],
                                                     "start_agents": 2}}
    assert overrides_from({"team": "docs", "max_agents": 2}, config, strict=True) == {
        "swarm": {"max_agents": 2, "starting_roles": ["writer", "tester"], "start_agents": 2}}
    assert overrides_from({"team": ["writer"], "start_agents": 3}, config, strict=True)["swarm"]["start_agents"] == 3
    for args, code in (({"team": "Broken"}, "unknown_role"), ({"team": ["ghost"]}, "unknown_role"),
                       ({"team": "nope"}, "unknown_team"), ({"model": "nope"}, "unknown_model"),
                       ({"model": 3}, "unknown_model"), ({"max_agents": 2}, "bad_request"),
                       ({"team": ["writer"] * 6}, "bad_request"), ({"team": ["a,b"]}, "bad_request"),
                       ({"team": ["x" * (roles.NAME_LIMIT + 1)]}, "bad_request"),
                       ({"max_cost_usd": float("inf")}, "bad_request"), ({"max_cost_usd": 10**400}, "bad_request"),
                       ({"max_cost_usd": -0.5}, "bad_request"), ({"clarify": 1}, "bad_request")):
        with pytest.raises(OptionError) as exc:
            overrides_from(args, config, strict=True)
        assert exc.value.code == code and exc.value.status == 400, args
    assert sessionopts.LANGUAGES == ("ru", "en")
