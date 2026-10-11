"""MCP clients of the hub (hub_mcp): a client that joined as an external member and also acts as the user never
gets its own messages back as news, while every other member still does. Clients are told apart by connection
(stdio, in-process), by the X-DreamTeam-Client header or by the MCP session id."""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.datastructures import Headers

from dreamteam.config import Config
from dreamteam.hub_mcp import CLIENT_HEADER, build_hub_server, client_key, hub_call, serving_client
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.models import USER, AgentKind
from dreamteam.swarm import Swarm

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

NOTHING = "No new events in 0s. Call wait_for_events again to keep listening."


def make_config(tmp_path: Path) -> Config:
    return Config.from_dict(tmp_path / "project", {
        "swarm": {"name": "Hub clients", "language": "en", "start_agents": 1, "max_agents": 2, "max_cost_usd": 0},
        "agents": {"stagger_seconds": 0},
        "workspace": {"git": False},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
    })


@pytest.fixture
async def swarm(tmp_path):
    sw = Swarm(config=make_config(tmp_path), llm_factory=lambda profile, agent: ScriptedLLM(lambda *a: "ok", profile))
    await sw.start(run_agents=False)
    try:
        yield sw
    finally:
        await sw.stop()
        sw.close()


async def call(client, tool: str, args: dict) -> str:
    res = await client.call_tool(tool, args)
    assert not res.is_error, res.content[0].text
    return res.content[0].text


def agent_of(swarm: Swarm) -> str:
    return next(m["name"] for m in swarm.members() if m["kind"] == AgentKind.LLM)


def test_client_key():
    assert client_key(SimpleNamespace(request=None), "local:1") == "local:1"  # stdio: one client per server
    assert client_key(SimpleNamespace(), "local:1") == "local:1"
    http = SimpleNamespace(request=SimpleNamespace(headers=Headers({"x-dreamteam-client": " proxy 7 "})))
    assert client_key(http, "local:1") == "client:proxy 7"
    both = Headers({"X-DreamTeam-Client": "p", "Mcp-Session-Id": "s1"})
    assert client_key(SimpleNamespace(request=SimpleNamespace(headers=both)), "local:1") == "client:p"
    session = SimpleNamespace(request=SimpleNamespace(headers=Headers({"mcp-session-id": "s1"})))
    assert client_key(session, "local:1") == "session:s1"
    # the 2026-07-28 protocol over HTTP: nothing names the client
    assert client_key(SimpleNamespace(request=SimpleNamespace(headers=Headers({}))), "local:1") is None


async def test_a_client_never_gets_its_own_message_back(swarm):
    """Session 002: a Claude Code joined as «Алиса Никитина», listened with wait_for_events as her and sent a
    message to ["all"] as the user, which came back to it as an event of its member."""
    from mcp import Client

    agent = agent_of(swarm)
    async with Client(build_hub_server(swarm)) as one, Client(build_hub_server(swarm)) as two:
        await call(one, "join_swarm", {"role": "Researcher", "name": "Алиса Никитина"})
        await call(two, "join_swarm", {"role": "Tester", "name": "Bob Stone"})
        text = await call(one, "send_message", {"to": ["all"], "text": "status, please"})  # as the user
        assert text == f"Sent message #1 to {agent}, Алиса Никитина, Bob Stone."
        # its own member: delivered (its history shows it) but already read, so no news and no wake-up
        assert await call(one, "wait_for_events", {"timeout": 0, "as_agent": "Алиса Никитина"}) == NOTHING
        assert "status, please" in await call(one, "read_messages", {"include_read": True,
                                                                     "as_agent": "Алиса Никитина"})
        # every other member gets it as news: the other client's member and the AI agents
        assert "status, please" in await call(two, "wait_for_events", {"timeout": 0, "as_agent": "Bob Stone"})
        assert swarm.has_pending(agent)
        # what its member sends to the user reaches the user (the board, other clients) as unread
        await call(one, "send_message", {"to": ["user"], "text": "done", "as_agent": "Алиса Никитина"})
        assert swarm.mail.unread_count(USER) == 1
        # a client that acts as no member: its messages are news to everyone
        async with Client(build_hub_server(swarm)) as watcher:
            await call(watcher, "send_message", {"to": ["all"], "text": "from the watcher"})
        assert "from the watcher" in await call(one, "wait_for_events", {"timeout": 0, "as_agent": "Алиса Никитина"})
        # a member belongs to the client that last acted as it: after a reconnect through another client, what the
        # first one sends is news to it again
        async with Client(build_hub_server(swarm)) as three:
            assert "Welcome back" in await call(three, "join_swarm", {"role": "Researcher", "name": "алиса  никитина"})
            await call(one, "send_message", {"to": ["all"], "text": "again"})
            assert "again" in await call(three, "wait_for_events", {"timeout": 0, "as_agent": "Алиса Никитина"})
            await call(three, "send_message", {"to": ["all"], "text": "mine"})
            assert await call(three, "wait_for_events", {"timeout": 0, "as_agent": "Алиса Никитина"}) == NOTHING
            assert "mine" in await call(two, "wait_for_events", {"timeout": 0, "as_agent": "Bob Stone"})


async def test_hub_call_takes_the_client_from_serving_client(swarm):
    """A launcher MCP server forwards calls to a session in a task of its own: serving_client names the client for
    them."""
    await hub_call(swarm, "join_swarm", {"role": "Researcher", "name": "Ann Lee"}, client="client:a")
    with serving_client("client:a"):
        text, is_error = await asyncio.ensure_future(hub_call(swarm, "send_message", {"to": ["all"], "text": "hi"}))
    assert not is_error and not swarm.has_pending("Ann Lee") and swarm.has_pending(agent_of(swarm))
    text, is_error = await hub_call(swarm, "send_message", {"to": ["all"], "text": "unknown client"})
    assert not is_error and swarm.has_pending("Ann Lee")  # nothing names the client: delivered as before


async def test_an_agent_the_client_hires_still_reaches_its_member(tmp_path):
    """A client acting as its member hires an agent: the agent's runtime starts inside that hub call, but what the
    agent later sends the member is news for it (unread, it wakes wait_for_events), not the client's own message."""
    def factory(profile, agent):
        def handler(system, messages, tools):
            if agent == "Zed Helper" and not any(m.get("role") == "tool" for m in messages):
                return {"tool_calls": [{"name": "send_message",
                                        "arguments": {"to": ["Ann Lee"], "text": "I found the bug"}}]}
            return {"tool_calls": [{"name": "wait", "arguments": {}}]}
        return ScriptedLLM(handler, profile)

    sw = Swarm(config=make_config(tmp_path), llm_factory=factory)
    await sw.start(run_agents=True)
    try:
        text, is_error = await hub_call(sw, "join_swarm", {"role": "Researcher", "name": "Ann Lee"}, client="client:a")
        assert not is_error, text
        text, is_error = await hub_call(sw, "hire_agent", {"role": "Helper", "name": "Zed Helper",
                                                           "instructions": "help Ann", "as_agent": "Ann Lee"},
                                        client="client:a")
        assert not is_error, text
        sw.mail.send(USER, ["Zed Helper"], "please report to Ann Lee")
        for _ in range(200):
            if sw.db.scalar("SELECT COUNT(*) FROM messages WHERE sender = 'Zed Helper'"):
                break
            await asyncio.sleep(0.05)
        rows = sw.db.all("SELECT i.read_at FROM inbox i JOIN messages m ON m.id = i.message_id"
                         " WHERE i.recipient = 'Ann Lee' AND m.sender = 'Zed Helper'")
        assert [r["read_at"] for r in rows] == [None] and sw.has_pending("Ann Lee")
        text, is_error = await hub_call(sw, "wait_for_events", {"timeout": 0, "as_agent": "Ann Lee"},
                                        client="client:a")
        assert not is_error and "I found the bug" in text
    finally:
        await sw.stop()
        sw.close()


async def test_a_stdio_proxy_names_its_client():
    """`dreamteam mcp` with a hub running is a stdio proxy: one client, named by a header of its own."""
    from dreamteam.hub_mcp import PROXY_READ_TIMEOUT, proxy_http_client

    async with proxy_http_client() as first, proxy_http_client() as second:
        assert first.headers[CLIENT_HEADER] and first.headers[CLIENT_HEADER] != second.headers[CLIENT_HEADER]
        assert first.timeout.read == PROXY_READ_TIMEOUT


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_clients_over_http_are_told_apart(swarm):
    """Over HTTP a client is known by the X-DreamTeam-Client header (the stdio proxy sends one) or by its MCP
    session id (handshake protocol versions). A 2026-07-28 client without the header cannot be told from another
    one: it gets its own message back, as before, and nobody else misses it."""
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    from dreamteam.web import serve

    started = asyncio.Event()
    server = asyncio.create_task(serve(swarm, "127.0.0.1", free_port(), on_started=started.set))
    try:
        await asyncio.wait_for(started.wait(), 30)
        url = swarm.hub_url
        async with httpx2.AsyncClient(timeout=30, headers={CLIENT_HEADER: "alpha"}) as h1, \
                httpx2.AsyncClient(timeout=30) as h2, httpx2.AsyncClient(timeout=30) as h3, \
                Client(streamable_http_client(url, http_client=h1)) as named, \
                Client(streamable_http_client(url, http_client=h2), mode="legacy") as session, \
                Client(streamable_http_client(url, http_client=h3)) as anonymous:
            members = [(named, "Ann Lee"), (session, "Bob Stone"), (anonymous, "Carl Grey")]
            for client, name in members:
                await call(client, "join_swarm", {"role": "Helper", "name": name})
            for sender, own in members:
                await call(sender, "send_message", {"to": ["all"], "text": f"from {own}'s client"})
                for client, name in members:
                    text = await call(client, "wait_for_events", {"timeout": 0, "as_agent": name})
                    if name == own and sender is not anonymous:
                        assert text == NOTHING, (own, name)
                    else:
                        assert f"from {own}'s client" in text, (own, name)
    finally:
        server.cancel()
        await asyncio.gather(server, return_exceptions=True)
