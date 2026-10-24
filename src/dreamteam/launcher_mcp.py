"""The launcher as one MCP server (docs/SPEC.md §18.2): list, create, start and stop
sessions, and use every hub tool inside a session by passing `session` (defaults to the running one)."""

from __future__ import annotations

import asyncio
import copy
import difflib
import logging
import uuid
from typing import TYPE_CHECKING, Any

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from . import __version__, roles
from . import skills as skill_lib
from .hub_mcp import INSTRUCTIONS as HUB_INSTRUCTIONS
from .hub_mcp import (LIBRARY_TOOLS, _StdinLines, client_key, hub_call, hub_specs, list_hub_tools, security_settings,
                      serving_client)
from .models import USER, ToolError
from .sessions import SessionError
from .skills import Library, SkillError
from .swarm_skills import files_text, source_spec
from .tools import REGISTRY
from .workspace import format_size

if TYPE_CHECKING:
    from .sessions import SessionManager
    from .swarm import Swarm

log = logging.getLogger(__name__)

INSTRUCTIONS = """You are connected to DreamTeam: a launcher of self-organizing AI agent swarms. Each SESSION is an
independent swarm with its own goal, task board, agents, messages and workspace.

Session tools: `sessions_list` shows the sessions; `session_create(title, goal, ...)` starts a new swarm
on a goal; `session_start` / `session_stop` resume or stop one.
A session works in its own folder by default. `session_cd(session, path)` moves it to another absolute folder:
an empty or missing folder gets the session's files (and git history); a folder with files, e.g. an existing
project, is adopted: the agents work with its files and commit to its current branch (its git repository must
have no uncommitted changes, also at every later start of the session; a folder that is not a repository gets
one). `path="default"` moves it back.
`session_create(..., workspace=<folder>)` creates a session that works in that folder right away.
`skill_library` lists, shows, imports (a folder, a .zip or a Git URL) and deletes the shared skills library;
`session_create(..., skills=[...])` and `session_start(session, skills=[...])` attach skills to a session by name or
source.
Every other tool works inside one session: pass `session="<id>"` (from sessions_list). Without it the call
goes to the running session (the most recently active one when several run).

""" + HUB_INSTRUCTIONS

SESSION_PROP = {"type": "string", "description": "Session id from sessions_list (default: the running session)."}
SKILLS_PROP = {"type": "array", "items": {"type": "string"},
               "description": "Skills to attach: names from the library or ~/.claude/skills (claude-user:NAME / "
                              "claude-project:NAME picks the source), or sources (folder/zip path, Git URL)"}

LAUNCHER_TOOLS: list[dict[str, Any]] = [
    {"name": "sessions_list", "description": "List DreamTeam sessions (swarms): id, title, status, goal, "
                                              "progress, agents and spending.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "session_create",
     "description": "Create and start a new session: a fresh swarm that plans and works on the goal. "
                    "Unset numbers come from the global settings.",
     "inputSchema": {"type": "object", "properties": {
         "title": {"type": "string", "description": "Short session name"},
         "goal": {"type": "string", "description": "The global goal of the swarm: one line, or several (the first "
                                                   "line becomes its title, the others its description)"},
         "description": {"type": "string", "description": "Details, constraints, definition of done"},
         "language": {"type": "string", "enum": ["ru", "en"], "description": "Language of the agents' prompts"},
         "start_agents": {"type": "integer"}, "max_agents": {"type": "integer"},
         "team": {"type": "array", "items": {"type": "string"},
                  "description": "Starting team: role names in order, a name repeated for several agents, e.g. "
                                 "[\"architect\", \"developer\", \"developer\", \"tester\"]. Built-in roles: generalist, "
                                 "developer, tester, reviewer, analyst, architect, writer, researcher (or their names "
                                 "in either language); other names are new roles without a description. Sets "
                                 "start_agents unless it is given."},
         "max_cost_usd": {"type": "number", "description": "Budget in USD"},
         "model": {"type": "string", "description": "Model profile name from the settings"},
         "network": {"type": "string", "enum": ["on", "off"],
                     "description": "off: the agents' commands, background jobs and MCP servers get no internet "
                                    "(the LLM keeps it); strict in the Docker sandbox and on Linux, best effort "
                                    "(proxy and offline variables) on Windows and macOS"},
         "start": {"type": "boolean", "description": "Start right away (default true)"},
         "workspace": {"type": "string", "description": "Absolute folder to work in instead of the session's own "
                                                        "one: empty/missing = new, with files = adopted (an existing "
                                                        "project; its git repo must be clean)"},
         "skills": SKILLS_PROP,
     }, "required": ["goal"]}},
    {"name": "session_start", "description": "Start (resume) a stopped session: its agents continue working. "
                                             "skills are attached first (live when it is running).",
     "inputSchema": {"type": "object", "properties": {"session": SESSION_PROP, "skills": SKILLS_PROP},
                     "required": ["session"]}},
    {"name": "session_stop", "description": "Stop a running session: its agents stop; the board and files stay.",
     "inputSchema": {"type": "object", "properties": {"session": SESSION_PROP}, "required": ["session"]}},
    {"name": "session_cd",
     "description": "Move a session to another folder. Empty or missing folder: the session's files, git history "
                    "and MCP servers move there. Folder with files (e.g. an existing project): adopted, the agents "
                    "work with its files and commit to its current branch; refused while its git repository has "
                    "uncommitted changes or untracked files (commit or stash first), a detached HEAD or a merge or "
                    "rebase in progress (checked again at every start). A running session is stopped and started "
                    "again.",
     "inputSchema": {"type": "object", "properties": {
         "session": SESSION_PROP,
         "path": {"type": "string", "description": "Absolute folder path, or \"default\" for the session's own "
                                                   "folder"},
     }, "required": ["session", "path"]}},
    {"name": "skill_library",
     "description": "The shared skills library (~/.dreamteam/skills) and Claude Code's skills: list them, show one, "
                    "import a folder, .zip or Git URL into the library, or delete one. Attaching to a session: the "
                    "session tools skill_attach / skill_detach / skill_approve.",
     "inputSchema": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["list", "show", "import", "delete"]},
         "name": {"type": "string", "description": "show / delete: a skill name (show: also claude-user:NAME or "
                                                   "host-library:NAME); import: claude-user:NAME copies that Claude "
                                                   "Code skill into the library"},
         "source": {"type": "string", "description": "import: a folder or .zip path on the hub's machine, or a Git "
                                                     "URL (a subfolder: URL#ref:path)"},
         "pick": {"type": "array", "items": {"type": "string"},
                  "description": "import: the skills to take from a source that holds several"},
         "replace": {"type": "boolean", "description": "import: replace a library skill of the same name that has "
                                                      "other content"}},
         "required": ["action"]}},
]


def workspace_text(result: dict[str, Any]) -> str:
    """A set_workspace (or create) result in words."""
    mode = result.get("mode")
    if mode == "unchanged":
        return f"The session already works in {result.get('to')}."
    if mode == "moved" and (result.get("old_kept") or result.get("old_missing")):
        head = "The session now works in"  # nothing of the session's moved: the user's folder stays, or none was there
    else:
        head = {"moved": "Moved the session's files to", "created": "The session works in the new folder",
                "adopted": "The session now works with the files in"}.get(str(mode), "The session now works in")
    lines = [f"{head} {result.get('to')}."]
    git = {"moved": "the git history moved along", "existing": "its git repository is used: the agents commit to "
                                                                 "its current branch",
           "initialized": "a git repository was initialized there (with an initial commit)",
           "disabled": "git is off in the settings", "none": "git is not installed: no versioning"}.get(
        str(result.get("git")))
    if git:
        lines.append(f"Git: {git}.")
    if result.get("mcp_moved"):
        lines.append(f"MCP servers moved along: {', '.join(result['mcp_moved'])}.")
    changed = result.get("old_changed") if isinstance(result.get("old_changed"), dict) else None
    if changed and changed.get("paths"):  # the session's MCP servers left the user's repository
        paths = ", ".join(map(str, changed["paths"]))
        where = result.get("old_kept") or result.get("from")
        if changed.get("commit"):
            lines.append(f"The previous folder {where} keeps its files; the session's MCP servers ({paths}) left it, "
                         f"and their removal is committed there ({str(changed['commit'])[:8]}).")
        else:
            lines.append(f"The previous folder {where} keeps its files, but the session's MCP servers ({paths}) "
                         f"left it and their removal is not committed there ({changed.get('error') or '?'}): commit "
                         f"it, or restore them with git checkout -- {paths}.")
    elif result.get("old_kept"):
        lines.append(f"The previous folder stays as it was: {result['old_kept']}.")
    if result.get("old_missing"):
        lines.append(f"The previous folder {result.get('from')} did not exist: nothing was moved from it.")
    if result.get("start_error"):
        lines.append(f"The session could not start again: {result['start_error']}")
    return "\n".join(lines)


def session_line(s: dict[str, Any]) -> str:
    status = s.get("status", "?")
    if status == "running" and s.get("paused"):
        status = f"paused ({s.get('pause_reason') or 'user'})"
    if status == "running" and s.get("wrap_up"):
        status = "running, wrapping up"
    goal = s.get("goal") or {}
    tasks = s.get("tasks") or {}
    agents = s.get("agents") or {}
    jobs = int((s.get("jobs") or {}).get("running") or 0)  # the agents' background jobs (read_stats)
    line = (f"{s['id']} [{status}] {s.get('title', '')}"
            f" | tasks {tasks.get('done', 0)}/{tasks.get('total', 0)} done"
            f" | agents {agents.get('total', 0)} ({agents.get('working', 0)} working)"
            + (f" | jobs {jobs} running" if jobs and s.get("status") == "running" else "")
            + f" | ${float(s.get('spent_usd') or 0):.2f} of ${float(s.get('max_cost_usd') or 0):.2f}"
            + (" | no network" if s.get("network") == "off" else ""))
    if goal:
        line += f"\n    goal [{goal.get('status', 'active')}]: {goal.get('title', '')}"
    if s.get("skills"):
        pending = s.get("skills_pending") or []
        line += f"\n    skills: {', '.join(s['skills'])}" + (
            f" (waiting for your library decision: {', '.join(pending)})" if pending else "")
    workspace = s.get("workspace") or {}
    if workspace and not workspace.get("default"):  # its own folder is created on demand: never "missing"
        notes = [n for n, on in (("adopted", workspace.get("adopted")), ("MISSING", not workspace.get("exists", True)))
                 if on]
        line += f"\n    folder: {workspace.get('path')}" + (f" ({', '.join(notes)})" if notes else "")
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


def _skills_arg(value: Any) -> list[str] | None:
    """`skills` of session_create / session_start: a list of references or sources (None when absent or empty)."""
    if value in (None, []):
        return None
    if not isinstance(value, list) or not all(isinstance(x, str) and x.strip() for x in value):
        raise ToolError("skills must be a list of skill names or sources, e.g. [\"pdf\", \"claude-user:review\"].")
    return [x.strip() for x in value]


def _entry_line(entry: dict[str, Any], origin: str | None = None) -> str:
    origin = origin or entry.get("origin") or "?"
    if entry.get("error"):
        return f"- {entry['name']} [{origin}]: not usable: {entry['error']}"
    notes = [files_text(int(entry.get("files") or 0)), format_size(int(entry.get("size") or 0))]
    if entry.get("changed_outside"):
        notes.append("changed outside DreamTeam")
    if entry.get("agent_writable"):
        notes.append("from the workspace: agents can write here")
    if entry.get("manual"):
        notes.append("only when the user asks for it")
    if entry.get("library") == "different":
        notes.append("the library has another skill of this name")
    return f"- {entry['name']} [{origin}, {', '.join(notes)}]: {entry.get('description') or ''}"


def _deps_line(deps: dict[str, Any], enabled: bool = True) -> str:
    """A skill's dependency preview (skills.deps_preview) in words ("" without manifests); `enabled`: [skills]
    install."""
    if deps and not enabled:
        return "Its packages are not installed automatically: [skills] install is off."
    parts = []
    for ecosystem, found in deps.items():
        files = ", ".join(found.get("files") or [])
        if found.get("unsupported"):
            parts.append(f"{files} is not installed automatically: {found['unsupported']}")
        else:
            packages = ", ".join((found.get("packages") or [])[:10]) or "no packages"
            parts.append(f"{files} ({ecosystem}): {packages}")
    return "Packages installed when it is attached: " + "; ".join(parts) + "." if parts else ""


async def _skill_library(manager: "SessionManager", args: dict[str, Any]) -> str:
    """The launcher MCP's `skill_library` tool (the home page's Skills tab in words)."""
    action = str(args.get("action") or "")
    config = manager.global_config()
    lib = Library.from_config(config)
    if action == "list":
        def gather() -> list[str]:
            entries = lib.list()
            claude = skill_lib.claude_skills(config)
            host = skill_lib.host_skills(config)
            lines = [f"Skills library ({lib.root}):"] + ([_entry_line(e) for e in entries] or ["  (empty)"])
            if host:
                lines += ["The host's library (read-only, attach as host-library:NAME):",
                          *[_entry_line(e, "host-library") for e in host]]
            if claude:
                lines += ["Claude Code's skills (attach as claude-user:NAME):", *[_entry_line(e) for e in claude]]
            elif not config.skills.claude:
                lines.append("Claude Code's skills are not offered ([skills] claude = false).")
            return lines
        return "\n".join(await asyncio.to_thread(gather))
    if action == "show":
        source, name = skill_lib.parse_ref(str(args.get("name") or ""))

        def show() -> dict[str, Any]:
            if source in (None, "library"):
                return lib.show(name)
            if source == "host-library":
                host = Library.host(config)
                if host is None:
                    raise SkillError("The host's skills library exists only inside the Docker sandbox.", 404)
                return host.show(name)
            found = skill_lib.resolve_ref(config, f"{source}:{name}")
            entry = next((i for i in skill_lib.claude_skills(config) if i["name"] == name
                          and i["origin"] == source), {"name": name, "origin": source})
            return {**entry, **skill_lib.show_folder(found["path"], lib.limits)}

        entry = await asyncio.to_thread(show)
        files = ", ".join(f"{f['path']} ({format_size(f['size'])})" for f in entry.get("files_list") or [])
        deps = _deps_line(entry.get("deps") or {}, config.skills.install)
        source_text = ", ".join(f"{k}: {v}" for k, v in (entry.get("source") or {}).items() if v)
        return "\n".join(line for line in (
            _entry_line(entry), f"Source: {source_text}" if source_text else "", f"Files: {files}" if files else "",
            deps, "SKILL.md:", entry.get("skill_md") or "") if line)
    if action == "import":
        name, source = str(args.get("name") or "").strip(), str(args.get("source") or "").strip()
        if bool(name) == bool(source):
            raise ToolError("import takes either source (a folder or .zip path, or a Git URL) or name "
                            "(claude-user:NAME, a Claude Code skill).")
        if name:
            origin, bare = skill_lib.parse_ref(name)
            if origin not in ("claude-user", "host-library"):
                raise ToolError("name imports a Claude Code skill (claude-user:NAME) or, in the sandbox, one of the "
                                "host's library (host-library:NAME).")
            body: dict[str, Any] = {"claude": bare, "origin": origin} if origin == "claude-user" else {"host": bare}
        else:
            body = source_spec(source)
        if args.get("pick") is not None:
            body["pick"] = args["pick"]
        body["replace"] = bool(args.get("replace"))
        report = await asyncio.to_thread(skill_lib.import_request, config, body, None, by=USER, via="launcher-mcp")
        if report["need_pick"]:
            listed = "; ".join(c["name"] + (f" [{c['error']}]" if c.get("error") else "")
                               for c in report["candidates"])
            return f"{source or name} holds several skills: {listed}. Call import again with pick, the names to take."
        manager._changed(None, "skills")
        lines = [f"Skill \"{e['name']}\" " + ("is in the library already (unchanged)." if e.get("unchanged") else
                                                f"imported into the library ({files_text(int(e.get('files') or 0))}).")
                 for e in report["imported"]]
        if report["skipped"]:
            lines.append("Left out: " + "; ".join(f"{s['path']} ({s['error']})" for s in report["skipped"][:20]) + ".")
        return "\n".join(lines) + "\nAttach it to a session with skill_attach (pass session)."
    if action == "delete":
        name = skill_lib.check_name(str(args.get("name") or "").strip())
        await asyncio.to_thread(lib.remove, name)
        manager._changed(None, "skills")
        return (f"Skill \"{name}\" deleted from the skills library. Sessions that attached it keep their own "
                "copies.")
    raise ToolError("action must be list, show, import or delete.")


def _int_or_none(value: Any) -> int | None:
    try:
        return None if value in (None, "") else int(value)
    except (TypeError, ValueError):
        raise ToolError(f"Expected an integer, got {value!r}.") from None


def _team(value: Any) -> list[str]:
    """`session_create`'s `team`: role names in order (normalized; [] when absent or empty)."""
    if value is None:
        return []
    names = [roles.normalize(x) for x in value] if isinstance(value, list) and all(
        isinstance(x, str) for x in value) else None
    if names is None or not all(names):
        raise ToolError("team must be a list of role names, e.g. [\"architect\", \"developer\", \"tester\"].")
    if len(names) > roles.TEAM_MAX:
        raise ToolError(f"A team has at most {roles.TEAM_MAX} agents.")
    long = next((name for name in names if len(name) > roles.NAME_LIMIT), None)
    if long is not None:
        raise ToolError(f"'{long[:80]}': a role name cannot be longer than {roles.NAME_LIMIT} characters.")
    # "developer*2" / "architect, developer" is the --team syntax: here each item is one agent's role
    joined = next((name for name in names if "," in name or "*" in name), None)
    if joined is not None:
        raise ToolError(f"'{joined}': a role name cannot contain ',' or '*'; give one item per agent and repeat a "
                        "name for several agents, e.g. [\"developer\", \"developer\"].")
    return names


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
            team = _team(args.get("team"))
            if team:  # the starting roles in order; the team's size unless start_agents is given
                swarm_over["starting_roles"] = team
                swarm_over.setdefault("start_agents", len(team))
            if args.get("max_cost_usd") not in (None, ""):
                swarm_over["max_cost_usd"] = float(args["max_cost_usd"])
            if args.get("language"):
                swarm_over["language"] = str(args["language"])
            if args.get("network"):
                swarm_over["network"] = str(args["network"])
            overrides: dict[str, Any] = {"swarm": swarm_over} if swarm_over else {}
            if args.get("model"):
                overrides["agents"] = {"default_model": str(args["model"])}
            workspace = args.get("workspace")
            if workspace is not None and not isinstance(workspace, str):
                raise ToolError("workspace must be an absolute folder path.")
            summary = await manager.create(str(args.get("title") or ""), goal, str(args.get("description") or ""),
                                           overrides=overrides, start=args.get("start", True) is not False,
                                           workspace=workspace.strip() if workspace and workspace.strip() else None,
                                           skills=_skills_arg(args.get("skills")), via="launcher-mcp")
            result = summary.pop("workspace_result", None)
            board = f" Board: {manager.hub_url}/s/{summary['id']}/" if manager.hub_url else ""  # no web UI: no URL
            return (f"Created session {summary['id']} ({summary['status']}).{board}\n"
                    + (workspace_text(result) + "\n" if result else "")
                    + f"Pass session=\"{summary['id']}\" to work inside it.\n" + session_line(summary)), False
        if name == "session_cd":
            sid = str(args.get("session") or "")
            path = args.get("path")
            if not isinstance(path, str) or not path.strip():
                raise ToolError("path is required: an absolute folder, or \"default\" for the session's own folder.")
            path = path.strip()
            result = await manager.set_workspace(sid, None if path.lower() == "default" else path)
            return workspace_text(result) + "\n" + session_line(manager.summary(sid)), False
        if name in ("session_start", "session_stop"):
            sid = str(args.get("session") or "")
            if name == "session_start":
                await manager.start(sid, skills=_skills_arg(args.get("skills")), via="launcher-mcp")
            else:
                await manager.stop(sid)
            return session_line(manager.summary(sid)), False
        if name == "skill_library":
            return await _skill_library(manager, args), False
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
    call = asyncio.ensure_future(hub_call(manager.swarms[sid], name, args, via="launcher-mcp"))
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
    text, is_error = call.result()
    if not is_error and name in LIBRARY_TOOLS:   # an open Skills tab of the home page reloads the library
        manager._changed(None, "skills")
    return text, is_error


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
    local = f"local:{uuid.uuid4().hex}"  # the client of a connection without HTTP (stdio, in-process)

    async def on_list_tools(ctx, params) -> types.ListToolsResult:
        return types.ListToolsResult(tools=list_launcher_tools(manager))

    async def on_call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        # the session's hub_call knows which client calls (hub_mcp.client_key): what the client sends never comes
        # back to the external member it acts as
        with serving_client(client_key(ctx, local)):
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
