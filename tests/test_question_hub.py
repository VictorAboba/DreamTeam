"""Clarify mode and a member's question that pauses the swarm, on the hub (docs/SPEC.md §3 "Clarify mode", issue #32):
the auto-reply lease of a `dreamteam -p` client (models.AUTO_REPLY_HEADER on a create, a start, /api/act and the event
stream), the session summary and read_stats (`clarify`, `question`), the launcher MCP (`session_create clarify`,
session_line, the instructions) and the hub MCP's `send_message pause_swarm`. Real sessions on ScriptedLLM only; the
launcher app through Starlette's TestClient (no port is bound)."""

from __future__ import annotations

import json
import re
import time
from types import SimpleNamespace

import pytest

from dreamteam import prompts
from dreamteam import swarm as swarm_module
from dreamteam.hub_mcp import INSTRUCTIONS as HUB_INSTRUCTIONS
from dreamteam.hub_mcp import list_hub_tools
from dreamteam.launcher_mcp import INSTRUCTIONS as LAUNCHER_INSTRUCTIONS
from dreamteam.launcher_mcp import call_launcher_tool, list_launcher_tools, session_line
from dreamteam.models import AUTO_REPLY_HEADER
from dreamteam.sessions import SessionError, read_stats
from dreamteam.swarm import AUTO_REPLY_GRACE
from dreamteam.tools.comm_tools import ANSWER_NOTES, PAUSE_NOTES
from test_launcher import _manager, _post, client  # noqa: F401

AUTO = {AUTO_REPLY_HEADER: "1"}
WS = "ws://127.0.0.1:8765"
CLARIFY = {"swarm": {"clarify": True}}


def _post_with(c, path, body, headers):
    return c.post(path, content=json.dumps(body), headers={"content-type": "application/json", **headers})


def _planning(c, sid) -> dict:
    """The planning card of a session, as the board reads it."""
    tasks = c.get(f"/s/{sid}/api/state").json()["tasks"]
    return next(t for t in tasks if "planning" in t["tags"])


def _until(check, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.02)
    return check()


@pytest.fixture
def clock(monkeypatch):
    """The lease's clock (swarm._monotonic; never time.monotonic itself, which the event loop reads too)."""
    now = [1000.0]
    monkeypatch.setattr(swarm_module, "_monotonic", lambda: now[0])
    return now


# --- the auto-reply lease --------------------------------------------------------------------------------------

def test_the_auto_reply_header_leases_a_new_session_before_its_start(client):
    """A `dreamteam -p` client with automatic replies creates its session with the header: the lease holds before the
    first start, so a session that clarifies gets the classic planning card (nobody answers the questions). The same
    create without the header gets the clarify card. The request body is the same."""
    c = client
    body = {"title": "Auto", "goal": "Write snake", "overrides": CLARIFY}
    resp = _post_with(c, "/api/sessions", body, AUTO)
    assert resp.status_code == 201, resp.text
    auto = resp.json()["session"]
    assert auto["clarify"] is True and auto["status"] == "running"
    card = _planning(c, auto["id"])
    assert card["title"] == prompts.PLANNING_TASK["en"][0] and card["tags"] == ["planning"]
    state = c.get(f"/s/{auto['id']}/api/state").json()["swarm"]   # (read without the header: the lease holds)
    assert state["auto_reply"] is True and state["clarify"] is True
    assert c.manager.swarms[auto["id"]].clarifying is False

    resp = _post(c, "/api/sessions", {**body, "title": "Human"})
    assert resp.status_code == 201, resp.text
    human = resp.json()["session"]
    card = _planning(c, human["id"])
    assert card["title"] == prompts.planning_task("en", True)[0] and card["tags"] == ["planning", prompts.CLARIFY_TAG]
    state = c.get(f"/s/{human['id']}/api/state").json()["swarm"]
    assert state["auto_reply"] is False and state["clarify"] is True
    assert c.manager.swarms[human["id"]].clarifying is True


def test_act_and_stream_with_the_header_hold_the_lease(client, clock):
    """/api/act with the header touches the lease (AUTO_REPLY_GRACE after the last sign); an event stream opened with
    it holds the lease while it is open and for the grace after it closed. Without the header nothing changes."""
    c = client
    resp = _post(c, "/api/sessions", {"title": "Lease", "goal": "Lease it", "overrides": CLARIFY})
    sid = resp.json()["session"]["id"]
    swarm = c.manager.swarms[sid]
    assert not swarm.auto_reply_active
    assert _post(c, f"/s/{sid}/api/act/swarm_status", {}).json()["ok"]
    assert not swarm.auto_reply_active   # the board, the TUI, the console: no header
    with c.websocket_connect(f"{WS}/s/{sid}/ws"):
        assert not swarm.auto_reply_active
    resp = _post_with(c, f"/s/{sid}/api/act/swarm_status", {}, AUTO)
    assert resp.json()["ok"], resp.text
    assert swarm.auto_reply_active and not swarm.clarifying
    assert "CLARIFY MODE" not in resp.json()["result"]   # (the act runs after the touch: the status says so)
    clock[0] += AUTO_REPLY_GRACE - 1
    assert swarm.auto_reply_active
    clock[0] += 2
    assert not swarm.auto_reply_active and swarm.clarifying

    with c.websocket_connect(f"{WS}/s/{sid}/ws", headers=AUTO):
        assert swarm.auto_reply_active
        clock[0] += 10 * AUTO_REPLY_GRACE   # open: no deadline
        assert swarm.auto_reply_active
        assert c.get(f"/s/{sid}/api/state").json()["swarm"]["auto_reply"] is True
    assert _until(lambda: swarm._auto_reply_streams == 0)   # the server saw the close
    assert swarm.auto_reply_active   # the grace after the last sign
    clock[0] += AUTO_REPLY_GRACE + 1
    assert not swarm.auto_reply_active
    assert c.get(f"/s/{sid}/api/state").json()["swarm"]["auto_reply"] is False


def test_start_with_the_header_leases_a_running_session(client):
    """POST /api/sessions/{sid}/start with the header touches the lease of a running session (a no-op start), and the
    first start of a stopped session holds it before its pending goal is set: the classic card."""
    c = client
    sid = _post(c, "/api/sessions", {"title": "Running", "goal": "Run it"}).json()["session"]["id"]
    swarm = c.manager.swarms[sid]
    assert not swarm.auto_reply_active
    assert _post(c, f"/api/sessions/{sid}/start", {}).status_code == 200
    assert not swarm.auto_reply_active
    resp = _post_with(c, f"/api/sessions/{sid}/start", {}, AUTO)
    assert resp.status_code == 200, resp.text
    assert swarm.auto_reply_active and c.manager.swarms[sid] is swarm

    body = {"goal": "Plan it", "overrides": CLARIFY, "start": False}
    auto = _post(c, "/api/sessions", {**body, "title": "Later auto"}).json()["session"]["id"]
    human = _post(c, "/api/sessions", {**body, "title": "Later human"}).json()["session"]["id"]
    assert auto not in c.manager.swarms   # nothing to lease before the start
    resp = _post_with(c, f"/api/sessions/{auto}/start", {}, AUTO)
    assert resp.status_code == 200 and resp.json()["session"]["status"] == "running", resp.text
    assert _post(c, f"/api/sessions/{human}/start", {}).json()["session"]["status"] == "running"
    assert _planning(c, auto)["tags"] == ["planning"] and c.manager.swarms[auto].auto_reply_active
    assert _planning(c, human)["tags"] == ["planning", prompts.CLARIFY_TAG]
    assert not c.manager.swarms[human].auto_reply_active


async def test_the_manager_touches_the_lease_before_the_swarm_starts(tmp_path, monkeypatch):
    """SessionManager.create/start(auto_reply=True): the lease holds when swarm.start() runs (an agent may ask before
    the `-p` client's event stream connects), and again right before the pending goal is set (a long start would
    otherwise outlast the grace)."""
    manager = _manager(tmp_path)
    seen: list[tuple[str, bool]] = []
    real_start, real_set_goal = swarm_module.Swarm.start, swarm_module.Swarm.set_goal
    now = [1000.0]
    monkeypatch.setattr(swarm_module, "_monotonic", lambda: now[0])

    async def start(self):
        seen.append(("start", self.auto_reply_active))
        now[0] += AUTO_REPLY_GRACE + 1   # a start longer than the grace
        await real_start(self)

    def set_goal(self, *args, **kwargs):
        seen.append(("set_goal", self.auto_reply_active))
        return real_set_goal(self, *args, **kwargs)

    monkeypatch.setattr(swarm_module.Swarm, "start", start)
    monkeypatch.setattr(swarm_module.Swarm, "set_goal", set_goal)
    try:
        summary = await manager.create("Auto", "Plan it", overrides=CLARIFY, auto_reply=True)
        assert seen == [("start", True), ("set_goal", True)]
        swarm = manager.swarms[summary["id"]]
        assert [t.tags for t in swarm.board.list(tag="planning")] == [["planning"]]
        seen.clear()
        summary = await manager.create("Human", "Plan it", overrides=CLARIFY)
        assert seen == [("start", False), ("set_goal", False)]
        assert [t.tags for t in manager.swarms[summary["id"]].board.list(tag="planning")] == [
            ["planning", prompts.CLARIFY_TAG]]
    finally:
        await manager.shutdown()


# --- the session summary, read_stats and session_line -----------------------------------------------------------

async def test_session_create_and_update_with_clarify(tmp_path):
    """session_create's `clarify` is a session override, set only when given (false overrides a global true); any
    other value is refused before anything is created. The summary has the effective `clarify`."""
    manager = _manager(tmp_path)
    try:
        create = next(t for t in list_launcher_tools(manager) if t.name == "session_create")
        prop = create.input_schema["properties"]["clarify"]
        assert prop["type"] == "boolean" and "[swarm] clarify" in prop["description"]
        assert "clarify" not in create.input_schema["required"]
        text, err = await call_launcher_tool(manager, "session_create", {"goal": "Ask first", "clarify": True,
                                                                         "start": False})
        assert not err, text
        sid = manager.ids()[0]
        assert manager.meta(sid)["overrides"] == {"swarm": {"clarify": True}}
        summary = manager.summary(sid)
        assert summary["clarify"] is True and summary["question"] is None
        assert text.splitlines()[-2].endswith(" | clarify")   # the session_line of the result (then its goal line)
        text, err = await call_launcher_tool(manager, "session_create", {"goal": "Plain", "start": False})
        assert not err and manager.meta(manager.ids()[1])["overrides"] == {}   # only when given
        assert manager.summary(manager.ids()[1])["clarify"] is False and " | clarify" not in text

        for bad in ("true", 1, "yes", [True]):
            text, err = await call_launcher_tool(manager, "session_create", {"goal": "Bad", "clarify": bad})
            assert err and "clarify must be true or false" in text, (bad, text)
        assert len(manager.ids()) == 2   # nothing was created

        # a global true: false is kept as the session's own setting, and a session without one inherits true
        await manager.update_settings({"swarm": {**manager.raw["swarm"], "clarify": True}})
        assert manager.summary(manager.ids()[1])["clarify"] is True
        text, err = await call_launcher_tool(manager, "session_create", {"goal": "Not this one", "clarify": False,
                                                                         "start": False})
        assert not err, text
        off = manager.ids()[2]
        assert manager.meta(off)["overrides"] == {"swarm": {"clarify": False}}
        assert manager.summary(off)["clarify"] is False

        # the session settings (the ⚙ dialog, /clarify in the TUI): null inherits again, a non-bool is refused
        assert manager.update(off, settings={"swarm": {"clarify": None}})["clarify"] is True
        assert manager.meta(off)["overrides"] == {}
        assert manager.update(off, settings={"swarm": {"clarify": False}})["clarify"] is False
        for bad in ("yes", 1):
            with pytest.raises(SessionError, match=r"\[swarm\] clarify must be true or false"):
                manager.update(off, settings={"swarm": {"clarify": bad}})
        assert manager.meta(off)["overrides"] == {"swarm": {"clarify": False}}   # nothing written
        with pytest.raises(SessionError, match=r"\[swarm\] clarify must be true or false"):
            await manager.create("x", "goal", overrides={"swarm": {"clarify": "on"}}, start=False)
    finally:
        await manager.shutdown()


async def test_read_stats_and_session_line_show_a_question(tmp_path):
    """kv `paused` holding a member's question gives the summary's `question` {asker, message_id} (from the loaded
    swarm's connection and from the file); session_line names it, and ends its first line with " | clarify" in a
    session that clarifies. Other pauses, and a damaged slot, have no question."""
    manager = _manager(tmp_path)
    try:
        summary = await manager.create("Q", "Plan it", overrides=CLARIFY, start=False)
        sid = summary["id"]
        assert summary["question"] is None and summary["paused"] is False
        swarm = await manager.load(sid)
        swarm.db.set("paused", {"paused": True, "reason": "question", "asker": "Anna Smith", "message_id": 12,
                                "since": 1730000000.0})
        loaded = manager.summary(sid)
        assert loaded["question"] == {"asker": "Anna Smith", "message_id": 12}
        assert loaded["paused"] is True and loaded["pause_reason"] == "question"
        db_path = swarm.config.data_dir / "swarm.db"
        await manager.unload_idle(sid)
        assert sid not in manager.swarms
        assert manager.summary(sid)["question"] == {"asker": "Anna Smith", "message_id": 12}   # read from the file
        assert read_stats(db_path)["question"] == {"asker": "Anna Smith", "message_id": 12}

        running = {**manager.summary(sid), "status": "running"}
        first = session_line(running).splitlines()[0]
        assert first.startswith(f"{sid} [paused (waiting for your answer to Anna Smith, question #12)] Q | tasks")
        assert first.endswith(" | clarify")
        assert not session_line({**running, "clarify": False}).splitlines()[0].endswith(" | clarify")
        assert session_line({**running, "status": "stopped"}).startswith(f"{sid} [stopped] Q")

        swarm = await manager.load(sid)
        for slot in ({"paused": True, "reason": "budget"},
                     {"paused": True, "reason": "question", "asker": "Anna Smith"},   # damaged: no message id
                     {"paused": True, "reason": "question", "asker": "", "message_id": 12},
                     {"paused": False, "reason": ""}):
            swarm.db.set("paused", slot)
            assert manager.summary(sid)["question"] is None, slot
        swarm.db.set("paused", {"paused": True, "reason": "budget"})
        assert session_line({**manager.summary(sid), "status": "running"}).startswith(f"{sid} [paused (budget)] Q")
    finally:
        await manager.shutdown()


# --- the MCP servers ---------------------------------------------------------------------------------------------

def test_hub_tools_offer_pause_swarm():
    """The hub MCP lists the live registry: send_message has pause_swarm (toolset revision 4), for external members
    and for the launcher MCP (which adds `session`). The instructions say how the user answers a question pause; the
    launcher's own part adds only the clarify=true sentence (the answer rule is in the hub's part it ends with)."""
    for tools in (list_hub_tools(SimpleNamespace(language="en")), list_hub_tools(SimpleNamespace(language="ru")),
                  list_launcher_tools(SimpleNamespace(config=SimpleNamespace(swarm=SimpleNamespace(language="en"))))):
        schema = next(t for t in tools if t.name == "send_message").input_schema
        assert schema["properties"]["pause_swarm"]["type"] == "boolean"
        assert "pause_swarm" not in schema.get("required", []) and "as_agent" in schema["properties"]
    answer = ("When `swarm_status` says the swarm is paused waiting for\nyour answer to a member's question, your next "
              "`send_message` to that member (or to all) is the answer: every member\ngets it with the question and "
              "the swarm goes on; `swarm_control` action=resume goes on\nwithout it.")
    assert answer in HUB_INSTRUCTIONS
    assert "`jobs` and `job_stop` list and stop every member's\nbackground jobs." in HUB_INSTRUCTIONS
    own = LAUNCHER_INSTRUCTIONS[:-len(HUB_INSTRUCTIONS)]
    assert LAUNCHER_INSTRUCTIONS.endswith(HUB_INSTRUCTIONS)
    assert ("A session created with clarify=true first asks you about its goal; `sessions_list` shows a session that "
            "waits for\nyour answer as \"paused (waiting for your answer to X, question #N)\".") in own
    assert "is the answer" not in own and LAUNCHER_INSTRUCTIONS.count("is the answer") == 1


def test_hub_tools_offer_crew_and_directions():
    """The hub MCP lists the live registry: task_create and task_update have crew and directions (toolset revision 5),
    optional, for external members and the user (`as_agent`) and for the launcher MCP (which adds `session`)."""
    for tools in (list_hub_tools(SimpleNamespace(language="en")), list_hub_tools(SimpleNamespace(language="ru")),
                  list_launcher_tools(SimpleNamespace(config=SimpleNamespace(swarm=SimpleNamespace(language="en"))))):
        for name in ("task_create", "task_update"):
            schema = next(t for t in tools if t.name == name).input_schema
            props = schema["properties"]
            assert props["crew"]["type"] == "integer", name
            assert props["directions"]["type"] == "array" and props["directions"]["items"] == {"type": "string"}
            assert not {"crew", "directions"} & set(schema.get("required", [])) and "as_agent" in props, name


async def test_a_question_pause_through_the_launcher_mcp(tmp_path):
    """L20/L21 through the launcher MCP: an external member asks the user with pause_swarm (hub_call with as_agent),
    sessions_list and swarm_status name the question, and the user's send_message to the member answers it."""
    manager = _manager(tmp_path)
    try:
        text, err = await call_launcher_tool(manager, "session_create", {"goal": "Ask it", "clarify": True})
        assert not err, text
        sid = manager.ids()[0]
        text, err = await call_launcher_tool(manager, "join_swarm", {"session": sid, "role": "Helper"})
        assert not err, text
        swarm = manager.swarms[sid]
        ext = next(m["name"] for m in swarm.members() if m["kind"] == "external")
        text, err = await call_launcher_tool(manager, "send_message", {
            "session": sid, "as_agent": ext, "to": ["user"], "text": "SQLite or Postgres?", "pause_swarm": True})
        assert not err, text
        mid = int(re.match(r"Sent message #(\d+) to user\.", text).group(1))
        assert text.endswith(PAUSE_NOTES["asked_ext"]) and swarm.paused
        assert swarm.pending_question()["asker"] == ext

        text, err = await call_launcher_tool(manager, "sessions_list", {})
        assert not err and f"{sid} [paused (waiting for your answer to {ext}, question #{mid})]" in text, text
        assert text.splitlines()[1].endswith(" | clarify")
        text, err = await call_launcher_tool(manager, "swarm_status", {"session": sid})
        assert not err and f"PAUSED: waiting for the user's answer to {ext}'s question #{mid}" in text, text

        text, err = await call_launcher_tool(manager, "send_message", {"session": sid, "to": [ext], "text": "SQLite."})
        assert not err, text
        assert text.endswith(ANSWER_NOTES["ok"].format(asker=ext, id=mid)) and not swarm.paused
        text, err = await call_launcher_tool(manager, "sessions_list", {})
        assert not err and f"{sid} [running]" in text, text
        assert manager.summary(sid)["question"] is None
    finally:
        await manager.shutdown()
