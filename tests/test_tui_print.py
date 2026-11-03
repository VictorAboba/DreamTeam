"""Print mode (`dreamteam -p`, dreamteam.tui.printmode) against a real in-process launcher with scripted agents."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import socket
import string
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import httpx2
import pytest

from dreamteam import prompts
from dreamteam import settings as st
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.models import AUTO_REPLY_HEADER
from dreamteam.swarm import Swarm
from dreamteam.tui import client as client_module
from dreamteam.tui import printmode
from dreamteam.tui.client import Connection, HubClient, HubError, connect
from dreamteam.tui.printmode import AUTO_REPLY, AUTO_REVIEW, run_print

MARK = "haiku"


def call(name: str, **arguments: Any) -> dict:
    return {"tool_calls": [{"name": name, "arguments": arguments}]}


def texts(messages: list[dict]) -> list[str]:
    return [m.get("content") or "" for m in messages if m["role"] in ("user", "tool")]


def finisher(summary: str = "The haiku is in out/haiku.txt.") -> Callable:
    """An agent that declares the goal achieved as soon as it hears about it."""

    def policy(system, messages, tools):
        seen = texts(messages)
        last = seen[-1] if seen else ""
        if "marked as achieved" in last or "already marked" in last:
            return call("wait")
        if any(MARK in t for t in seen):
            return call("goal_complete", summary=summary)
        return call("wait")

    return policy


def asker(system, messages, tools):
    """Asks the user first; completes the goal once told to decide alone."""
    seen = texts(messages)
    last = seen[-1] if seen else ""
    if "decide yourself" in last:
        return call("goal_complete", summary="Picked the colour myself: blue.")
    if "marked as achieved" in last or "already marked" in last:
        return call("wait")
    if any(MARK in t for t in seen) and not any("Sent message" in t for t in seen):
        return call("send_message", to=["user"], text="Which colour should the haiku be about?")
    return call("wait")


def idler(system, messages, tools):
    return call("wait")


def planner(system, messages, tools):
    """The only agent: claims the planning task and completes it (its review goes to the user: nobody else can
    review it), then completes the goal once the board says that every task is finished."""
    seen = texts(messages)
    last = seen[-1] if seen else ""
    if "marked as achieved" in last or "already marked" in last:
        return call("wait")
    if any("All tasks on the board are finished" in t for t in seen):
        return call("goal_complete", summary="Planned, reviewed and done.")
    if "You now work on" in last:
        return call("task_complete", task_id=1, result="One task is enough: the haiku itself.")
    if any(MARK in t for t in seen) and not any("You now work on" in t for t in seen):
        return call("task_claim", task_id=1)
    return call("wait")


def asking_planner(system, messages, tools):
    """The planner, after one question to the user (answered automatically)."""
    seen = texts(messages)
    if any("decide yourself" in t for t in seen):
        return planner(system, messages, tools)
    if any(MARK in t for t in seen) and not any("Sent message" in t for t in seen):
        return call("send_message", to=["user"], text="Which colour should the haiku be about?")
    return call("wait")


def pausing_asker(results: list[str] | None = None) -> Callable:
    """`asker`, whose question asks to pause the whole swarm until the user answers (send_message pause_swarm,
    docs/SPEC.md §3 "Clarify mode"); it completes the goal on any answer. `results`: its send_message results."""

    def policy(system, messages, tools):
        seen = texts(messages)
        if results is not None:
            results.extend(t for t in seen if t.startswith("Sent message") and t not in results)
        last = seen[-1] if seen else ""
        if "marked as achieved" in last or "already marked" in last:
            return call("wait")
        if "decide yourself" in last or "blue" in last:
            return call("goal_complete", summary="The haiku is blue.")
        if any(MARK in t for t in seen) and not any("Sent message" in t for t in seen):
            return call("send_message", to=["user"], text="Which colour should the haiku be about?",
                        pause_swarm=True)
        return call("wait")

    return policy


def spender(system, messages, tools):
    return {**call("board"), "usage": {"input_tokens": 10, "output_tokens": 5, "cost_usd": 5.0}}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    st.write_raw(home, {
        "swarm": {"name": "Home", "language": "en", "start_agents": 1, "max_agents": 3, "max_cost_usd": 1.0},
        "agents": {"stagger_seconds": 0, "idle_nudge_seconds": 30},
        "server": {"port": free_port()},
        "workspace": {"git": False},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
    })
    return home


@pytest.fixture
async def launch(tmp_path):
    opened: list[Connection] = []

    async def start(policy: Callable) -> Connection:
        conn = await connect(make_home(tmp_path), web=False,
                             llm_factory=lambda profile, agent: ScriptedLLM(policy, profile))
        opened.append(conn)
        return conn

    yield start
    for conn in opened:
        await conn.close()


async def run(conn: Connection, prompt: str, **kwargs: Any) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    kwargs.setdefault("timeout", 30)
    code = await run_print(conn, prompt, out=out, err=err, **kwargs)
    return code, out.getvalue(), err.getvalue()


async def test_new_session_text_and_json(launch):
    conn = await launch(finisher())
    code, out, err = await run(conn, f"Write a {MARK} about the sea")
    assert code == 0, err
    assert out.strip() == "The haiku is in out/haiku.txt."
    sessions = await conn.client.sessions()
    assert len(sessions) == 1 and sessions[0]["title"] == f"Write a {MARK} about the sea"
    assert sessions[0]["goal"]["status"] == "done"

    code, out, err = await run(conn, f"Another {MARK}", output_format="json", title="Second")
    assert code == 0, err
    result = json.loads(out)
    assert result["type"] == "result" and result["status"] == "done"
    assert result["goal"]["status"] == "done" and result["goal"]["title"] == f"Another {MARK}"
    assert result["summary"] == "The haiku is in out/haiku.txt."
    sid = result["session"]
    assert (await conn.client.session(sid))["session"]["title"] == "Second"
    assert result["tasks"] and set(result["tasks"][0]) == {"id", "title", "status", "assignee"}
    assert isinstance(result["cost_usd"], float) and result["duration_s"] >= 0
    assert len(result["agents"]) == 1 and isinstance(result["agents"][0], str)


async def test_stream_json_and_verbose_progress(launch):
    conn = await launch(finisher())
    code, out, err = await run(conn, f"Stream a {MARK}", output_format="stream-json")
    assert code == 0, err
    lines = [json.loads(line) for line in out.splitlines()]
    result = lines[-1]
    assert result["type"] == "result" and result["status"] == "done"
    events = lines[:-1]
    assert events and all(set(ev) == {"type", "ts", "actor", "data"} for ev in events)
    types = [ev["type"] for ev in events]
    assert "goal.set" in types and types[-1] == "goal.completed"
    assert not any(t == "_connection" for t in types)

    code, out, err = await run(conn, f"Verbose {MARK}", verbose=True)
    assert code == 0 and out.strip() == "The haiku is in out/haiku.txt."
    assert err.strip()  # the progress feed


async def test_auto_reply_answers_agent_questions(launch):
    conn = await launch(asker)
    code, out, err = await run(conn, f"A {MARK} in a colour", output_format="json")
    assert code == 0, err
    result = json.loads(out)
    assert result["summary"] == "Picked the colour myself: blue."
    sid = result["session"]
    agent = result["agents"][0]
    conversation = await conn.client.messages(sid, with_=agent)
    question = next(m for m in conversation if m["sender"] == agent)
    reply = next(m for m in conversation if m["sender"] == "user")
    assert reply["text"] == AUTO_REPLY["en"] and reply["reply_to"] == question["id"]


async def test_a_goal_completed_during_the_catch_up_is_streamed(launch):
    """The scripted agent asks before the stream connects; the auto-reply sent from the catch-up makes it
    complete the goal at once, so the state says "done" while events are still unreported: they are
    reported (up to goal.completed) before the result."""
    conn = await launch(asker)
    code, out, err = await run(conn, f"A {MARK} in a colour", output_format="stream-json")
    assert code == 0, err
    lines = [json.loads(line) for line in out.splitlines()]
    assert lines[-1]["type"] == "result" and lines[-1]["status"] == "done"
    types = [ev["type"] for ev in lines[:-1]]
    assert types[-1] == "goal.completed"
    replies = [ev for ev in lines if ev["type"] == "message.sent" and ev["data"]["message"]["sender"] == "user"]
    assert [ev["data"]["message"]["text"] for ev in replies] == [AUTO_REPLY["en"]]


async def test_without_auto_reply_the_question_stays_unanswered(launch):
    conn = await launch(asker)
    code, out, err = await run(conn, f"A {MARK} in a colour", auto_reply=False, timeout=1.5)
    assert code == 3 and out == "" and "1.5" in err
    sid = (await conn.client.sessions())[0]["id"]
    assert all(m["sender"] != "user" for m in await conn.client.messages(sid))


async def test_user_reviews_are_auto_approved(launch):
    conn = await launch(planner)
    code, out, err = await run(conn, f"Plan a {MARK}", output_format="json")
    assert code == 0, err
    result = json.loads(out)
    assert result["summary"] == "Planned, reviewed and done."
    assert [t["status"] for t in result["tasks"]] == ["done"]
    data = await conn.client.task(result["session"], 1)
    assert data["task"]["status"] == "done"
    assert [p["text"] for p in data["discussion"] if p["author"] == "user"] == [AUTO_REVIEW["en"]]


async def test_auto_approvals_are_not_limited_by_the_auto_reply_cap(launch, monkeypatch):
    # the cap guards against a question / auto-answer ping-pong; a long one-agent run sends every task to the user
    monkeypatch.setattr(printmode, "AUTO_REPLY_MAX", 1)
    conn = await launch(asking_planner)
    code, out, err = await run(conn, f"Plan a {MARK}", output_format="json")
    assert code == 0, err
    result = json.loads(out)
    assert result["summary"] == "Planned, reviewed and done."
    assert [t["status"] for t in result["tasks"]] == ["done"]
    sid = result["session"]
    assert [m["text"] for m in await conn.client.messages(sid) if m["sender"] == "user"] == [AUTO_REPLY["en"]]


async def test_without_auto_reply_a_user_review_waits_until_the_timeout(launch):
    conn = await launch(planner)
    code, out, err = await run(conn, f"Plan a {MARK}", auto_reply=False, timeout=2.5, output_format="json")
    assert code == 3, err
    result = json.loads(out)
    assert result["status"] == "timeout" and [t["status"] for t in result["tasks"]] == ["review"]
    task = (await conn.client.task(result["session"], 1))["task"]
    assert task["reviewer"] == "user" and task["status"] == "review"


# ---- clarify mode and questions that pause the swarm (docs/SPEC.md §3 "Clarify mode") -------------------------
CLARIFY = {"swarm": {"clarify": True}}


def board_url(conn: Connection, sid: str) -> str:
    return f"{conn.client.base_url}/s/{sid}/"


async def test_clarify_with_auto_reply_never_pauses(launch):
    """With automatic replies the run holds the hub's lease (models.AUTO_REPLY_HEADER) from the session's creation
    on, before the agent's first step: the session plans as usual and a question with pause_swarm pauses nothing,
    even when the session clarifies; everything works as before."""
    results: list[str] = []
    conn = await launch(pausing_asker(results))
    code, out, err = await run(conn, f"A {MARK} in a colour", overrides=CLARIFY, output_format="stream-json")
    assert code == 0, err
    lines = [json.loads(line) for line in out.splitlines()]
    result = lines[-1]
    assert result["status"] == "done" and result["summary"] == "The haiku is blue."
    assert not [ev for ev in lines[:-1] if ev["type"] in ("swarm.paused", "swarm.resumed")]
    sid = result["session"]
    assert (await conn.client.session(sid))["session"]["overrides"] == CLARIFY
    planning = next(t for t in (await conn.client.state(sid))["tasks"] if "planning" in t["tags"])
    assert planning["title"] == prompts.PLANNING_TASK["en"][0] and planning["tags"] == ["planning"]
    assert any("pause_swarm was ignored: the user is not available" in r for r in results), results
    agent = result["agents"][0]
    conversation = await conn.client.messages(sid, with_=agent)
    question = next(m for m in conversation if m["sender"] == agent)
    reply = next(m for m in conversation if m["sender"] == "user")
    assert reply["text"] == AUTO_REPLY["en"] and reply["reply_to"] == question["id"]
    assert conn.manager.swarms[sid].auto_reply_active   # (the grace after the run's last request)


async def test_print_mode_restores_the_clients_header(launch):
    conn = await launch(finisher())
    assert conn.client.auto_reply is False
    code, out, err = await run(conn, f"Write a {MARK}", output_format="json")
    assert code == 0, err
    assert conn.client.auto_reply is False   # the shared client of the connection sends no header after the run
    sid = json.loads(out)["session"]
    assert conn.manager.swarms[sid].auto_reply_active      # it did during the run
    code, out, err = await run(conn, f"Write another {MARK}", output_format="json", auto_reply=False)
    assert code == 0, err
    assert not conn.manager.swarms[json.loads(out)["session"]].auto_reply_active and conn.client.auto_reply is False


async def test_print_client_sends_the_auto_reply_header(monkeypatch):
    """HubClient.auto_reply: every request and the stream's handshake carry the header; off by default."""
    seen: list[Any] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers.get(AUTO_REPLY_HEADER))
        return httpx2.Response(200, json={"mode": "launcher", "ok": True, "result": "ok"})

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    client = HubClient("http://127.0.0.1:9", http=http)
    handshakes: list[Any] = []

    async def no_hub(url, **kw):
        handshakes.append(dict(kw.get("additional_headers") or {}))
        raise OSError("no hub here")

    monkeypatch.setattr(client_module, "ws_connect", no_hub)
    try:
        await client.hub_info()
        await client.act("001-x", "swarm_status")
        client.auto_reply = True
        await client.hub_info()
        await client.act("001-x", "send_message", {"to": ["user"], "text": "x"})
        with pytest.raises(OSError):
            await client._ws_open(client.ws_url("001-x"), {})
        client.auto_reply = False
        with pytest.raises(OSError):
            await client._ws_open(client.ws_url("001-x"), {})
    finally:
        await http.aclose()
    assert seen == [None, None, "1", "1"]
    assert [h.get(AUTO_REPLY_HEADER) for h in handshakes] == ["1", None]


async def test_without_auto_reply_a_question_pause_waits_and_says_so(launch):
    """--no-auto-reply in a clarifying session: a question with pause_swarm pauses the swarm and nobody answers; the
    run says so once on stderr (text and json) and ends at --timeout; stream-json has the swarm.paused event."""
    conn = await launch(pausing_asker())
    code, out, err = await run(conn, f"A {MARK} in a colour", overrides=CLARIFY, auto_reply=False, timeout=3)
    assert code == 3 and out == "", err
    sid = (await conn.client.sessions())[0]["id"]
    swarm = (await conn.client.state(sid))["swarm"]
    assert swarm["paused"] and swarm["pause_reason"] == "question" and not swarm["auto_reply"]
    question = swarm["question"]
    told = printmode.TEXTS["en"]["question"].format(name=question["asker"], id=question["message_id"],
                                                    url=board_url(conn, sid))
    assert err.count(told) == 1, err
    asked = printmode.TEXTS["en"]["asked_timeout"].format(name=question["asker"], id=question["message_id"],
                                                          url=board_url(conn, sid))
    assert err.count(asked) == 1   # (every agent message to the user of a clarifying session)
    assert all(m["sender"] != "user" for m in await conn.client.messages(sid))

    code, out, err = await run(conn, f"Another {MARK} in a colour", overrides=CLARIFY, auto_reply=False, timeout=3,
                               output_format="stream-json")
    assert code == 3
    events = [json.loads(line) for line in out.splitlines()][:-1]
    paused = [ev for ev in events if ev["type"] == "swarm.paused"]
    assert len(paused) == 1 and paused[0]["data"]["reason"] == "question" and paused[0]["data"]["asker"]
    assert "The swarm waits for your answer" not in err and " asks you (message #" not in err


async def test_without_auto_reply_a_clarify_question_is_announced(launch):
    """--no-auto-reply in a clarifying session: the planner's ordinary question (it pauses nothing) gets one stderr
    line naming the asker and the board; without clarify nothing is printed, as before."""
    conn = await launch(asker)
    code, out, err = await run(conn, f"A {MARK} in a colour", overrides=CLARIFY, auto_reply=False, timeout=2.5)
    assert code == 3 and out == "", err
    sid = (await conn.client.sessions())[0]["id"]
    planning = next(t for t in (await conn.client.state(sid))["tasks"] if "planning" in t["tags"])
    assert planning["tags"] == ["planning", "clarify"]
    agent = next(m for m in await conn.client.messages(sid) if "user" in m["recipients"])
    text = printmode.TEXTS["en"]["asked_timeout"].format(name=agent["sender"], id=agent["id"],
                                                         url=board_url(conn, sid))
    assert err.count(text) == 1 and f"/s/{sid}/" in text, err
    assert "The swarm waits for your answer" not in err   # an ordinary question: no pause

    code, out, err = await run(conn, f"Another {MARK} in a colour", auto_reply=False, timeout=2.5)
    assert code == 3 and " asks you (message #" not in err
    code, out, err = await run(conn, f"A third {MARK}", overrides=CLARIFY, auto_reply=False, timeout=2.5,
                               output_format="stream-json")
    assert code == 3 and " asks you (message #" not in err   # stream-json: unchanged


async def test_without_a_timeout_the_clarify_question_line_says_the_run_waits(launch):
    """Without --timeout only the goal ends the run: the `asked` line says that the run waits too (no word of
    --timeout); a human's answer on the board then lets the planner go on."""
    conn = await launch(asker)
    out, err = io.StringIO(), io.StringIO()
    task = asyncio.create_task(run_print(conn, f"A {MARK} in a colour", overrides=CLARIFY, auto_reply=False,
                                         out=out, err=err))
    try:
        async with asyncio.timeout(20):
            while " asks you (message #" not in err.getvalue():
                assert not task.done(), err.getvalue()
                await asyncio.sleep(0.05)
        sid = (await conn.client.sessions())[0]["id"]
        agent = next(m for m in await conn.client.messages(sid) if "user" in m["recipients"])
        text = printmode.TEXTS["en"]["asked"].format(name=agent["sender"], id=agent["id"], url=board_url(conn, sid))
        assert err.getvalue().count(text) == 1 and "--timeout" not in err.getvalue(), err.getvalue()
        ok, result = await conn.client.act(sid, "send_message", {"to": [agent["sender"]],
                                                                 "text": "Please decide yourself."})
        assert ok, result
        async with asyncio.timeout(20):
            code = await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert code == 0 and out.getvalue().strip() == "Picked the colour myself: blue.", err.getvalue()


def test_texts_are_in_both_languages():
    def fields(text: str) -> list[str]:
        return sorted(name for _, name, _, _ in string.Formatter().parse(text) if name)

    assert {k: fields(v) for k, v in printmode.TEXTS["ru"].items()} == {
        k: fields(v) for k, v in printmode.TEXTS["en"].items()}


async def test_continuing_a_question_paused_session_answers_it(launch, monkeypatch):
    """`-c` / `-r` on a session that waits for the user's answer to a member's question: the prompt goes to the asker,
    which makes it the answer (every member gets it, the swarm goes on); no resume drops the question."""
    conn = await launch(pausing_asker())
    code, out, err = await run(conn, f"A {MARK} in a colour", overrides=CLARIFY, auto_reply=False, timeout=3)
    assert code == 3, err
    sid = (await conn.client.sessions())[0]["id"]
    question = (await conn.client.state(sid))["swarm"]["question"]
    assert question, "no question pause"
    acts: list[tuple[str, dict]] = []
    act = conn.client.act

    async def recording(sid_, tool, args=None):
        acts.append((tool, dict(args or {})))
        return await act(sid_, tool, args)

    monkeypatch.setattr(conn.client, "act", recording)
    code, out, err = await run(conn, "blue", session=sid, output_format="json", timeout=20)
    assert code == 0, err
    assert json.loads(out)["summary"] == "The haiku is blue."
    assert ("send_message", {"to": [question["asker"]], "text": "blue"}) in acts
    assert not [a for a in acts if a[0] == "swarm_control"]
    answer = next(m for m in await conn.client.messages(sid) if m["sender"] == "user" and m["text"] == "blue")
    assert answer["recipients"] == [question["asker"]] and answer["reply_to"] == question["message_id"]
    resumed = [ev for ev in await conn.client.events(sid, types=["swarm.*"]) if ev["type"] == "swarm.resumed"]
    assert [ev["data"].get("question", {}).get("outcome") for ev in resumed] == ["answered"]
    assert resumed[0]["data"]["question"]["answer_id"] == answer["id"]


async def test_a_question_pause_found_at_catch_up_is_answered_past_the_cap(launch, monkeypatch):
    """A question pause the run finds in the state (the hub had no lease of it when the member asked) is answered
    with the automatic reply even past AUTO_REPLY_MAX: otherwise it would hold the run until --timeout. Here the
    session's goal was completed while its question waited, so the prompt reopens the goal instead of answering."""
    conn = await launch(pausing_asker())
    code, out, err = await run(conn, f"A {MARK} in a colour", overrides=CLARIFY, auto_reply=False, timeout=3)
    assert code == 3, err
    sid = (await conn.client.sessions())[0]["id"]
    question = (await conn.client.state(sid))["swarm"]["question"]
    assert question, "no question pause"
    conn.manager.swarms[sid].complete_goal(question["asker"], "Done before the answer.")
    monkeypatch.setattr(printmode, "AUTO_REPLY_MAX", 0)
    code, out, err = await run(conn, f"One more {MARK}, please", session=sid, output_format="json", timeout=20)
    assert code == 0, err
    replies = [m for m in await conn.client.messages(sid) if m["sender"] == "user"]
    assert [(m["text"], m["recipients"], m["reply_to"]) for m in replies] == [
        (AUTO_REPLY["en"], [question["asker"]], question["message_id"])]
    resumed = [ev for ev in await conn.client.events(sid, types=["swarm.*"]) if ev["type"] == "swarm.resumed"]
    assert [ev["data"]["question"]["outcome"] for ev in resumed] == ["answered"]
    assert not (await conn.client.state(sid))["swarm"]["paused"]


@pytest.mark.parametrize("failure", ["error", "refused", "lost"])
async def test_a_failed_auto_reply_to_a_question_pause_is_sent_again(launch, monkeypatch, failure):
    """A gap of the lease (here the hub never sees it) lets a question pause the swarm, and the run's first
    auto-reply fails: a hub error, a refused call, or a lost answer to a call the hub did take. The run reads the
    state again and answers the question while it still waits, never twice; the goal is then reached."""
    monkeypatch.setattr(Swarm, "auto_reply_seen", lambda self: None)
    monkeypatch.setattr(Swarm, "auto_reply_stream", lambda self: contextlib.nullcontext())
    conn = await launch(pausing_asker())
    attempts: list[int] = []
    act = conn.client.act

    async def flaky(sid_, tool, args=None):
        if tool == "send_message" and "reply_to" in (args or {}):
            attempts.append(args["reply_to"])
            if len(attempts) == 1:
                if failure == "refused":
                    return False, "Refused."
                if failure == "lost":
                    await act(sid_, tool, args)
                raise HubError(503, "Service unavailable.")
        return await act(sid_, tool, args)

    monkeypatch.setattr(conn.client, "act", flaky)
    code, out, err = await run(conn, f"A {MARK} in a colour", overrides=CLARIFY, output_format="json", timeout=20)
    assert code == 0, err
    result = json.loads(out)
    assert result["summary"] == "The haiku is blue."
    sid = result["session"]
    events = await conn.client.events(sid, types=["swarm.*"])
    paused = [ev["data"] for ev in events if ev["type"] == "swarm.paused"]
    assert [p["reason"] for p in paused] == ["question"]   # the gap: the question did pause the swarm
    replies = [m for m in await conn.client.messages(sid) if m["sender"] == "user"]
    assert [(m["text"], m["reply_to"]) for m in replies] == [(AUTO_REPLY["en"], paused[0]["message_id"])]
    assert attempts == [paused[0]["message_id"]] * (1 if failure == "lost" else 2)
    resumed = [ev["data"] for ev in events if ev["type"] == "swarm.resumed"]
    assert [r["question"]["outcome"] for r in resumed] == ["answered"]


async def test_a_multi_line_prompt_leaves_the_title_to_the_hub(launch, monkeypatch):
    conn = await launch(finisher())
    created: list[tuple] = []
    create_session = conn.client.create_session

    async def recording(title, goal, *args, **kwargs):
        created.append((title, goal))
        return await create_session(title, goal, *args, **kwargs)

    monkeypatch.setattr(conn.client, "create_session", recording)
    code, out, err = await run(conn, f"A {MARK}\nabout the sea", output_format="json")
    assert code == 0, err
    assert created == [("", f"A {MARK}\nabout the sea")]
    result = json.loads(out)
    assert result["goal"]["title"] == f"A {MARK}" and result["goal"]["description"] == "about the sea"
    assert (await conn.client.session(result["session"]))["session"]["title"] == f"A {MARK}"


async def test_timeout_budget_and_interrupt(launch, monkeypatch):
    conn = await launch(idler)
    code, out, err = await run(conn, "Never ends", timeout=1.0, output_format="json")
    assert code == 3
    result = json.loads(out)
    assert result["status"] == "timeout" and result["goal"]["status"] == "active" and result["summary"] == ""

    out = io.StringIO()
    task = asyncio.create_task(run_print(conn, "Interrupted", output_format="json", out=out, err=io.StringIO()))
    for _ in range(200):
        if len(conn.manager.running) == 2:
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.3)
    task.cancel()  # what Ctrl+C does to the main task under asyncio.run
    assert await asyncio.wait_for(task, 10) == 130
    assert json.loads(out.getvalue())["status"] == "interrupted"
    assert len(conn.manager.running) == 2  # the session is left as it is


async def test_budget_pause_exits_with_2(launch):
    conn = await launch(spender)
    code, out, err = await run(conn, "Spend it all")
    assert code == 2 and out == "" and "budget" in err


async def test_attach_to_session_and_continue_last(launch):
    conn = await launch(finisher("Attached and done."))
    sid = (await conn.client.create_session("Later", "", start=False))["id"]
    code, out, err = await run(conn, f"Write a {MARK} later", session=sid, output_format="json")
    assert code == 0, err
    result = json.loads(out)
    assert result["session"] == sid and result["goal"]["title"] == f"Write a {MARK} later"
    assert result["summary"] == "Attached and done."

    # the goal is done: continuing reopens it with the prompt as the comment
    code, out, err = await run(conn, f"One more {MARK}, please", continue_last=True)
    assert code == 0, err
    assert out.strip() == "Attached and done."
    events = await conn.client.events(sid, types=["goal.*"])
    assert [e["type"] for e in events][-2:] == ["goal.reopened", "goal.completed"]

    code, out, err = await run(conn, "Nope", session="no-such-session")
    assert code == 1 and "no-such-session" in err


class GonePipe(io.StringIO):
    """stdout of `dreamteam -p ... | head -1` after head exited."""

    def __init__(self, lines_before_eof: int = 0):
        super().__init__()
        self.left = lines_before_eof

    def write(self, text: str) -> int:
        if self.left <= 0:
            raise BrokenPipeError(32, "Broken pipe")
        self.left -= 1
        return super().write(text)


async def test_a_reader_that_goes_away_does_not_crash_the_run(launch):
    conn = await launch(finisher())
    for output_format in ("stream-json", "json", "text"):
        out, err = GonePipe(1), GonePipe(0)
        code = await run_print(conn, f"Piped {MARK}", output_format=output_format, verbose=True, out=out, err=err,
                               timeout=30)
        assert code == 0, output_format
        assert len(out.getvalue().splitlines()) <= 1


async def test_continuing_a_session_paused_by_the_user_resumes_it(launch):
    conn = await launch(finisher("Resumed and done."))
    sid = (await conn.client.create_session("Paused", "", start=True))["id"]
    ok, text = await conn.client.act(sid, "swarm_control", {"action": "pause"})
    assert ok, text
    assert (await conn.client.state(sid))["swarm"]["paused"] is True
    code, out, err = await run(conn, f"Write a {MARK} now", session=sid, timeout=20)
    assert code == 0, err
    assert out.strip() == "Resumed and done."
    assert (await conn.client.state(sid))["swarm"]["paused"] is False


async def test_a_deleted_session_ends_the_run(launch, monkeypatch):
    monkeypatch.setattr(printmode, "LOST_LIMIT", 60.0)
    conn = await launch(idler)
    sessions: list[str] = []

    async def delete_it():
        while not sessions or sessions[0] not in conn.manager.running:
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.3)
        await conn.manager.delete(sessions[0])

    deleter = asyncio.create_task(delete_it())
    started = asyncio.get_running_loop().time()
    code, out, err = await run(conn, "Doomed", timeout=30, on_session=sessions.append)
    await deleter
    assert code == 1 and sessions[0] in err
    assert asyncio.get_running_loop().time() - started < 15


async def test_verbose_feed_keeps_results_under_their_calls(launch):
    def busy(system, messages, tools):
        seen = texts(messages)
        if any(MARK in t for t in seen) and not any("Created step.txt" in t for t in seen):
            return {"tool_calls": [{"name": "write_file", "arguments": {"path": "step.txt", "content": "one"}},
                                   {"name": "board", "arguments": {}}]}
        if any("Created step.txt" in t for t in seen) and not any("marked as achieved" in t for t in seen):
            return call("goal_complete", summary="Paired.")
        return call("wait")

    conn = await launch(busy)
    code, out, err = await run(conn, f"Pair a {MARK}", verbose=True)
    assert code == 0, err
    lines = err.splitlines()
    create = next(i for i, line in enumerate(lines) if "write_file(" in line)
    assert lines[create + 1].strip().startswith("⎿") and "Created step.txt" in lines[create + 1]
    board = next(i for i, line in enumerate(lines) if "board()" in line)
    assert lines[board + 1].strip().startswith("⎿")
    assert "wait()" not in err  # housekeeping


async def test_hub_gone_is_an_error(launch, monkeypatch):
    conn = await launch(idler)
    monkeypatch.setattr(printmode, "LOST_LIMIT", 0.5)

    async def stop_hub():
        for _ in range(200):
            if conn.manager.running:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.3)
        conn._server.cancel()
        await asyncio.gather(conn._server, return_exceptions=True)

    stopper = asyncio.create_task(stop_hub())
    code, out, err = await run(conn, "Hub goes away", timeout=20)
    await stopper
    assert code == 1 and err.strip()


def py_command(script: str, *args: str) -> str:
    """`$PYEXE script args` in the syntax of the test shell (cmd on Windows, sh elsewhere)."""
    if sys.platform == "win32":
        return '"%PYEXE%" ' + " ".join(f'"{a}"' for a in (script, *args))
    return '"$PYEXE" ' + " ".join(f"'{a}'" for a in (script, *args))


async def test_the_result_lists_the_jobs_still_running(tmp_path, monkeypatch):
    """The goal is done while an agent's dev server runs as a background job: the JSON result lists it; the text
    result says so on stderr when the hub is not this process's (an owned launcher stops them right after)."""
    from test_procs import Children

    monkeypatch.setenv("PYEXE", sys.executable)
    (tmp_path / "kids").mkdir()
    kids = Children(tmp_path / "kids")
    commands: list[str] = []

    def server(system, messages, tools):
        seen = texts(messages)
        last = seen[-1] if seen else ""
        if "marked as achieved" in last or "already marked" in last:
            return call("wait")
        if any("STARTED in the background as job" in t for t in seen):
            return call("goal_complete", summary="The server runs.")
        if any(MARK in t for t in seen):
            commands.append(py_command(str(tmp_path / "kids" / "child.py"), "60", kids.pidfile()))
            return call("shell", command=commands[-1], background=True)
        return call("wait")

    home = make_home(tmp_path)
    raw = st.read_raw(home)
    st.write_raw(home, {**raw, "shell": {"program": "cmd" if sys.platform == "win32" else "sh"}})
    conn = await connect(home, web=False, llm_factory=lambda profile, agent: ScriptedLLM(server, profile))
    try:
        code, out, err = await run(conn, f"Serve the {MARK}")         # owned: no line about the jobs
        assert code == 0, err
        assert out.strip() == "The server runs." and "Background jobs" not in err
        external = SimpleNamespace(client=conn.client, owned=False)  # a hub of another process
        code, out, err = await run(external, f"Serve another {MARK}", output_format="json")
        assert code == 0, err
        result = json.loads(out)
        (job,) = result["jobs"]
        assert job == {"id": 1, "label": "j1", "owner": result["agents"][0], "command": commands[-1][:200],
                       "origin": "background"}                         # (the state clips a command to 200)
        code, out, err = await run(external, f"Serve a third {MARK}", lang="ru")
        assert code == 0, err
        line = err.strip().splitlines()[-1]
        assert line.startswith("Ещё работают фоновые процессы агентов: j1 (") and "PYEXE" in line
        assert line.endswith("Они остановятся вместе с сессией (остановить раньше можно командой /jobs stop jN в "
                             "dreamteam tui).")
        assert len(kids.pids()) == 3
    finally:
        await conn.close()
    assert kids.check() == [], "child processes were left running"
