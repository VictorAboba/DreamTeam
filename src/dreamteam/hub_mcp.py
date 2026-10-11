"""The swarm itself as an MCP server, so that any MCP client (Claude Code, Claude Desktop, other agents)
can watch and steer the swarm as the user, or join it as a working member."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import copy
import logging
import os
import threading
import time
import uuid
import weakref
from typing import TYPE_CHECKING, Any, Iterable, Iterator

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings

from . import __version__
from .mailbox import sent_as_seen_by
from .models import USER, AgentKind, ToolError
from .tools import REGISTRY, ToolContext, call_tool, tools_for

if TYPE_CHECKING:
    from .swarm import Swarm

log = logging.getLogger(__name__)

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}

INSTRUCTIONS = """You are connected to a DreamTeam swarm: a self-organizing team of AI agents with human names that
work on a shared task board toward one global goal, talk through direct messages and per-task discussions,
share a git-versioned workspace, set triggers and build their own MCP tools.

By default every call acts as the human USER of the swarm: you can read the board (`board`, `task_get`,
`task_discussion`), set the global goal (`set_goal`), create and edit tasks, talk to any agent
(`send_message`, `read_messages`), hire or retire agents, pause/resume and change limits (`swarm_control`),
approve MCP servers (`mcp_approve`) and wait for messages addressed to the user (`wait_for_events`; with
`progress=true` it also returns when the board moves on). `jobs` and `job_stop` list and stop every member's
background jobs.

To work INSIDE the swarm as a member, call `join_swarm(role=...)`: you get a unique human name; then pass
`as_agent="<that name>"` in every call. Members claim tasks (`task_claim`), write shared files (`write_file`,
`edit_file`), run commands in the swarm workspace (`shell`), build and call MCP tools (`mcp_create`,
`mcp_call`), finish tasks (`task_complete`) and receive events with `wait_for_events`. Members who join this way do
not count toward the swarm's agent limit (max_agents). A `shell` command still running at its timeout (at most 600
seconds over MCP) continues as a background job, and background=true starts one at once; processes a command leaves
running become a job too. `jobs` shows jobs and their output, `job_stop` stops one, and a job's end reaches you
through `wait_for_events`, so don't poll."""

AS_AGENT = {
    "type": "string",
    "description": "Act as this swarm member (name returned by join_swarm). Omit to act as the user.",
}


def hub_specs() -> list:
    """Tools offered over MCP (external members and the user), in registry order."""
    tools_for("user")  # makes sure the registry is loaded
    return [s for s in REGISTRY.values() if s.audience & {"external", "user"}]


def _description(spec, language: str) -> str:
    text = spec.describe(language)
    if "user" not in spec.audience:
        text += " [Members only: call join_swarm first and pass as_agent.]"
    elif "external" not in spec.audience and spec.name != "join_swarm":
        text += " [User only: omit as_agent.]"
    return text


def list_hub_tools(swarm: "Swarm") -> list[types.Tool]:
    tools = []
    for spec in hub_specs():
        schema = copy.deepcopy(spec.parameters)
        if spec.name != "join_swarm":
            schema["properties"] = {**schema.get("properties", {}), "as_agent": AS_AGENT}
        tools.append(types.Tool(name=spec.name, description=_description(spec, swarm.language), input_schema=schema))
    return tools


def resolve_actor(swarm: "Swarm", as_agent: Any) -> str:
    if as_agent in (None, ""):
        return USER
    name = swarm.resolve_member(str(as_agent), allow_user=True)
    if name != USER and swarm.kind_of(name) != AgentKind.EXTERNAL:
        raise ToolError(f"{name} is an internal swarm agent; as_agent must be a member created with join_swarm.")
    return name


def _text_result(text: str, is_error: bool) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=is_error)


# Calls of external members are recorded in their traces, except polling for events.
UNTRACED_TOOLS = frozenset({"wait_for_events"})

# Names one MCP client over any transport and protocol version: serve_stdio_proxy sends it with every request (the
# 2026-07-28 protocol has no session id); any HTTP client may send it too (e.g. a `--header` of `claude mcp add`).
CLIENT_HEADER = "X-DreamTeam-Client"
_CLIENT: contextvars.ContextVar[str | None] = contextvars.ContextVar("dreamteam_mcp_client", default=None)


class HubClients:
    """Which MCP client acts as which external member of a swarm. A client may send as the user (no as_agent)
    and listen as its member: what it sends must not come back to it as news (wait_for_events). A member belongs
    to the client that last acted as it (join_swarm, or a call with its as_agent)."""

    def __init__(self) -> None:
        self._client_of: dict[str, str] = {}  # external member -> client

    def acted_as(self, client: str | None, member: str) -> None:
        if client:
            self._client_of[member] = client

    def members_of(self, client: str | None) -> frozenset[str]:
        return frozenset(m for m, c in self._client_of.items() if client and c == client)


_HUB_CLIENTS: "weakref.WeakKeyDictionary[Any, HubClients]" = weakref.WeakKeyDictionary()


def hub_clients(swarm: "Swarm") -> HubClients:
    clients = _HUB_CLIENTS.get(swarm)
    if clients is None:
        clients = _HUB_CLIENTS[swarm] = HubClients()
    return clients


def client_key(ctx: Any, local: str) -> str | None:
    """Which MCP client a request comes from: `local` without HTTP (a stdio server has one client), else the
    CLIENT_HEADER or the MCP session id (handshake protocol versions). None when nothing names it: the 2026-07-28
    protocol and stateless HTTP serve every request alone, and a client cannot be told from another one then."""
    request = getattr(ctx, "request", None)
    if request is None:
        return local
    headers = getattr(request, "headers", None) or {}
    named = " ".join(str(headers.get(CLIENT_HEADER) or "").split())[:200]
    if named:
        return f"client:{named}"
    session = str(headers.get("mcp-session-id") or "").strip()
    return f"session:{session}" if session else None


@contextlib.contextmanager
def serving_client(client: str | None) -> Iterator[None]:
    """hub_call inside this block (also in tasks it starts) comes from `client` (client_key), e.g. for a launcher
    MCP server that forwards calls to a session."""
    token = _CLIENT.set(client)
    try:
        yield
    finally:
        _CLIENT.reset(token)


def _externals(swarm: "Swarm") -> set[str]:
    return {m["name"] for m in swarm.members() if m["kind"] == AgentKind.EXTERNAL}


async def hub_call(swarm: "Swarm", name: str, args: dict[str, Any], client: str | None = None) -> tuple[str, bool]:
    """Run a tool over MCP with the hub actor rules (`as_agent`); calls of external members are traced. `client`
    (client_key; default: serving_client's): a message the client sends reaches the external members it acts as
    already read, so it never comes back to it as news (user broadcasts still reach every other member)."""
    args = dict(args or {})
    try:
        actor = resolve_actor(swarm, args.pop("as_agent", None))
    except ToolError as exc:
        return f"Error: {exc}", True
    client = _CLIENT.get() if client is None else client
    clients = hub_clients(swarm)
    if actor != USER:
        clients.acted_as(client, actor)
    joining = bool(client) and actor == USER and name == "join_swarm"
    before = _externals(swarm) if joining else set()
    started = time.time()
    with sent_as_seen_by(clients.members_of(client), actor):
        text, is_error = await call_tool(ToolContext(swarm, actor), name, args)
    if joining and not is_error:
        # the member it became: a new one, or the one it continues as after a reconnect (join_swarm with its name)
        joined = _externals(swarm) - before
        wanted = " ".join(str(args.get("name") or "").split()).lower()
        for member in joined or {n for n in _externals(swarm) if wanted and n.lower() == wanted}:
            clients.acted_as(client, member)
    tracer = getattr(swarm, "tracer", None)
    # not while the swarm stops: a call that returns then (e.g. a shell command killed by the session stop) must not
    # write into a database that Swarm.stop's caller closes right after
    if (tracer is not None and actor != USER and name not in UNTRACED_TOOLS and not getattr(swarm, "stopping", False)
            and swarm.is_member(actor)):
        try:
            tracer.external_call(actor, name, args, text, is_error, started)
        except Exception:  # tracing must never break a call
            log.exception("could not trace %s of %s", name, actor)
    return text, is_error


def build_hub_server(swarm: "Swarm") -> Server:
    local = f"local:{uuid.uuid4().hex}"  # the client of a connection without HTTP (stdio, in-process)

    async def on_list_tools(ctx, params) -> types.ListToolsResult:
        return types.ListToolsResult(tools=list_hub_tools(swarm))

    async def on_call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        text, is_error = await hub_call(swarm, params.name, dict(params.arguments or {}),
                                        client=client_key(ctx, local))
        return _text_result(text, is_error)

    return Server("dreamteam", version=__version__, title="DreamTeam swarm", instructions=INSTRUCTIONS,
                  on_list_tools=on_list_tools, on_call_tool=on_call_tool)


class _StdinLines:
    """Async iterator over the lines of the process stdin, read by a daemon thread.

    The SDK's default stdin reader runs in a worker thread that cancellation must wait for: on a stop
    signal while the client keeps stdin open, the server could neither finish (no swarm.stop()) nor
    exit. A daemon thread can be abandoned. It reads a private duplicate of fd 0 with os.read (no
    buffered-IO lock that interpreter shutdown would need), which is never closed because the thread
    may still block on it. (Agents' subprocesses never inherit stdin: they get DEVNULL or pipes.)"""

    def __init__(self, fd: int = 0):
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._loop = asyncio.get_running_loop()
        self._fd = os.dup(fd)
        threading.Thread(target=self._pump, name="mcp-stdin", daemon=True).start()

    def _put(self, item: str | None) -> None:
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, item)
        except RuntimeError:  # loop closed: nobody is listening any more
            pass

    def _pump(self) -> None:
        pending = b""
        try:
            while True:
                chunk = os.read(self._fd, 65536)
                if not chunk:
                    break
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    self._put(line.decode("utf-8", "replace") + "\n")
            if pending.strip():
                self._put(pending.decode("utf-8", "replace"))
        except OSError:
            pass
        finally:
            self._put(None)

    def __aiter__(self) -> "_StdinLines":
        return self

    async def __anext__(self) -> str:
        line = await self._queue.get()
        if line is None:
            raise StopAsyncIteration
        return line


async def serve_stdio(swarm: "Swarm") -> None:
    server = build_hub_server(swarm)
    async with stdio_server(stdin=_StdinLines()) as (read, write):
        await server.run(read, write, server.create_initialization_options())


# `wait_for_events` may block for up to 600 s without sending anything; the MCP HTTP client's default
# read timeout (300 s) would cut such a call off in the proxy.
PROXY_READ_TIMEOUT = 700.0


def proxy_http_client():
    """The HTTP client of a stdio proxy. Its CLIENT_HEADER names the proxy's one client to the hub, whatever
    protocol version the proxy speaks (the 2026-07-28 one has no session id)."""
    import httpx2

    return httpx2.AsyncClient(timeout=httpx2.Timeout(30.0, read=PROXY_READ_TIMEOUT),
                              headers={CLIENT_HEADER: uuid.uuid4().hex})


async def serve_stdio_proxy(url: str, instructions: str | None = None) -> None:
    """Expose a running hub (HTTP) over stdio, for MCP clients that only launch commands. The client gets the
    upstream server's instructions (a launcher's explain sessions), unless `instructions` is given."""
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    http = proxy_http_client()
    async with http, Client(streamable_http_client(url, http_client=http)) as client:
        async def on_list_tools(ctx, params) -> types.ListToolsResult:
            return await client.list_tools()

        async def on_call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
            return await client.call_tool(params.name, dict(params.arguments or {}))

        server = Server("dreamteam", version=__version__, title="DreamTeam (proxy)",
                        instructions=instructions or client.instructions or INSTRUCTIONS,
                        on_list_tools=on_list_tools, on_call_tool=on_call_tool)
        async with stdio_server(stdin=_StdinLines()) as (read, write):
            await server.run(read, write, server.create_initialization_options())


def security_settings(host: str, extra_hosts: Iterable[str] = ()) -> TransportSecuritySettings | None:
    """SDK-level DNS-rebinding check for loopback binds (web.GuardMiddleware checks every bind in front of it)."""
    if host not in LOOPBACK_HOSTS:
        return None
    names = ["127.0.0.1", "localhost", "[::1]", *sorted(f"[{h}]" if ":" in h else h for h in extra_hosts)]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[*names, *(f"{n}:*" for n in names)],
        allowed_origins=[f"{scheme}://{n}{port}" for n in names for scheme in ("http", "https") for port in ("", ":*")],
    )


class MCPHttpEndpoint:
    """ASGI app serving the hub over streamable HTTP; mount at /mcp and run `manager.run()` in the lifespan."""

    def __init__(self, swarm: "Swarm", host: str, extra_hosts: Iterable[str] = ()):
        self.server = build_hub_server(swarm)
        self.manager = StreamableHTTPSessionManager(app=self.server,
                                                    security_settings=security_settings(host, extra_hosts))

    async def __call__(self, scope, receive, send) -> None:
        await self.manager.handle_request(scope, receive, send)
