"""The external HTTP API v1's sessions (docs/SPEC.md §21.2-§21.4, §21.7): uploads, create (the body's checks, the
team, model and limits through sessionopts, the skills, the uploads with every token given back after a refusal, a
5xx or a cancel, client_ref, max_running, the start in the background), the one resolver of `{id}` and the scope of
the API token, the summary and its states, stop and DELETE. Every request goes through the launcher's app and its
guard (create_launcher_app), on the test's own loop: no port, ScriptedLLM only."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import sqlite3
import threading
import zipfile
from pathlib import Path
from typing import Any

import pytest

from conftest import make_skill
from dreamteam import api_v1, copyfrom, roles, sessionopts, web
from dreamteam import sessions as sessions_module
from dreamteam import settings as st
from dreamteam.api_v1 import ApiV1, CreateRequest, ApiError, request_digest, state_of
from dreamteam.config import Config
from dreamteam.copyfrom import CopyError
from dreamteam.launcher import create_launcher_app
from dreamteam.llm.scripted import ScriptedLLM
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
        if method in ("POST", "PATCH"):
            headers["content-type"] = content_type or "application/json"
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
    ({"goal": "G", "folder": {"root": "jobs"}}, "unknown_field"),   # (with the server folders, §21.10)
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
