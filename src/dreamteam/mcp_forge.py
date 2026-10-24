"""MCP forge: agent-built stdio MCP servers, a shared connection pool and a cross-run library.

Agents write `workspace/mcp/<name>/server.py` (mcp 2.x `MCPServer`), the forge smoke-tests it, keeps one
live connection per server shared by every agent and copies swarm-scoped servers to the library
(`config.library_dir`) so later runs and other swarms can import them. External servers from
`[mcp.external]` in `dreamteam.toml` go through the same pool.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import anyio
from mcp import Client, MCPError, StdioServerParameters, stdio_client
from mcp import types as mcp_types

from . import netoff
from .db import dumps, loads
from .models import USER, ToolError
from .shell import scrubbed_env, truncate_middle

log = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
# names of [mcp.external.<name>] entries are chosen by the user, so they may contain '-' or '.'
EXTERNAL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.\-]{0,63}$")
SCOPES = ("private", "task", "swarm")
MCP_REQUIREMENT = "mcp>=2.3,<3"

ACTIVE, PENDING, FAILED, REJECTED = "active", "pending", "failed", "rejected"
AVAILABLE = "available"  # library entry not imported into this project yet
REMOVED = "removed"
ORIGIN_WORKSPACE, ORIGIN_CONFIG, ORIGIN_LIBRARY = "workspace", "config", "library"

# a string of requirements: split on newlines and on commas that start a new package name (not the commas of
# a version range like "httpx>=0.27,<1" or of extras like "pkg[a,b]")
_REQ_SPLIT_RE = re.compile(r"[\r\n]+|,(?=\s*[A-Za-z0-9])(?![^\[\]]*\])")

MAX_CODE_CHARS = 200_000
MAX_DESCRIPTION_CHARS = 1000
MAX_REQUIREMENTS = 30
MAX_ERROR_CHARS = 4000
LOG_MARKER = "===== dreamteam: server start"
LOG_MAX_BYTES = 2_000_000  # the log restarts from scratch when a server starts with a bigger file
CRASH_PREFIX = "Error executing tool"
# MCPServer logs through rich when rich is installed (DreamTeam depends on it): with rich's default 80 columns
# a traceback is wrapped inside narrow boxes, and the log tails shown to agents lose the error line.
LOG_COLUMNS = "200"
_CAUSE_RE = re.compile(r"The\s+above\s+exception\s+was\s+the\s+direct\s+cause")  # also when wrapped

SERVER_TEMPLATE = '''from mcp.server.mcpserver import MCPServer

mcp = MCPServer("name")


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


if __name__ == "__main__":
    mcp.run()
'''


class ConnectionFailed(ToolError):
    """The server process could not be started or did not complete the MCP handshake."""

    def __init__(self, name: str, reason: str, log_tail: str = ""):
        self.reason = reason
        self.log_tail = log_tail
        text = f"MCP server '{name}' failed to start: {reason}"
        if log_tail:
            text += f"\n--- server log (tail) ---\n{log_tail}"
        super().__init__(text)


class ConnectionLost(ToolError):
    """The server process exited while the connection was in use."""


# --- helpers -----------------------------------------------------------------------------------

def _norm(name: Any) -> str:
    return str(name or "").strip().lower()


def _leaf_errors(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        leaves: list[BaseException] = []
        for inner in exc.exceptions:
            leaves.extend(_leaf_errors(inner))
        return leaves
    return [exc]


def describe_error(exc: BaseException) -> str:
    """Short human text for a startup failure (unwraps anyio exception groups)."""
    leaves = _leaf_errors(exc) or [exc]
    for err in leaves:
        if isinstance(err, MCPError) and err.code == mcp_types.CONNECTION_CLOSED:
            return "the server process exited during startup"
    err = leaves[0]
    if isinstance(err, FileNotFoundError):
        return f"command or directory not found ({err})"
    if isinstance(err, OSError):
        return f"cannot start the process ({err})"
    if isinstance(err, MCPError):
        return f"MCP error {err.code}: {err.message}"
    return f"{type(err).__name__}: {err}"


def _log_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _read_log(path: Path, start: int | None = None, max_bytes: int = 65536) -> str:
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            begin = max(0, size - max_bytes) if start is None else max(start, size - max_bytes)
            f.seek(begin)
            data = f.read()
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace").replace("\r\n", "\n")


_BOX_LINE = re.compile(r"^(\s*[│|].*?)\s*[│|]$")  # a line inside a rich box: padding and right border dropped
_RULE = re.compile(r"[─-]{4,}")


def _tidy(line: str) -> str:
    """A log line without the padding of rich's boxed tracebacks (MCPServer logs through rich)."""
    line = _BOX_LINE.sub(r"\1", line.rstrip())
    if line.lstrip()[:1] in ("╭", "╰", "+"):  # the top or bottom of a box
        line = _RULE.sub("──", line)
    return line.rstrip()


def _tail(text: str, lines: int, chars: int) -> str:
    out = textwrap.dedent("\n".join(_tidy(line) for line in text.strip("\n").splitlines()[-lines:]))
    return out.strip("\n")[-chars:]


def read_log_tail(path: Path, lines: int = 40, chars: int = 4000) -> str:
    """Tail of the server log since the last start marker."""
    text = _read_log(path)
    idx = text.rfind(LOG_MARKER)
    if idx >= 0:
        rest = text[idx:]
        text = rest.split("\n", 1)[1] if "\n" in rest else ""
    return _tail(text, lines, chars)


def _crash_details(path: Path, since: int) -> str:
    """The original exception a server logged while handling a call (the client only sees a stub)."""
    text = _read_log(path, start=since)
    cause = _CAUSE_RE.search(text)
    if cause:
        text = text[:cause.start()]
    return _tail(text, 15, 3000)


def render_result(result: mcp_types.CallToolResult) -> str:
    """Text of a tool result: text blocks verbatim, placeholders for media, structured JSON if no text."""
    parts: list[str] = []
    has_text = False
    for block in result.content:
        if isinstance(block, mcp_types.TextContent):
            has_text = True
            parts.append(block.text)
        elif isinstance(block, mcp_types.ImageContent):
            parts.append(f"[image: {block.mime_type}, ~{len(block.data) * 3 // 4} bytes]")
        elif isinstance(block, mcp_types.AudioContent):
            parts.append(f"[audio: {block.mime_type}, ~{len(block.data) * 3 // 4} bytes]")
        elif isinstance(block, mcp_types.EmbeddedResource):
            res = block.resource
            if isinstance(res, mcp_types.TextResourceContents):
                has_text = True
                parts.append(f"[resource {res.uri}]\n{res.text}")
            else:
                parts.append(f"[binary resource {res.uri}: {res.mime_type or 'unknown type'}]")
        elif isinstance(block, mcp_types.ResourceLink):
            desc = f" - {block.description}" if block.description else ""
            parts.append(f"[resource link: {block.uri} ({block.name}){desc}]")
        else:
            parts.append(f"[{getattr(block, 'type', 'unknown')} content]")
    if not has_text and result.structured_content is not None:
        parts.append(json.dumps(result.structured_content, ensure_ascii=False))
    return "\n".join(parts) if parts else "(empty result)"


def tool_signature(tool: dict) -> str:
    """`name(a, b?)`: required arguments plain, optional ones with '?'."""
    schema = tool.get("input_schema") or {}
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    args = ", ".join(p if p in required else f"{p}?" for p in props)
    return f"{tool.get('name', '?')}({args})"


def _tool_names(tools: list) -> list[str]:
    return [t.get("name", "") if isinstance(t, dict) else str(t) for t in tools]


async def _list_tools(client: Client) -> list[dict]:
    tools: list[dict] = []
    cursor: str | None = None
    for _ in range(100):  # pagination guard
        page = await client.list_tools(cursor=cursor)
        tools.extend(
            {"name": t.name, "description": t.description or "", "input_schema": t.input_schema} for t in page.tools
        )
        cursor = page.next_cursor
        if not cursor:
            break
    return tools


# --- connection ------------------------------------------------------------------------------

class MCPConnection:
    """One running stdio MCP server. A dedicated asyncio task enters and exits the client (anyio
    requires both in the same task); calls are made from any task through the live session."""

    CLOSE_TIMEOUT = 10.0

    def __init__(self, name: str, command: list[str], cwd: Path, env: dict, log_path: Path):
        self.name = name
        self.command = list(command)
        self.cwd = Path(cwd)
        self.env = dict(env)
        self.env.setdefault("COLUMNS", LOG_COLUMNS)  # readable rich logs (see LOG_COLUMNS)
        self.log_path = Path(log_path)
        self.tools: list[dict] = []
        self.offline = False   # started in a session without network (MCPForge._new_connection)
        self._client: Client | None = None
        self._task: asyncio.Task | None = None
        self._scope: anyio.CancelScope | None = None
        self._ready = asyncio.Event()
        self._closing = asyncio.Event()
        self._lost = False

    @property
    def alive(self) -> bool:
        return (
            self._client is not None and not self._lost and not self._closing.is_set()
            and self._task is not None and not self._task.done()
        )

    async def start(self, timeout: float) -> list[dict]:
        """Spawn the process, complete the handshake and list tools; ConnectionFailed with the log tail."""
        if self._task is not None:
            raise RuntimeError(f"MCP connection '{self.name}' was already started")
        self._task = asyncio.create_task(self._run(), name=f"mcp-{self.name}")
        waiter = asyncio.create_task(self._ready.wait())
        try:
            await asyncio.wait({self._task, waiter}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        except BaseException:
            await self._abort()
            raise
        finally:
            waiter.cancel()
        if self._ready.is_set() and not self._task.done():
            return list(self.tools)
        if self._task.done():
            reason = self._task_error() or "the server stopped during startup"
        else:
            reason = f"no answer to the MCP handshake within {timeout:g}s"
            await self._abort()
        raise ConnectionFailed(self.name, reason, read_log_tail(self.log_path))

    async def _run(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        mode = "w" if _log_size(self.log_path) > LOG_MAX_BYTES else "a"
        with open(self.log_path, mode, encoding="utf-8", errors="replace") as errlog:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            errlog.write(f"\n{LOG_MARKER} {stamp}: {' '.join(self.command)} =====\n")
            errlog.flush()
            params = StdioServerParameters(
                command=self.command[0], args=self.command[1:], cwd=str(self.cwd), env=self.env
            )
            try:
                with anyio.CancelScope() as scope:
                    self._scope = scope
                    async with Client(stdio_client(params, errlog=errlog)) as client:
                        self.tools = await _list_tools(client)
                        self._client = client
                        self._ready.set()
                        await self._closing.wait()
            finally:
                self._client = None
                self._scope = None

    async def call(self, tool: str, arguments: dict, timeout: float) -> tuple[str, bool]:
        """Returns (text, is_error). Raises ConnectionLost if the process is gone."""
        client = self._client
        if client is None or not self.alive:
            raise ConnectionLost(f"MCP server '{self.name}' is not running.")
        mark = _log_size(self.log_path)
        try:
            result = await asyncio.wait_for(client.call_tool(tool, arguments), timeout)
        except TimeoutError:
            await self.close()  # a stuck tool may block the server; the pool restarts it on next use
            return (f"Tool '{tool}' of MCP server '{self.name}' timed out after {timeout:g}s; "
                    "the server was stopped and will restart on the next call."), True
        except MCPError as exc:
            if exc.code == mcp_types.CONNECTION_CLOSED:
                self._lost = True
                raise ConnectionLost(f"MCP server '{self.name}' process exited.") from exc
            return f"MCP error {exc.code}: {exc.message}", True
        except (anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.EndOfStream) as exc:
            self._lost = True
            raise ConnectionLost(f"MCP server '{self.name}' connection closed.") from exc
        except Exception as exc:  # e.g. a result that does not validate
            log.warning("MCP call %s.%s failed", self.name, tool, exc_info=True)
            return f"MCP call failed: {type(exc).__name__}: {exc}", True
        text = render_result(result)
        if result.is_error and text.strip() == f"{CRASH_PREFIX} {tool}":
            details = _crash_details(self.log_path, mark)
            if details:
                text += f"\n--- server log ---\n{details}"
        return text, bool(result.is_error)

    async def close(self) -> None:
        self._closing.set()
        task = self._task
        if task is None:
            return
        if not task.done():
            done, _ = await asyncio.wait({task}, timeout=self.CLOSE_TIMEOUT)
            if not done:
                await self._abort()
        self._task_error()

    async def _abort(self) -> None:
        task = self._task
        if task is None or task.done():
            return
        if self._scope is not None:
            self._scope.cancel()
        else:
            task.cancel()
        await asyncio.wait({task}, timeout=self.CLOSE_TIMEOUT)

    def _task_error(self) -> str | None:
        """Describe (and mark as retrieved) the exception of the finished task, if any."""
        task = self._task
        if task is None or not task.done() or task.cancelled():
            return None
        exc = task.exception()
        if exc is None:
            return None
        log.debug("MCP server %s task ended with %r", self.name, exc)
        return describe_error(exc)


# --- forge -----------------------------------------------------------------------------------

class MCPForge:
    def __init__(self, swarm):
        self.swarm = swarm
        self._conns: dict[str, MCPConnection] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._external: dict[str, dict] = {}
        self._stopped = False

    @property
    def config(self):
        return self.swarm.config

    @property
    def db(self):
        return self.swarm.db

    # --- lifecycle -------------------------------------------------------------------------
    async def start(self) -> None:
        """Register `[mcp.external]` servers (origin "config"); processes start lazily on first use."""
        self._stopped = False
        self._external = {}
        for raw_name, spec in (self.config.mcp.external or {}).items():
            try:
                rec = self._external_record(raw_name, spec)
            except ValueError as exc:
                log.warning("Skipping [mcp.external.%s]: %s", raw_name, exc)
                continue
            self._register_external(rec, spec)
        for row in self.db.all("SELECT name FROM mcp_servers WHERE origin = ?", (ORIGIN_CONFIG,)):
            if row["name"] not in self._external:
                await self._drop_connection(row["name"])
                self.db.execute("DELETE FROM mcp_servers WHERE name = ?", (row["name"],))
                self.swarm.bus.emit("mcp.status", None, name=row["name"], status=REMOVED)
        # forged and imported servers live in the workspace (server_dir), which a session can move elsewhere
        for row in self.db.all("SELECT name, cwd FROM mcp_servers WHERE origin != ?", (ORIGIN_CONFIG,)):
            cwd = str(self.server_dir(row["name"]))
            if row["cwd"] != cwd:
                self.db.execute("UPDATE mcp_servers SET cwd = ? WHERE name = ?", (cwd, row["name"]))

    async def stop(self) -> None:
        self._stopped = True
        conns = list(self._conns.values())
        self._conns.clear()
        await asyncio.gather(*(c.close() for c in conns), return_exceptions=True)

    async def close_connections(self) -> None:
        """Close the running servers (the session's network changed): each starts again at its next use."""
        conns = list(self._conns.values())
        self._conns.clear()
        await asyncio.gather(*(c.close() for c in conns), return_exceptions=True)

    def connection(self, name: str) -> MCPConnection | None:
        """The pooled connection of a server, if one was started."""
        return self._conns.get(_norm(name))

    # --- public API --------------------------------------------------------------------------
    async def create(self, actor: str, name: str, description: str, code: str, *, scope: str = "swarm",
                     task_id: int | None = None, requirements: list[str] | None = None,
                     save_to_library: bool = True) -> str:
        name = self._check_name(name)
        description = self._check_description(description)
        code = self._check_code(code)
        scope, task_id = self._check_scope(scope, task_id)
        reqs = self._check_requirements(requirements)
        async with self._lock(name):
            existing = self._record(name)
            self._check_overwrite(actor, name, existing)
            owner = existing["owner"] if existing is not None and actor == USER else actor
            command = self.build_command(reqs)
            rec = {
                "name": name, "owner": owner, "scope": scope, "task_id": task_id, "description": description,
                "command": command, "cwd": str(self.server_dir(name)), "requirements": reqs,
                "origin": ORIGIN_WORKSPACE, "status": FAILED, "tools": [], "error": None,
                "created_at": existing["created_at"] if existing else None,
            }
            # From here on server.py may already hold the new, untested code: if this call is interrupted
            # (the agent is cancelled, the hub client disconnects...) the old record must not stay active.
            try:
                await self._drop_connection(name)
                await asyncio.to_thread(self._write_code, name, code)
                conn, tools, error = await self._smoke_test(name, code, command)
                if error:
                    status = FAILED
                elif self.config.mcp.require_approval and actor != USER:
                    status = PENDING
                else:
                    status = ACTIVE
                if conn is not None and (status != ACTIVE or self._stopped):
                    await conn.close()
                    conn = None
                if conn is not None:
                    self._conns[name] = conn
                rec.update(status=status, tools=tools, error=error[:MAX_ERROR_CHARS] if error else None)
                self._save(rec)
            except BaseException as exc:
                self._record_interrupted(actor, rec, exc)
                raise
            # announce before the next await so a late cancellation cannot leave a silent pending server
            self.swarm.bus.emit("mcp.created", actor, server=self._public(self._record(name)))
            if status == PENDING:
                self.swarm.notify(USER, "u_mcp_pending", owner=owner, name=name, description=description)
            await asyncio.to_thread(self._write_workspace_meta, rec, save_to_library)
            note = ""
            if status == ACTIVE and scope == "swarm" and save_to_library:
                note = await self._save_library(rec)
            return self._create_report(rec, note)

    def _record_interrupted(self, actor: str, rec: dict, exc: BaseException) -> None:
        """create() did not finish: mark the server failed so the unverified code is never started."""
        if isinstance(exc, Exception):
            reason = f"mcp_create failed ({type(exc).__name__}: {exc})"
        else:
            reason = "mcp_create was interrupted"
        rec.update(status=FAILED, tools=[],
                   error=f"{reason} before the server was verified; call mcp_create again with the same name.")
        conn = self._conns.get(rec["name"])
        if conn is not None:
            conn._closing.set()  # its task stops the process; close() cannot be awaited here
            self._conns.pop(rec["name"], None)
        try:
            self._save(rec)
            self.swarm.bus.emit("mcp.created", actor, server=self._public(self._record(rec["name"])))
        except Exception:
            log.exception("Could not record the interrupted creation of MCP server %s", rec["name"])

    def list(self, actor: str, query: str = "") -> list[dict]:
        terms = str(query or "").lower().split()
        out: list[dict] = []
        known: set[str] = set()
        for row in self.db.all("SELECT * FROM mcp_servers ORDER BY name"):
            rec = self._decode(row)
            known.add(rec["name"])
            if self._can_use(actor, rec) and self._matches(terms, rec):
                out.append(self._public(rec))
        for name, entry in self._library_entries().items():
            if name not in known and self._matches(terms, entry):
                out.append(self._library_public(entry))
        return out

    async def tools(self, actor: str, name: str) -> list[dict]:
        conn = await self._open(actor, name)
        return [dict(t) for t in conn.tools]

    async def call(self, actor: str, server: str, tool: str, arguments: dict | None = None,
                   timeout: float | None = None, max_chars: int | None = None) -> tuple[str, bool]:
        """The result text is cut to `max_chars` (default agents.tool_output_chars)."""
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ToolError('arguments must be a JSON object, e.g. {"a": 1}.')
        tool = str(tool or "").strip()
        if not tool:
            raise ToolError("tool is required: see mcp_tools for the server's tools.")
        limit = float(timeout) if timeout and timeout > 0 else float(self.config.mcp.call_timeout)
        conn = await self._open(actor, server)
        self._check_tool(conn, tool)
        try:
            text, is_error = await conn.call(tool, arguments, limit)
        except ConnectionLost:
            log.info("MCP server %s died; restarting it", conn.name)
            conn = await self._open(actor, server, dead=conn)
            self._check_tool(conn, tool)
            try:
                text, is_error = await conn.call(tool, arguments, limit)
            except ConnectionLost as exc:
                await self._discard(conn)
                tail = read_log_tail(conn.log_path, lines=20)
                text, is_error = f"{exc} It crashed again after a restart.\n--- server log (tail) ---\n{tail}", True
        return truncate_middle(text, max_chars or self.config.agents.tool_output_chars), is_error

    async def approve(self, actor: str, name: str, approve: bool, comment: str = "") -> str:
        if actor != USER:
            raise ToolError("Only the user can approve or reject MCP servers.")
        key = _norm(name)
        comment = str(comment or "").strip()
        async with self._lock(key):
            rec = self._record(key)
            if rec is None:
                raise self._unknown(actor, key)
            if rec["status"] != PENDING:
                raise ToolError(f"MCP server '{key}' is not waiting for approval (status: {rec['status']}).")
            if approve:
                rec.update(status=ACTIVE, error=None)
            else:
                await self._drop_connection(key)  # the user may have started it to try it out
                rec.update(status=REJECTED, error=f"Rejected by the user. {comment}".strip())
            self._save(rec)
            self.swarm.bus.emit("mcp.status", actor, name=key, status=rec["status"])
            if not approve:
                if rec["owner"] != USER:
                    self.swarm.notify(rec["owner"], "n_mcp_rejected", name=key, comment=comment)
                return f"MCP server '{key}' rejected."
            if rec["owner"] != USER:
                self.swarm.notify(rec["owner"], "n_mcp_approved", name=key)
            note = ""
            if rec["scope"] == "swarm" and self._wants_library(key):
                note = await self._save_library(rec)
            return f"MCP server '{key}' approved and active.{note}"

    async def remove(self, actor: str, name: str) -> str:
        key = _norm(name)
        async with self._lock(key):
            rec = self._record(key)
            if rec is None and self._library_entry(key) is not None:
                if actor != USER:
                    raise ToolError(f"MCP server '{key}' is only in the shared library; only the user can remove it.")
                await asyncio.to_thread(shutil.rmtree, self.config.library_dir / key, True)
                self.swarm.bus.emit("mcp.status", actor, name=key, status=REMOVED)
                return f"MCP server '{key}' removed from the library."
            if rec is None or not self._can_use(actor, rec):
                raise self._unknown(actor, key)
            if rec["origin"] == ORIGIN_CONFIG:
                raise ToolError(f"MCP server '{key}' is defined in dreamteam.toml ([mcp.external]); remove it there.")
            if actor != USER and rec["owner"] != actor:
                raise ToolError(f"Only the owner ({rec['owner']}) or the user can remove MCP server '{key}'.")
            await self._drop_connection(key)
            self.db.execute("DELETE FROM mcp_servers WHERE name = ?", (key,))
            await asyncio.to_thread(shutil.rmtree, self.server_dir(key), True)
            note = ""
            lib = self._library_entry(key)
            if lib is not None:
                own_copy = lib["owner"] == actor and lib.get("project") == str(self.config.project_dir)
                if actor == USER or own_copy:
                    await asyncio.to_thread(shutil.rmtree, self.config.library_dir / key, True)
                    note = " It was also removed from the library."
                else:
                    note = " The library copy remains (only the user can remove it)."
            self.swarm.bus.emit("mcp.status", actor, name=key, status=REMOVED)
            return f"MCP server '{key}' removed.{note}"

    def get(self, name: str) -> dict | None:
        key = _norm(name)
        rec = self._record(key)
        if rec is not None:
            return self._public(rec)
        entry = self._library_entry(key)
        return self._library_public(entry) if entry else None

    # --- paths / commands -----------------------------------------------------------------------
    def server_dir(self, name: str) -> Path:
        return self.config.workspace_dir / "mcp" / name

    def log_path(self, name: str) -> Path:
        return self.config.data_dir / "logs" / f"mcp-{name}.log"

    @staticmethod
    def build_command(requirements: list[str]) -> list[str]:
        uv = shutil.which("uv")
        # On Windows a .cmd/.bat shim would route agent-controlled requirement strings through cmd.exe,
        # where < and > are redirections; only a real uv.exe is used there.
        if requirements and uv and (os.name != "nt" or uv.lower().endswith(".exe")):
            command = [uv, "run", "--no-project", "--with", MCP_REQUIREMENT]
            for req in requirements:
                command += ["--with", req]
            return command + ["python", "server.py"]
        return [sys.executable, "server.py"]

    def _launch_spec(self, rec: dict) -> tuple[list[str], Path, dict[str, str]]:
        name = rec["name"]
        if rec["origin"] == ORIGIN_CONFIG:
            extra = self._external.get(name, {}).get("env") or {}
            cwd = Path(rec["cwd"]) if rec["cwd"] else self.config.workspace_dir
            env = scrubbed_env(self.config, extra={str(k): str(v) for k, v in extra.items()})
            return list(rec["command"]), cwd, env
        return self.build_command(rec["requirements"]), self.server_dir(name), scrubbed_env(self.config)

    # --- validation ---------------------------------------------------------------------------
    @staticmethod
    def _check_name(name: Any) -> str:
        value = str(name or "").strip()
        if not NAME_RE.match(value):
            raise ToolError(
                f"Invalid MCP server name '{value}': use 2-40 characters - lowercase latin letters, digits "
                "and '_', starting with a letter (e.g. 'pdf_tools')."
            )
        return value

    @staticmethod
    def _check_description(description: Any) -> str:
        value = str(description or "").strip()
        if not value:
            raise ToolError("description is required: say in one sentence what the server's tools do.")
        if len(value) > MAX_DESCRIPTION_CHARS:
            raise ToolError(f"description is too long ({len(value)} chars); keep it under {MAX_DESCRIPTION_CHARS}.")
        return value

    @staticmethod
    def _check_code(code: Any) -> str:
        value = str(code or "")
        if not value.strip():
            raise ToolError("code is empty: pass the full source of server.py (see the template in mcp_create).")
        if len(value) > MAX_CODE_CHARS:
            raise ToolError(f"code is too long ({len(value)} chars, max {MAX_CODE_CHARS}); split the server.")
        return value if value.endswith("\n") else value + "\n"

    def _check_scope(self, scope: Any, task_id: Any) -> tuple[str, int | None]:
        value = str(scope or "swarm").strip().lower()
        if value not in SCOPES:
            raise ToolError(f"Invalid scope '{scope}': use 'private', 'task' or 'swarm'.")
        if task_id is None or task_id == "":
            if value == "task":
                raise ToolError("scope 'task' needs task_id: the task whose participants may use the server.")
            return value, None
        try:
            tid = int(task_id)
        except (TypeError, ValueError):
            raise ToolError(f"task_id must be an integer, got '{task_id}'.") from None
        if self.db.one("SELECT id FROM tasks WHERE id = ?", (tid,)) is None:
            raise ToolError(f"Task #{tid} does not exist.")
        return value, tid

    @staticmethod
    def _check_requirements(requirements: Any) -> list[str]:
        if requirements is None or requirements == "":
            return []
        if isinstance(requirements, str):
            requirements = _REQ_SPLIT_RE.split(requirements)
        if not isinstance(requirements, (list, tuple)):
            raise ToolError('requirements must be a list of pip requirement strings, e.g. ["httpx>=0.27"].')
        out: list[str] = []
        for item in requirements:
            req = str(item or "").strip()
            if not req:
                continue
            if req.startswith("-") or any(ch in req for ch in "\r\n\0") or len(req) > 200:
                raise ToolError(f"Invalid requirement '{req}': use a pip requirement such as 'httpx>=0.27'.")
            if req[0] in "<>=!~,;]":
                raise ToolError(
                    f"Invalid requirement '{req}': it has no package name. Keep a version range in one list "
                    'item, e.g. ["httpx>=0.27,<1"].'
                )
            if req not in out:
                out.append(req)
        if len(out) > MAX_REQUIREMENTS:
            raise ToolError(f"Too many requirements ({len(out)}, max {MAX_REQUIREMENTS}).")
        return out

    def _check_overwrite(self, actor: str, name: str, existing: dict | None) -> None:
        if existing is not None:
            if existing["origin"] == ORIGIN_CONFIG:
                raise ToolError(f"'{name}' is an external MCP server from dreamteam.toml; choose another name.")
            if actor != USER and existing["owner"] != actor:
                raise ToolError(
                    f"MCP server '{name}' belongs to {existing['owner']}; only its owner or the user can "
                    "replace it. Choose another name."
                )
            return
        entry = self._library_entry(name)
        if actor != USER and entry is not None:
            raise ToolError(
                f"The library already has an MCP server '{name}' ({entry['description']}). Use it via "
                "mcp_tools/mcp_call, or choose another name."
            )

    def _check_tool(self, conn: MCPConnection, tool: str) -> None:
        names = _tool_names(conn.tools)
        if tool not in names:
            raise ToolError(f"MCP server '{conn.name}' has no tool '{tool}'. Tools: {', '.join(names) or '(none)'}.")

    # --- access ----------------------------------------------------------------------------------
    def _can_use(self, actor: str, rec: dict) -> bool:
        if actor == USER or rec["owner"] == actor or rec["scope"] == "swarm":
            return True
        if rec["scope"] == "task" and rec["task_id"] is not None:
            row = self.db.one("SELECT created_by, assignee, reviewer FROM tasks WHERE id = ?", (rec["task_id"],))
            return row is not None and actor in (row["created_by"], row["assignee"], row["reviewer"])
        return False

    def _unknown(self, actor: str, name: str) -> ToolError:
        names = [s["name"] for s in self.list(actor)][:30]
        listing = ", ".join(names) if names else "(none yet - build one with mcp_create)"
        return ToolError(f"Unknown MCP server '{name}'. Available: {listing}.")

    async def _usable(self, actor: str, name: str) -> dict:
        rec = self._record(name)
        if rec is None:
            if self._library_entry(name) is None:
                raise self._unknown(actor, name)
            rec = await self._import_from_library(actor, name)
        if not self._can_use(actor, rec):
            raise self._unknown(actor, name)
        status = rec["status"]
        if status == ACTIVE or (status == PENDING and actor == USER):
            return rec
        if status == PENDING:
            raise ToolError(f"MCP server '{name}' is waiting for the user's approval; try again later.")
        if status == REJECTED:
            raise ToolError(f"MCP server '{name}' was rejected by the user.")
        if status == FAILED:
            reason = (rec["error"] or "unknown error").strip().splitlines()[0][:300]
            raise ToolError(
                f"MCP server '{name}' failed its smoke test ({reason}); its owner ({rec['owner']}) must fix "
                "it with mcp_create."
            )
        raise ToolError(f"MCP server '{name}' is not usable (status: {status}).")

    # --- connection pool ---------------------------------------------------------------------
    def _lock(self, name: str) -> asyncio.Lock:
        return self._locks.setdefault(name, asyncio.Lock())

    async def _open(self, actor: str, name: str, dead: MCPConnection | None = None) -> MCPConnection:
        key = _norm(name)
        if self._stopped:
            raise ToolError("The MCP forge is stopped.")
        async with self._lock(key):
            if self._stopped:  # stop() ran while this caller waited for the lock
                raise ToolError("The MCP forge is stopped.")
            rec = await self._usable(actor, key)
            conn = self._conns.get(key)
            # (a server started under the other network setting, docs/SPEC.md §7.2: close_connections may have run
            # while it was starting, so it is checked here too)
            if conn is not None and conn.alive and conn is not dead and self._same_network(conn):
                return conn
            if conn is not None:
                self._conns.pop(key, None)
                await conn.close()
            return await self._connect(actor, rec)

    def _isolated(self, command: list[str], env: dict[str, str]) -> tuple[list[str], dict[str, str]]:
        """A server's command and environment in a session without network (netoff.py): run like the members'
        commands, which wrote it or call it."""
        return netoff.command(self.config, command, env)

    def _new_connection(self, name: str, command: list[str], cwd: Path, env: dict) -> MCPConnection:
        offline = netoff.is_off(self.config)
        command, env = self._isolated(command, env)
        conn = MCPConnection(name, command, cwd, env, self.log_path(name))
        conn.offline = offline   # the setting it runs with (_same_network)
        return conn

    def _same_network(self, conn: MCPConnection) -> bool:
        return getattr(conn, "offline", False) == netoff.is_off(self.config)

    async def _connect(self, actor: str, rec: dict, again: bool = True) -> MCPConnection:
        name = rec["name"]
        command, cwd, env = self._launch_spec(rec)
        if netoff.is_off(self.config):
            # a stdio bridge to a remote server would fail inside the isolation, or (best effort) get through it
            url = netoff.remote_url(command)
            if url is not None:
                raise ToolError(f"MCP server '{name}' connects to {url}, and this session has no network ([swarm] "
                                "network = \"off\"): it is not started.")
        conn = self._new_connection(name, command, cwd, env)
        try:
            tools = await conn.start(self.config.mcp.startup_timeout)
        except ConnectionFailed as exc:
            self._update_runtime(actor, rec, rec["tools"], str(exc)[:MAX_ERROR_CHARS])
            raise
        if self._stopped:  # stop() ran while the server was starting
            await conn.close()
            raise ToolError("The MCP forge is stopped.")
        if not self._same_network(conn):  # the network setting changed while it was starting: start it again
            await conn.close()
            if not again:
                raise ToolError(f"MCP server '{name}': the session's network setting changed while it was starting; "
                                "call it again.")
            return await self._connect(actor, rec, again=False)
        self._conns[name] = conn
        self._update_runtime(actor, rec, tools, None)
        log.info("MCP server %s started (%d tools)", name, len(tools))
        return conn

    def _update_runtime(self, actor: str, rec: dict, tools: list[dict], error: str | None) -> None:
        if rec["tools"] == tools and rec["error"] == error:
            return
        self.db.execute(
            "UPDATE mcp_servers SET tools = ?, error = ?, updated_at = ? WHERE name = ?",
            (dumps(tools), error, time.time(), rec["name"]),
        )
        rec.update(tools=tools, error=error)
        self.swarm.bus.emit("mcp.status", actor, name=rec["name"], status=rec["status"])

    async def _drop_connection(self, name: str) -> None:
        conn = self._conns.pop(name, None)
        if conn is not None:
            await conn.close()

    async def _discard(self, conn: MCPConnection) -> None:
        if self._conns.get(conn.name) is conn:
            self._conns.pop(conn.name, None)
        await conn.close()

    async def _smoke_test(
        self, name: str, code: str, command: list[str]
    ) -> tuple[MCPConnection | None, list[dict], str | None]:
        """Start the freshly written server and list its tools. Returns (live connection, tools, error)."""
        syntax = _syntax_error(code)
        if syntax:
            return None, [], syntax
        conn = self._new_connection(name, command, self.server_dir(name), scrubbed_env(self.config))
        try:
            tools = await conn.start(self.config.mcp.startup_timeout)
        except ConnectionFailed as exc:
            return None, [], str(exc)
        if not tools:
            await conn.close()
            return None, [], "the server started but exposes no tools: decorate functions with @mcp.tool()."
        return conn, tools, None

    # --- storage -------------------------------------------------------------------------------
    def _record(self, name: str) -> dict | None:
        row = self.db.one("SELECT * FROM mcp_servers WHERE name = ?", (name,))
        return self._decode(row) if row is not None else None

    @staticmethod
    def _decode(row) -> dict:
        rec = dict(row)
        rec["command"] = loads(rec["command"], [])
        rec["requirements"] = loads(rec["requirements"], [])
        rec["tools"] = loads(rec["tools"], [])
        return rec

    def _save(self, rec: dict) -> None:
        now = time.time()
        self.db.execute(
            "INSERT INTO mcp_servers(name, owner, scope, task_id, description, command, cwd, requirements, origin,"
            " status, tools, error, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET owner = excluded.owner, scope = excluded.scope,"
            " task_id = excluded.task_id, description = excluded.description, command = excluded.command,"
            " cwd = excluded.cwd, requirements = excluded.requirements, origin = excluded.origin,"
            " status = excluded.status, tools = excluded.tools, error = excluded.error,"
            " updated_at = excluded.updated_at",
            (
                rec["name"], rec["owner"], rec["scope"], rec["task_id"], rec["description"], dumps(rec["command"]),
                rec["cwd"], dumps(rec["requirements"]), rec["origin"], rec["status"], dumps(rec["tools"]),
                rec["error"], rec.get("created_at") or now, now,
            ),
        )

    @staticmethod
    def _public(rec: dict) -> dict:
        return {
            "name": rec["name"], "owner": rec["owner"], "scope": rec["scope"], "task_id": rec["task_id"],
            "description": rec["description"] or "", "status": rec["status"], "origin": rec["origin"],
            "tools": _tool_names(rec["tools"]), "error": rec["error"], "requirements": list(rec["requirements"]),
            # a configured server's argv; [] for a forged one (it runs its server.py, see build_command)
            "command": list(rec["command"] or []) if rec["origin"] == ORIGIN_CONFIG else [],
            "created_at": rec["created_at"],
        }

    @staticmethod
    def _matches(terms: list[str], rec: dict) -> bool:
        if not terms:
            return True
        tools = rec.get("tools") or []
        hay = " ".join(
            [rec["name"], rec.get("description") or ""]
            + [f"{t.get('name', '')} {t.get('description', '')}" if isinstance(t, dict) else str(t) for t in tools]
        ).lower()
        return all(term in hay for term in terms)

    def _write_code(self, name: str, code: str) -> None:
        folder = self.server_dir(name)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "server.py").write_bytes(code.encode("utf-8"))

    def _write_workspace_meta(self, rec: dict, save_to_library: bool) -> None:
        meta = {
            "name": rec["name"], "description": rec["description"], "owner": rec["owner"], "scope": rec["scope"],
            "task_id": rec["task_id"], "requirements": rec["requirements"], "save_to_library": bool(save_to_library),
            "tools": rec["tools"], "updated_at": time.time(),
        }
        folder = self.server_dir(rec["name"])
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def _wants_library(self, name: str) -> bool:
        try:
            meta = json.loads((self.server_dir(name) / "meta.json").read_text("utf-8"))
        except (OSError, ValueError):
            return True
        return bool(meta.get("save_to_library", True)) if isinstance(meta, dict) else True

    # --- external servers ----------------------------------------------------------------------
    def _external_record(self, raw_name: Any, spec: Any) -> dict:
        name = _norm(raw_name)
        if not EXTERNAL_NAME_RE.match(name):
            raise ValueError("the name must be 1-64 characters: lowercase letters, digits, '_', '-', '.'")
        if not isinstance(spec, dict):
            raise ValueError("expected a table with `command`")
        command = spec.get("command")
        if isinstance(command, str):
            command = _split_command(command)
        if not isinstance(command, list) or not command or not all(isinstance(c, str) and c for c in command):
            raise ValueError("`command` must be a non-empty list of strings")
        env = spec.get("env") or {}
        if not isinstance(env, dict):
            raise ValueError("`env` must be a table of strings")
        cwd = spec.get("cwd")
        if cwd:
            path = Path(os.path.expanduser(str(cwd)))
            cwd = str(path if path.is_absolute() else self.config.project_dir / path)
        return {
            "name": name, "owner": USER, "scope": "swarm", "task_id": None,
            "description": str(spec.get("description") or ""), "command": list(command), "cwd": cwd or None,
            "requirements": [], "origin": ORIGIN_CONFIG, "status": ACTIVE, "tools": [], "error": None,
            "created_at": None,
        }

    def _register_external(self, rec: dict, spec: dict) -> None:
        name = rec["name"]
        existing = self._record(name)
        if existing is not None and existing["origin"] != ORIGIN_CONFIG:
            log.warning("[mcp.external.%s] clashes with an existing MCP server of that name; skipped", name)
            return
        self._external[name] = spec
        if existing is None:
            self._save(rec)
            self.swarm.bus.emit("mcp.created", None, server=self._public(self._record(name)))
            return
        same = (existing["command"], existing["cwd"], existing["description"]) == (
            rec["command"], rec["cwd"], rec["description"])
        if same and existing["status"] == ACTIVE:
            return
        rec["created_at"] = existing["created_at"]
        if existing["command"] == rec["command"]:
            rec["tools"] = existing["tools"]
        self._save(rec)
        self.swarm.bus.emit("mcp.status", None, name=name, status=ACTIVE)

    # --- library -------------------------------------------------------------------------------
    def _library_entry(self, name: str) -> dict | None:
        if not NAME_RE.match(name):
            return None
        folder = self.config.library_dir / name
        try:
            if not (folder / "server.py").is_file():
                return None
            meta = json.loads((folder / "meta.json").read_text("utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(meta, dict):
            return None
        reqs = meta.get("requirements") or []
        tools = meta.get("tools") or []
        return {
            "name": name, "owner": str(meta.get("owner") or USER), "description": str(meta.get("description") or ""),
            "requirements": [str(r) for r in reqs] if isinstance(reqs, list) else [],
            "tools": [t for t in tools if isinstance(t, dict)] if isinstance(tools, list) else [],
            "created_at": meta.get("created_at"), "project": meta.get("project"),
        }

    def _library_entries(self) -> dict[str, dict]:
        root = self.config.library_dir
        try:
            folders = sorted(p.name for p in root.iterdir() if p.is_dir())
        except OSError:
            return {}
        entries: dict[str, dict] = {}
        for name in folders:
            entry = self._library_entry(name)
            if entry is not None:
                entries[name] = entry
        return entries

    @staticmethod
    def _library_public(entry: dict) -> dict:
        return {
            "name": entry["name"], "owner": entry["owner"], "scope": "swarm", "task_id": None,
            "description": entry["description"], "status": AVAILABLE, "origin": ORIGIN_LIBRARY,
            "tools": _tool_names(entry["tools"]), "error": None, "requirements": list(entry["requirements"]),
            "command": [], "created_at": entry["created_at"],
        }

    async def _save_library(self, rec: dict) -> str:
        try:
            await asyncio.to_thread(self._copy_to_library, rec)
        except OSError as exc:
            log.warning("Could not save MCP server %s to the library: %s", rec["name"], exc)
            return f" Could not save it to the library: {exc}."
        return " Saved to the shared MCP library."

    def _copy_to_library(self, rec: dict) -> None:
        dest = self.config.library_dir / rec["name"]
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.server_dir(rec["name"]) / "server.py", dest / "server.py")
        previous = self._library_entry(rec["name"])
        meta = {
            "name": rec["name"], "description": rec["description"], "owner": rec["owner"],
            "requirements": rec["requirements"], "tools": rec["tools"],
            "created_at": (previous or {}).get("created_at") or time.time(), "updated_at": time.time(),
            "project": str(self.config.project_dir), "swarm": self.config.swarm.name,
        }
        (dest / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def _copy_from_library(self, name: str) -> None:
        folder = self.server_dir(name)
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.config.library_dir / name / "server.py", folder / "server.py")

    async def _import_from_library(self, actor: str, name: str) -> dict:
        entry = self._library_entry(name)
        if entry is None:
            raise self._unknown(actor, name)
        try:
            await asyncio.to_thread(self._copy_from_library, name)
        except OSError as exc:
            raise ToolError(f"Could not import MCP server '{name}' from the library: {exc}") from None
        rec = {
            "name": name, "owner": entry["owner"], "scope": "swarm", "task_id": None,
            "description": entry["description"], "command": self.build_command(entry["requirements"]),
            "cwd": str(self.server_dir(name)), "requirements": entry["requirements"], "origin": ORIGIN_LIBRARY,
            "status": ACTIVE, "tools": entry["tools"], "error": None, "created_at": None,
        }
        # Library code was approved (if at all) in another swarm; honour this swarm's approval policy.
        needs_approval = self.config.mcp.require_approval and actor != USER
        if needs_approval:
            rec["status"] = PENDING
        self._save(rec)
        await asyncio.to_thread(self._write_workspace_meta, rec, False)
        self.swarm.bus.emit("mcp.created", actor, server=self._public(self._record(name)))
        if needs_approval:
            self.swarm.notify(USER, "u_mcp_pending", owner=actor, name=name, description=entry["description"],
                              wake=False)
        log.info("Imported MCP server %s from the library", name)
        return self._record(name)

    # --- reports -------------------------------------------------------------------------------
    @staticmethod
    def _create_report(rec: dict, library_note: str) -> str:
        name = rec["name"]
        if rec["status"] == FAILED:
            hint = ""
            if rec["requirements"] and not shutil.which("uv"):
                hint = "\nNote: uv is not on PATH, so the requirements were not installed automatically."
            return (f"MCP server '{name}' was saved but FAILED its smoke test (status: failed).\n{rec['error']}{hint}\n"
                    "Fix the code and call mcp_create again with the same name.")
        lines = []
        for tool in rec["tools"]:
            desc = (tool.get("description") or "").strip().splitlines()
            lines.append(f"- {tool_signature(tool)}" + (f": {desc[0][:200]}" if desc else ""))
        if rec["status"] == PENDING:
            head = (f"MCP server '{name}' passed its smoke test and is waiting for the user's approval "
                    "(status: pending); you will be notified")
        else:
            head = f"MCP server '{name}' is active"
        text = f"{head}. Tools ({len(lines)}):\n" + "\n".join(lines)
        if rec["status"] == ACTIVE:
            text += f'\nCall them with mcp_call(server="{name}", tool=..., arguments={{...}}).'
        return text + library_note


def _split_command(command: str) -> list[str]:
    """Split a command line written as one string. Quotes group words and are removed on every OS; on
    Windows a backslash is a path separator, not an escape (shlex's posix=False would keep the quotes)."""
    if os.name != "nt":
        return shlex.split(command)
    lexer = shlex.shlex(command, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    lexer.escape = ""
    return list(lexer)


def _syntax_error(code: str) -> str | None:
    try:
        compile(code, "server.py", "exec")
    except SyntaxError as exc:
        line = (exc.text or "").strip()
        return f"SyntaxError in server.py line {exc.lineno}: {exc.msg}" + (f"\n    {line}" if line else "")
    except ValueError as exc:
        return f"Invalid source code: {exc}"
    except (RecursionError, MemoryError):
        return "server.py cannot be compiled: the code is too deeply nested; split long expressions."
    return None


__all__ = [
    "MCPConnection", "MCPForge", "ConnectionFailed", "ConnectionLost", "SERVER_TEMPLATE", "NAME_RE",
    "render_result", "tool_signature", "read_log_tail",
]
