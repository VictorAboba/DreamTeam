"""TUI hub client (dreamteam.tui.client): REST, WebSocket stream and connect() against a real in-process
launcher with scripted agents."""

from __future__ import annotations

import asyncio
import socket
import time
from pathlib import Path
from typing import Any

import pytest

from dreamteam import cli
from dreamteam import settings as st
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.tui.client import Connection, HubClient, HubError, connect


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_policy(system, messages, tools) -> Any:
    return {"tool_calls": [{"name": "wait", "arguments": {}}]}


def make_home(tmp_path: Path, port: int | None = None) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    st.write_raw(home, {
        "swarm": {"name": "Home", "language": "en", "start_agents": 1, "max_agents": 3, "max_cost_usd": 0},
        "agents": {"stagger_seconds": 0, "idle_nudge_seconds": 30},
        "server": {"port": port or free_port(), "resume_sessions": True},
        "workspace": {"git": False},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
    })
    return home


def factory(profile, agent):
    return ScriptedLLM(wait_policy, profile)


class Feed:
    """Consumes client.stream() in a task so a test can wait for events with timeouts."""

    def __init__(self, client: HubClient, sid: str | None):
        self.queue: asyncio.Queue = asyncio.Queue()
        self.stream = client.stream(sid)
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        async for ev in self.stream:
            self.queue.put_nowait(ev)

    async def until(self, predicate, timeout: float = 10.0) -> list[dict]:
        """Events up to and including the first one matching `predicate`."""
        seen = []
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"no matching event; got {[e.get('type') for e in seen]}")
            try:
                ev = await asyncio.wait_for(self.queue.get(), remaining)
            except asyncio.TimeoutError:
                continue
            seen.append(ev)
            if predicate(ev):
                return seen

    async def close(self) -> None:
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        await self.stream.aclose()


def connection(state: str):
    return lambda ev: ev.get("type") == "_connection" and ev["data"]["state"] == state


@pytest.fixture
async def conn(tmp_path):
    c = await connect(make_home(tmp_path), web=False, llm_factory=factory)
    try:
        yield c
    finally:
        await c.close()


async def test_rest_api_of_an_in_process_launcher(conn: Connection):
    client = conn.client
    assert conn.owned and conn.manager is not None and conn.server_running
    assert conn.base_url.startswith("http://127.0.0.1:") and client.mode == "launcher"
    assert not cli.hub_file(conn.home).exists()  # web=False writes no hub.json
    info = await client.hub_info()
    assert info["mode"] == "launcher" and Path(info["home"]) == conn.home
    assert await client.sessions() == []
    assert "models" in (await client.settings())["settings"]

    session = await client.create_session("Snake", "Write snake", description="Python")
    sid = session["id"]
    assert session["status"] == "running" and session["title"] == "Snake"
    assert [s["id"] for s in await client.sessions()] == [sid]
    assert (await client.session(sid))["session"]["id"] == sid
    state = await client.state(sid)
    assert state["swarm"]["goal"]["title"] == "Write snake"
    assert state["swarm"]["session"]["id"] == sid and len(state["agents"]) == 1

    ok, text = await client.act(sid, "task_create", {"title": "Extra", "goal": "More"})
    assert ok, text
    task_id = int(text.split("#")[1].split()[0].rstrip(".:,"))
    detail = await client.task(sid, task_id)
    assert detail["task"]["title"] == "Extra" and isinstance(detail["discussion"], list)
    ok, text = await client.act(sid, "no_such_tool", {})
    assert not ok and text

    agent = state["agents"][0]["name"]
    ok, text = await client.act(sid, "send_message", {"to": [agent], "text": "Hello there"})
    assert ok, text
    messages = await client.messages(sid, with_=agent)
    assert messages[-1]["text"] == "Hello there"
    assert any(m["text"] == "Hello there" for m in await client.messages(sid))
    conn.manager.swarms[sid].mail.send(agent, ["user"], "A question for you")
    inbox = await client.inbox(sid)
    msg = inbox["messages"][-1]
    assert msg["text"] == "A question for you" and msg["read"] is False
    assert (await client.inbox(sid, since_message=msg["id"]))["messages"] == []
    await client.mark_read(sid, with_=agent)
    assert (await client.inbox(sid))["messages"][-1]["read"] is True
    await client.mark_read(sid)

    events = await client.events(sid, limit=500)
    assert {"goal.set", "task.created", "message.sent"} <= {e["type"] for e in events}
    assert await client.events(sid, since=events[-1]["id"]) == []
    assert all(e["type"] == "goal.set" for e in await client.events(sid, types=["goal.*"]))
    assert (await client.files(sid))["type"] == "dir"

    assert isinstance(await client.traces(sid), list)
    assert isinstance(await client.traces(sid, agent=agent, limit=5), list)
    assert isinstance(await client.trace_stats(sid), dict)
    with pytest.raises(HubError) as exc:
        await client.trace(sid, 999999)
    assert exc.value.status == 404 and "not found" in exc.value.message.lower()
    with pytest.raises(HubError) as exc:
        await client.span(sid, 999999)
    assert exc.value.status == 404
    with pytest.raises(HubError) as exc:
        await client.state("nope")
    assert exc.value.status == 404 and "nope" in exc.value.message
    with pytest.raises(HubError) as exc:
        await client.start_session("nope")
    assert exc.value.status == 404

    stopped = await client.stop_session(sid)
    assert stopped["status"] == "stopped"
    assert (await client.state(sid))["swarm"]["session"]["status"] == "stopped"  # viewable
    ok, text = await client.act(sid, "board", {})
    assert not ok and "stopped" in text  # 409 in the act shape
    assert (await client.start_session(sid))["status"] == "running"
    assert (await client.act(sid, "board", {}))[0]


async def test_stream_events_and_reconnect_after_restart(conn: Connection):
    client = conn.client
    sid = (await client.create_session("Live", "Stream things"))["id"]
    feed = Feed(client, sid)
    try:
        await feed.until(connection("connected"))
        ok, _ = await client.act(sid, "task_create", {"title": "Streamed", "goal": "Seen live"})
        assert ok
        seen = await feed.until(lambda ev: ev.get("type") == "task.created"
                                and ev["data"]["task"]["title"] == "Streamed")
        assert all({"type", "ts", "data"} <= set(ev) for ev in seen)

        # stopping and starting the session closes the socket with 1012: the stream reconnects by itself
        await client.stop_session(sid)
        await feed.until(connection("lost"))
        await feed.until(connection("connected"))
        await client.start_session(sid)
        for attempt in range(10):  # the start may close the socket once more: retry until an event arrives
            title = f"After restart {attempt}"
            ok, _ = await client.act(sid, "task_create", {"title": title, "goal": "Seen"})
            assert ok
            try:
                await feed.until(lambda ev: ev.get("type") == "task.created"
                                 and ev["data"]["task"]["title"] == title, timeout=2.0)
                break
            except AssertionError:
                continue
        else:
            raise AssertionError("no events after the restart")
    finally:
        await feed.close()


async def test_stream_stops_when_the_client_closes(conn: Connection):
    sid = (await conn.client.create_session("Close", "Close me"))["id"]
    client = HubClient(conn.base_url)
    received: list[dict] = []

    async def consume():
        async for ev in client.stream(sid):
            received.append(ev)

    task = asyncio.create_task(consume())
    for _ in range(100):
        if received:
            break
        await asyncio.sleep(0.05)
    assert received[0] == {"type": "_connection", "data": {"state": "connected"}}
    await client.aclose()
    await asyncio.wait_for(task, 5)  # ends without an exception
    with pytest.raises(HubError) as exc:
        await client.state(sid)
    assert exc.value.status == 0


async def test_stream_without_hub_reports_lost_and_stops_on_close():
    client = HubClient(f"http://127.0.0.1:{free_port()}", timeout=2)
    with pytest.raises(HubError) as exc:
        await client.hub_info()
    assert exc.value.status == 0 and "not reachable" in exc.value.message
    stream = client.stream("abc")
    first = await asyncio.wait_for(stream.__anext__(), 10)
    assert first == {"type": "_connection", "data": {"state": "lost"}}
    nxt = asyncio.ensure_future(stream.__anext__())  # now sleeping in the backoff
    await asyncio.sleep(0.1)
    started = time.monotonic()
    await client.aclose()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(nxt, 5)
    assert time.monotonic() - started < 2
    assert client.ws_url("abc").startswith("ws://127.0.0.1:") and client.ws_url("abc").endswith("/s/abc/ws")
    assert client.ws_url(None).endswith(":%d/ws" % int(client.base_url.rsplit(":", 1)[1]))


async def test_connect_writes_hub_file_and_attaches_to_a_running_hub(tmp_path):
    port = free_port()
    home = make_home(tmp_path, port)
    with pytest.raises(HubError) as exc:
        await connect(home, start=False)
    assert exc.value.status == 0
    first = await connect(home, llm_factory=factory)
    try:
        assert first.owned and first.base_url == f"http://127.0.0.1:{port}"
        assert cli.hub_file(home).exists()
        assert await asyncio.to_thread(cli.running_hub, home) == first.base_url
        second = await connect(home)
        try:
            assert not second.owned and second.manager is None and second.base_url == first.base_url
            assert second.client.mode == "launcher"
            sid = (await second.client.create_session("Shared", "", start=False))["id"]
            assert [s["id"] for s in await first.client.sessions()] == [sid]
        finally:
            await second.close()
        assert cli.hub_file(home).exists() and first.server_running  # the attached client did not stop it
    finally:
        await first.close()
    assert not cli.hub_file(home).exists() and not first.server_running


async def test_connect_falls_back_to_a_free_port_and_cleans_up_on_failure(tmp_path, monkeypatch):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        home = make_home(tmp_path, port)
        conn = await connect(home, llm_factory=factory)
        try:
            assert conn.owned and not conn.base_url.endswith(f":{port}")
            assert (await conn.client.hub_info())["mode"] == "launcher"
            assert cli._read_hub_file(home)["base_url"] == conn.base_url
        finally:
            await conn.close()
    assert not cli.hub_file(home).exists()

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(cli, "_write_hub_file", broken)
    with pytest.raises(HubError) as exc:
        await connect(home, llm_factory=factory)
    assert "disk full" in exc.value.message
    assert not [t for t in asyncio.all_tasks() if t.get_name() == "dreamteam-launcher" and not t.done()]


async def test_hub_file_is_written_before_the_sessions_resume(tmp_path, monkeypatch):
    """A second `dreamteam` during a long resume must find the hub, not run the same sessions again."""
    from dreamteam.sessions import SessionManager

    port = free_port()
    home = make_home(tmp_path, port)
    first = await connect(home, llm_factory=factory)
    sid = (await first.client.create_session("Resumable", "Keep going"))["id"]
    await first.close()  # the running session stays marked for resume
    resuming, release = asyncio.Event(), asyncio.Event()
    original = SessionManager.resume

    async def slow_resume(self):
        resuming.set()
        await release.wait()
        return await original(self)

    monkeypatch.setattr(SessionManager, "resume", slow_resume)
    starting = asyncio.create_task(connect(home, llm_factory=factory))
    try:
        await asyncio.wait_for(resuming.wait(), 20)
        assert cli.hub_file(home).exists()
        assert await asyncio.to_thread(cli.running_hub, home) == f"http://127.0.0.1:{port}"
        second = await connect(home, llm_factory=factory)
        try:
            assert not second.owned and second.manager is None and second.base_url == f"http://127.0.0.1:{port}"
        finally:
            await second.close()
        release.set()
        owner = await asyncio.wait_for(starting, 30)
        try:
            assert owner.owned and owner.manager.running == {sid}
        finally:
            await owner.close()
    finally:
        release.set()
        if not starting.done():
            starting.cancel()
        await asyncio.gather(starting, return_exceptions=True)
    assert not cli.hub_file(home).exists()


async def test_busy_port_waits_for_the_hub_of_the_home(tmp_path, monkeypatch):
    """Two `dreamteam` started at once: the one that loses the port attaches to the other's hub (its hub.json
    shows up a moment later) instead of starting a second launcher on an ephemeral port."""
    port = free_port()
    home = make_home(tmp_path, port)
    first = await connect(home, llm_factory=factory)
    try:
        real = cli.running_hub
        calls = []

        def late_hub_file(project_dir):  # the first check runs before the other process wrote hub.json
            calls.append(1)
            return None if len(calls) == 1 else real(project_dir)

        monkeypatch.setattr(cli, "running_hub", late_hub_file)
        second = await connect(home, llm_factory=factory)
        try:
            assert len(calls) >= 2
            assert not second.owned and second.base_url == first.base_url == f"http://127.0.0.1:{port}"
        finally:
            await second.close()
    finally:
        await first.close()


async def test_stream_reports_a_deleted_session_as_rejected(conn: Connection):
    sid = (await conn.client.create_session("Doomed", "Go away"))["id"]
    feed = Feed(conn.client, sid)
    try:
        await feed.until(connection("connected"))
        await conn.manager.delete(sid)
        seen = await feed.until(connection("rejected"))
        assert seen[-1]["data"]["status"] == 403
        assert [ev["data"]["state"] for ev in seen if ev["type"] == "_connection"][-2:] == ["lost", "rejected"]
        with pytest.raises(HubError) as exc:
            await conn.client.state(sid)
        assert exc.value.status == 404
    finally:
        await feed.close()


async def test_unreachable_hub_is_a_distinct_error():
    from dreamteam.tui.client import HubUnreachable

    client = HubClient(f"http://127.0.0.1:{free_port()}", timeout=2)
    try:
        with pytest.raises(HubUnreachable) as exc:
            await client.sessions()
        assert isinstance(exc.value, HubError) and exc.value.status == 0
    finally:
        await client.aclose()


async def test_single_swarm_hub(tmp_path):
    from dreamteam.config import Config
    from dreamteam.swarm import Swarm
    from dreamteam.web import serve

    config = Config.from_dict(tmp_path / "project", {
        "swarm": {"name": "Solo", "language": "en", "start_agents": 0},
        "workspace": {"git": False},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted"}},
    })
    swarm = Swarm(config=config, llm_factory=factory)
    await swarm.start()
    port = free_port()
    started = asyncio.Event()
    server = asyncio.create_task(serve(swarm, "127.0.0.1", port, on_started=started.set))
    client = HubClient(f"http://127.0.0.1:{port}")
    try:
        await asyncio.wait_for(started.wait(), 10)
        assert (await client.hub_info())["name"] == "Solo" and client.mode == "swarm"
        with pytest.raises(HubError) as exc:
            await client.sessions()
        assert exc.value.status in (404, 405)
        assert client.session_base(None) == "" and client.ws_url(None).endswith(f":{port}/ws")
        ok, text = await client.act(None, "set_goal", {"title": "Solo goal"})
        assert ok, text
        assert (await client.state(None))["swarm"]["goal"]["title"] == "Solo goal"
        feed = Feed(client, None)
        try:
            await feed.until(connection("connected"))
            await client.act(None, "task_create", {"title": "Solo task", "goal": "g"})
            await feed.until(lambda ev: ev.get("type") == "task.created")
        finally:
            await feed.close()
    finally:
        await client.aclose()
        server.cancel()
        await asyncio.gather(server, return_exceptions=True)
        await swarm.stop()
        swarm.close()
