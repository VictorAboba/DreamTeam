"""Print mode (`dreamteam -p`, dreamteam.tui.printmode) against a real in-process launcher with scripted agents."""

from __future__ import annotations

import asyncio
import io
import json
import socket
from pathlib import Path
from typing import Any, Callable

import pytest

from dreamteam import settings as st
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.tui import printmode
from dreamteam.tui.client import Connection, connect
from dreamteam.tui.printmode import AUTO_REPLY, run_print

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
