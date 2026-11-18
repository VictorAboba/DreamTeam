"""The external HTTP API v1's requests and messages (docs/SPEC.md §21.5): what waits for the user (a question pause, a
clarify planner's question, a member's message, a review, an MCP server to approve) listed and answered by id, each
answer checked against what waits now and given only while the session runs; the user's conversation read oldest
first from `after`, and a message of the user sent. Every request goes through the launcher's app and its guard
(create_launcher_app), on the test's own loop: no port, ScriptedLLM only, manual answers."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from dreamteam import api_v1, prompts
from dreamteam.api_v1 import ApiError, answer_of, message_of, query_after, query_count
from dreamteam.board import MAX_POST
from dreamteam.db import dumps, reading_file
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.mailbox import MAX_TEXT
from dreamteam.mcp_forge import snapshot_digest
from dreamteam.models import USER, Status
from dreamteam.sessions import SessionManager, session_uid
from dreamteam.swarm import Swarm
from test_api_sessions import AUTO, make_earlier
from test_api_v1 import MANUAL, UID, Hub, _from_user, _home, _until
from test_api_v1_guard import api_token, hub_token  # noqa: F401 - fixtures
from test_hub_auth import TOKEN


def _manager(home: Path) -> SessionManager:
    """Members that only wait: they never write to the user on their own (a member that answers a message of the
    user with plain text sends it to the user, agent.py _forward_reply), so the requests and the conversation are
    what the test makes them."""
    def factory(profile, agent):
        return ScriptedLLM(lambda system, messages, tools: {"tool_calls": [{"name": "wait", "arguments": {}}]},
                           profile)

    return SessionManager(home, llm_factory=factory)


async def running(hub: Hub, **body: Any) -> tuple[str, str, Swarm]:
    """A v1 session with manual answers that keeps running once its goal is done: (its id, its folder name, its
    swarm)."""
    status, answer = await hub.create({"goal": "Write a haiku", **MANUAL, **body})
    assert status == 202, answer
    await hub.settled()
    made = answer["session"]
    return made["id"], made["name"], hub.manager.swarms[made["name"]]


def to_review(swarm: Swarm, title: str, assignee: str) -> int:
    """A task of `assignee`'s handed in for the user's review; its id."""
    task = swarm.board.create(USER, title, assignee=assignee, reviewer=USER)
    if swarm.board.get(task.id).status != Status.IN_PROGRESS:
        swarm.board.claim(assignee, task.id)
    swarm.board.complete(assignee, task.id, f"{title}: done")
    assert swarm.board.get(task.id).status == Status.REVIEW
    return task.id


def review_event(swarm: Swarm, task_id: int) -> int:
    """The newest task.updated that put task `task_id` in review: its round."""
    return int(swarm.db.scalar("SELECT MAX(id) FROM events WHERE type = 'task.updated' "
                               "AND json_extract(data, '$.task.id') = ? "
                               "AND json_extract(data, '$.changes.status[1]') = 'review'", (task_id,)))


def read_by_user(swarm: Swarm, mid: int) -> bool:
    return swarm.db.scalar("SELECT read_at FROM inbox WHERE message_id = ? AND recipient = ?", (mid, USER)) is not None


def pending_server(swarm: Swarm, name: str, owner: str, code: bytes = b"print('ok')\n") -> str:
    """An MCP server `owner` built that waits for the user's approval (its snapshot and working copy on disk, not
    saved to the library when approved), as MCPForge.create leaves it with [mcp] require_approval; its digest."""
    digest = snapshot_digest(code, [])
    for folder in (swarm.forge.snapshot_dir(name), swarm.forge.server_dir(name)):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "server.py").write_bytes(code)
    (swarm.forge.server_dir(name) / "meta.json").write_text(json.dumps({"save_to_library": False}), encoding="utf-8")
    now = time.time()
    swarm.db.execute(
        "INSERT INTO mcp_servers(name, owner, scope, task_id, description, command, cwd, requirements, origin, status,"
        " tools, error, created_at, updated_at, digest, approved_digest)"
        " VALUES (?, ?, 'swarm', NULL, ?, '[]', NULL, '[]', 'workspace', 'pending', ?, NULL, ?, ?, ?, NULL)",
        (name, owner, f"The {name} tools", dumps([{"name": "add", "description": "Add two numbers"}]), now, now,
         digest))
    return digest


async def listed(hub: Hub, uid: str, **kwargs: Any) -> list[dict[str, Any]]:
    status, answer = await hub.call("GET", f"/api/v1/sessions/{uid}/requests", **kwargs)
    assert status == 200, answer
    return answer["requests"]


async def state(hub: Hub, uid: str) -> str:
    return (await hub.call("GET", f"/api/v1/sessions/{uid}"))[1]["session"]["state"]


# ---------------------------------------------------------------------------------------------- the kinds of requests
async def test_a_question_pause_waits_and_its_answer_ends_the_pause(tmp_path, api_token):
    hub = Hub(_manager(_home(tmp_path)))
    try:
        uid, sid, swarm = await running(hub)
        base, member = f"/api/v1/sessions/{uid}", swarm.members()[0]["name"]
        assert await listed(hub, uid) == [] and await state(hub, uid) == "running"
        question = "Which season should the haiku be about? " + "Some details. " * 60   # (over the snapshot's 600)
        asked = swarm.mail.send(member, [USER], question)
        swarm.pause(member, "question", asker=member, message_id=asked["id"], since=time.time())
        assert await listed(hub, uid) == [{"id": f"q{asked['id']}", "kind": "question", "ts": asked["ts"],
                                           "from": member, "text": question.strip(), "pauses_swarm": True}]
        session = (await hub.call("GET", base))[1]["session"]
        assert session["state"] == "waiting" and session["requests"] == 1
        status, answer = await hub.call("POST", f"{base}/requests/q{asked['id']}", body={"text": "  Spring.  "})
        assert status == 200 and answer["ok"] is True, answer
        assert answer["session"]["state"] == "running" and answer["session"]["requests"] == 0
        assert _from_user(swarm)[-1] == ([member], "Spring.", asked["id"]) and not swarm.paused
        assert read_by_user(swarm, asked["id"])   # (read in the user's inbox: the client answered it)
        given = len(_from_user(swarm))
        status, answer = await hub.call("POST", f"{base}/requests/q{asked['id']}", body={"text": "Spring!"})
        assert status == 404 and answer["code"] == "not_found"   # answered: a retry never lands on a newer one
        assert len(_from_user(swarm)) == given and await listed(hub, uid) == []
    finally:
        await hub.close()


async def test_a_clarify_planner_asks_a_question_that_does_not_pause(tmp_path, api_token):
    """The planner's last message to the user while its clarify planning card waits; its [swarm] clarify is the
    session's own (an override of the create), also when the stopped session is read from its file."""
    hub = Hub(_manager(_home(tmp_path)))
    try:
        uid, sid, swarm = await running(hub, clarify=True)
        base, member = f"/api/v1/sessions/{uid}", swarm.members()[0]["name"]
        card = swarm.board.list(tag="planning")[0]
        assert prompts.CLARIFY_TAG in card.tags
        if swarm.board.get(card.id).assignee is None:
            swarm.board.claim(member, card.id)
        asked = swarm.mail.send(member, [USER], "1. Which form? (default: a haiku)")
        assert await listed(hub, uid) == [{"id": f"q{asked['id']}", "kind": "question", "ts": asked["ts"],
                                           "from": member, "text": "1. Which form? (default: a haiku)",
                                           "pauses_swarm": False}]
        assert await state(hub, uid) == "waiting" and not swarm.paused
        status, answer = await hub.call("POST", f"{base}/requests/q{asked['id']}", body={"text": "A haiku."})
        assert status == 200 and answer["session"]["state"] == "running", answer
        assert _from_user(swarm)[-1] == ([member], "A haiku.", asked["id"]) and await listed(hub, uid) == []
        again = swarm.mail.send(member, [USER], "2. Which season?")
        waiting = await listed(hub, uid)
        assert [(r["id"], r["pauses_swarm"]) for r in waiting] == [(f"q{again['id']}", False)]
        assert (await hub.call("POST", f"{base}/stop"))[0] == 200
        assert await listed(hub, uid) == waiting and sid not in hub.manager.swarms   # (read from its file)
        status, answer = await hub.call("POST", f"{base}/requests/q{again['id']}", body={"text": "Spring."})
        assert status == 409 and answer["code"] == "not_running"
    finally:
        await hub.close()


async def test_a_members_message_is_a_request_that_does_not_block(tmp_path, api_token):
    hub = Hub(_manager(_home(tmp_path)))
    try:
        uid, sid, swarm = await running(hub)
        base, member = f"/api/v1/sessions/{uid}", swarm.members()[0]["name"]
        first = swarm.mail.send(member, [USER], "Started with the first line.")
        second = swarm.mail.send(member, [USER], "The first line is done.")
        assert await listed(hub, uid) == [{"id": f"m{second['id']}", "kind": "message", "ts": second["ts"],
                                           "from": member, "text": "The first line is done."}]
        session = (await hub.call("GET", base))[1]["session"]
        assert session["state"] == "running" and session["requests"] == 1   # (a report does not hold the work)
        status, answer = await hub.call("POST", f"{base}/requests/m{first['id']}", body={"text": "Thanks."})
        assert status == 404 and answer["code"] == "not_found"   # (not the member's last message)
        status, answer = await hub.call("POST", f"{base}/requests/m{second['id']}", body={"text": "Thanks."})
        assert status == 200 and answer["session"]["requests"] == 0, answer
        assert _from_user(swarm)[-1] == ([member], "Thanks.", second["id"]) and read_by_user(swarm, second["id"])
        assert not read_by_user(swarm, first["id"])   # (only what the request showed the client is read)
        assert await listed(hub, uid) == []
    finally:
        await hub.close()


async def test_a_review_is_answered_once_per_round(tmp_path, api_token):
    """The id names the round: the same while the task waits (another write of its row), new after it is handed in
    again, so an answer sent for an earlier round is 404 and never decides the new one. Rejected with a comment, then
    approved."""
    hub = Hub(_manager(_home(tmp_path)))
    try:
        uid, sid, swarm = await running(hub)
        base, member = f"/api/v1/sessions/{uid}", swarm.members()[0]["name"]
        task_id = to_review(swarm, "Write the haiku", member)
        first = f"r{task_id}-{review_event(swarm, task_id)}"
        stamp = swarm.db.scalar("SELECT ts FROM events WHERE id = ?", (review_event(swarm, task_id),))
        assert await listed(hub, uid) == [{"id": first, "kind": "review", "ts": stamp, "from": member,
                                           "task": {"id": task_id, "title": "Write the haiku",
                                                    "result": "Write the haiku: done"}}]
        assert await state(hub, uid) == "waiting"
        swarm.board.update(USER, task_id, priority=2)   # another write of the row: the same round
        assert [r["id"] for r in await listed(hub, uid)] == [first]
        status, answer = await hub.call("POST", f"{base}/requests/{first}",
                                        body={"approve": False, "comment": " Count the syllables. "})
        assert status == 200 and answer["session"]["state"] == "running", answer
        task = swarm.board.get(task_id)
        assert task.status == Status.IN_PROGRESS and task.assignee == member
        assert [(p["author"], p["text"]) for p in swarm.board.discussion(task_id)][-1] == (USER, "Count the syllables.")
        assert (await hub.call("POST", f"{base}/requests/{first}", body={"approve": True}))[0] == 404
        swarm.board.complete(member, task_id, "Write the haiku: done, 5-7-5")   # handed in again: a new round
        second = f"r{task_id}-{review_event(swarm, task_id)}"
        assert second != first and [r["id"] for r in await listed(hub, uid)] == [second]
        status, answer = await hub.call("POST", f"{base}/requests/{first}", body={"approve": True})
        assert status == 404 and swarm.board.get(task_id).status == Status.REVIEW   # (the earlier round's answer)
        status, answer = await hub.call("POST", f"{base}/requests/{second}", body={"approve": True, "comment": None})
        assert status == 200 and swarm.board.get(task_id).status == Status.DONE, answer
        assert await listed(hub, uid) == []
    finally:
        await hub.close()


async def test_an_mcp_server_is_approved_through_its_digest(tmp_path, api_token):
    """[mcp] require_approval: the request names the server's digest, so an answer for a server built again meanwhile
    is 404 and the server still waits; approved, it is active; rejected, it is rejected."""
    hub = Hub(_manager(_home(tmp_path, mcp={"require_approval": True})))
    try:
        uid, sid, swarm = await running(hub)
        base, member = f"/api/v1/sessions/{uid}", swarm.members()[0]["name"]
        digest = pending_server(swarm, "calc", member)
        rid = f"s-calc-{digest[:12]}"
        (request,) = await listed(hub, uid)
        assert request == {"id": rid, "kind": "mcp", "ts": request["ts"], "from": member,
                           "server": {"name": "calc", "description": "The calc tools", "tools": ["add"]}}
        assert await state(hub, uid) == "waiting"
        stale = "s-calc-" + ("1" if digest[0] == "0" else "0") * 12
        status, answer = await hub.call("POST", f"{base}/requests/{stale}", body={"approve": True})
        assert status == 404 and answer["code"] == "not_found" and swarm.forge.get("calc")["status"] == "pending"
        status, answer = await hub.call("POST", f"{base}/requests/{rid}", body={"approve": True, "comment": "ok"})
        assert status == 400 and answer["code"] == "unknown_field"   # (a review's answer has a comment)
        status, answer = await hub.call("POST", f"{base}/requests/{rid}", body={"approve": True})
        assert status == 200 and answer["session"]["state"] == "running", answer
        assert swarm.forge.get("calc")["status"] == "active" and await listed(hub, uid) == []
        statuses = [e["data"] for e in swarm.bus.recent(types=("mcp.status",), limit=10)]
        assert statuses[-1]["name"] == "calc" and statuses[-1]["status"] == "active"
        other = pending_server(swarm, "draw", member, b"print('draw')\n")
        status, answer = await hub.call("POST", f"{base}/requests/s-draw-{other[:12]}", body={"approve": False})
        assert status == 200 and swarm.forge.get("draw")["status"] == "rejected", answer
        assert await listed(hub, uid) == []
    finally:
        await hub.close()


async def test_an_mcp_server_whose_code_changed_after_the_listing_is_not_approved(tmp_path, api_token, monkeypatch):
    """Its working copy changed after GET requests: the approval tests that code again (the smoke test is the
    test's) and refuses, 409 `conflict` with the forge's text, which names no path of the hub's computer. A new build
    that passes waits under a new id; the old id is then 404 for an approval and a rejection alike."""
    hub = Hub(_manager(_home(tmp_path, mcp={"require_approval": True})))
    try:
        uid, sid, swarm = await running(hub)
        base, member = f"/api/v1/sessions/{uid}", swarm.members()[0]["name"]
        where = swarm.forge.server_dir("calc") / "python.exe"
        tests = [(None, [], f"could not start {where}: no such file"), (None, [{"name": "draw"}], None)]

        async def smoke_test(name: str, code: str, command: list[str]) -> tuple[Any, list[dict], str | None]:
            return tests.pop(0)

        monkeypatch.setattr(swarm.forge, "_smoke_test", smoke_test)
        digest = pending_server(swarm, "calc", member)
        assert [r["id"] for r in await listed(hub, uid)] == [f"s-calc-{digest[:12]}"]
        (swarm.forge.server_dir("calc") / "server.py").write_bytes(b"print('changed')\n")
        status, answer = await hub.call("POST", f"{base}/requests/s-calc-{digest[:12]}", body={"approve": True})
        assert status == 409 and answer["code"] == "conflict" and "failed (could not start" in answer["error"], answer
        for spelled in (str(tmp_path), str(tmp_path).replace("\\", "/")):
            assert spelled.lower() not in answer["error"].lower()
        assert swarm.forge.get("calc")["status"] == "failed" and await listed(hub, uid) == []

        first = pending_server(swarm, "draw", member, b"print('draw')\n")
        old = f"s-draw-{first[:12]}"
        (swarm.forge.server_dir("draw") / "server.py").write_bytes(b"print('draw again')\n")
        status, answer = await hub.call("POST", f"{base}/requests/{old}", body={"approve": True})
        assert status == 409 and answer["code"] == "conflict" and "waits for your approval" in answer["error"], answer
        (request,) = await listed(hub, uid)
        assert request["id"] != old and request["id"].startswith("s-draw-") and request["server"]["tools"] == ["draw"]
        for body in ({"approve": True}, {"approve": False}):
            status, answer = await hub.call("POST", f"{base}/requests/{old}", body=body)
            assert status == 404 and answer["code"] == "not_found", body
        assert swarm.forge.get("draw")["status"] == "pending" and await listed(hub, uid) == [request]
        status, answer = await hub.call("POST", f"{base}/requests/{request['id']}", body={"approve": False})
        assert status == 200 and swarm.forge.get("draw")["status"] == "rejected", answer
    finally:
        await hub.close()


async def test_an_answer_that_waits_for_the_forge_meets_the_server_built_again(tmp_path, api_token):
    """While its owner builds the server again (the forge's lock of that server held), an answer that passed the
    check waits for that lock; then the forge's digest check refuses a rejection, as an approval, of code it was not
    asked about. The server still waits, under its new id."""
    hub = Hub(_manager(_home(tmp_path, mcp={"require_approval": True})))
    try:
        uid, sid, swarm = await running(hub)
        base, member = f"/api/v1/sessions/{uid}", swarm.members()[0]["name"]
        digest = pending_server(swarm, "calc", member)
        newer = ("e" if digest[0] == "f" else "f") * 64
        lock = swarm.forge._lock("calc")
        await lock.acquire()
        try:
            answering = asyncio.create_task(hub.call("POST", f"{base}/requests/s-calc-{digest[:12]}",
                                                     body={"approve": False}))
            await _until(lambda: bool(getattr(lock, "_waiters", None)), "the answer waiting for the forge's lock")
            swarm.db.execute("UPDATE mcp_servers SET digest = ? WHERE name = 'calc'", (newer,))
        finally:
            lock.release()
        status, answer = await answering
        assert status == 409 and answer["code"] == "conflict" and "was not rejected: digest" in answer["error"], answer
        assert swarm.forge.get("calc")["status"] == "pending"
        assert [r["id"] for r in await listed(hub, uid)] == [f"s-calc-{newer[:12]}"]
    finally:
        await hub.close()


async def test_a_stop_does_not_wait_for_an_approval_that_tests_the_server_again(tmp_path, api_token, monkeypatch):
    """An approval whose working copy changed smoke-tests it again (up to [mcp] startup_timeout): it holds no lock of
    the session, so a stop goes on meanwhile, and the approval then finds the session stopped (409 `not_running`)."""
    hub = Hub(_manager(_home(tmp_path, mcp={"require_approval": True})))
    gate = asyncio.Event()
    try:
        uid, sid, swarm = await running(hub)
        base, member = f"/api/v1/sessions/{uid}", swarm.members()[0]["name"]

        async def smoke_test(name: str, code: str, command: list[str]) -> tuple[Any, list[dict], str | None]:
            await gate.wait()
            return None, [{"name": "add"}], None

        monkeypatch.setattr(swarm.forge, "_smoke_test", smoke_test)
        digest = pending_server(swarm, "calc", member)
        (swarm.forge.server_dir("calc") / "server.py").write_bytes(b"print('changed')\n")
        answering = asyncio.create_task(hub.call("POST", f"{base}/requests/s-calc-{digest[:12]}",
                                                 body={"approve": True}))
        await _until(lambda: swarm.forge._lock("calc").locked(), "the approval testing the server again")
        status, answer = await asyncio.wait_for(hub.call("POST", f"{base}/stop"), 5)
        assert status == 200 and sid not in hub.manager.running and not answering.done(), answer
        gate.set()
        status, answer = await answering
        assert status == 409 and answer["code"] == "not_running", answer
    finally:
        gate.set()
        await hub.close()


# ---------------------------------------------------------------------------------------------- the answers' checks
@pytest.mark.parametrize("kind, body, code", [
    ("question", {}, "bad_request"),
    ("question", {"text": " \n "}, "bad_request"),
    ("question", {"text": None}, "bad_request"),
    ("question", {"text": 5}, "bad_request"),
    ("question", {"text": "x" * (MAX_TEXT + 1)}, "bad_request"),
    ("question", {"text": "Spring \ud800"}, "bad_request"),
    ("question", {"approve": True}, "unknown_field"),
    ("question", {"text": "Spring.", "reply_to": 3}, "unknown_field"),
    ("message", {"text": "Thanks.", "comment": "!"}, "unknown_field"),
    ("review", {}, "bad_request"),
    ("review", {"approve": "yes"}, "bad_request"),
    ("review", {"approve": True, "comment": 5}, "bad_request"),
    ("review", {"approve": False, "comment": "x" * (MAX_POST + 1)}, "bad_request"),
    ("review", {"text": "Fine."}, "unknown_field"),
    ("mcp", {"approve": 1}, "bad_request"),
    ("mcp", {"approve": True, "comment": "ok"}, "unknown_field"),
])
def test_an_answer_has_the_fields_of_its_kind(kind, body, code):
    with pytest.raises(ApiError) as caught:
        answer_of(kind, body)
    assert caught.value.code == code


def test_answers_and_messages_as_given():
    assert answer_of("question", {"text": "  Spring.\n"}) == {"text": "Spring."}
    assert answer_of("message", {"text": "Thanks."}) == {"text": "Thanks."}
    assert answer_of("review", {"approve": False}) == {"approve": False, "comment": ""}
    assert answer_of("review", {"approve": True, "comment": " Good. "}) == {"approve": True, "comment": "Good."}
    assert answer_of("mcp", {"approve": False}) == {"approve": False}
    assert message_of({"to": ["all"], "text": " Hi. ", "reply_to": None}) == (["all"], "Hi.", None)
    assert message_of({"to": ["Anna", "all"], "text": "Hi.", "reply_to": 7}) == (["Anna", "all"], "Hi.", 7)


async def test_an_answer_needs_its_request_and_a_running_session(tmp_path, api_token):
    """Refusals give nothing: a body of the wrong shape (400), an id that names no request, an unknown session (404);
    the query's fields too. A stopped session (409 `not_running`) lists its requests from its file, unloaded."""
    hub = Hub(_manager(_home(tmp_path)))
    try:
        uid, sid, swarm = await running(hub)
        base, member = f"/api/v1/sessions/{uid}", swarm.members()[0]["name"]
        task_id = to_review(swarm, "Write the haiku", member)
        mid = swarm.mail.send(member, [USER], "Handed in.")["id"]
        rid = f"r{task_id}-{review_event(swarm, task_id)}"
        waiting = await listed(hub, uid)
        assert sorted(r["id"] for r in waiting) == sorted([rid, f"m{mid}"])   # (asked at once: their ts may tie)
        given = len(_from_user(swarm))
        for path, body, query, status, code in (
                (f"{base}/requests/{rid}", {"text": "Fine."}, "", 400, "unknown_field"),
                (f"{base}/requests/m{mid}", {"approve": True}, "", 400, "unknown_field"),
                (f"{base}/requests/{rid}", {"approve": True}, "now=1", 400, "unknown_field"),
                (f"{base}/requests/x{mid}", {"text": "Fine."}, "", 404, "not_found"),
                (f"{base}/requests/q0", {"text": "Fine."}, "", 404, "not_found"),
                (f"{base}/requests/r{task_id}", {"approve": True}, "", 404, "not_found"),
                (f"{base}/requests/q{mid}", {"text": "Fine."}, "", 404, "not_found"),   # (a message, not a question)
                # (numbers no request has: thousands of digits never reach int(), which refuses over 4300)
                (f"{base}/requests/q{'9' * 5000}", {"text": "Fine."}, "", 404, "not_found"),
                (f"{base}/requests/r1-{'9' * 5000}", {"approve": True}, "", 404, "not_found"),
                (f"{base}/requests/m{'1' * 4400}", {"text": "Fine."}, "", 404, "not_found"),
                (f"{base}/requests/m{api_v1.ID_MAX + 1}", {"text": "Fine."}, "", 404, "not_found"),
                (f"/api/v1/sessions/{UID}/requests/{rid}", {"approve": True}, "", 404, "not_found")):
            status_given, answer = await hub.call("POST", path, body=body, query=query)
            assert (status_given, answer["code"]) == (status, code), path
        assert (await hub.call("GET", f"{base}/requests", query="all=1"))[1]["code"] == "unknown_field"
        assert swarm.board.get(task_id).status == Status.REVIEW and len(_from_user(swarm)) == given
        assert (await hub.call("POST", f"{base}/stop"))[0] == 200
        assert await listed(hub, uid) == waiting and sid not in hub.manager.swarms   # (read from its file)
        for path, body in ((f"{base}/requests/{rid}", {"approve": True}), (f"{base}/requests/m{mid}", {"text": "Ok"})):
            status, answer = await hub.call("POST", path, body=body)
            assert status == 409 and answer["code"] == "not_running", path
            assert "/api/v1/sessions/{id}/continue" in answer["error"]
        assert sid not in hub.manager.swarms and await listed(hub, uid) == waiting
    finally:
        await hub.close()


async def test_an_answer_waiting_for_the_lock_of_a_session_that_stops_is_not_running(tmp_path, api_token):
    """The answer checks again under the session's lock: a stop that held it took the session away meanwhile."""
    hub = Hub(_manager(_home(tmp_path)))
    gate = asyncio.Event()
    try:
        uid, sid, swarm = await running(hub)
        member = swarm.members()[0]["name"]
        mid = swarm.mail.send(member, [USER], "A report.")["id"]

        async def stop_under_the_lock() -> None:
            async with hub.manager._lock(sid):
                await gate.wait()
                await hub.manager._unload(sid, "user")

        holder = asyncio.create_task(stop_under_the_lock())
        await _until(lambda: hub.manager._lock(sid).locked(), "the lock held")
        answering = asyncio.create_task(hub.call("POST", f"/api/v1/sessions/{uid}/requests/m{mid}",
                                                 body={"text": "Thanks."}))
        await asyncio.sleep(0.02)
        assert not answering.done()   # (it waits for the lock)
        gate.set()
        await holder
        status, answer = await answering
        assert status == 409 and answer["code"] == "not_running", answer
        with reading_file(hub.manager.dir_of(sid) / ".dreamteam" / "swarm.db") as (conn, _immutable):
            assert conn.execute("SELECT COUNT(*) FROM messages WHERE sender = ?", (USER,)).fetchone()[0] == 0
    finally:
        gate.set()
        await hub.close()


# ---------------------------------------------------------------------------------------------- messages
async def test_messages_are_the_users_conversation_oldest_first_after_an_id(tmp_path, api_token):
    hub = Hub(_manager(_home(tmp_path)))
    try:
        uid, sid, swarm = await running(hub, max_agents=3, start_agents=2)
        base = f"/api/v1/sessions/{uid}"
        anna, boris = [m["name"] for m in swarm.members()][:2]
        report = swarm.mail.send(anna, [USER], "A report.")
        swarm.mail.send(anna, [boris], "Not for the user.")   # never in the user's conversation
        status, answer = await hub.call("POST", f"{base}/messages",
                                        body={"to": [anna], "text": " Thanks, go on. ", "reply_to": report["id"]})
        assert status == 201, answer
        to_anna = answer["message"]
        assert to_anna == {"id": to_anna["id"], "ts": to_anna["ts"], "from": USER, "to": [anna],
                           "text": "Thanks, go on.", "reply_to": report["id"]}
        status, answer = await hub.call("POST", f"{base}/messages", body={"to": ["all"], "text": "Rhyme, all of you."})
        assert status == 201 and answer["message"]["to"] == [anna, boris] and answer["message"]["reply_to"] is None
        to_all = answer["message"]
        copy = swarm.mail.send(boris, [USER, anna], "Copied to Anna.")
        mine = [{"id": report["id"], "ts": report["ts"], "from": anna, "to": [USER], "text": "A report.",
                 "reply_to": None}, to_anna, to_all,
                {"id": copy["id"], "ts": copy["ts"], "from": boris, "to": [USER, anna], "text": "Copied to Anna.",
                 "reply_to": None}]
        assert await hub.call("GET", f"{base}/messages") == (200, {"messages": mine, "last": copy["id"]})
        status, answer = await hub.call("GET", f"{base}/messages", query=f"after={report['id']}&limit=2")
        assert status == 200 and answer == {"messages": mine[1:3], "last": to_all["id"]}
        status, answer = await hub.call("GET", f"{base}/messages", query=f"after={copy['id']}")
        assert answer == {"messages": [], "last": copy["id"]}   # (nothing newer: `last` is `after`)
        for query in ("limit=0", "limit=501", "limit=two", "after=-1", "after=1.5", f"after={api_v1.ID_MAX + 1}",
                      "after=" + "9" * 5000, "limit=" + "9" * 5000):
            status, answer = await hub.call("GET", f"{base}/messages", query=query)
            assert status == 400 and answer["code"] == "bad_request", query
        assert (await hub.call("GET", f"{base}/messages", query="since=1"))[1]["code"] == "unknown_field"
        assert (await hub.call("POST", f"{base}/stop"))[0] == 200
        assert await hub.call("GET", f"{base}/messages") == (200, {"messages": mine, "last": copy["id"]})
        assert sid not in hub.manager.swarms   # (read from its file)
        status, answer = await hub.call("POST", f"{base}/messages", body={"to": [anna], "text": "Still there?"})
        assert status == 409 and answer["code"] == "not_running"
    finally:
        await hub.close()


async def test_a_message_to_the_asker_answers_its_question(tmp_path, api_token):
    """As the board's user sends it: a message to the asker of a question pause is its answer, which ends the
    pause."""
    hub = Hub(_manager(_home(tmp_path)))
    try:
        uid, sid, swarm = await running(hub)
        member = swarm.members()[0]["name"]
        asked = swarm.mail.send(member, [USER], "Which season?")
        swarm.pause(member, "question", asker=member, message_id=asked["id"], since=time.time())
        status, answer = await hub.call("POST", f"/api/v1/sessions/{uid}/messages", body={"to": [member],
                                                                                            "text": "Autumn."})
        assert status == 201 and answer["message"]["reply_to"] == asked["id"] and not swarm.paused, answer
        assert await state(hub, uid) == "running"
    finally:
        await hub.close()


@pytest.mark.parametrize("body, code", [
    ({}, "bad_request"),
    ({"to": "all", "text": "Hi."}, "bad_request"),
    ({"to": [], "text": "Hi."}, "bad_request"),
    ({"to": ["all", 5], "text": "Hi."}, "bad_request"),
    ({"to": [" "], "text": "Hi."}, "bad_request"),
    ({"to": ["all"] * (api_v1.TO_MAX + 1), "text": "Hi."}, "bad_request"),
    ({"to": ["all"]}, "bad_request"),
    ({"to": ["all"], "text": "  "}, "bad_request"),
    ({"to": ["all"], "text": "Hi \udc00"}, "bad_request"),
    ({"to": ["all"], "text": "Hi.", "reply_to": "1"}, "bad_request"),
    ({"to": ["all"], "text": "Hi.", "reply_to": True}, "bad_request"),
    ({"to": ["all"], "text": "Hi.", "reply_to": 0}, "bad_request"),
    ({"to": ["all"], "text": "Hi.", "cc": ["all"]}, "unknown_field"),
])
def test_a_message_of_the_client_has_its_fields(body, code):
    with pytest.raises(ApiError) as caught:
        message_of(body)
    assert caught.value.code == code


async def test_what_the_mailbox_refuses_is_a_bad_request(tmp_path, api_token):
    hub = Hub(_manager(_home(tmp_path)))
    try:
        uid, sid, swarm = await running(hub)
        path = f"/api/v1/sessions/{uid}/messages"
        member = swarm.members()[0]["name"]
        for body, words in (({"to": ["Nobody Here"], "text": "Hi."}, "Unknown member"),
                            ({"to": ["user"], "text": "Hi."}, "only to yourself"),
                            ({"to": [member], "text": "Hi.", "reply_to": 99999}, "does not exist")):
            status, answer = await hub.call("POST", path, body=body)
            assert status == 400 and answer["code"] == "bad_request" and words in answer["error"], body
        assert _from_user(swarm) == []
    finally:
        await hub.close()


async def test_a_session_unloaded_while_it_is_read_is_read_from_its_file(tmp_path, api_token, monkeypatch):
    """An unload closes the swarm's connection: a read that meets it closed reads the session's file instead."""
    hub = Hub(_manager(_home(tmp_path)))
    try:
        uid, sid, swarm = await running(hub)
        report = swarm.mail.send(swarm.members()[0]["name"], [USER], "A report.")
        waiting = await listed(hub, uid)
        assert [r["id"] for r in waiting] == [f"m{report['id']}"]

        def closed(*args: Any, **kwargs: Any) -> Any:
            raise sqlite3.ProgrammingError("Cannot operate on a closed database.")

        monkeypatch.setattr(api_v1, "requests_of", closed)
        monkeypatch.setattr(swarm.mail, "user_conversation", closed)
        assert await listed(hub, uid) == waiting
        status, answer = await hub.call("GET", f"/api/v1/sessions/{uid}/messages")
        assert status == 200 and [m["id"] for m in answer["messages"]] == [report["id"]], answer
    finally:
        await hub.close()


async def test_a_stopped_session_whose_database_is_damaged_has_no_requests_or_messages(tmp_path, api_token):
    """As its summary counts no requests then: GET requests and GET messages answer an empty page, never a 500."""
    hub = Hub(_manager(_home(tmp_path)))
    try:
        uid, sid, swarm = await running(hub)
        base = f"/api/v1/sessions/{uid}"
        report = swarm.mail.send(swarm.members()[0]["name"], [USER], "A report.")
        assert (await hub.call("POST", f"{base}/stop"))[0] == 200
        assert sid not in hub.manager.swarms
        (hub.manager.dir_of(sid) / ".dreamteam" / "swarm.db").write_bytes(b"not a database " * 512)
        assert await hub.call("GET", f"{base}/requests") == (200, {"requests": []})
        assert await hub.call("GET", f"{base}/messages") == (200, {"messages": [], "last": 0})
        status, answer = await hub.call("GET", f"{base}/messages", query=f"after={report['id']}")
        assert (status, answer) == (200, {"messages": [], "last": report["id"]})
        status, answer = await hub.call("GET", base)
        assert status == 200 and answer["session"]["requests"] == 0, answer
    finally:
        await hub.close()


# ---------------------------------------------------------------------------------------------- scope, versions
async def test_the_api_token_reaches_no_requests_or_messages_of_another_session(tmp_path, api_token, hub_token):
    hub = Hub(_manager(_home(tmp_path)))
    try:
        sid = (await hub.manager.create("Owner's", "Write a haiku", start=False))["id"]   # (not made through v1)
        base = f"/api/v1/sessions/{session_uid(hub.manager.meta(sid))}"
        calls = (("GET", f"{base}/requests", None), ("POST", f"{base}/requests/m1", {"text": "Hi."}),
                 ("GET", f"{base}/messages", None), ("POST", f"{base}/messages", {"to": ["all"], "text": "Hi."}))
        for method, path, body in calls:
            status, answer = await hub.call(method, path, body=body)
            assert status == 404 and answer["code"] == "not_found", path
        assert await hub.call("GET", f"{base}/requests", token=TOKEN) == (200, {"requests": []})
        assert await hub.call("GET", f"{base}/messages", token=TOKEN) == (200, {"messages": [], "last": 0})
        status, answer = await hub.call("POST", f"{base}/messages", body={"to": ["all"], "text": "Hi."}, token=TOKEN)
        assert status == 409 and answer["code"] == "not_running"   # (never started)
        assert sid not in hub.manager.swarms
    finally:
        await hub.close()


async def test_a_session_of_an_earlier_version_is_read_but_never_answered(tmp_path, api_token):
    hub = Hub(_manager(_home(tmp_path)))
    try:
        sid = (await hub.manager.create("Old", "Do it", start=False, api=AUTO))["id"]
        make_earlier(hub.manager, sid)
        base = f"/api/v1/sessions/{session_uid(hub.manager.meta(sid))}"
        assert await hub.call("GET", f"{base}/requests") == (200, {"requests": []})
        assert await hub.call("GET", f"{base}/messages") == (200, {"messages": [], "last": 0})
        for path, body in ((f"{base}/requests/m1", {"text": "Hi."}), (f"{base}/messages", {"to": ["all"],
                                                                                          "text": "Hi."})):
            status, answer = await hub.call("POST", path, body=body)
            assert status == 409 and answer["code"] == "earlier_version", path
        assert sid not in hub.manager.swarms
    finally:
        await hub.close()


# ---------------------------------------------------------------------------------------------- query helpers
def test_ids_and_counts_in_a_query():
    assert query_after(None) == 0 and query_after("0") == 0 and query_after("0042") == 42
    assert query_after(str(api_v1.ID_MAX)) == api_v1.ID_MAX
    assert query_count("007", "limit", 50, 200) == 7 and query_count(None, "limit", 50, 200) == 50
    for value in ("-1", "1.0", " 1", "", "٣", str(api_v1.ID_MAX + 1), "9" * 5000):   # (never int() of 5000 digits)
        with pytest.raises(ApiError):
            query_after(value)
    for value in ("0", "201", "9" * 5000, "0" * 5000):
        with pytest.raises(ApiError):
            query_count(value, "limit", 50, 200)
