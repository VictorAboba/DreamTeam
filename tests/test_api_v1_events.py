"""The external HTTP API v1's events (docs/SPEC.md §21.6): the journal read by id from `after` in pages
(events.read_page: the head first, a window of SCAN ids, the timeline's exact types and the user's notifications),
the long poll (`wait`: answered at once when something can be, else woken by an event, a change of state or the end
of an operation that held the session's lock; `final` never before `session.stopped`), a stopped session read from
its file without loading it, and the stream (Server-Sent Events: pages while `more`, `Last-Event-ID`, across a stop
and a start under one hold of the lock, pings, `result` at the end of a final session, the end without one on a
delete). Every request goes through the launcher's app and its guard on the test's own loop (test_api_v1.Hub; the
stream through an ASGI call read as it comes): no port, ScriptedLLM only."""

from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from dreamteam import api_v1, events
from dreamteam import sessions as sessions_module
from dreamteam.api_v1 import TIMELINE, ApiError, query_wait, safe_json, sse_event
from dreamteam.autoreply import journal_head
from dreamteam.db import SCHEMA, dumps
from dreamteam.events import read_page
from dreamteam.eventtext import event_text
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.models import USER
from dreamteam.sessions import JOURNAL_TEXT, SessionManager, session_uid
from dreamteam.swarm import Swarm
from test_api_v1 import LOCAL, MANUAL, Hub, _home, _until, no_start  # noqa: F401 - a fixture
from test_api_v1_guard import api_token, hub_token  # noqa: F401 - fixtures
from test_hub_auth import API_TOKEN, TOKEN


def _manager(home: Path) -> SessionManager:
    """Members that only wait: the journal holds what the test makes, besides their own activity."""
    def factory(profile, agent):
        return ScriptedLLM(lambda system, messages, tools: {"tool_calls": [{"name": "wait", "arguments": {}}]},
                           profile)

    return SessionManager(home, llm_factory=factory)


async def running(hub: Hub, *, paused: bool = False) -> tuple[str, str, Swarm]:
    """A v1 session that keeps running (manual answers, no stop_when_done): (its id, its folder name, its swarm);
    `paused`: paused by the user, so that its members add nothing to the journal."""
    status, answer = await hub.create({"goal": "Write a haiku\nAbout spring.", **MANUAL})
    assert status == 202, answer
    await hub.settled()
    made = answer["session"]
    swarm = hub.manager.swarms[made["name"]]
    if paused:
        swarm.pause(USER, "user")
    return made["id"], made["name"], swarm


async def poll(hub: Hub, uid: str, query: str = "", **kwargs: Any) -> tuple[int, Any]:
    return await hub.call("GET", f"/api/v1/sessions/{uid}/events", query=query, **kwargs)


def _row(event: dict[str, Any]) -> dict[str, Any]:
    return {key: event[key] for key in ("id", "type", "ts", "actor", "data")}


class Stream:
    """GET .../events/stream of a hub, called on the test's loop as the server would call it (ASGI 2.3: Starlette
    listens for the disconnect), its body read as it comes; close() is the client going away."""

    def __init__(self, hub: Hub, uid: str, *, query: str = "", headers: dict[str, str] | None = None,
                 token: str = API_TOKEN):
        path = f"/api/v1/sessions/{uid}/events/stream"
        sent = [(b"host", b"127.0.0.1:8765"), (b"authorization", f"Bearer {token}".encode()),
                *((k.encode(), v.encode()) for k, v in (headers or {}).items())]
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "method": "GET", "path": path,
                 "raw_path": path.encode(), "headers": sent, "query_string": query.encode(), "scheme": "http",
                 "root_path": "", "server": ("172.17.0.2", 8765), "client": LOCAL, "http_version": "1.1"}
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self._chunks: asyncio.Queue[str | None] = asyncio.Queue()
        self._gone = asyncio.Event()
        self._asked = False
        self._text = ""
        self._ended = False
        self.task = asyncio.ensure_future(hub.app(scope, self._receive, self._send))

    async def _receive(self) -> dict[str, Any]:
        if not self._asked:
            self._asked = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self._gone.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in message.get("headers", [])}
        elif message["type"] == "http.response.body":
            if message.get("body"):
                await self._chunks.put(message["body"].decode("utf-8"))
            if not message.get("more_body", False):
                await self._chunks.put(None)

    async def frame(self, timeout: float = 5.0) -> dict[str, Any] | None:
        """The next frame: {"id"?, "event", "data"} (data parsed) or {"comment"}; None once the stream ended."""
        while "\n\n" not in self._text:
            if self._ended:
                return None
            chunk = await asyncio.wait_for(self._chunks.get(), timeout)
            if chunk is None:
                self._ended = True
            else:
                self._text += chunk
        block, self._text = self._text.split("\n\n", 1)
        out: dict[str, Any] = {}
        for line in block.split("\n"):
            if line.startswith(":"):
                out["comment"] = line[1:].strip()
            else:
                name, _, value = line.partition(": ")
                out[name] = json.loads(value) if name == "data" else value
        return out

    async def until(self, wanted: str, timeout: float = 5.0) -> list[dict[str, Any]]:
        """Frames up to and including the first of event type `wanted`."""
        seen = []
        while True:
            frame = await self.frame(timeout)
            assert frame is not None, f"the stream ended before {wanted}: {seen}"
            seen.append(frame)
            if frame.get("event") == wanted:
                return seen

    async def rest(self, timeout: float = 5.0) -> list[dict[str, Any]]:
        """Every frame until the stream ends."""
        seen = []
        while (frame := await self.frame(timeout)) is not None:
            seen.append(frame)
        return seen

    async def body(self, timeout: float = 5.0) -> bytes:
        parts = []
        while (chunk := await asyncio.wait_for(self._chunks.get(), timeout)) is not None:
            parts.append(chunk)
        return "".join(parts).encode("utf-8")

    async def close(self) -> None:
        self._gone.set()
        await asyncio.wait_for(self.task, 5.0)


# ---------------------------------------------------------------------------------------------- events.read_page
def _journal(*rows: tuple[str, Any]) -> sqlite3.Connection:
    """A journal of `rows` (type, data; data a str is written as it is), ids from 1."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    for kind, data in rows:
        conn.execute("INSERT INTO events(ts, type, actor, data) VALUES (?, ?, ?, ?)",
                     (time.time(), kind, "Anna Smith", data if isinstance(data, str) else dumps(data)))
    return conn


def test_a_page_is_read_from_after_in_id_order_and_says_where_it_ended():
    conn = _journal(*(("task.created", {"n": n}) for n in range(5)))
    page, last, more = read_page(conn, 0, 2)
    assert [e["id"] for e in page] == [1, 2] and last == 2 and more is True   # (a full page: its last row)
    assert list(page[0]) == ["id", "type", "ts", "actor", "data"] and page[0]["data"] == {"n": 0}
    page, last, more = read_page(conn, 2, 10)
    assert [e["id"] for e in page] == [3, 4, 5] and last == 5 and more is False
    assert read_page(conn, 5, 10) == ([], 5, False)
    assert read_page(conn, 50, 10) == ([], 5, False)   # past the head: last is the head
    assert read_page(_journal(), 0, 10) == ([], 0, False)


def test_a_window_the_filter_empties_still_moves_on():
    conn = _journal(("task.created", {}), *(("agent.activity", {}) for _ in range(10)), ("task.updated", {}))
    kept = ("task.created", "task.updated")
    page, last, more = read_page(conn, 0, 10, kept, scan=5)
    assert [e["type"] for e in page] == ["task.created"] and last == 5 and more is True
    page, last, more = read_page(conn, 1, 10, kept, scan=5)
    assert page == [] and last == 6 and more is True        # nothing kept: the upper end of the window
    page, last, more = read_page(conn, 6, 10, kept, scan=5)
    assert page == [] and last == 11 and more is True
    page, last, more = read_page(conn, 11, 10, kept, scan=5)
    assert [e["type"] for e in page] == ["task.updated"] and last == 12 and more is False
    assert read_page(conn, 0, 10, (), scan=5) == ([], 5, True)   # no type at all: nothing, the window moves on
    page, last, more = read_page(conn, 0, 1, None, scan=3)
    assert [e["id"] for e in page] == [1] and (last, more) == (1, True)


def test_a_notification_counts_only_for_its_recipient_and_damaged_data_reads_as_empty():
    conn = _journal(("notification", {"recipient": USER, "kind": "k", "text": "for you"}),
                    ("notification", {"recipient": "Anna Smith", "kind": "k", "text": "for Anna"}),
                    ("notification", "{not json"),
                    ("task.created", "{not json either"))
    page, last, more = read_page(conn, 0, 10, ("notification", "task.created"), recipient=USER)
    assert [e["id"] for e in page] == [1, 4] and page[1]["data"] == {} and (last, more) == (4, False)
    page, _, _ = read_page(conn, 0, 10, ("notification",))
    assert [e["id"] for e in page] == [1, 2, 3] and page[2]["data"] == {}
    page, _, _ = read_page(conn, 0, 10)
    assert len(page) == 4


def test_scan_is_read_at_each_call(monkeypatch):
    conn = _journal(*(("agent.activity", {}) for _ in range(10)))
    monkeypatch.setattr(events, "SCAN", 3)
    assert read_page(conn, 0, 10, ("task.created",)) == ([], 3, True)


def test_a_page_can_leave_its_data_to_be_decoded_later():
    """SessionManager.journal decodes the rows after it released the Database's lock, which the event loop takes."""
    conn = _journal(("task.created", {"n": 1}), ("task.updated", "{not json"))
    page, last, more = read_page(conn, 0, 10, decode=False)
    assert [e["data"] for e in page] == [dumps({"n": 1}), "{not json"] and (last, more) == (2, False)
    assert [events.event_data(e["data"]) for e in page] == [{"n": 1}, {}] and events.event_data(None) == {}


# ---------------------------------------------------------------------------------------------- polling
async def test_a_poll_reads_the_journal_from_the_oldest_with_its_lines(tmp_path, api_token):
    hub = Hub(_manager(_home(tmp_path)))
    try:
        uid, sid, swarm = await running(hub, paused=True)
        status, page = await poll(hub, uid, "detail=all")
        assert status == 200, page
        assert list(page) == ["events", "last", "more", "state", "final"]
        found = page["events"]
        ids = [e["id"] for e in found]
        assert ids[0] == 1 and ids == sorted(ids) and found[0]["type"] == "session.started"
        assert list(found[0]) == ["id", "type", "ts", "actor", "data", "text"]
        assert page["last"] == ids[-1] and page["more"] is False
        assert page["state"] == "paused" and page["final"] is False
        goal = next(e for e in found if e["type"] == "goal.set")
        assert goal["text"] == event_text(_row(goal), "en") and "Write a haiku" in goal["text"]   # (the session's)
        status, page = await poll(hub, uid, f"detail=all&lang=ru&after={goal['id'] - 1}&limit=1")
        assert status == 200 and page["events"][0]["id"] == goal["id"] and page["more"] is True
        assert page["events"][0]["text"] == event_text(_row(goal), "ru") != goal["text"]
        # the timeline, a page at a time
        status, page = await poll(hub, uid, "limit=1")
        assert [e["type"] for e in page["events"]] == ["session.started"] and page["last"] == 1 and page["more"]
    finally:
        await hub.close()


async def test_the_lines_are_in_the_sessions_language_unless_lang_names_one(tmp_path, api_token):
    """DreamTeam's own default language is ru: a session in it gets ru lines from a poll and a stream that name none."""
    hub = Hub(_manager(_home(tmp_path, swarm={"language": "ru"})))
    try:
        uid, sid, swarm = await running(hub, paused=True)
        status, page = await poll(hub, uid, "detail=all")
        assert status == 200, page
        goal = next(e for e in page["events"] if e["type"] == "goal.set")
        assert goal["text"] == event_text(_row(goal), "ru") != event_text(_row(goal), "en")
        status, page = await poll(hub, uid, "detail=all&lang=en")
        assert next(e for e in page["events"] if e["type"] == "goal.set")["text"] == event_text(_row(goal), "en")
        stream = Stream(hub, uid, query="detail=all")
        try:
            frames = await stream.until("goal.set")
            assert frames[-1]["data"]["text"] == event_text(_row(goal), "ru")
        finally:
            await stream.close()
    finally:
        await hub.close()


async def test_a_journal_that_cannot_be_read_is_never_answered_as_complete(tmp_path, api_token, monkeypatch):
    """A stopped session's file that cannot be read now (a sharing violation): never `final` with `more` false and no
    events, which would make a client stop early; the poll is an error it asks again after, the stream ends with no
    result (the client reconnects with Last-Event-ID)."""
    hub = Hub(_manager(_home(tmp_path)))
    try:
        uid, sid, swarm = await running(hub)
        assert (await hub.call("POST", f"/api/v1/sessions/{uid}/stop"))[0] == 200

        def unreadable(*args: Any, **kwargs: Any) -> Any:
            raise OSError("a sharing violation")

        monkeypatch.setattr(sessions_module, "reading_file", unreadable)
        started = time.monotonic()
        status, answer = await poll(hub, uid, "after=0&wait=5")
        assert status == 500 and answer["code"] == "internal" and answer["error"] == JOURNAL_TEXT, answer
        assert time.monotonic() - started < 2
        stream = Stream(hub, uid)
        try:
            assert stream.status in (None, 200) and await stream.rest() == []
        finally:
            await stream.close()
    finally:
        await hub.close()


async def test_a_long_poll_is_not_found_once_its_session_is_deleted(tmp_path, api_token):
    hub = Hub(_manager(_home(tmp_path)))
    try:
        uid, sid, swarm = await running(hub, paused=True)
        waiting = asyncio.ensure_future(poll(hub, uid, f"after={journal_head(swarm)}&wait=5"))
        await asyncio.sleep(0.2)
        assert not waiting.done()
        started = time.monotonic()
        status, answer = await hub.call("DELETE", f"/api/v1/sessions/{uid}", query=f"confirm={uid}")
        assert status == 200, answer
        status, answer = await asyncio.wait_for(waiting, 2)
        assert status == 404 and answer["code"] == "not_found" and time.monotonic() - started < 1.5
    finally:
        await hub.close()


async def test_the_timeline_has_the_users_notifications_and_all_has_every_event(tmp_path, api_token):
    hub = Hub(_manager(_home(tmp_path)))
    try:
        uid, sid, swarm = await running(hub, paused=True)
        member = swarm.members()[0]["name"]
        head = journal_head(swarm)
        swarm.bus.emit("agent.activity", member, kind="thinking", text="Hmm.")
        swarm.bus.emit("notification", None, recipient=member, kind="n_x", text="For the member")
        swarm.bus.emit("notification", None, recipient=USER, kind="n_x", text="For you")
        status, page = await poll(hub, uid, f"after={head}")
        assert status == 200
        assert [(e["type"], e["data"]["recipient"]) for e in page["events"]] == [("notification", USER)]
        assert page["events"][0]["text"].endswith("For you") and page["last"] >= head + 3
        status, page = await poll(hub, uid, f"after={head}&detail=all")
        mine = [(e["type"], e["data"]["text"]) for e in page["events"]   # (the members' own activity aside)
                if e["data"].get("text") in ("Hmm.", "For the member", "For you")]
        assert mine == [("agent.activity", "Hmm."), ("notification", "For the member"), ("notification", "For you")]
        assert all(kind in TIMELINE for kind in ("session.started", "session.stopped", "notification"))
        assert "agent.activity" not in TIMELINE and "agent.usage" not in TIMELINE
    finally:
        await hub.close()


async def test_a_page_the_filter_emptied_and_an_after_past_the_head_are_answered_at_once(tmp_path, api_token,
                                                                                          monkeypatch):
    monkeypatch.setattr(events, "SCAN", 5)
    hub = Hub(_manager(_home(tmp_path)))
    try:
        uid, sid, swarm = await running(hub, paused=True)
        member = swarm.members()[0]["name"]
        head = journal_head(swarm)
        for n in range(12):
            swarm.bus.emit("agent.activity", member, kind="thinking", text=f"step {n}")
        swarm.board.create(USER, "Draft the haiku")
        started = time.monotonic()
        status, page = await poll(hub, uid, f"after={head}&wait=1")
        assert status == 200 and page["events"] == [] and page["last"] == head + 5 and page["more"] is True
        assert time.monotonic() - started < 0.8
        after, seen = page["last"], []
        while True:
            status, page = await poll(hub, uid, f"after={after}&wait=1")
            seen += page["events"]
            after = page["last"]
            if not page["more"]:
                break
        assert [e["type"] for e in seen] == ["task.created"]
        started = time.monotonic()
        status, page = await poll(hub, uid, f"after={after + 100}&wait=1")
        assert status == 200 and page["events"] == [] and after <= page["last"] < after + 100   # (the head)
        assert page["more"] is False and page["state"] == "paused" and page["final"] is False
        assert time.monotonic() - started < 0.8
    finally:
        await hub.close()


async def test_a_long_poll_is_woken_by_an_event(tmp_path, api_token):
    hub = Hub(_manager(_home(tmp_path)))
    try:
        uid, sid, swarm = await running(hub, paused=True)
        head = journal_head(swarm)
        waiting = asyncio.ensure_future(poll(hub, uid, f"after={head}&wait=5"))
        await asyncio.sleep(0.2)
        assert not waiting.done()
        started = time.monotonic()
        swarm.board.create(USER, "Draft the haiku")
        status, page = await asyncio.wait_for(waiting, 2)
        assert status == 200 and [e["type"] for e in page["events"]] == ["task.created"]
        assert time.monotonic() - started < 1.0
        # nothing comes: the deadline answers
        started = time.monotonic()
        status, page = await poll(hub, uid, f"after={page['last']}&wait=0.3")
        assert status == 200 and page["events"] == [] and 0.25 < time.monotonic() - started < 1.5
    finally:
        await hub.close()


async def test_a_long_poll_is_woken_by_a_change_of_state_or_of_final(tmp_path, api_token):
    hub = Hub(_manager(_home(tmp_path)))
    manager = hub.manager
    try:
        uid, sid, swarm = await running(hub, paused=True)
        head = journal_head(swarm)
        waiting = asyncio.ensure_future(poll(hub, uid, f"after={head}&wait=5"))
        await asyncio.sleep(0.2)
        assert not waiting.done()
        mark = manager.mark_starting(sid)   # (no event: the state alone changes)
        status, page = await asyncio.wait_for(waiting, 2)
        assert status == 200 and page["events"] == [] and page["state"] == "starting" and page["final"] is False
        manager.unmark_starting(sid, mark)
        assert (await hub.call("POST", f"/api/v1/sessions/{uid}/stop"))[0] == 200
        status, page = await poll(hub, uid, "wait=5")   # final: at once
        assert page["final"] is True and page["state"] == "stopped"
        # an operation that holds the session's lock: not final until it ends, which wakes the poll
        lock = manager._lock(sid)
        await lock.acquire()
        try:
            waiting = asyncio.ensure_future(poll(hub, uid, f"after={page['last']}&wait=5"))
            await asyncio.sleep(0.2)
            assert not waiting.done()
        finally:
            lock.release()
        status, page = await asyncio.wait_for(waiting, 2)
        assert status == 200 and page["events"] == [] and page["final"] is True
    finally:
        await hub.close()


async def test_final_never_comes_before_session_stopped(tmp_path, api_token):
    hub = Hub(_manager(_home(tmp_path)))
    try:
        uid, sid, swarm = await running(hub)
        pages: list[dict[str, Any]] = []

        async def follow() -> None:
            after = 0
            while True:
                status, page = await poll(hub, uid, f"after={after}&wait=1")
                assert status == 200, page
                pages.append(page)
                after = page["last"]
                if page["final"] and not page["more"]:
                    return

        reader = asyncio.ensure_future(follow())
        await asyncio.sleep(0.1)
        assert (await hub.call("POST", f"/api/v1/sessions/{uid}/stop"))[0] == 200
        await asyncio.wait_for(reader, 5)
        seen: list[str] = []
        for page in pages:
            seen += [e["type"] for e in page["events"]]
            if page["final"]:
                assert "session.stopped" in seen
        assert seen[-1] == "session.stopped"
    finally:
        await hub.close()


async def test_a_stopped_session_is_read_from_its_file_without_loading_it(tmp_path, api_token):
    hub = Hub(_manager(_home(tmp_path)))
    try:
        uid, sid, swarm = await running(hub)
        assert (await hub.call("POST", f"/api/v1/sessions/{uid}/stop"))[0] == 200
        assert sid not in hub.manager.swarms
        status, page = await poll(hub, uid, "detail=all&wait=5")
        assert status == 200 and page["final"] is True and page["state"] == "stopped" and page["more"] is False
        assert page["events"][-1]["type"] == "session.stopped" and page["events"][-1]["data"] == {"reason": "user"}
        assert sid not in hub.manager.swarms
        db = hub.manager.dir_of(sid) / ".dreamteam" / "swarm.db"
        assert not db.with_name("swarm.db-wal").exists() and not db.with_name("swarm.db-shm").exists()
    finally:
        await hub.close()


async def test_an_event_whose_data_holds_nan_is_answered(tmp_path, api_token):
    hub = Hub(_manager(_home(tmp_path)))
    try:
        uid, sid, swarm = await running(hub, paused=True)
        head = journal_head(swarm)
        swarm.bus.emit("task.comment", USER, task={"id": 1}, value=float("nan"), values=[1.0, float("inf")])
        status, page = await poll(hub, uid, f"after={head}")
        assert status == 200 and page["events"][0]["data"]["value"] is None
        assert page["events"][0]["data"]["values"] == [1.0, None]
    finally:
        await hub.close()


@pytest.mark.parametrize("query, code", [
    ("wait=61", "bad_request"), ("wait=-1", "bad_request"), ("wait=1e1", "bad_request"), ("wait=nan", "bad_request"),
    ("limit=0", "bad_request"), ("limit=501", "bad_request"), ("detail=some", "bad_request"),
    ("lang=de", "bad_request"), ("after=-1", "bad_request"), ("since=3", "unknown_field"),
    ("after=1&after=2", "bad_request"),
])
async def test_a_poll_refused_by_its_query(tmp_path, api_token, no_start, query, code):
    hub = Hub(_manager(_home(tmp_path)))
    no_start(hub.manager)
    try:
        status, answer = await hub.create({"goal": "G"})
        uid = answer["session"]["id"]
        status, answer = await poll(hub, uid, query)
        assert status == 400 and answer["code"] == code, answer
    finally:
        await hub.close()


def test_wait_is_a_number_of_seconds():
    assert query_wait(None) == 0.0 and query_wait("0") == 0.0 and query_wait("2.5") == 2.5
    assert query_wait("60") == 60.0 and query_wait("007") == 7.0
    for bad in ("60.5", "", " 1", "+1", "1e1", "inf", "１"):
        with pytest.raises(ApiError):
            query_wait(bad)


async def test_a_session_of_another_origin_or_gone_is_not_found(tmp_path, api_token, hub_token):
    hub = Hub(_manager(_home(tmp_path)))
    try:
        made = await hub.manager.create("Board session", "A board session", start=False)
        uid = session_uid(hub.manager.meta(made["id"]))
        for path in ("events", "events/stream", "result"):
            status, answer = await hub.call("GET", f"/api/v1/sessions/{uid}/{path}")
            assert status == 404 and answer["code"] == "not_found", path
        status, page = await poll(hub, uid, token=TOKEN)
        assert status == 200 and page == {"events": [], "last": 0, "more": False, "state": "created", "final": True}
        status, answer = await poll(hub, "f" * 32)
        assert status == 404 and answer["code"] == "not_found"
    finally:
        await hub.close()


# ---------------------------------------------------------------------------------------------- the stream
async def test_a_stream_reads_the_events_and_ends_with_the_result(tmp_path, api_token):
    hub = Hub(_manager(_home(tmp_path)))
    try:
        uid, sid, swarm = await running(hub)
        stream = Stream(hub, uid)
        try:
            frames = await stream.until("goal.set")
            assert stream.status == 200 and stream.headers["content-type"].startswith("text/event-stream")
            assert stream.headers["cache-control"] == "no-cache" and stream.headers["x-accel-buffering"] == "no"
            assert frames[0]["event"] == "session.started" and frames[0]["id"] == "1"
            assert frames[0]["data"]["id"] == 1 and frames[0]["data"]["type"] == "session.started"
            assert all(frame["data"]["type"] in TIMELINE for frame in frames)
            assert (await hub.call("POST", f"/api/v1/sessions/{uid}/stop"))[0] == 200
            frames = await stream.until("session.stopped")
            assert frames[-1]["data"]["data"] == {"reason": "user"}
            rest = await stream.rest()
            assert [frame.get("event") for frame in rest] == ["result"] and "id" not in rest[0]
            result = rest[0]["data"]
            assert result["type"] == "result" and result["session"] == uid and result["final"] is True
            assert result["status"] == "stopped"
            status, answer = await hub.call("GET", f"/api/v1/sessions/{uid}/result")
            assert status == 200 and answer == result
        finally:
            await stream.close()
        # resumed after the last id it saw: Last-Event-ID wins over the URL's after
        stopped = int(frames[-1]["id"])
        stream = Stream(hub, uid, query="after=0", headers={"last-event-id": str(stopped - 1)})
        try:
            rest = await stream.rest()
            assert [(frame.get("event"), frame.get("id")) for frame in rest] == [("session.stopped", str(stopped)),
                                                                                ("result", None)]
        finally:
            await stream.close()
    finally:
        await hub.close()


async def test_a_stream_follows_a_stop_and_a_start_into_a_new_swarm(tmp_path, api_token):
    hub = Hub(_manager(_home(tmp_path)))
    manager = hub.manager
    try:
        uid, sid, swarm = await running(hub, paused=True)
        stream = Stream(hub, uid, query=f"after={journal_head(swarm)}")
        try:
            await asyncio.sleep(0.1)
            async with manager._lock(sid):   # (a restart: a workspace move holds the lock across both)
                await manager._unload(sid, "restart")
                await manager._start_locked(sid)
            again = manager.swarms[sid]
            assert again is not swarm
            frames = await stream.until("session.started")
            assert [(f["event"], f["data"]["data"]) for f in frames][:1] == [("session.stopped", {"reason": "restart"})]
            again.board.create(USER, "Draft the haiku")
            frames = await stream.until("task.created")
            assert frames[-1]["data"]["data"]["task"]["title"] == "Draft the haiku"
        finally:
            await stream.close()
    finally:
        await hub.close()


async def test_a_stream_with_every_event_in_another_language(tmp_path, api_token):
    hub = Hub(_manager(_home(tmp_path)))
    try:
        uid, sid, swarm = await running(hub, paused=True)
        member = swarm.members()[0]["name"]
        stream = Stream(hub, uid, query="detail=all&lang=ru")
        try:
            goal = (await stream.until("goal.set"))[-1]["data"]
            assert goal["text"] == event_text(_row(goal), "ru") != event_text(_row(goal), "en")
            swarm.bus.emit("agent.activity", member, kind="thinking", text="Hmm.")
            while (activity := (await stream.until("agent.activity"))[-1]["data"])["data"].get("text") != "Hmm.":
                assert activity["text"] == event_text(_row(activity), "ru")   # (the members' own activity first)
            assert activity["text"] == event_text(_row(activity), "ru") and "Hmm." in activity["text"]
        finally:
            await stream.close()
    finally:
        await hub.close()


async def test_a_stream_sends_no_result_for_a_session_started_again_meanwhile(tmp_path, api_token, monkeypatch):
    """A `continue` that arrives while the final result is built: that result says final, so it is not sent; the
    stream reads on and sends the result once the session is final again."""
    hub = Hub(_manager(_home(tmp_path)))
    manager = hub.manager
    try:
        uid, sid, swarm = await running(hub, paused=True)
        assert (await hub.call("POST", f"/api/v1/sessions/{uid}/stop"))[0] == 200
        loop = asyncio.get_running_loop()
        real = hub.api.result
        marks: list[object] = []

        def result(sid_: str, summary: dict[str, Any], jobs: list[Any]) -> dict[str, Any]:
            built = real(sid_, summary, jobs)
            if not marks:   # (the first one: a start is announced while it is built)
                loop.call_soon_threadsafe(lambda: marks.append(manager.mark_starting(sid_)))
                time.sleep(0.2)
            return built

        monkeypatch.setattr(hub.api, "result", result)
        stream = Stream(hub, uid, query="after=999999")   # (past the head: no event, only the result)
        try:
            first = asyncio.ensure_future(stream.frame(5.0))
            await _until(lambda: bool(marks), "the start announced while the result was built")
            await asyncio.sleep(0.4)
            assert not first.done()   # (starting: no result)
            manager.unmark_starting(sid, marks[0])
            frame = await asyncio.wait_for(first, 3)
            assert frame["event"] == "result" and frame["data"]["final"] is True
            assert frame["data"]["status"] == "stopped"
            assert await stream.rest() == []
        finally:
            await stream.close()
    finally:
        await hub.close()


async def test_a_stream_pings_while_nothing_comes(tmp_path, api_token, monkeypatch):
    monkeypatch.setattr(api_v1, "PING_SECONDS", 0.1)
    hub = Hub(_manager(_home(tmp_path)))
    try:
        uid, sid, swarm = await running(hub, paused=True)
        stream = Stream(hub, uid, query=f"after={journal_head(swarm)}")
        try:
            assert await stream.frame(1.0) == {"comment": "ping"}
            assert await stream.frame(1.0) == {"comment": "ping"}
        finally:
            await stream.close()
        assert sid not in hub.manager._wakers   # (the waiter went with the client)
    finally:
        await hub.close()


async def test_a_final_session_behind_a_gap_the_filter_empties_ends_with_its_result(tmp_path, api_token,
                                                                                     monkeypatch):
    monkeypatch.setattr(events, "SCAN", 5)
    hub = Hub(_manager(_home(tmp_path)))
    try:
        uid, sid, swarm = await running(hub, paused=True)
        member = swarm.members()[0]["name"]
        head = journal_head(swarm)
        for n in range(30):
            swarm.bus.emit("agent.activity", member, kind="thinking", text=f"step {n}")
        assert (await hub.call("POST", f"/api/v1/sessions/{uid}/stop"))[0] == 200
        stream = Stream(hub, uid, query=f"after={head}")
        try:
            frames = await stream.rest()
            assert [frame.get("event") for frame in frames] == ["session.stopped", "result"]
            assert frames[1]["data"]["final"] is True
        finally:
            await stream.close()
    finally:
        await hub.close()


async def test_a_delete_ends_the_stream_without_a_result(tmp_path, api_token):
    hub = Hub(_manager(_home(tmp_path)))
    try:
        uid, sid, swarm = await running(hub, paused=True)
        stream = Stream(hub, uid, query=f"after={journal_head(swarm)}")
        try:
            await asyncio.sleep(0.1)
            status, answer = await hub.call("DELETE", f"/api/v1/sessions/{uid}", query=f"confirm={uid}")
            assert status == 200, answer
            frames = await stream.rest()
            assert "result" not in [frame.get("event") for frame in frames]
        finally:
            await stream.close()
    finally:
        await hub.close()


async def test_a_stream_is_refused_before_it_starts(tmp_path, api_token, no_start):
    hub = Hub(_manager(_home(tmp_path)))
    no_start(hub.manager)
    try:
        stream = Stream(hub, "f" * 32)
        assert json.loads(await stream.body())["code"] == "not_found" and stream.status == 404
        await stream.close()
        status, answer = await hub.create({"goal": "G"})
        uid = answer["session"]["id"]
        for kwargs, code in (({"headers": {"last-event-id": "abc"}}, "bad_request"),
                             ({"query": "wait=1"}, "unknown_field"), ({"query": "detail=x"}, "bad_request")):
            stream = Stream(hub, uid, **kwargs)
            assert stream.status is None or stream.status == 400
            assert json.loads(await stream.body())["code"] == code and stream.status == 400
            await stream.close()
    finally:
        await hub.close()


def test_an_sse_event_is_one_frame():
    frame = sse_event({"id": 3, "type": "task\ncreated", "ts": 1.0, "actor": None,
                       "data": {"text": "two\nlines", "x": math.nan}, "text": None})
    lines = frame.split("\n")
    assert frame.endswith("\n\n") and lines[0] == "id: 3" and lines[1] == "event: task created"
    assert lines[2].startswith("data: ") and json.loads(lines[2][6:])["data"] == {"text": "two\nlines", "x": None}
    assert len(lines) == 5
    assert safe_json({"a": float("inf")}) == '{"a":null}' and safe_json({"a": 1.5}) == '{"a":1.5}'
