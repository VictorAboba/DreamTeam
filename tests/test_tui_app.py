"""The interactive TUI (dreamteam.tui.app) driven through a prompt_toolkit pipe input against a real in-process
launcher with scripted agents; output is captured as plain text through the `printer` hook."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

import pytest
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.vt100 import Vt100_Output

from dreamteam import prompts
from dreamteam import settings as st
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.tui import app as app_module
from dreamteam.tui import main_print, render
from dreamteam.tui.app import TUI
from dreamteam.tui.client import Connection, HubError, connect
from dreamteam.tui.dialogs import BusyDialog, ConfirmDialog, MCPApprovalDialog, SessionPicker
from dreamteam.tui.input import load_prefs

CTRL_C, ESC, SHIFT_TAB, CTRL_O, DOWN, CTRL_J = "\x03", "\x1b", "\x1b[Z", "\x0f", "\x1b[B", "\n"
PLANNING = prompts.PLANNING_TASK["en"][0]  # the title of the planning task of an English swarm
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[=>78]")


def call(name: str, **arguments: Any) -> dict:
    return {"tool_calls": [{"name": name, "arguments": arguments}]}


def texts(messages: list[dict]) -> list[str]:
    return [m.get("content") or "" for m in messages if m["role"] in ("user", "tool")]


def idle(system, messages, tools):
    return call("wait")


def asker(system, messages, tools):
    """Asks the user one question about the haiku, then waits."""
    seen = texts(messages)
    if any("haiku" in t for t in seen) and not any("Sent message" in t for t in seen):
        return call("send_message", to=["user"], text="Which colour should it be?")
    return call("wait")


def finisher(system, messages, tools):
    """Declares the goal achieved as soon as it hears about a haiku."""
    seen = texts(messages)
    last = seen[-1] if seen else ""
    if "marked as achieved" in last or "already marked" in last:
        return call("wait")
    if any("haiku" in t for t in seen):
        return call("goal_complete", summary="Done: the haiku.")
    return call("wait")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    st.write_raw(home, {
        "swarm": {"name": "Home", "language": "en", "start_agents": 1, "max_agents": 3, "max_cost_usd": 0},
        "agents": {"stagger_seconds": 0, "idle_nudge_seconds": 30},
        "server": {"port": free_port()},
        "workspace": {"git": False},
        "mcp": {"library_dir": str(tmp_path / "library"), "require_approval": True},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
    })
    return home


class Harness:
    """A running TUI fed through a pipe input; everything it prints is kept as plain text."""

    def __init__(self, conn: Connection, stack: contextlib.ExitStack, width: int = 100, **kwargs: Any):
        self.conn = conn
        self.width = width  # of the captured text (long paths: wider, so that they are not wrapped)
        self.out: list[str] = []
        self.pipe = stack.enter_context(create_pipe_input())
        kwargs.setdefault("lang", "en")
        self.tui = TUI(conn, input=self.pipe, output=DummyOutput(), printer=self.capture, **kwargs)
        self.task = asyncio.create_task(self.tui.run())

    def capture(self, renderable: Any) -> None:
        self.out.append(render.to_plain(renderable, self.width))

    @property
    def text(self) -> str:
        return "\n".join(self.out)

    def type(self, keys: str) -> None:
        self.pipe.send_text(keys)

    async def until(self, predicate: Callable[[], Any], timeout: float = 10.0, what: str = "") -> Any:
        deadline = time.monotonic() + timeout
        while True:
            value = predicate()
            if asyncio.iscoroutine(value):
                value = await value
            if value:
                return value
            if self.task.done():
                self.task.result()
                raise AssertionError(f"the TUI ended while waiting for {what or predicate}")
            if time.monotonic() > deadline:
                state = (f"input={self.tui.buffer.text!r} verbosity={self.tui.ctx.verbosity} "
                         f"suggestions={[s.label for s in self.tui.suggestions]} dialog={self.tui.dialog!r}")
                raise AssertionError(f"timed out waiting for {what or predicate} ({state}); output:\n"
                                     f"{self.text[-3000:]}")
            await asyncio.sleep(0.03)

    async def see(self, needle: str, timeout: float = 10.0) -> None:
        await self.until(lambda: needle in self.text, timeout, repr(needle))

    async def run(self, line: str, needle: str, timeout: float = 10.0) -> None:
        """Type `line` + Enter and wait until `needle` is printed after it."""
        start = len(self.out)
        self.type(line + "\r")
        await self.until(lambda: any(needle in chunk for chunk in self.out[start:]), timeout, repr(needle))

    async def reply(self, line: str, timeout: float = 10.0) -> str:
        """Type `line` + Enter; what is printed after its echo (at least one chunk)."""
        start = len(self.out)
        self.type(line + "\r")
        await self.until(lambda: len(self.out) >= start + 2, timeout, f"the output of {line!r}")
        return "\n".join(self.out[start + 1:])

    async def quit(self) -> int:
        self.type(CTRL_C + CTRL_C)
        return await asyncio.wait_for(self.task, 10)

    async def close(self) -> None:
        if not self.task.done():
            self.tui.exit()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.task, 10)


@pytest.fixture
async def launch(tmp_path):
    opened: list[Connection] = []
    harnesses: list[Harness] = []
    stack = contextlib.ExitStack()

    async def start(policy: Callable = idle) -> Connection:
        conn = await connect(make_home(tmp_path), web=False,
                             llm_factory=lambda profile, agent: ScriptedLLM(policy, profile))
        opened.append(conn)
        return conn

    def tui(conn: Connection, **kwargs: Any) -> Harness:
        harness = Harness(conn, stack, **kwargs)
        harnesses.append(harness)
        return harness

    start.tui = tui
    yield start
    for harness in harnesses:
        await harness.close()
    stack.close()
    for conn in opened:
        await conn.close()


async def messages_from_user(conn: Connection, sid: str, text: str) -> list[dict]:
    return [m for m in await conn.client.messages(sid) if m["sender"] == "user" and m["text"] == text]


async def test_goal_creates_a_session_and_the_conversation_flows(launch):
    conn = await launch(asker)
    h = launch.tui(conn)
    await h.until(lambda: h.tui.ready.is_set())
    assert "Welcome to DreamTeam!" in h.text and "Tips for getting started" in h.text
    assert h.tui.box_title() == "new session" and not h.tui.attached

    h.type("Write a haiku about the sea\r")
    await h.until(lambda: h.tui.attached and h.tui.members, 30, "the new session")
    sessions = await conn.client.sessions()
    assert len(sessions) == 1 and sessions[0]["status"] == "running"
    assert sessions[0]["goal"]["title"] == "Write a haiku about the sea"
    sid = sessions[0]["id"]
    assert h.tui.sid == sid
    await h.see("Created session")
    assert "> Write a haiku about the sea" in h.text
    # a session created here replays its journal: the starting team joining, the goal once, the first task
    agent = h.tui.members[0]
    await h.until(lambda: re.search(rf"\+ {re.escape(agent)} \(.+\) joined the team", h.text), what="the team")
    await h.see("╭─ Goal")
    await h.see("#1 ")
    assert h.text.count("╭─ Goal") == 1

    # the agent's question: printed in full, marked read, and its sender becomes the addressee
    await h.see("Which colour should it be?")
    assert f"{agent} → you" in h.text
    assert h.tui.box_title() == f"→ {agent}"
    await h.until(lambda: inbox_read(conn, sid), what="the question marked read")

    h.type("blue, please\r")  # plain text goes to the addressee
    await h.until(lambda: messages_from_user(conn, sid, "blue, please"), what="the answer")
    assert (await messages_from_user(conn, sid, "blue, please"))[0]["recipients"] == [agent]
    await h.see(f"> blue, please   → {agent}")
    await asyncio.sleep(0.3)
    assert h.text.count("> blue, please") == 1  # the bus copy is not echoed again

    h.type(f"@{agent.split()[0]} hello there\r")  # a unique first name is enough
    await h.until(lambda: messages_from_user(conn, sid, "hello there"), what="the mention")
    assert (await messages_from_user(conn, sid, "hello there"))[0]["recipients"] == [agent]

    h.type("@all everyone listen\r")
    await h.until(lambda: messages_from_user(conn, sid, "everyone listen"), what="the broadcast")
    assert h.tui.box_title() == "→ everyone"
    h.type("@Nobody Here hi\r")
    await h.see("No such member: Nobody")
    assert await h.quit() == 0
    assert "Sessions stopped" in h.text


async def inbox_read(conn: Connection, sid: str) -> bool:
    messages = (await conn.client.inbox(sid))["messages"]
    return bool(messages) and all(m["read"] for m in messages)


async def test_commands_on_an_attached_session(launch, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(app_module.webbrowser, "open", opened.append)
    conn = await launch()
    sid = (await conn.client.create_session("Calc", "Build a calculator"))["id"]
    h = launch.tui(conn, continue_last=True)
    await h.until(lambda: h.tui.attached and h.tui.members, 20, "the attach")
    assert h.tui.sid == sid
    assert "Build a calculator" in h.text  # the goal panel
    agent = h.tui.members[0]

    await h.run("/board", PLANNING)
    await h.run("/agents", agent)
    await h.run("/status", "Version")
    assert "Calc" in h.out[-1] and "in this process" in h.out[-1]
    await h.run("/help", "Commands")
    assert "/board" in h.out[-1] and "task board" in h.out[-1]
    await h.run("/frobnicate", "Unknown command /frobnicate")
    await h.run("/bord", "Did you mean: /board")
    await h.run("/boa", PLANNING)    # Enter runs the selected suggestion
    await h.run("/task 1", PLANNING)
    await h.run("/roles", "Role catalog")                 # the session's catalog (GET /api/roles)
    assert "Tester" in h.out[-1] and "Generalist" in h.out[-1]
    await h.run("/roles reviewer", "Reviewer")
    assert "built-in" in h.out[-1] and "second pair of eyes" in h.out[-1]
    await h.run("/cost", "Total")
    await h.run(f"/to {agent.split()[0]}", f"Now writing to: {agent}")
    assert h.tui.box_title() == f"→ {agent}"
    await h.run("/budget 3", "Limits: budget $3.00")
    assert (await conn.client.state(sid))["swarm"]["limits"]["max_cost_usd"] == 3.0
    await h.run("/files", ".scratch/")  # only the agents' scratch folder so far (never committed)
    workspace = Path((await conn.client.session(sid))["session"]["dir"]) / "workspace"
    (workspace / "marker-file.txt").write_text("made by the test\n", encoding="utf-8")
    await h.run("!ls", "marker-file.txt", timeout=30)  # runs in the session workspace
    assert "! ls" in h.out[-1]
    await h.run("/cat marker-file.txt", "made by the test")
    await h.run("/goal", "Build a calculator")
    await h.run("/new", "")  # asks for the goal next
    await h.until(lambda: h.tui.input_mode == ("goal", None))
    assert h.tui.box_title() == "new session"
    h.type(CTRL_C)  # cancels the question
    await h.until(lambda: h.tui.input_mode is None)

    assert "trace" in (await h.reply("/trace")).lower()
    assert "Model profiles" in await h.reply("/model")
    assert 'No profile "nope"' in await h.reply("/model nope")
    assert "Default profile: default" in await h.reply("/model default")
    assert (await conn.client.settings())["settings"]["agents"]["default_model"] == "default"
    await h.run("/web", "Opening in the browser")
    assert opened == [f"{conn.base_url}/s/{sid}/"]
    await h.run("/lang ru", "Язык интерфейса: русский")
    assert load_prefs(conn.home) == {"lang": "ru"} and h.tui.box_title() == f"→ {agent}"
    await h.run("/lang en", "Interface language: English")

    await h.run("/stop", 'Session "Calc" stopped')
    assert (await conn.client.session(sid))["session"]["status"] == "stopped"
    h.type("are you there?\r")  # a message to a stopped session starts it first
    await h.until(lambda: messages_from_user(conn, sid, "are you there?"), 20, "the message")
    assert (await conn.client.session(sid))["session"]["status"] == "running"
    assert await h.quit() == 0


async def test_a_multi_line_goal_goes_whole_to_the_hub_which_splits_it(launch, monkeypatch):
    conn = await launch()
    created: list[tuple] = []
    create_session = conn.client.create_session

    async def recording(title, goal, *args, **kwargs):
        created.append((title, goal))
        return await create_session(title, goal, *args, **kwargs)

    monkeypatch.setattr(conn.client, "create_session", recording)
    h = launch.tui(conn)
    await h.until(lambda: h.tui.ready.is_set())
    h.type("/new\r")
    await h.until(lambda: h.tui.input_mode == ("goal", None))
    assert h.tui._placeholder().startswith("Describe the goal: the first line is its title")
    h.type("Line one" + CTRL_J + "Line two\r")
    await h.until(lambda: h.tui.attached and h.tui.sid, 30, "the new session")
    assert created == [("", "Line one\nLine two")]  # the title comes from the hub: the goal's first line
    await h.see('Created session "Line one"')
    goal = (await conn.client.state(h.tui.sid))["swarm"]["goal"]
    assert (goal["title"], goal["description"]) == ("Line one", "Line two")

    acts: list[tuple] = []
    act = conn.client.act

    async def recording_act(sid, tool, args=None):
        acts.append((tool, args))
        return await act(sid, tool, args)

    monkeypatch.setattr(conn.client, "act", recording_act)
    h.type("/goal A" + CTRL_J + "B\r")
    await h.until(lambda: acts, what="the new goal")
    assert acts[0] == ("set_goal", {"title": "A\nB"})
    goal = (await conn.client.state(h.tui.sid))["swarm"]["goal"]
    assert (goal["title"], goal["description"]) == ("A", "B")
    assert await h.quit() == 0


async def test_team_sets_the_starting_team_of_the_next_session(launch):
    conn = await launch()
    h = launch.tui(conn)
    await h.until(lambda: h.tui.ready.is_set())
    await h.run("/team", "Starting team of new sessions: Generalist ×1")
    await h.run("/team tester, reviewer", "New sessions from this window start with: Tester ×1 · Reviewer ×1")
    h.type("Check the parser\r")
    await h.until(lambda: h.tui.attached and len(h.tui.members) == 2, 30, "the team of two")
    agents = (await conn.client.state(h.tui.sid))["agents"]
    assert sorted(a["role"] for a in agents) == ["Reviewer", "Tester"]
    assert await h.quit() == 0


async def test_session_picker_and_resume(launch):
    conn = await launch()
    other = (await conn.client.create_session("Other", "", start=False))["id"]
    main = (await conn.client.create_session("Main", "Main goal"))["id"]
    h = launch.tui(conn, session=main)
    await h.until(lambda: h.tui.attached and h.tui.sid == main, 20, "the attach")

    h.type("/sessions\r")
    await h.until(lambda: isinstance(h.tui.dialog, SessionPicker), what="the picker")
    assert h.tui.dialog.select.selected.value == main  # the current session is preselected
    h.type(DOWN)
    await h.until(lambda: h.tui.dialog.select.selected.value == other)
    h.type("\r")
    await h.until(lambda: h.tui.attached and h.tui.sid == other, 20, "the switch")
    await h.see('Starting session "Other"')
    await h.see("No goal yet")
    assert next(s for s in await conn.client.sessions() if s["id"] == other)["status"] == "running"

    await h.run(f"/resume {main}", "Main goal")
    assert h.tui.sid == main
    await h.run("/resume 999-nope", 'No session "999-nope"')
    assert await h.quit() == 0


async def test_text_after_an_achieved_goal_asks_before_a_new_session(launch):
    conn = await launch(finisher)
    sid = (await conn.client.create_session("Haiku", "Write a haiku"))["id"]
    h = launch.tui(conn, session=sid)
    await h.until(lambda: h.tui.attached, 20, "the attach")
    await h.see("Goal achieved", timeout=20)
    await h.until(lambda: ((h.tui.state or {}).get("swarm") or {}).get("goal", {}).get("status") == "done")

    h.type("Another haiku, about snow\r")
    await h.until(lambda: isinstance(h.tui.dialog, ConfirmDialog), what="the confirmation")
    h.type(ESC)  # nothing is sent: the text comes back
    await h.until(lambda: h.tui.dialog is None and h.tui.buffer.text == "Another haiku, about snow")
    h.type("\r")
    await h.until(lambda: isinstance(h.tui.dialog, ConfirmDialog))
    h.type("\r")  # "Yes": a new session with this goal
    await h.until(lambda: h.tui.attached and h.tui.sid not in (None, sid), 30, "the new session")
    sessions = {s["id"]: s for s in await conn.client.sessions()}
    assert len(sessions) == 2 and sessions[h.tui.sid]["goal"]["title"] == "Another haiku, about snow"
    assert await h.quit() == 0


async def test_keys_ctrl_c_verbosity_and_shortcuts(launch):
    conn = await launch()
    h = launch.tui(conn)
    await h.until(lambda: h.tui.ready.is_set())
    h.type("?")
    await h.see("Keyboard shortcuts")
    assert h.tui.buffer.text == ""

    h.type("some text")
    await h.until(lambda: h.tui.buffer.text == "some text")
    h.type(CTRL_C)  # clears the input, does not exit
    await h.until(lambda: h.tui.buffer.text == "")
    assert not h.task.done()
    h.type("line one" + ESC + "\r" + "line two\\\rline three")  # Alt+Enter, and `\` + Enter: newlines
    await h.until(lambda: h.tui.buffer.text == "line one\nline two\nline three")
    h.type(CTRL_C)
    await h.until(lambda: h.tui.buffer.text == "")

    assert h.tui.ctx.verbosity == "normal"
    h.type(SHIFT_TAB)
    await h.until(lambda: h.tui.ctx.verbosity == "verbose")
    h.type(SHIFT_TAB)
    await h.until(lambda: h.tui.ctx.verbosity == "quiet")
    h.type(CTRL_O)
    await h.until(lambda: h.tui.ctx.verbosity == "verbose")
    h.type(CTRL_O)
    await h.until(lambda: h.tui.ctx.verbosity == "normal")

    h.type("/bo")
    await h.until(lambda: [s.label for s in h.tui.suggestions][:1] == ["/board"])
    h.type(ESC)  # closes the suggestions
    await h.until(lambda: not h.tui.suggestions)
    h.type(CTRL_C)
    await h.until(lambda: h.tui.buffer.text == "")
    h.type("/he\t")  # Tab completes
    await h.until(lambda: h.tui.buffer.text == "/help")
    h.type(CTRL_C)
    await h.until(lambda: h.tui.buffer.text == "")

    h.type(CTRL_C)
    await h.until(lambda: h.tui.hint == "Press Ctrl+C again to exit")
    assert not h.task.done()
    h.type(CTRL_C)
    assert await asyncio.wait_for(h.task, 10) == 0


async def test_mcp_approval_dialog(launch, monkeypatch):
    conn = await launch()
    sid = (await conn.client.create_session("Tools", "Make tools"))["id"]
    h = launch.tui(conn, session=sid)
    await h.until(lambda: h.tui.attached and h.tui.members, 20, "the attach")
    agent = h.tui.members[0]
    forge = conn.manager.swarms[sid].forge

    async def smoke_test(name, code, command):  # no server process: the code is never run
        return None, [{"name": "add", "description": "Add two numbers"}], None

    monkeypatch.setattr(forge, "_smoke_test", smoke_test)

    async def status_is(name: str, expected: str) -> bool:
        servers = (await conn.client.state(sid))["mcp"]
        return next((s["status"] for s in servers if s["name"] == name), None) == expected

    def dialog_for(name: str) -> bool:
        return isinstance(h.tui.dialog, MCPApprovalDialog) and h.tui.dialog.name == name

    monkeypatch.setattr(MCPApprovalDialog, "settle", 0.3)

    # the dialog waits while the user types: Enter sends the message instead of approving the server
    h.type("half a message")
    await h.until(lambda: h.tui.buffer.text == "half a message")
    await forge.create(agent, "calc", "Arithmetic helpers", "print('calc')\n")
    assert await status_is("calc", "pending")
    await h.until(lambda: h.tui._mcp_waiting == "calc", what="the dialog held back")
    assert h.tui.dialog is None and "calc" in "".join(text for _, text in h.tui._status_text())
    h.type("\r")
    await h.until(lambda: messages_from_user(conn, sid, "half a message"), what="the message")

    # 1 approves, once the dialog has settled (a key typed at once is not an answer)
    await h.until(lambda: dialog_for("calc"), what="the approval dialog")
    text = "".join(part for _, part in h.tui.dialog.fragments(100))
    assert "An MCP server waits for approval" in text and "Arithmetic helpers" in text and agent in text
    assert "1. Yes, allow" in text and "add" in text
    assert "python mcp/calc/server.py" in text and "/cat mcp/calc/server.py" in text
    h.type("1")
    await asyncio.sleep(0.1)
    assert dialog_for("calc") and await status_is("calc", "pending")
    await asyncio.sleep(0.3)
    h.type("1")
    await h.until(lambda: h.tui.dialog is None)
    await h.until(lambda: status_is("calc", "active"), what="calc approved")
    await h.see("approved")

    # Esc keeps it pending for later; /approve decides
    await forge.create(agent, "calc2", "More helpers", "print('calc2')\n")
    await h.until(lambda: dialog_for("calc2"))
    h.type(ESC)
    await h.until(lambda: h.tui.dialog is None)
    await h.see("/approve calc2")
    assert await status_is("calc2", "pending")
    await asyncio.sleep(2.5)  # a state refresh meanwhile does not bring the answered dialog back
    assert h.tui.dialog is None
    await h.run("/approve calc2", "approved")
    assert await status_is("calc2", "active")

    # 2 rejects with a comment typed into the input box
    await forge.create(agent, "calc3", "Risky helpers", "print('calc3')\n")
    await h.until(lambda: dialog_for("calc3"))
    await asyncio.sleep(0.35)
    h.type("2")
    await h.until(lambda: h.tui.input_mode == ("reject", "calc3"))
    assert h.tui.box_title() == "MCP calc3"
    h.type("too risky\r")
    await h.until(lambda: status_is("calc3", "rejected"), what="calc3 rejected")
    await h.until(lambda: h.tui.input_mode is None)
    assert await h.quit() == 0


async def test_escape_pauses_a_working_swarm(launch):
    release = asyncio.Event()

    async def busy(system, messages, tools):  # the agent stays "working" until released
        await release.wait()
        return call("wait")

    conn = await launch(busy)
    try:
        sid = (await conn.client.create_session("Busy", "Think hard"))["id"]
        h = launch.tui(conn, session=sid)
        await h.until(lambda: h.tui.attached and h.tui.working, 20, "a working agent")
        assert h.tui.spinner_visible()
        spinner = "".join(text for _, text in h.tui._spinner_text())
        assert "esc to pause" in spinner and h.tui.members[0].split()[0] in spinner
        h.type(ESC)
        await h.until(lambda: h.tui.paused, what="the pause")
        assert (await conn.client.state(sid))["swarm"]["paused"] is True
        await h.see("The swarm is paused")
        await h.run("/pause", "The swarm is back to work")  # /pause toggles
        assert await h.quit() == 0
    finally:
        release.set()


async def test_connection_loss_is_reported_and_resynced(launch, monkeypatch):
    monkeypatch.setattr(app_module, "LOST_GRACE", 0.05)
    conn = await launch()
    sid = (await conn.client.create_session("Calc", "Build a calculator"))["id"]
    h = launch.tui(conn, session=sid)
    await h.until(lambda: h.tui.attached and h.tui.members and not h.tui._first_connect, 20, "the stream")
    agent = h.tui.members[0]
    await h.tui._cancel(h.tui._stream_task)  # the stream goes away...
    await h.tui.on_event({"type": "_connection", "data": {"state": "lost"}})
    await h.until(lambda: not h.tui.connected)
    await h.see("Lost connection to the hub")
    assert "no connection" in "".join(text for _, text in h.tui._status_text())
    conn.manager.swarms[sid].mail.send(agent, ["user"], "Sent while you were away")  # ...and misses this
    await h.tui.on_event({"type": "_connection", "data": {"state": "connected"}})
    await h.see("Reconnected to the hub")
    await h.see("Sent while you were away")
    assert h.tui.connected and h.tui.box_title() == f"→ {agent}"
    assert await h.quit() == 0


class Terminal(io.StringIO):
    """What a UTF-8 terminal would receive."""

    @property
    def encoding(self) -> str:
        return "utf-8"

    def plain(self) -> str:
        return ANSI_ESCAPE.sub("", self.getvalue())


async def test_output_is_printed_above_the_input_box(launch):
    conn = await launch()
    term = Terminal()
    output = Vt100_Output(term, lambda: Size(rows=30, columns=90), term="xterm-256color")
    with create_pipe_input() as pipe:
        tui = TUI(conn, lang="en", input=pipe, output=output)
        task = asyncio.create_task(tui.run())
        deadline = time.monotonic() + 10
        while "Tips for getting started" not in term.plain():
            assert time.monotonic() < deadline and not task.done(), term.plain()[-2000:]
            await asyncio.sleep(0.03)
        assert "Welcome to DreamTeam!" in term.plain()
        assert "╭─ new session" in term.plain() and "Describe a goal" in term.plain()  # the bottom area
        # rich colours reach the terminal at its colour depth (xterm-256color: the accent becomes colour 173)
        assert re.search(r"\x1b\[[0-9;]*38;5;173m", term.getvalue())
        pipe.send_text("/help\r")
        while "task board" not in term.plain():
            assert time.monotonic() < deadline and not task.done(), term.plain()[-2000:]
            await asyncio.sleep(0.03)
        pipe.send_text(CTRL_C + CTRL_C)
        assert await asyncio.wait_for(task, 10) == 0
    assert "Sessions stopped" in term.plain()


def test_main_print_runs_a_goal(tmp_path):
    home = make_home(tmp_path)
    out, err = io.StringIO(), io.StringIO()
    code = main_print(home, "Write a haiku", llm_factory=lambda profile, agent: ScriptedLLM(finisher, profile),
                      overrides={"agents": 1, "max_agents": 2, "title": "Haiku"}, timeout=30, out=out, err=err)
    assert code == 0, err.getvalue()
    assert out.getvalue().strip() == "Done: the haiku."
    meta = json.loads(next((home / "sessions").glob("*/session.json")).read_text(encoding="utf-8"))
    assert meta["title"] == "Haiku"
    assert meta["overrides"] == {"swarm": {"start_agents": 1, "max_agents": 2}}
    # a one-shot run is not resumed by the next `dreamteam` (its own launcher stopped the session explicitly)
    assert meta["autostart"] is False

    code = main_print(home, "Never ends", llm_factory=lambda profile, agent: ScriptedLLM(idle, profile),
                      timeout=1.5, out=io.StringIO(), err=io.StringIO())
    assert code == 3
    metas = [json.loads(p.read_text(encoding="utf-8")) for p in (home / "sessions").glob("*/session.json")]
    assert len(metas) == 2 and not any(m["autostart"] for m in metas)


UP = "\x1b[A"


def long_command() -> str:
    """A shell command that runs for half a minute unless it is killed."""
    return "ping -n 30 127.0.0.1" if sys.platform == "win32" else "sleep 30"


def echoer(system, messages, tools):
    """Answers every "ping-N" from the user with "pong ping-N"."""
    seen = texts(messages)
    last = seen[-1] if seen else ""
    found = re.findall(r"ping-\d+", last)
    if found and "Sent message" not in last:
        return call("send_message", to=["user"], text=f"pong {found[-1]}")
    return call("wait")


async def test_suggestions_do_not_outlive_the_input(launch):
    conn = await launch()
    h = launch.tui(conn)
    await h.until(lambda: h.tui.ready.is_set())
    await h.run("/help", "Commands")
    assert h.tui.buffer.text == "" and h.tui.suggestions == []
    h.type("\r")  # Enter on the empty box runs nothing (not /help again)
    await asyncio.sleep(0.3)
    assert sum("Commands" in chunk for chunk in h.out) == 1

    h.type("/stat")
    await h.until(lambda: [s.label for s in h.tui.suggestions][:1] == ["/status"])
    h.type(CTRL_C)
    await h.until(lambda: h.tui.buffer.text == "")
    assert h.tui.suggestions == []
    h.type("\r")
    await asyncio.sleep(0.3)
    assert "Version" not in h.text  # /status did not run

    h.type(UP)  # no suggestions: Up browses the history
    await h.until(lambda: h.tui.buffer.text == "/help", what="the history")
    h.type(CTRL_C)
    await h.until(lambda: h.tui.buffer.text == "")
    assert await h.quit() == 0


async def test_bash_command_runs_aside_and_can_be_interrupted(launch):
    conn = await launch()
    h = launch.tui(conn)
    await h.until(lambda: h.tui.ready.is_set())
    h.type(f"!{long_command()}\r")
    await h.until(lambda: h.tui._bash is not None and h.tui._bash.running, 20, "the running command")
    run = h.tui._bash
    assert "esc to interrupt" in "".join(text for _, text in h.tui._status_text())
    await h.run("/help", "Commands")  # other input is not held up by it
    h.type("!echo second\r")
    await h.see("still running")
    started = time.monotonic()
    h.type(ESC)
    await h.see("interrupted", timeout=15)
    assert time.monotonic() - started < 10 and h.tui._bash is None and not run.running
    assert f"! {long_command()}" in h.text

    # quitting while a command runs kills it at once
    h.type(f"!{long_command()}\r")
    await h.until(lambda: h.tui._bash is not None and h.tui._bash.running, 20, "the second command")
    run = h.tui._bash
    started = time.monotonic()
    h.tui.exit()
    assert await asyncio.wait_for(h.task, 15) == 0
    assert time.monotonic() - started < 10 and not run.running


async def test_shell_command_timeout_and_decoding(tmp_path):
    from dreamteam.tui.bash import ShellCommand, decode

    started = time.monotonic()
    code, output = await ShellCommand(long_command(), cwd=tmp_path).run(timeout=1.5)
    assert code is None and time.monotonic() - started < 15
    assert decode("привет\n".encode("utf-8"), "cmd") == "привет\n"
    assert isinstance(decode(b"\xff\xfe\x80", "bash"), str)
    if sys.platform == "win32":  # cmd.exe writes in the console's OEM code page (cp866 on a Russian system)
        import ctypes

        oem = f"cp{ctypes.windll.kernel32.GetOEMCP()}"
        try:
            raw = "привет".encode(oem)
        except UnicodeEncodeError:
            raw = None
        if raw is not None and raw != "привет".encode("utf-8"):
            assert decode(raw, "cmd") == "привет"


def test_history_file_that_cannot_be_written(tmp_path):
    history = app_module._History(str(tmp_path))  # a directory: neither readable nor writable as a file
    assert list(history.load_history_strings()) == []
    history.append_string("/help")  # no exception: the entry stays in memory
    assert history.get_strings() == ["/help"]


async def test_placeholder_hub_errors_and_a_single_swarm_status(launch, tmp_path):
    conn = await launch()
    with create_pipe_input() as pipe:
        tui = TUI(conn, lang="ru", input=pipe, output=DummyOutput(), printer=lambda r: None)
        text = tui._placeholder()  # DummyOutput: 80 columns
        assert render.cells(text) <= 80 - 7 and text.endswith("…") and text.startswith("Опишите цель")
        tui.attached = True
        tui._apply_state({"swarm": {"name": "Solo"}, "agents": [], "tasks": []})  # a single-swarm hub
        assert tui.session == {"title": "Solo", "status": "running"}

    from dreamteam.tui.client import HubClient

    dead = f"http://127.0.0.1:{free_port()}"
    printed: list[str] = []
    gone = Connection(HubClient(dead, timeout=2), dead, tmp_path, owned=False)
    try:
        with create_pipe_input() as pipe:
            tui = TUI(gone, lang="ru", input=pipe, output=DummyOutput(),
                      printer=lambda r: printed.append(render.to_plain(r, 100)))
            tui.client.mode = "launcher"
            await tui.handle_input("/sessions")
        assert printed[-1] == f"⚠ Хаб {dead} недоступен — проверьте, что DreamTeam запущен"
    finally:
        await gone.client.aclose()


async def test_failed_attach_keeps_the_current_session(launch, monkeypatch):
    conn = await launch()
    main = (await conn.client.create_session("Main", "Main goal"))["id"]
    other = (await conn.client.create_session("Other", "Other goal", start=False))["id"]
    h = launch.tui(conn, session=main)
    await h.until(lambda: h.tui.attached and h.tui.members and not h.tui._first_connect, 20, "the attach")
    stream = h.tui._stream_task
    real_state = h.tui.client.state

    async def flaky_state(sid):
        if sid == other:
            raise HubError(503, "Service busy")
        return await real_state(sid)

    monkeypatch.setattr(h.tui.client, "state", flaky_state)
    await h.run(f"/resume {other}", "Service busy")
    assert h.tui.attached and h.tui.sid == main and h.tui._stream_task is stream and not stream.done()
    assert h.tui.box_title() != "new session"

    async def broken_start(sid):
        raise HubError(400, "Invalid settings of the session")

    monkeypatch.setattr(h.tui.client, "state", real_state)
    await conn.client.stop_session(other)
    monkeypatch.setattr(h.tui.client, "start_session", broken_start)
    await h.run(f"/resume {other}", "Invalid settings")
    assert h.tui.attached and h.tui.sid == main and not h.tui._stream_task.done()
    assert len(await conn.client.sessions()) == 2
    assert await h.quit() == 0


async def test_a_slow_refresh_of_a_left_session_is_not_applied(launch, monkeypatch):
    conn = await launch()
    old = (await conn.client.create_session("Old", "Old goal"))["id"]
    new = (await conn.client.create_session("New", "New goal"))["id"]
    h = launch.tui(conn, session=old)
    await h.until(lambda: h.tui.attached and h.tui.members, 20, "the attach")
    real_state = h.tui.client.state
    slow = asyncio.Event()

    async def state(sid):
        result = await real_state(sid)
        if sid == old and slow.is_set():
            await asyncio.sleep(0.5)
        return result

    monkeypatch.setattr(h.tui.client, "state", state)
    slow.set()
    refresh = asyncio.create_task(h.tui.refresh_state())
    await asyncio.sleep(0.1)
    await h.run(f"/resume {new}", "New goal")
    await refresh
    assert h.tui.sid == new
    assert h.tui.state["swarm"]["session"]["id"] == new and h.tui.state["swarm"]["goal"]["title"] == "New goal"
    assert h.tui.members == [a["name"] for a in (await conn.client.state(new))["agents"]]
    assert await h.quit() == 0


async def test_attach_history_is_printed_once_and_includes_the_inbox(launch, monkeypatch):
    conn = await launch()
    sid = (await conn.client.create_session("Chat", "Talk", overrides={"swarm": {"start_agents": 2}}))["id"]
    swarm = conn.manager.swarms[sid]
    for _ in range(100):
        agents = [a["name"] for a in (await conn.client.state(sid))["agents"]]
        if len(agents) >= 2:
            break
        await asyncio.sleep(0.05)
    assert len(agents) == 2
    swarm.mail.send(agents[0], ["user"], "IMPORTANT QUESTION for the user")
    for i in range(210):  # pushes the question out of the newest 200 messages
        swarm.mail.send(agents[1], [agents[0]], f"chatter {i}")

    real_messages = app_module.TUI._recent_messages

    async def racing(self, sid_):  # an agent writes to the user while the history is fetched
        swarm.mail.send(agents[1], ["user"], "UNIQUE-QUESTION-42")
        return await real_messages(self, sid_)

    monkeypatch.setattr(app_module.TUI, "_recent_messages", racing)
    h = launch.tui(conn, session=sid)
    await h.until(lambda: h.tui.attached and not h.tui._first_connect, 20, "the stream")
    await h.see("IMPORTANT QUESTION for the user")
    await asyncio.sleep(1.0)  # the first catch-up replays the message.sent of the racing question
    assert h.text.count("UNIQUE-QUESTION-42") == 1
    assert h.tui.box_title() == f"→ {agents[1]}"
    assert await h.quit() == 0


async def test_catch_up_does_not_drop_live_events_of_other_types(launch):
    conn = await launch()
    sid = (await conn.client.create_session("Calc", "Build a calculator"))["id"]
    h = launch.tui(conn, session=sid)
    await h.until(lambda: h.tui.attached and h.tui.members and not h.tui._first_connect, 20, "the stream")
    agent = h.tui.members[0]
    base = h.tui.last_event + 1000
    h.tui.last_event = base
    message = {"id": base + 2, "type": "message.sent", "actor": agent,
               "data": {"message": {"id": 99999, "sender": agent, "recipients": ["user"], "text": "Replayed one"}}}
    await h.tui.on_event(message, replayed=True)  # the catch-up (messages only) got ahead of the live stream
    # live events buffered meanwhile: an older id of another type must still count...
    await h.tui.on_event({"id": base + 1, "type": "agent.status", "actor": agent,
                          "data": {"name": agent, "status": "working"}})
    assert agent in h.tui.working
    await h.tui.on_event(message)  # ...the replayed one must not be printed twice
    await h.tui.on_event({"id": base + 3, "type": "agent.status", "actor": agent,
                          "data": {"name": agent, "status": "idle"}})
    assert agent not in h.tui.working and h.tui.last_event == base + 3
    await asyncio.sleep(0.1)
    assert h.text.count("Replayed one") == 1
    assert await h.quit() == 0


async def test_session_restart_reconnects_quietly_and_a_deleted_session_detaches(launch):
    conn = await launch(echoer)
    sid = (await conn.client.create_session("Echo", "Answer pings"))["id"]
    h = launch.tui(conn, session=sid)
    await h.until(lambda: h.tui.attached and h.tui.members and not h.tui._first_connect, 20, "the stream")
    from dreamteam.tui.client import HubClient

    other = HubClient(conn.base_url)  # e.g. the web UI
    try:
        await other.stop_session(sid)
        await h.until(lambda: (h.tui.session or {}).get("status") == "stopped", 20, "the stopped status")
        await other.start_session(sid)
        await h.until(lambda: (h.tui.session or {}).get("status") == "running", 20, "the running status")
    finally:
        await other.aclose()
    h.type("ping-1\r")  # the stream that followed the restart still delivers the answer
    await h.see("pong ping-1", timeout=20)
    assert "Lost connection" not in h.text and h.tui.connected

    await conn.manager.delete(sid)
    await h.see("no longer exists", timeout=20)
    assert not h.tui.attached and h.tui.box_title() == "new session"
    assert await h.quit() == 0


DELETE_KEY = "\x1b[3~"


async def test_sessions_are_deleted_from_the_picker_and_by_command(launch, monkeypatch):
    conn = await launch()
    old = (await conn.client.create_session("Old", "", start=False))["id"]
    spare = (await conn.client.create_session("Spare", "", start=False))["id"]
    main = (await conn.client.create_session("Main", "Main goal"))["id"]
    h = launch.tui(conn, session=main)
    await h.until(lambda: h.tui.attached and h.tui.sid == main, 20, "the attach")

    async def ids() -> list[str]:
        return [s["id"] for s in await conn.client.sessions()]

    async def select(sid: str) -> None:
        await h.until(lambda: isinstance(h.tui.dialog, SessionPicker), what="the picker")
        for _ in range(len(h.tui.dialog.select.options)):
            if h.tui.dialog.select.selected.value == sid:
                return
            index = h.tui.dialog.select.index
            h.type(DOWN)
            await h.until(lambda: h.tui.dialog.select.index != index)
        raise AssertionError(f"{sid} is not in the picker")

    async def confirm() -> ConfirmDialog:
        await h.until(lambda: isinstance(h.tui.dialog, ConfirmDialog), what="the confirmation")
        assert h.tui.dialog.select.selected.value is False  # "No" is preselected: Enter keeps the session
        return h.tui.dialog

    # /sessions: Delete asks first; "No" brings the picker back on the same session
    h.type("/sessions\r")
    await select(old)
    assert "Del to delete" in h.tui.dialog.footer
    h.type(DELETE_KEY)
    dialog = await confirm()
    assert dialog.title == 'Delete session "Old"?' and "cannot be undone" in dialog.text
    assert "agents will be stopped" not in dialog.text  # a stopped session
    h.type("\r")
    await h.until(lambda: isinstance(h.tui.dialog, SessionPicker), what="the picker again")
    assert h.tui.dialog.select.selected.value == old and old in await ids()
    h.type(DELETE_KEY)
    await confirm()
    h.type("1")  # "Yes"
    await h.see(f'Session "Old" ({old}) deleted', 20)
    await h.until(lambda: isinstance(h.tui.dialog, SessionPicker), what="the picker with the sessions left")
    assert old not in [o.value for o in h.tui.dialog.select.options]
    assert old not in await ids() and not conn.manager.dir_of(old).exists()
    h.type(ESC)
    await h.until(lambda: h.tui.dialog is None)
    assert h.tui.attached and h.tui.sid == main

    # /delete without an id: a picker that deletes the chosen session, then closes
    await h.run("/delete nope", 'No session "nope"')
    h.type("/delete\r")
    await select(spare)
    assert h.tui.dialog.title.startswith("Which session to delete?")
    h.type("\r")
    await confirm()
    h.type("1")
    await h.see(f'Session "Spare" ({spare}) deleted', 20)
    await h.until(lambda: h.tui.dialog is None)
    assert await ids() == [main]

    # a refusal (files in use) keeps the current session
    async def refuse(sid: str) -> None:
        raise HubError(409, "Could not delete session: its files are in use")

    monkeypatch.setattr(conn.client, "delete_session", refuse)
    h.type(f"/delete {main}\r")
    dialog = await confirm()
    assert "agents will be stopped" in dialog.text  # the session is running
    h.type("1")
    await h.see("its files are in use")
    assert h.tui.attached and h.tui.sid == main and h.tui.dialog is None
    monkeypatch.undo()

    # the main session of a project home is refused before any question; a folder outside the session is kept
    summary = next(s for s in await conn.client.sessions() if s["id"] == main)
    assert await h.tui.delete_session(main, {**summary, "legacy": True}) is False
    await h.see("cannot be deleted here")
    task = asyncio.create_task(h.tui.delete_session(main, {**summary, "workspace": {"path": "D:/proj", "default": False}}))
    dialog = await confirm()
    assert "the folder D:/proj stays" in dialog.text and "workspace files" not in dialog.text
    h.type(ESC)
    assert await task is False and main in await ids()

    # the current session, with a slow hub (a big workspace to purge): a busy notice takes the keys meanwhile,
    # and the session is left quietly (its stream is refused before the hub answers: no "no longer exists")
    purge = conn.manager._purge_trash

    def slow_purge() -> None:
        time.sleep(app_module.LOST_GRACE + 1)
        purge()

    monkeypatch.setattr(conn.manager, "_purge_trash", slow_purge)
    h.type(f"/delete {main}\r")
    await confirm()
    h.type("1")
    await h.until(lambda: isinstance(h.tui.dialog, BusyDialog), what="the busy notice")
    h.type("typed meanwhile\r")  # swallowed: neither sent to the agents nor a new session's goal
    await h.see(f'Session "Main" ({main}) deleted', 20)
    await h.until(lambda: not h.tui.attached and h.tui.dialog is None, what="the detach")
    assert h.tui.sid is None and h.tui.box_title() == "new session" and h.tui.buffer.text == ""
    await asyncio.sleep(app_module.LOST_GRACE + 0.5)
    assert "no longer exists" not in h.text and "Lost connection" not in h.text and "typed meanwhile" not in h.text
    assert await ids() == [] and not conn.manager.dir_of(main).exists()
    await h.run("/sessions", "No sessions yet")
    assert await h.quit() == 0


async def test_a_dialog_of_another_task_waits_for_the_picker_and_its_confirmation(launch):
    """An MCP approval (any dialog of another task) must not pop up between the picker, the delete
    confirmation and the picker again: the keys meant for them would answer it."""
    conn = await launch()
    first = (await conn.client.create_session("First", "", start=False))["id"]
    second = (await conn.client.create_session("Second", "", start=False))["id"]
    h = launch.tui(conn)
    await h.until(lambda: h.tui.ready.is_set(), what="the start")
    h.type("/sessions\r")
    await h.until(lambda: isinstance(h.tui.dialog, SessionPicker), what="the picker")
    other = asyncio.create_task(h.tui.ask(ConfirmDialog("en", "Another task's question")))
    await asyncio.sleep(0.2)
    h.type(DELETE_KEY)  # on the first session
    await h.until(lambda: isinstance(h.tui.dialog, ConfirmDialog), what="a confirmation")
    assert h.tui.dialog.title.startswith("Delete session"), h.tui.dialog.title
    h.type("\r")  # "No"
    await h.until(lambda: isinstance(h.tui.dialog, SessionPicker), what="the picker again")
    assert not other.done()
    h.type(ESC)
    await h.until(lambda: isinstance(h.tui.dialog, ConfirmDialog) and h.tui.dialog.title == "Another task's question",
                  what="the other dialog after the picker")
    h.type(ESC)
    assert await other is None
    assert sorted(s["id"] for s in await conn.client.sessions()) == sorted([first, second])
    assert await h.quit() == 0


# ---- /cd: the folder a session works in (docs/TUI.md §4.5, SPEC §17.2) -------------------------------------
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8",
                          check=True).stdout


def user_repo(path: Path) -> Path:
    """A project of the user: a git repository with one commit on the branch `feature`."""
    path.mkdir(parents=True)
    git(path, "init", "-q")
    for key, value in (("user.name", "Owner"), ("user.email", "owner@example.com"), ("core.autocrlf", "false")):
        git(path, "config", "--local", key, value)
    (path / "README.md").write_text("# My project\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", "Initial")
    git(path, "checkout", "-q", "-b", "feature")
    return path


async def confirm_dialog(h: Harness, title_part: str) -> ConfirmDialog:
    await h.until(lambda: isinstance(h.tui.dialog, ConfirmDialog) and title_part in h.tui.dialog.title,
                  what=f"the confirmation {title_part!r}")
    return h.tui.dialog


@needs_git
async def test_cd_moves_the_session_adopts_a_project_and_goes_back(launch, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # relative /cd paths resolve against the TUI's current directory
    conn = await launch()
    sid = (await conn.client.create_session("Calc", "Build a calculator",
                                            overrides={"workspace": {"git": True}}))["id"]
    h = launch.tui(conn, session=sid, width=400)
    await h.until(lambda: h.tui.attached and h.tui.members, 30, "the attach")
    default = Path((await conn.client.session(sid))["session"]["dir"]) / "workspace"
    (default / "marker-file.txt").write_text("made by the test\n", encoding="utf-8")

    async def folder() -> dict:
        return (await conn.client.session(sid))["session"]["workspace"]

    await h.run("/cd", "Session folder:")
    assert f"Session folder: {default} (the session's own folder)" in h.text
    assert "/cd --default" in h.out[-1]

    # an empty (here: missing) folder: the files and the git history move there, "Yes" preselected
    moved = tmp_path / "moved" / "here"
    h.type("/cd moved/here\r")
    dialog = await confirm_dialog(h, 'Move session "Calc" to')
    assert str(moved) in dialog.title and "it will be created" in dialog.text and "agents stop" in dialog.text
    assert dialog.select.selected.value is True
    h.type("\r")
    await h.see(f"now works in {moved}: its files moved there", 60)
    await h.see("git: the history moved along")
    assert (moved / "marker-file.txt").exists() and (moved / ".git").is_dir() and not default.exists()
    assert (await folder()) == {"path": str(moved), "default": False, "adopted": False, "exists": True}
    assert (await conn.client.session(sid))["session"]["status"] == "running"  # restarted in its new folder
    (moved / "only-in-moved.txt").write_text("x\n", encoding="utf-8")
    await h.run("!ls", "only-in-moved.txt", timeout=30)  # `!` runs in the session's new folder

    # cancelled (Esc, or "No"): nothing changes
    empty = tmp_path / "empty"
    empty.mkdir()
    h.type(f"/cd {empty}\r")
    dialog = await confirm_dialog(h, "Move session")
    assert "The folder is empty" in dialog.text
    h.type(ESC)
    await h.see("The session folder is unchanged")
    h.type(f"/cd {empty}\r")
    await confirm_dialog(h, "Move session")
    h.type("2")
    await h.until(lambda: h.text.count("The session folder is unchanged") == 2, what="the second cancel")
    assert (await folder())["path"] == str(moved) and not any(empty.iterdir())

    # back to its own folder: the files move back
    h.type("/cd --default\r")
    await confirm_dialog(h, 'Move session "Calc" back to its own folder')
    h.type("\r")
    await h.see(f"now works in {default}: its files moved there", 60)
    assert (default / "marker-file.txt").exists() and (await folder())["default"] is True
    assert moved.is_dir() and not any(moved.iterdir())  # the other old folder is left, empty

    # the user's project with uncommitted changes: refused, nothing changes
    repo = user_repo(tmp_path / "project")
    (repo / "notes.txt").write_text("my own changes\n", encoding="utf-8")
    h.type(f"/cd {repo}\r")
    dialog = await confirm_dialog(h, "Move session")
    assert "not empty" in dialog.text and "current branch" in dialog.text
    assert dialog.select.selected.value is False  # adopting a folder with files: "No" preselected
    h.type("1")
    await h.see("uncommitted changes", 30)
    assert "Commit or stash" in h.text
    assert (await folder())["default"] is True and (default / "marker-file.txt").exists()
    assert git(repo, "status", "--porcelain").strip() == "?? notes.txt"

    # a clean one: adopted, the agents commit to its current branch, the old folder stays
    (repo / "notes.txt").unlink()
    config_before = git(repo, "config", "--local", "--list")
    h.type(f"/cd {repo}\r")
    await confirm_dialog(h, "Move session")
    h.type("1")
    await h.see(f"now works with the files of {repo}", 60)
    await h.see("git: the agents commit to the current branch")
    await h.see(f"The previous folder {default} stays where it is")
    assert (await folder()) == {"path": str(repo), "default": False, "adopted": True, "exists": True}
    assert (default / "marker-file.txt").exists() and git(repo, "branch", "--show-current").strip() == "feature"
    assert git(repo, "config", "--local", "--list") == config_before  # the user's repository config is untouched
    await h.run("!ls", "README.md", timeout=30)
    await h.run("/cd", "Session folder:")
    assert f"{repo} (a project folder: the agents commit to its current branch)" in h.text
    await h.run("/status", "Version")
    assert str(repo) in h.out[-1]
    # the current folder: nothing to ask, and a running `!` command goes on (nothing moves)
    h.type(f"!{long_command()}\r")
    await h.until(lambda: h.tui._bash is not None and h.tui._bash.running, 20, "the running command")
    run = h.tui._bash
    await h.run(f"/cd {repo}", "already works in")
    assert h.tui._bash is run and run.running and "interrupted" not in h.text
    # leaving the project for an empty folder: the project's files stay where they are, so the command goes on
    other = tmp_path / "other-empty"
    other.mkdir()
    h.type(f"/cd {other}\r")
    dialog = await confirm_dialog(h, "Move session")
    assert "starts afresh" in dialog.text and "! command" not in dialog.text
    h.type("\r")
    await h.see(f'Session "Calc" now works in {other}', 60)
    await h.see(f"The previous folder {repo} stays where it is")
    assert h.tui._bash is run and run.running and "interrupted" not in h.text
    h.type(ESC)  # (interrupts the command)
    await h.until(lambda: h.tui._bash is None, 15, "the interrupted command")
    # a command in the folder whose files move: the question says that it will be stopped
    h.type(f"!{long_command()}\r")
    await h.until(lambda: h.tui._bash is not None and h.tui._bash.running, 20, "the second running command")
    run = h.tui._bash
    assert run.cwd == other
    h.type(f"/cd {tmp_path / 'third'}\r")
    dialog = await confirm_dialog(h, "Move session")
    assert "The running ! command will be stopped" in dialog.text
    h.type(ESC)
    await h.until(lambda: h.text.count("The session folder is unchanged") == 3, what="the third cancel")
    assert run.running
    h.type(ESC)  # (interrupts the command)
    await h.until(lambda: h.tui._bash is None, 15, "the interrupted command")
    # the restarts of the session during the moves were not reported as a lost connection (a loss that is not
    # made good is reported only after LOST_GRACE)
    await asyncio.sleep(app_module.LOST_GRACE + 0.5)
    assert "Lost connection" not in h.text and "Reconnected to the hub" not in h.text
    assert await h.quit() == 0


async def test_a_session_in_a_missing_folder_attaches_stopped_and_cd_brings_it_back(launch, tmp_path):
    conn = await launch()
    project = tmp_path / "proj"
    h = launch.tui(conn, workspace=str(project), width=400)  # `dreamteam --workspace DIR`: the next new session
    await h.until(lambda: h.tui.ready.is_set())
    await h.see(f"A new session will work in the folder {project}")
    await h.run("/cd --default", "A new session will work in its own folder")  # no session: /cd picks the folder
    assert h.tui.workspace_override is None
    await h.run(f"/cd {project}", f"A new session will work in the folder {project}")
    h.type("Write a haiku about the sea\r")
    await h.until(lambda: h.tui.attached and h.tui.members, 30, "the new session")
    sid = h.tui.sid
    await h.see(f"works in the new folder {project}")
    assert (await conn.client.session(sid))["session"]["workspace"]["path"] == str(project)
    assert h.tui.workspace_override is None  # only the first session
    assert await h.quit() == 0

    await conn.client.stop_session(sid)
    shutil.rmtree(project)  # e.g. a project folder deleted, or a drive unplugged
    h = launch.tui(conn, session=sid, width=400)
    await h.see("its folder", 30)
    await h.until(lambda: h.tui.attached and h.tui.sid == sid, what="the attach of the stopped session")
    assert f'Session "Write a haiku about the sea" is not running: its folder {project} does not exist' in h.text
    assert "╭─ Goal" in h.text and h.tui.last_event > 0  # an ordinary attach (no replay from the first event)
    assert (await conn.client.session(sid))["session"]["status"] == "stopped" and not project.exists()
    await h.run("!ls", "/cd to choose another one")  # no stand-in folder for the user's project
    h.tui.local = False  # a hub on another computer: a relative path means nothing here
    await h.run("/cd some/where", "The hub is not on this computer: give the absolute path")
    h.tui.local = True
    h.type("hello?\r")  # a message tries to start it: the same clear error
    await h.until(lambda: h.text.count("is not running: its folder") == 2, what="the start error")

    h.type("/cd --default\r")
    dialog = await confirm_dialog(h, "back to its own folder")
    assert "does not exist, so nothing moves" in dialog.text
    h.type("\r")
    await h.see("did not exist: nothing was moved", 30)
    workspace = (await conn.client.session(sid))["session"]["workspace"]
    assert workspace["default"] is True and not project.exists()
    h.type("are you there?\r")  # now it starts
    await h.until(lambda: messages_from_user(conn, sid, "are you there?"), 30, "the message")
    assert (await conn.client.session(sid))["session"]["status"] == "running"
    assert await h.quit() == 0


async def test_console_cd_asks_then_moves_and_comes_back(launch, tmp_path, capsys):
    """`dreamteam console`: /cd shows the folder; /cd PATH asks y/N (Enter = no), --yes does not ask."""
    from dreamteam import console as console_module

    conn = await launch()
    sid = (await conn.client.create_session("Calc", "Build a calculator"))["id"]
    default = Path((await conn.client.session(sid))["session"]["dir"]) / "workspace"
    (default / "marker.txt").write_text("x\n", encoding="utf-8")
    con = console_module.Console(f"{conn.base_url}/s/{sid}", "en")
    assert con.local and con.session_id == sid

    async def say(line: str) -> str:
        capsys.readouterr()
        assert await asyncio.to_thread(con.handle, line) is True
        return " ".join(capsys.readouterr().out.split())

    assert f"Session folder: {default} (the session's own folder)" in await say("/cd")
    target = tmp_path / "target"
    out = await say(f"/cd {target}")
    assert f'Move session "Calc" to {target}?' in out and "it will be created" in out and "[y/N]" in out
    assert "agents stop during the move" in out and con.pending_cd is not None
    assert "Cancelled" in await say("")  # Enter: no
    assert not target.exists() and (default / "marker.txt").exists()

    await say(f'/cd "{target}"')
    out = await say("y")
    assert f"now works in {target}: its files moved there" in out and (target / "marker.txt").exists()
    assert (await conn.client.session(sid))["session"]["status"] == "running"
    assert f"Session folder: {target} (the session's own folder, moved here)" in await say("/cd")

    out = await say("/cd --default --yes")  # no question
    assert f"now works in {default}: its files moved there" in out and (default / "marker.txt").exists()
    assert (await conn.client.session(sid))["session"]["workspace"]["default"] is True

    await say(f"/cd {target}")
    out = await say("/help")  # another line than y/n: cancelled, and handled as usual
    assert "Cancelled" in out and "/cd [path | --default]" in out
    assert (await conn.client.session(sid))["session"]["workspace"]["default"] is True

    con.local = False  # a hub on another computer: a relative path means nothing here
    assert "give the absolute path" in await say("/cd some/where")
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("print('hi')\n", encoding="utf-8")
    out = await say(f"/cd {project}")  # the folder cannot be looked at from here: both cases are explained
    assert "An empty folder:" in out and "A folder with files:" in out
    out = await say("yes")
    assert f"now works with the files of {project}" in out and f"The previous folder {default} stays" in out
    assert "git is off in the settings" in out

    # a stopped session whose folder is gone (`dreamteam console --session` attaches to it for this): /cd fixes it
    lost = tmp_path / "lost"
    usb = (await conn.client.create_session("Usb", "Later", start=False, workspace=str(lost)))["id"]
    shutil.rmtree(lost)
    stopped = console_module.Console(f"{conn.base_url}/s/{usb}", "en", local=True)
    await asyncio.to_thread(stopped._prime)
    capsys.readouterr()
    await asyncio.to_thread(stopped.handle, "/cd")
    assert f"The folder {lost} does not exist" in " ".join(capsys.readouterr().out.split())
    await asyncio.to_thread(stopped.handle, "/cd --default --yes")
    assert "did not exist: nothing was moved" in " ".join(capsys.readouterr().out.split())
    assert (await conn.client.session(usb))["session"]["workspace"]["default"] is True

    single = console_module.Console(conn.base_url, "ru")  # not a launcher session: no /cd
    capsys.readouterr()
    await asyncio.to_thread(single.handle, "/cd")
    assert "только с сессией лаунчера" in capsys.readouterr().out


async def test_background_jobs_in_the_status_line_and_the_jobs_command(tmp_path, monkeypatch):
    """An agent's dev server as a background job: "jobs 1" in the status line, the job.* lines, /jobs (the table),
    /status, /jobs j1 (its output) and /jobs stop j1. A pause does not stop it. No process is left behind."""
    from test_procs import Children, pid_alive

    monkeypatch.setenv("PYEXE", sys.executable)
    (tmp_path / "kids").mkdir()
    kids = Children(tmp_path / "kids")
    script, pidfile = str(tmp_path / "kids" / "child.py"), kids.pidfile()
    if sys.platform == "win32":
        program, command = "cmd", f'"%PYEXE%" "{script}" "60" "{pidfile}"'
    else:
        program, command = "sh", f"\"$PYEXE\" '{script}' 60 '{pidfile}'"
    started: list[bool] = []

    def server(system, messages, tools):
        if not started:          # its first turn: the server, in the background; then it sleeps
            started.append(True)
            return call("shell", command=command, background=True)
        return call("wait")

    home = make_home(tmp_path)
    st.write_raw(home, {**st.read_raw(home), "shell": {"program": program}})
    conn = await connect(home, web=False, llm_factory=lambda profile, agent: ScriptedLLM(server, profile))
    stack = contextlib.ExitStack()
    h = None
    try:
        sid = (await conn.client.create_session("Dev server", "Run the dev server"))["id"]
        h = Harness(conn, stack, width=140, continue_last=True)
        await h.until(lambda: h.tui.attached and h.tui.members, 20, "the attach")
        await h.until(lambda: h.tui.jobs == ["j1"], 30, "the running job")
        pid = await asyncio.to_thread(kids.pid)
        agent = h.tui.members[0]
        assert "jobs 1" in "".join(text for _, text in h.tui._status_text())
        await h.see(f"{agent} · background job j1: ")
        await h.run("/jobs", "/jobs jN — a job's output")
        table = h.out[-1]
        assert "j1" in table and agent in table and "running" in table and "started in the background" in table
        await h.run("/status", "Jobs")
        assert re.search(r"Jobs +1 running \(j1\)", h.out[-1])

        async def logged() -> bool:
            ok, text = await conn.client.act(sid, "jobs", {"job_id": "j1"})
            return ok and "child up" in text

        await h.until(logged, 20, "the job's first output")
        await h.run("/jobs j1", "child up")
        assert f"Job j1 of {agent}: " in h.out[-1]
        await h.run("/pause", "The swarm is paused")   # a pause does not stop the background jobs
        await asyncio.sleep(0.5)
        assert pid_alive(pid) and h.tui.jobs == ["j1"]
        await h.run("/jobs stop j1", "Stopped job j1 (")
        assert not pid_alive(pid)
        await h.until(lambda: not h.tui.jobs, 10, "the job gone from the status line")
        await h.see(f"{agent} · job j1 stopped by you (")
        assert "jobs" not in "".join(text for _, text in h.tui._status_text())
        await h.run("/jobs", "j1")
        assert "stopped by you" in h.out[-1]
        assert await h.quit() == 0
    finally:
        if h is not None:
            await h.close()
        stack.close()
        await conn.close()
    assert kids.check() == [], "child processes were left running"
