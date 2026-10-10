"""The launcher as one MCP server (docs/SPEC.md §18.2): list, create, start and stop
sessions, and use every hub tool inside a session by passing `session` (defaults to the running one)."""

from __future__ import annotations

import asyncio
import copy
import difflib
import logging
from typing import TYPE_CHECKING, Any

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from . import __version__
from .hub_mcp import INSTRUCTIONS as HUB_INSTRUCTIONS
from .hub_mcp import _StdinLines, hub_call, hub_specs, list_hub_tools, security_settings
from .models import ToolError
from .sessions import SessionError
from .tools import REGISTRY

if TYPE_CHECKING:
    from .sessions import SessionManager
    from .swarm import Swarm

log = logging.getLogger(__name__)

INSTRUCTIONS = """You are connected to DreamTeam: a launcher of self-organizing AI agent swarms. Each SESSION is an
independent swarm with its own goal, task board, agents, messages and workspace.

Session tools: `sessions_list` shows the sessions; `session_create(title, goal, ...)` starts a new swarm
on a goal; `session_start` / `session_stop` resume or stop one.
Every other tool works inside one session: pass `session="<id>"` (from sessions_list). Without it the call
goes to the running session (the most recently active one when several run).

""" + HUB_INSTRUCTIONS

SESSION_PROP = {"type": "string", "description": "Session id from sessions_list (default: the running session)."}

LAUNCHER_TOOLS: list[dict[str, Any]] = [
    {"name": "sessions_list", "description": "List DreamTeam sessions (swarms): id, title, status, goal, "
                                              "progress, agents and spending.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "session_create",
     "description": "Create and start a new session: a fresh swarm that plans and works on the goal. "
                    "Unset numbers come from the global settings.",
     "inputSchema": {"type": "object", "properties": {
         "title": {"type": "string", "description": "Short session name"},
         "goal": {"type": "string", "description": "The global goal of the swarm"},
         "description": {"type": "string", "description": "Details, constraints, definition of done"},
         "language": {"type": "string", "enum": ["ru", "en"], "description": "Language of the agents' prompts"},
         "start_agents": {"type": "integer"}, "max_agents": {"type": "integer"},
         "max_cost_usd": {"type": "number", "description": "Budget in USD"},
         "model": {"type": "string", "description": "Model profile name from the settings"},
         "start": {"type": "boolean", "description": "Start right away (default true)"},
     }, "required": ["goal"]}},
    {"name": "session_start", "description": "Start (resume) a stopped session: its agents continue working.",
     "inputSchema": {"type": "object", "properties": {"session": SESSION_PROP}, "required": ["session"]}},
    {"name": "session_stop", "description": "Stop a running session: its agents stop; the board and files stay.",
     "inputSchema": {"type": "object", "properties": {"session": SESSION_PROP}, "required": ["session"]}},
]


def session_line(s: dict[str, Any]) -> str:
    status = s.get("status", "?")
    if status == "running" and s.get("paused"):
        status = f"paused ({s.get('pause_reason') or 'user'})"
    goal = s.get("goal") or {}
    tasks = s.get("tasks") or {}
    agents = s.get("agents") or {}
    line = (f"{s['id']} [{status}] {s.get('title', '')}"
            f" | tasks {tasks.get('done', 0)}/{tasks.get('total', 0)} done"
            f" | agents {agents.get('total', 0)} ({agents.get('working', 0)} working)"
            f" | ${float(s.get('spent_usd') or 0):.2f} of ${float(s.get('max_cost_usd') or 0):.2f}")
    if goal:
        line += f"\n    goal [{goal.get('status', 'active')}]: {goal.get('title', '')}"
    if s.get("error") or s.get("config_error"):
        line += f"\n    error: {s.get('error') or s.get('config_error')}"
    return line


def list_launcher_tools(manager: "SessionManager") -> list[types.Tool]:
    tools = [types.Tool(name=t["name"], description=t["description"], input_schema=copy.deepcopy(t["inputSchema"]))
             for t in LAUNCHER_TOOLS]
    language = manager.config.swarm.language
    for tool in list_hub_tools(_LanguageOnly(language)):
        schema = copy.deepcopy(tool.input_schema)
        schema["properties"] = {**schema.get("properties", {}), "session": SESSION_PROP}
        tools.append(types.Tool(name=tool.name, description=tool.description, input_schema=schema))
    return tools


class _LanguageOnly:
    """list_hub_tools only needs `.language`."""

    def __init__(self, language: str):
        self.language = language


def _int_or_none(value: Any) -> int | None:
    try:
        return None if value in (None, "") else int(value)
    except (TypeError, ValueError):
        raise ToolError(f"Expected an integer, got {value!r}.") from None


async def call_launcher_tool(manager: "SessionManager", name: str, args: dict[str, Any]) -> tuple[str, bool]:
    args = dict(args or {})
    try:
        if name == "sessions_list":
            items = manager.list()
            if not items:
                return "No sessions yet. Create one with session_create(title, goal).", False
            return "Sessions:\n" + "\n".join(session_line(s) for s in items), False
        if name == "session_create":
            goal = str(args.get("goal") or "").strip()
            if not goal:
                raise ToolError("goal is required.")
            swarm_over: dict[str, Any] = {}
            for key in ("start_agents", "max_agents"):
                if args.get(key) not in (None, ""):
                    swarm_over[key] = _int_or_none(args[key])
            if args.get("max_cost_usd") not in (None, ""):
                swarm_over["max_cost_usd"] = float(args["max_cost_usd"])
            if args.get("language"):
                swarm_over["language"] = str(args["language"])
            overrides: dict[str, Any] = {"swarm": swarm_over} if swarm_over else {}
            if args.get("model"):
                overrides["agents"] = {"default_model": str(args["model"])}
            summary = await manager.create(str(args.get("title") or ""), goal, str(args.get("description") or ""),
                                           overrides=overrides, start=args.get("start", True) is not False)
            board = f" Board: {manager.hub_url}/s/{summary['id']}/" if manager.hub_url else ""  # no web UI: no URL
            return (f"Created session {summary['id']} ({summary['status']}).{board}\n"
                    f"Pass session=\"{summary['id']}\" to work inside it.\n" + session_line(summary)), False
        if name in ("session_start", "session_stop"):
            sid = str(args.get("session") or "")
            if name == "session_start":
                await manager.start(sid)
            else:
                await manager.stop(sid)
            return session_line(manager.summary(sid)), False
    except (SessionError, ToolError, ValueError) as exc:
        return f"Error: {exc}", True
    except Exception as exc:  # e.g. the swarm failed to start: tell the client instead of a bare internal error
        log.exception("%s failed", name)
        return f"Error: {type(exc).__name__}: {exc}", True
    # a hub tool inside a session
    sid = args.pop("session", None) or manager.default_session()
    if not sid:
        listing = "\n".join(session_line(s) for s in manager.list()) or "(none)"
        return ("Error: pass session=\"<id>\" (no single running session to default to). Sessions:\n"
                + listing), True
    if not manager.exists(str(sid)):
        return f"Error: unknown session '{sid}'. Use sessions_list.", True
    if str(sid) not in manager.running:
        return f"Error: session {sid} is stopped. Start it with session_start(session=\"{sid}\").", True
    return await _call_in_session(manager, str(sid), name, args)


async def _call_in_session(manager: "SessionManager", sid: str, name: str, args: dict[str, Any]) -> tuple[str, bool]:
    """hub_call in a running session, cut short when the session stops meanwhile: a `wait_for_events` long poll
    would otherwise wait out its whole timeout on a swarm whose database is already closed."""
    gone = asyncio.Event()

    def on_change(event: dict[str, Any]) -> None:
        if event.get("session") == sid and event.get("change") in ("stopped", "deleted"):
            gone.set()

    manager.listen(on_change)
    call = asyncio.ensure_future(hub_call(manager.swarms[sid], name, args))
    waiter = asyncio.ensure_future(gone.wait())
    try:
        await asyncio.wait({call, waiter}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        manager.unlisten(on_change)
        waiter.cancel()
        if not call.done():
            call.cancel()
            await asyncio.gather(call, return_exceptions=True)
    if call.cancelled():
        return f"Error: session {sid} was stopped while {name} was running.", True
    return call.result()


async def dispatch_launcher_call(manager: "SessionManager", name: str, args: dict[str, Any]) -> tuple[str, bool]:
    """An MCP tool call on the launcher. A registry tool outside the MCP audience (wait, update_notes) goes on
    to call_tool, which explains who may use it; only a name nobody knows is answered here."""
    launcher_names = {t["name"] for t in LAUNCHER_TOOLS}
    if name not in launcher_names and name not in {s.name for s in hub_specs()} and name not in REGISTRY:
        visible = sorted(launcher_names | {s.name for s in hub_specs()})
        close = difflib.get_close_matches(str(name), visible, n=3, cutoff=0.5)
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        return f"Unknown tool '{name}'.{hint} Available tools: {', '.join(visible)}.", True
    return await call_launcher_tool(manager, name, args)


def build_launcher_server(manager: "SessionManager") -> Server:
    async def on_list_tools(ctx, params) -> types.ListToolsResult:
        return types.ListToolsResult(tools=list_launcher_tools(manager))

    async def on_call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        text, is_error = await dispatch_launcher_call(manager, params.name, dict(params.arguments or {}))
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=is_error)

    return Server("dreamteam", version=__version__, title="DreamTeam", instructions=INSTRUCTIONS,
                  on_list_tools=on_list_tools, on_call_tool=on_call_tool)


async def serve_stdio_launcher(manager: "SessionManager") -> None:
    server = build_launcher_server(manager)
    # _StdinLines (a daemon reader thread) as in hub_mcp.serve_stdio: with the SDK's default stdin reader a stop
    # signal could not end the server while the client keeps stdin open (no sessions stopped, no exit)
    async with stdio_server(stdin=_StdinLines()) as (read, write):
        await server.run(read, write, server.create_initialization_options())


class LauncherMCPEndpoint:
    """ASGI app serving the launcher MCP over streamable HTTP; run `manager.run()` in the lifespan."""

    def __init__(self, manager: "SessionManager", host: str, extra_hosts=()):
        self.server = build_launcher_server(manager)
        self.manager = StreamableHTTPSessionManager(app=self.server,
                                                    security_settings=security_settings(host, extra_hosts))

    async def __call__(self, scope, receive, send) -> None:
        await self.manager.handle_request(scope, receive, send)
