"""The HTTP API v1 end to end, as an integrator's site uses it (docs/SPEC.md §21): options, an upload, a create with
that file, a saved team, a model profile, a budget and manual answers; the timeline polled until the agent asks the
user; the answer; the timeline until final; the result; the files, one downloaded, the archive; a `continue` with a
follow-up; the stream resumed by Last-Event-ID; a create in a server folder (copy and in place); DELETE. Then the
same job with automatic answers and stop_when_done. Everything goes through the real launcher app
(create_launcher_app) driven by Starlette's TestClient (its own loop in a thread, as a server would run it), with a
ScriptedLLM agent that acts on the session's real state: no port, no network. Also: a live stream answered mid-way
and resumed by Last-Event-ID, a job that waits for its answer across a hub restart, and a job out of budget raised
with PATCH and continued."""

from __future__ import annotations

import io
import json
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from dreamteam import settings as st
from dreamteam.launcher import create_launcher_app
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.models import USER
from dreamteam.sessions import SessionManager
from dreamteam.swarm import Swarm
from test_api_v1_guard import api_token  # noqa: F401 - a fixture
from test_hub_auth import API_TOKEN

BRIEF = "Quarterly brief: revenue up 12%, two open hires, one risk (supplier delay).\n"
ANSWER = "Tone: FRIENDLY-42"
FOLLOW_UP = "QUESTIONS-PLEASE: add a list of the open questions"
AUTH = {"Authorization": f"Bearer {API_TOKEN}"}


def call(name: str, **arguments: Any) -> dict:
    return {"tool_calls": [{"name": name, "arguments": arguments}]}


class Worker:
    """One scripted member that works on the session's real state: it asks the user one question, waits for any
    answer, writes summary.md from the uploaded brief (or the copied folder's README) and the answer, completes the
    goal; after the goal is reopened with QUESTIONS-PLEASE it writes questions.md and completes it again."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, swarm: Any, name: str, messages: list[dict]) -> Any:
        goal = swarm.goal()
        ws = Path(swarm.config.workspace_dir)
        if not goal or goal.get("status") != "active":
            return call("wait", reason="no active goal")
        asked = swarm.db.scalar("SELECT MAX(id) FROM messages WHERE sender = ? AND recipients LIKE '%\"user\"%'",
                                (name,))
        if not (ws / "summary.md").exists():
            if asked is None:
                self.calls.append((name, "ask"))
                return call("send_message", to=["user"], text="Which tone should the summary have?")
            answer = swarm.db.one("SELECT text FROM messages WHERE sender = ? AND id > ? ORDER BY id LIMIT 1",
                                  (USER, asked))
            if answer is None:
                return call("wait", reason="the user's answer")
            source = next((ws / n for n in ("brief.txt", "README.md") if (ws / n).exists()), None)
            body = source.read_text(encoding="utf-8") if source else "(no input)"
            self.calls.append((name, "summary"))
            return call("write_file", path="summary.md", content=f"# Summary\n\n{body}\n{answer['text']}\n")
        reopened = swarm.db.scalar("SELECT COUNT(*) FROM events WHERE type = 'goal.reopened'")
        if reopened and not (ws / "questions.md").exists():
            self.calls.append((name, "questions"))
            return call("write_file", path="questions.md", content="# Open questions\n\n- supplier delay?\n")
        self.calls.append((name, "complete"))
        return call("goal_complete", summary="summary.md is written" + (" with questions.md" if reopened else ""))


def make_manager(home: Path, worker: Worker) -> SessionManager:
    def factory(profile, agent):
        swarm = sys._getframe(1).f_locals["self"]   # (Swarm.make_llm calls the factory: the swarm of this member)
        assert isinstance(swarm, Swarm), swarm
        return ScriptedLLM(lambda system, messages, tools: worker(swarm, agent, messages), profile)

    return SessionManager(home, llm_factory=factory)


def make_home(tmp_path: Path) -> tuple[Path, Path, Path]:
    home = tmp_path / "home"
    home.mkdir()
    jobs, places = tmp_path / "srv" / "jobs", tmp_path / "srv" / "places"
    jobs.mkdir(parents=True)
    places.mkdir(parents=True)
    st.write_raw(home, {
        "swarm": {"name": "Home", "language": "en", "start_agents": 1, "max_agents": 3, "max_cost_usd": 0},
        "agents": {"stagger_seconds": 0, "idle_nudge_seconds": 60},
        "workspace": {"git": True},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0},
                   "second": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
        "teams": [{"name": "Solo", "roles": ["writer"]}],
        "api": {"max_running": 0, "folders": {"jobs": {"path": str(jobs)},
                                              "places": {"path": str(places), "in_place": True}}},
    })
    return home, jobs, places


@pytest.fixture
def site(tmp_path, api_token):
    """(TestClient of the launcher app, the manager, the worker, jobs root, places root)."""
    home, jobs, places = make_home(tmp_path)
    worker = Worker()
    manager = make_manager(home, worker)
    app = create_launcher_app(manager, "127.0.0.1")
    with TestClient(app, base_url="http://127.0.0.1:8765", headers=AUTH) as client:
        try:
            yield client, manager, worker, jobs, places
        finally:
            client.portal.call(app.api.aclose)
            client.portal.call(manager.shutdown)


def ok(response, status: int = 200) -> Any:
    assert response.status_code == status, (response.status_code, response.text)
    return response.json() if response.headers.get("content-type", "").startswith("application/json") else response


def poll(client: TestClient, uid: str, after: int, until, *, seconds: float = 20.0) -> tuple[int, list[dict], dict]:
    """Long-poll the timeline from `after` until `until(events_so_far, page)` holds: (last, events, last page)."""
    seen: list[dict] = []
    deadline = time.monotonic() + seconds
    while True:
        page = ok(client.get(f"/api/v1/sessions/{uid}/events", params={"after": after, "wait": 1}))
        seen += page["events"]
        after = page["last"]
        if until(seen, page):
            return after, seen, page
        assert time.monotonic() < deadline, (page, [e["type"] for e in seen])


def final(seen: list[dict], page: dict) -> bool:
    return page["final"] and not page["more"]


def sse_events(text: str) -> list[dict]:
    out = []
    for block in text.split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if line.startswith(":"):
                continue
            key, _, value = line.partition(": ")
            fields[key] = value
        if fields:
            out.append(fields)
    return out


# ----------------------------------------------------------------------------------------------- manual answers
def test_a_site_runs_a_job_with_manual_answers_end_to_end(site):
    client, manager, worker, jobs, places = site

    options = ok(client.get("/api/v1/options"))
    assert options["api"] == 1 and "Solo" in [t["name"] for t in options["teams"]]
    assert {m["name"] for m in options["models"]} == {"default", "second"}
    assert {"name": "places", "in_place": True} in options["folders"]
    assert "path" not in json.dumps(options["folders"])

    up = ok(client.post("/api/v1/uploads", params={"filename": "brief.txt"}, content=BRIEF.encode(),
                        headers={"Content-Type": "application/octet-stream"}), 201)
    assert up["filename"] == "brief.txt" and up["bytes"] == len(BRIEF.encode()) and up["expires_in"] == 3600

    body = {"goal": "Summarize the brief\nOne page, as summary.md", "files": [up["upload"]], "team": "Solo",
            "model": "second", "max_cost_usd": 5.0, "answers": "manual", "client_ref": "job-42", "language": "en"}
    created = ok(client.post("/api/v1/sessions", json=body), 202)["session"]
    uid = created["id"]
    assert created["state"] == "starting" and created["answers"] == "manual" and created["model"] == "second"
    assert created["max_cost_usd"] == 5.0 and created["client_ref"] == "job-42"
    again = ok(client.post("/api/v1/sessions", json=body), 200)["session"]   # a retry after a timeout
    assert again["id"] == uid

    # the timeline until the agent asks the user
    after, seen, page = poll(client, uid, 0, lambda seen, page: ok(
        client.get(f"/api/v1/sessions/{uid}"))["session"]["requests"] > 0)
    types = [e["type"] for e in seen]
    assert "session.started" in types and all(e["text"] is None or isinstance(e["text"], str) for e in seen)
    requests = ok(client.get(f"/api/v1/sessions/{uid}/requests"))["requests"]
    assert [r["kind"] for r in requests] == ["message"], requests
    rid = requests[0]["id"]
    answered = ok(client.post(f"/api/v1/sessions/{uid}/requests/{rid}", json={"text": ANSWER}))
    assert answered["ok"] is True and answered["session"]["requests"] == 0
    twice = client.post(f"/api/v1/sessions/{uid}/requests/{rid}", json={"text": ANSWER})
    assert twice.status_code == 404 and twice.json()["code"] == "not_found"   # (answered: a retry lands nowhere)

    after, seen, page = poll(client, uid, after, final)
    assert page["state"] == "done", page
    stopped = [e for e in seen if e["type"] == "session.stopped"]
    assert stopped and stopped[-1]["data"]["reason"] == "done", [e["type"] for e in seen]

    result = ok(client.get(f"/api/v1/sessions/{uid}/result"))
    assert result["status"] == "done" and result["final"] is True and result["session"] == uid
    assert result["summary"].startswith("summary.md is written")
    changed = {c["path"]: c["change"] for c in result["changed_files"] or []}
    assert changed.get("summary.md") == "added", result

    listing = ok(client.get(f"/api/v1/sessions/{uid}/files"))
    names = {e["name"] for e in listing["entries"]}
    assert {"brief.txt", "summary.md"} <= names and ".git" not in names and ".scratch" not in names
    got = client.get(f"/api/v1/sessions/{uid}/files/summary.md")
    assert got.status_code == 200 and BRIEF.strip() in got.text and ANSWER in got.text
    assert got.headers["content-type"] == "application/octet-stream"
    assert int(got.headers["content-length"]) == len(got.content)
    archive = client.get(f"/api/v1/sessions/{uid}/archive")
    assert archive.status_code == 200 and archive.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(archive.content)) as zf:
        assert {"brief.txt", "summary.md"} <= set(zf.namelist())
        assert ANSWER in zf.read("summary.md").decode("utf-8")

    messages = ok(client.get(f"/api/v1/sessions/{uid}/messages"))["messages"]
    assert [(m["from"], m["text"]) for m in messages][-1] == (USER, ANSWER)
    assert messages[-1]["reply_to"] == messages[-2]["id"] and messages[-2]["to"] == [USER]

    # the next instruction to the same (done, stopped) session
    resumed = ok(client.post(f"/api/v1/sessions/{uid}/continue", json={"text": FOLLOW_UP}))["session"]
    assert resumed["state"] == "starting", resumed
    after, more, page = poll(client, uid, after, final)
    assert page["state"] == "done"
    result = ok(client.get(f"/api/v1/sessions/{uid}/result"))
    changed = {c["path"]: c["change"] for c in result["changed_files"] or []}
    assert changed.get("questions.md") == "added" and changed.get("summary.md") == "added", result

    # the whole timeline once more as a stream, resumed after an id in its middle (Last-Event-ID wins over after)
    middle = seen[len(seen) // 2]["id"]
    stream = client.get(f"/api/v1/sessions/{uid}/events/stream", params={"after": 0},
                        headers={"Last-Event-ID": str(middle)})
    assert stream.status_code == 200 and stream.headers["content-type"].startswith("text/event-stream")
    items = sse_events(stream.text)
    ids = [int(i["id"]) for i in items if "id" in i]
    assert ids and min(ids) > middle and ids == sorted(ids)
    assert items[-1]["event"] == "result" and json.loads(items[-1]["data"])["status"] == "done"

    deleted = ok(client.delete(f"/api/v1/sessions/{uid}", params={"confirm": uid}))
    assert deleted == {"ok": True}
    assert client.get(f"/api/v1/sessions/{uid}").status_code == 404


# ----------------------------------------------------------------------------------------------- auto answers
def test_the_same_job_with_automatic_answers_stops_when_done(site):
    client, manager, worker, jobs, places = site
    up = ok(client.post("/api/v1/uploads", params={"filename": "brief.txt"}, content=BRIEF.encode(),
                        headers={"Content-Type": "application/octet-stream"}), 201)
    created = ok(client.post("/api/v1/sessions", json={
        "goal": "Summarize the brief", "files": [up["upload"]], "team": ["writer"], "answers": "auto",
        "stop_when_done": True, "max_cost_usd": 1}), 202)["session"]
    uid = created["id"]
    after, seen, page = poll(client, uid, 0, final)
    assert page["state"] == "done", page
    result = ok(client.get(f"/api/v1/sessions/{uid}/result"))
    assert result["status"] == "done" and result["error"] is None
    summary = client.get(f"/api/v1/sessions/{uid}/files/summary.md").text
    assert "not available" in summary   # (the hub's automatic answer)
    assert ok(client.get(f"/api/v1/sessions/{uid}/requests"))["requests"] == []
    ok(client.delete(f"/api/v1/sessions/{uid}", params={"confirm": uid}))


# ----------------------------------------------------------------------------------------------- server folders
def test_a_copy_of_a_server_folder_and_a_folder_worked_in_place(site):
    client, manager, worker, jobs, places = site
    (jobs / "acme").mkdir()
    (jobs / "acme" / "README.md").write_text("Acme site notes\n", encoding="utf-8")
    copied = ok(client.post("/api/v1/sessions", json={
        "goal": "Summarize the notes", "folder": {"root": "jobs", "path": "acme"}, "answers": "auto"}), 202)["session"]
    assert copied["folder"] == {"root": "jobs", "path": "acme", "mode": "copy"}
    _, _, page = poll(client, copied["id"], 0, final)
    assert page["state"] == "done"
    assert not (jobs / "acme" / "summary.md").exists()   # (the original is never written)
    names = {e["name"] for e in ok(client.get(f"/api/v1/sessions/{copied['id']}/files"))["entries"]}
    assert {"README.md", "summary.md"} <= names

    inplace = ok(client.post("/api/v1/sessions", json={
        "goal": "Summarize the notes", "folder": {"root": "places", "path": "acme/site", "mode": "in_place"},
        "answers": "auto"}), 202)["session"]
    _, _, page = poll(client, inplace["id"], 0, final)
    assert page["state"] == "done", page
    assert (places / "acme" / "site" / "summary.md").exists()
    busy = client.post("/api/v1/sessions", json={
        "goal": "Again", "folder": {"root": "places", "path": "acme/site", "mode": "in_place"}})
    assert busy.status_code == 409 and busy.json()["code"] == "folder_busy"
    assert busy.json().get("session") == inplace["id"]
    result = ok(client.get(f"/api/v1/sessions/{inplace['id']}/result"))
    assert {c["path"] for c in result["changed_files"] or []} >= {"summary.md"}, result

    gone = ok(client.delete(f"/api/v1/sessions/{inplace['id']}", params={"confirm": inplace["id"]}))
    assert gone == {"ok": True, "folder_kept": {"root": "places", "path": "acme/site"}}
    assert (places / "acme" / "site" / "summary.md").exists()
    ok(client.delete(f"/api/v1/sessions/{copied['id']}", params={"confirm": copied["id"]}))
    again = client.post("/api/v1/sessions", json={
        "goal": "Again", "folder": {"root": "places", "path": "acme/site", "mode": "in_place"}, "answers": "auto"})
    assert again.status_code == 202, again.text   # (DELETE freed the folder)
    _, _, page = poll(client, again.json()["session"]["id"], 0, final)
    assert page["state"] == "done", page   # (the folder the first session left, adopted as a clean repository)


# ----------------------------------------------------------------------------------------------- live stream
def _items(lines, stop):
    """SSE blocks of `lines` until `stop(block)` holds (comments as {"comment": line})."""
    block: dict[str, str] = {}
    for line in lines:
        if line == "":
            if block:
                yield block
                if stop(block):
                    return
            block = {}
        elif line.startswith(":"):
            yield {"comment": line}
        else:
            key, _, value = line.partition(": ")
            block[key] = value


def test_a_live_stream_is_answered_mid_way_and_resumed_by_last_event_id(site):
    client, manager, worker, jobs, places = site
    up = ok(client.post("/api/v1/uploads", params={"filename": "brief.txt"}, content=BRIEF.encode(),
                        headers={"Content-Type": "application/octet-stream"}), 201)
    uid = ok(client.post("/api/v1/sessions", json={"goal": "Summarize the brief", "files": [up["upload"]],
                                                    "answers": "manual"}), 202)["session"]["id"]
    got: list[dict] = []
    with client.stream("GET", f"/api/v1/sessions/{uid}/events/stream") as first:
        assert first.headers["cache-control"] == "no-cache" and first.headers["x-accel-buffering"] == "no"
        for item in _items(first.iter_lines(), lambda b: b.get("event") == "message.sent"):
            got.append(item)
    # (the client went away at the agent's question; it answers, then reconnects as an EventSource does)
    rid = ok(client.get(f"/api/v1/sessions/{uid}/requests"))["requests"][0]["id"]
    ok(client.post(f"/api/v1/sessions/{uid}/requests/{rid}", json={"text": ANSWER}))
    with client.stream("GET", f"/api/v1/sessions/{uid}/events/stream",
                       headers={"Last-Event-ID": got[-1]["id"]}) as second:
        for item in _items(second.iter_lines(), lambda b: b.get("event") == "result"):
            got.append(item)
    ids = [int(item["id"]) for item in got if "id" in item]
    timeline = ok(client.get(f"/api/v1/sessions/{uid}/events", params={"limit": 500}))["events"]
    assert ids == [e["id"] for e in timeline]   # (no event lost or sent twice across the reconnect)
    assert got[-1]["event"] == "result" and json.loads(got[-1]["data"])["status"] == "done"


# ----------------------------------------------------------------------------------------------- hub restart
def test_a_job_waiting_for_its_answer_survives_a_hub_restart(tmp_path, api_token):
    home, jobs, places = make_home(tmp_path)
    manager = make_manager(home, Worker())
    app = create_launcher_app(manager, "127.0.0.1")
    with TestClient(app, base_url="http://127.0.0.1:8765", headers=AUTH) as client:
        uid = ok(client.post("/api/v1/sessions", json={"goal": "Summarize the brief", "answers": "manual"}),
                 202)["session"]["id"]
        after, _, _ = poll(client, uid, 0, lambda seen, page: ok(
            client.get(f"/api/v1/sessions/{uid}"))["session"]["requests"] > 0)
        rid = ok(client.get(f"/api/v1/sessions/{uid}/requests"))["requests"][0]["id"]
        client.portal.call(app.api.aclose)
        client.portal.call(manager.shutdown)
    manager = make_manager(home, Worker())   # (the hub starts again: serve_launcher's plan_resume, then resume)
    app = create_launcher_app(manager, "127.0.0.1")
    with TestClient(app, base_url="http://127.0.0.1:8765", headers=AUTH) as client:
        try:
            manager.plan_resume()
            assert ok(client.get(f"/api/v1/sessions/{uid}"))["session"]["final"] is False
            client.portal.call(manager.resume)
            assert [r["id"] for r in ok(client.get(f"/api/v1/sessions/{uid}/requests"))["requests"]] == [rid]
            ok(client.post(f"/api/v1/sessions/{uid}/requests/{rid}", json={"text": ANSWER}))
            _, seen, page = poll(client, uid, after, final)
            assert page["state"] == "done"
            reasons = [e["data"]["reason"] for e in seen if e["type"] == "session.stopped"]
            assert reasons == ["shutdown", "done"], reasons
        finally:
            client.portal.call(app.api.aclose)
            client.portal.call(manager.shutdown)


# ----------------------------------------------------------------------------------------------- budget
def test_a_job_out_of_budget_is_raised_with_patch_and_continued(tmp_path, api_token):
    home, jobs, places = make_home(tmp_path)
    raw = st.read_raw(home)
    raw["models"]["pricey"] = {"provider": "scripted", "model": "scripted", "input_price": 1000, "output_price": 1000}
    st.write_raw(home, raw)
    manager = make_manager(home, Worker())
    app = create_launcher_app(manager, "127.0.0.1")
    with TestClient(app, base_url="http://127.0.0.1:8765", headers=AUTH) as client:
        try:
            uid = ok(client.post("/api/v1/sessions", json={"goal": "Summarize", "model": "pricey", "max_cost_usd": 0.5,
                                                            "answers": "auto"}), 202)["session"]["id"]
            after, seen, page = poll(client, uid, 0, final)
            assert page["state"] == "budget" and seen[-1]["data"]["reason"] == "budget", page
            spent = ok(client.get(f"/api/v1/sessions/{uid}"))["session"]["cost_usd"]
            refused = client.post(f"/api/v1/sessions/{uid}/continue", json={"text": "go on"})
            assert refused.status_code == 409 and refused.json()["code"] == "budget_exhausted"
            raised = ok(client.patch(f"/api/v1/sessions/{uid}", json={"max_cost_usd": spent + 1000}))["session"]
            assert raised["max_cost_usd"] == spent + 1000 and raised["final"] is True
            resumed = ok(client.post(f"/api/v1/sessions/{uid}/continue", json={"text": "go on"}))["session"]
            assert resumed["state"] == "starting"
            _, more, page = poll(client, uid, after, final)
            assert page["state"] == "done", page
            assert client.get(f"/api/v1/sessions/{uid}/files/summary.md").status_code == 200
        finally:
            client.portal.call(app.api.aclose)
            client.portal.call(manager.shutdown)
