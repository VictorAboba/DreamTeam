"""Web server: REST API + WebSocket for the online board UI and console, and the hub MCP endpoint."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import os
import time
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import __version__, roles
from . import skills as skill_lib
from .db import loads
from .hub_mcp import LIBRARY_TOOLS, LOOPBACK_HOSTS, MCPHttpEndpoint
from .httpjson import JSONResponse, json_text
from .models import USER, ToolError
from .skills import Library, Limits, SkillError
from .tools import ToolContext, call_tool
from .trace_api import trace_routes

if TYPE_CHECKING:
    from .config import Config
    from .swarm import Swarm

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
# hostnames as urllib.parse reports them (IPv6 without brackets)
LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})
ALLOWED_HOSTS_ENV = "DREAMTEAM_ALLOWED_HOSTS"   # extra Host names (comma-separated), e.g. behind a reverse proxy
PUBLIC_URL_ENV = "DREAMTEAM_PUBLIC_URL"         # base URL users reach the hub at (e.g. a Docker port mapping)
GRACEFUL_SECONDS = 3                            # open connections (long polls) get this long on shutdown
INBOX_PAGE = 500                                # messages / notifications per /api/inbox response


def _split_hostport(value: str) -> tuple[str, int | None] | None:
    """`name[:port]` or `[v6][:port]` -> (lowercase hostname without brackets, port); None if malformed."""
    value = (value or "").strip().lower()
    if not value:
        return None
    try:
        parts = urllib.parse.urlsplit("//" + value)
        hostname, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not hostname or parts.username is not None or parts.path or parts.query or parts.fragment:
        return None
    return hostname, port


def _parse_origin(value: str) -> tuple[str, int] | None:
    """Browser Origin `scheme://host[:port]` -> (hostname, effective port); None for `null` or garbage."""
    try:
        parts = urllib.parse.urlsplit((value or "").strip().lower())
        hostname, port = parts.hostname, parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not hostname or parts.username is not None \
            or parts.path not in ("", "/") or parts.query or parts.fragment:
        return None
    return hostname, port or (443 if parts.scheme == "https" else 80)


def _is_ip(hostname: str) -> bool:
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        return False


def env_allowed_hosts() -> frozenset[str]:
    raw = os.environ.get(ALLOWED_HOSTS_ENV, "")
    names = set()
    for item in raw.split(","):
        parsed = _split_hostport(item)
        if parsed:
            names.add(parsed[0])
    return frozenset(names)


class GuardMiddleware:
    """DNS-rebinding guard (Host check), cross-site guard (Origin check, also for WebSockets) and
    JSON-only POSTs (blocks cross-site form posts).

    Loopback bind: Host must be localhost / 127.0.0.1 / [::1]. Any other bind (0.0.0.0 in Docker, a LAN
    address): Host may also be an IP literal (a rebinding attack always arrives with the attacker's domain
    name) or the bind address. Names in DREAMTEAM_ALLOWED_HOSTS are accepted in both cases. An Origin must be
    a loopback origin, an allowed name, or the very origin the request was made to.
    """

    def __init__(self, app, loopback: bool | None = None, *, host: str = "127.0.0.1",
                 extra_hosts: frozenset[str] | set[str] = frozenset()):
        self.app = app
        self.loopback = (host in LOOPBACK_HOSTS) if loopback is None else loopback
        bind = _split_hostport(host if not (":" in host and not host.startswith("[")) else f"[{host}]")
        self.bind_host = bind[0] if bind else ""
        self.extra = frozenset(h.lower() for h in extra_hosts)

    def host_allowed(self, hostname: str) -> bool:
        if hostname in LOOPBACK_NAMES or hostname in self.extra:
            return True
        if self.loopback:
            return False
        return hostname == self.bind_host or _is_ip(hostname)

    def origin_allowed(self, origin: str, host: tuple[str, int | None], scheme: str) -> bool:
        parsed = _parse_origin(origin)
        if parsed is None:
            return False
        o_host, o_port = parsed
        if o_host in LOOPBACK_NAMES or o_host in self.extra:
            return True
        default = 443 if scheme in ("https", "wss") else 80
        return (o_host, o_port) == (host[0], host[1] or default)

    async def _reject(self, scope, receive, send, text: str, status: int) -> None:
        if scope["type"] == "http":
            await PlainTextResponse(text, status_code=status)(scope, receive, send)
        else:
            await send({"type": "websocket.close", "code": 1008})

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            host = _split_hostport(headers.get("host", ""))
            if host is None or not self.host_allowed(host[0]):
                await self._reject(scope, receive, send, "Forbidden host", 403)
                return
            origin = headers.get("origin")
            if origin is not None and not self.origin_allowed(origin, host, scope.get("scheme", "http")):
                await self._reject(scope, receive, send, "Forbidden origin", 403)
                return
            if (scope["type"] == "http" and scope.get("method") == "POST" and scope.get("path", "").startswith("/api/")
                    and not headers.get("content-type", "").lower().startswith("application/json")):
                await PlainTextResponse("Content-Type must be application/json", status_code=415)(scope, receive, send)
                return
        await self.app(scope, receive, send)


class UIStaticFiles(StaticFiles):
    """Static UI files that browsers revalidate on every load (ETag), so an upgraded DreamTeam never runs a
    stale app.js against a newer API."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def _int(request: Request, name: str, default: int, lo: int = 0, hi: int = 10_000) -> int:
    try:
        value = int(request.query_params.get(name, default))
    except ValueError:
        value = default
    return max(lo, min(value, hi))


STOPPED_SKILLS_TEXT = "The session is stopped: start it to attach skills (importing into the library works)."


def skill_error_response(exc: Exception) -> JSONResponse:
    """A SkillError, or a skills ToolError / SessionError carrying `status` and `extra` (`exists`, ...), as the JSON
    answer `{"error", **extra}`."""
    return JSONResponse({"error": str(exc), **(getattr(exc, "extra", None) or {})},
                        status_code=getattr(exc, "status", 400))


def skill_routes(config_of: Callable[[], "Config"], workspace_of: Callable[[Request], Path | None], *, via: str,
                 swarm: "Swarm | None" = None, changed: Callable[[], None] | None = None) -> list[Route]:
    """The skills library routes of both apps (docs/SPEC.md §14, §18.1): GET / POST `/api/skills`, GET / DELETE
    `/api/skills/{name}` and, in a session, GET `/api/skills/{name}/deps-log`. `config_of()`: the settings (the
    launcher's global ones, the swarm's own); `workspace_of(request)`: the folder whose .claude/skills is the
    project's (the session's workspace; the launcher's `?workspace=`); `swarm`: the session (its attached skills,
    `attach`, `from=session`, the install logs); `changed()` after a library change; `via` is recorded in the
    library. Every `{name}` goes through skills.check_name first (400)."""

    def limits_view(config: "Config") -> dict[str, int]:
        s = config.skills
        return {"max_mb": s.max_mb, "max_files": s.max_files, "max_attached": s.max_attached,
                "max_archive_mb": s.max_archive_mb}

    async def index(request: Request):
        try:
            config = config_of()
            workspace = workspace_of(request)
            if request.method == "POST":
                return await import_skills(request, config, workspace)
            attached = swarm.skills.public() if swarm is not None else []   # (on the loop: deps read live state)
            current = swarm.skills.list() if swarm is not None else []

            def gather() -> dict[str, Any]:
                lib = Library.from_config(config)
                out: dict[str, Any] = {
                    "library_dir": str(lib.root), "library": lib.list(),
                    "claude": skill_lib.claude_skills(config, workspace), "host": skill_lib.host_skills(config),
                    "sandbox": config.skills_host_library_dir is not None, "limits": limits_view(config),
                    "install": skill_lib.install_info(config)}
                if swarm is not None:   # "Update" compares with the skill's own source only, never by name elsewhere
                    digests = {s.name: swarm.skills.source_digest(s) for s in current}
                    out["attached"] = [{**item, "source_digest": digests.get(item["name"])} for item in attached]
                return out

            return JSONResponse(await asyncio.to_thread(gather))
        except (SkillError, ToolError) as exc:
            return skill_error_response(exc)

    async def import_skills(request: Request, config: "Config", workspace: Path | None):
        body = await skill_lib.read_import_body(request, config)
        attach = body.pop("attach", False)
        if not isinstance(attach, bool):
            raise SkillError("'attach' must be true or false.")
        if attach and swarm is None:
            raise SkillError("'attach' attaches to a session: post to the session's /api/skills.")
        if attach and not swarm.started:
            raise SkillError(STOPPED_SKILLS_TEXT, 409)
        report = await asyncio.to_thread(skill_lib.import_request, config, body, workspace, by=USER, via=via)
        if report["imported"] and changed is not None:
            changed()
        report["install"] = skill_lib.install_info(config)
        if attach:
            attached, failed = [], []
            for entry in report["imported"]:
                try:   # one refusal (the cap, a clash) leaves the others attached: all of it is reported
                    skill, status = await swarm.skills.attach(USER, f"library:{entry['name']}",
                                                              replace=body.get("replace") is True, via=via)
                except ToolError as exc:
                    failed.append({"name": entry["name"], "error": str(exc), "status": getattr(exc, "status", 400)})
                    continue
                attached.append({"name": skill.name, "status": status, "files": skill.files})
            report.update(attached=attached, failed=failed)
        return JSONResponse(report)

    def show(config: "Config", name: str, frm: str, workspace: Path | None) -> dict[str, Any]:
        limits = Limits.from_config(config)
        if frm == "library":
            return Library.from_config(config).show(name)
        if frm == "host-library":
            host = Library.host(config)
            if host is None:
                raise SkillError("The host's skills library exists only inside the Docker sandbox.", 404)
            entry = host.show(name)
            return {**entry, "origin": "host-library", "host_origin": entry.get("origin"), "read_only": True}
        if frm in ("claude-user", "claude-project"):
            if not config.skills.claude:
                raise SkillError("Claude Code skills are off ([skills] claude = false).", 404)
            found = next((i for i in skill_lib.claude_skills(config, workspace)
                          if i["origin"] == frm and i["name"] == name), None)
            if found is None:
                where = "~/.claude/skills" if frm == "claude-user" else "the workspace's .claude/skills"
                raise SkillError(f"No skill '{name}' in {where}.", 404)
            if "error" in found:
                return {**found, "files_list": [], "skill_md": "", "deps": {}}
            return {**found, **skill_lib.show_folder(Path(found["path"]), limits)}
        if frm == "session" and swarm is not None:
            skill = swarm.skills.get(name)
            if skill is None:
                raise SkillError(f"No skill '{name}' is attached to this swarm.", 404)
            return {**skill.meta(), "path": swarm.skills.shown_path(name)[0],
                    **skill_lib.show_folder(swarm.skills.dir / name, limits)}
        allowed = "library, claude-user, claude-project, host-library" + (" or session" if swarm is not None else "")
        raise SkillError(f"'from' must be {allowed}.")

    async def item(request: Request):
        try:
            name = skill_lib.check_name(request.path_params["name"])   # before any path is built
            config = config_of()
            if request.method == "DELETE":
                if request.query_params.get("confirm") != name:
                    raise SkillError(f"Pass ?confirm={name} to delete the skill from the library.")
                await asyncio.to_thread(Library.from_config(config).remove, name)
                if changed is not None:
                    changed()
                return JSONResponse({"removed": name})
            shown = await asyncio.to_thread(show, config, name, request.query_params.get("from") or "library",
                                            workspace_of(request))
            return JSONResponse({**shown, "install": skill_lib.install_info(config)})
        except (SkillError, ToolError) as exc:
            return skill_error_response(exc)

    async def deps_log(request: Request):
        try:
            name = skill_lib.check_name(request.path_params["name"])
            ecosystem = request.query_params.get("ecosystem") or None
            if ecosystem not in (None, "python", "node"):
                raise SkillError("ecosystem is python or node.")
            if swarm.skills.get(name) is None:
                raise SkillError(f"No skill '{name}' is attached to this swarm.", 404)
            states = [s for s in swarm.skills.deps(name)
                      if s.get("env") and ecosystem in (None, s.get("ecosystem"))]
            if not states:
                what = {"python": "Python ", "node": "Node "}.get(ecosystem or "", "")
                raise SkillError(f"Skill '{name}' has no {what}packages installed by DreamTeam, so no install log.",
                                 404)
            state = next((s for s in states if s.get("status") == "failed"), states[0])
            dirname = Path(state["env"]).name
            manager = swarm.skills.manager
            text = await asyncio.to_thread(manager.log_tail, dirname)
            return JSONResponse({"name": name, "ecosystem": state["ecosystem"], "status": state["status"],
                                 "path": str(manager.log_path(dirname)), "log": text})
        except (SkillError, ToolError) as exc:
            return skill_error_response(exc)

    routes = [Route("/api/skills", index, methods=["GET", "POST"]),
              Route("/api/skills/{name}", item, methods=["GET", "DELETE"])]
    if swarm is not None:
        routes.append(Route("/api/skills/{name}/deps-log", deps_log))
    return routes


def create_app(swarm: "Swarm", host: str = "127.0.0.1", *,
               on_library_change: Callable[[], None] | None = None) -> Starlette:
    """The app of one swarm. `on_library_change`: called after this app changed the skills library (the launcher
    tells its home page)."""
    extra_hosts = env_allowed_hosts()
    endpoint = MCPHttpEndpoint(swarm, host, extra_hosts=extra_hosts, on_library_change=on_library_change)

    async def index(request: Request):
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    async def state(request: Request):
        return JSONResponse(swarm.snapshot())

    async def role_catalog(request: Request):
        # the visible roles in the swarm's language (the hire dialog, the TUI's /roles); not in the snapshot,
        # which is polled after every event while the descriptions are several KB. "names": the spellings a hire
        # resolves to the role (roles.find_role: its id and a built-in role's name in either language); "hidden":
        # the roles not offered, which a hire by name still resolves
        found = roles.catalog(swarm.config.roles, swarm.language, include_hidden=True)

        def view(r: roles.Role) -> dict[str, Any]:
            names = [r.name]
            if r.builtin:  # its id and its name in either language
                names += [r.key, *(roles.ROLE_PRESETS[lang][r.key][0] for lang in roles.ROLE_PRESETS)]
            return {"key": r.key, "name": r.name, "description": r.description, "instructions": r.instructions,
                    "model": r.model, "builtin": r.builtin, "names": list(dict.fromkeys(names)),
                    "skills": list(r.skills)}

        return JSONResponse({"language": swarm.language, "roles": [view(r) for r in found if not r.hidden],
                             "hidden": [view(r) for r in found if r.hidden]})

    async def hub_info(request: Request):
        # identity of this hub process: `dreamteam` commands compare it with hub.json (stale file, pid reuse)
        # sandbox: inside the Docker sandbox (the host library is a read-only source; folder paths are the container's)
        return JSONResponse({"pid": os.getpid(), "project": str(swarm.config.project_dir),
                             "name": swarm.config.swarm.name, "version": __version__,
                             "sandbox": swarm.config.skills_host_library_dir is not None})

    async def task_detail(request: Request):
        task_id = request.path_params["task_id"]
        try:
            task = swarm.board.get(task_id)
        except ToolError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        rows = swarm.db.all(
            "SELECT * FROM events WHERE type LIKE 'task.%' AND (json_extract(data, '$.task.id') = ? "
            "OR json_extract(data, '$.task_id') = ?) ORDER BY id DESC LIMIT 100",
            (task_id, task_id),
        )
        events = [{"id": r["id"], "ts": r["ts"], "type": r["type"], "actor": r["actor"], "data": loads(r["data"], {})}
                  for r in reversed(rows)]
        return JSONResponse({"task": task.to_dict(), "discussion": swarm.board.discussion(task_id), "events": events})

    async def messages(request: Request):
        limit = _int(request, "limit", 200, 1, 1000)
        since = _int(request, "since", 0)
        other = request.query_params.get("with")
        if other:
            try:
                name = swarm.resolve_member(other, allow_retired=True)
            except ToolError as exc:
                return JSONResponse({"error": str(exc)}, status_code=404)
            return JSONResponse({"messages": swarm.mail.conversation(USER, name, limit=limit, since_id=since)})
        return JSONResponse({"messages": swarm.mail.recent(limit=limit, since_id=since)})

    async def inbox(request: Request):
        # Rows with id > since (oldest first, at most INBOX_PAGE); without since, the newest INBOX_PAGE, so a
        # client priming itself (UI unread badges, console) starts from the latest state, not the oldest.
        # `since` (SPEC §14) is an alias of since_message.
        since_msg = _int(request, "since_message", _int(request, "since", 0, 0, 10**12), 0, 10**12)
        since_note = _int(request, "since_notification", 0, 0, 10**12)
        msg_sql = ("SELECT m.*, i.read_at FROM messages m JOIN inbox i ON i.message_id = m.id "
                   "WHERE i.recipient = ?")
        if since_msg:
            rows = swarm.db.all(f"{msg_sql} AND m.id > ? ORDER BY m.id ASC LIMIT {INBOX_PAGE}", (USER, since_msg))
        else:
            rows = swarm.db.all(f"{msg_sql} ORDER BY m.id DESC LIMIT {INBOX_PAGE}", (USER,))[::-1]
        msgs = [{"id": r["id"], "ts": r["ts"], "sender": r["sender"], "recipients": loads(r["recipients"], []),
                 "text": r["text"], "reply_to": r["reply_to"], "read": r["read_at"] is not None} for r in rows]
        note_sql = "SELECT id, ts, kind, text FROM notifications WHERE recipient = ?"
        if since_note:
            notes = swarm.db.all(f"{note_sql} AND id > ? ORDER BY id ASC LIMIT {INBOX_PAGE}", (USER, since_note))
        else:
            notes = swarm.db.all(f"{note_sql} ORDER BY id DESC LIMIT {INBOX_PAGE}", (USER,))[::-1]
        return JSONResponse({"messages": msgs, "notifications": [dict(n) for n in notes]})

    async def mark_read(request: Request):
        body = await _json(request)
        other = body.get("with") if isinstance(body, dict) else None
        now = time.time()
        if other:
            swarm.db.execute(
                "UPDATE inbox SET read_at = ? WHERE recipient = ? AND read_at IS NULL AND message_id IN "
                "(SELECT id FROM messages WHERE sender = ?)", (now, USER, other))
        else:
            swarm.db.execute("UPDATE inbox SET read_at = ? WHERE recipient = ? AND read_at IS NULL", (now, USER))
        return JSONResponse({"ok": True, "unread": swarm.mail.unread_count(USER)})

    async def events(request: Request):
        types = tuple(t for t in request.query_params.get("types", "").split(",") if t) or None
        items = swarm.bus.recent(since=_int(request, "since", 0, 0, 10**12), limit=_int(request, "limit", 200, 1, 2000),
                                 types=types)
        return JSONResponse({"events": items})

    async def activity(request: Request):
        name = request.path_params["name"]
        limit = _int(request, "limit", 200, 1, 2000)
        rows = swarm.db.all(
            "SELECT * FROM events WHERE type = 'agent.activity' AND actor = ? ORDER BY id DESC LIMIT ?", (name, limit))
        items = [{"id": r["id"], "ts": r["ts"], "data": loads(r["data"], {})} for r in reversed(rows)]
        return JSONResponse({"activity": items})

    async def files(request: Request):
        path = request.query_params.get("path") or "."
        try:
            target = swarm.workspace.resolve(path)
            if target.is_dir():
                return JSONResponse({"type": "dir", "path": path, "entries": swarm.workspace.tree(path, depth=1)})
            text, truncated = swarm.workspace.read_raw(path)
            return JSONResponse({"type": "file", "path": path, "content": text, "truncated": truncated})
        except ToolError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)

    async def act(request: Request):
        tool_name = request.path_params["tool"]
        body = await _json(request)
        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "result": "Body must be a JSON object."}, status_code=400)
        text, is_error = await call_tool(ToolContext(swarm, USER), tool_name, body)
        # these may have changed the library (a proposal saved, a source imported first): the home page reloads it
        if not is_error and on_library_change is not None and tool_name in LIBRARY_TOOLS:
            on_library_change()
        return JSONResponse({"ok": not is_error, "result": text})

    async def ws_endpoint(websocket: WebSocket):
        await websocket.accept()
        queue = swarm.bus.subscribe()

        async def pump():
            while True:
                event = await queue.get()
                await websocket.send_text(json_text(event))

        sender = asyncio.create_task(pump())
        try:
            while True:
                await websocket.receive_text()
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            sender.cancel()
            swarm.bus.unsubscribe(queue)
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sender

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with endpoint.manager.run():
            yield

    routes = [
        Route("/", index),
        Route("/api/state", state),
        Route("/api/roles", role_catalog),
        Route("/api/hub", hub_info),
        Route("/api/tasks/{task_id:int}", task_detail),
        Route("/api/messages", messages),
        Route("/api/inbox", inbox),
        Route("/api/read", mark_read, methods=["POST"]),
        Route("/api/events", events),
        Route("/api/agents/{name}/activity", activity),
        Route("/api/files", files),
        Route("/api/act/{tool}", act, methods=["POST"]),
        *skill_routes(lambda: swarm.config, lambda request: swarm.workspace.root, via="act", swarm=swarm,
                      changed=on_library_change),
        *trace_routes(swarm),
        WebSocketRoute("/ws", ws_endpoint),
        Route("/mcp", endpoint),
        Route("/mcp/", endpoint),
        Mount("/static", UIStaticFiles(directory=STATIC_DIR, check_dir=False), name="static"),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.add_middleware(GuardMiddleware, host=host, extra_hosts=extra_hosts)
    return app


async def _json(request: Request) -> Any:
    try:
        return await request.json()
    except Exception:
        return {}


def display_host(host: str) -> str:
    """Host part of a URL that reaches a server bound to `host` from this machine."""
    if host in ("0.0.0.0", ""):
        return "127.0.0.1"
    if host == "::":
        return "[::1]"
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def public_base(host: str, port: int) -> str:
    """Base URL users open for a hub bound to host:port (DREAMTEAM_PUBLIC_URL wins, e.g. in Docker)."""
    env = os.environ.get(PUBLIC_URL_ENV, "").strip().rstrip("/")
    return env or f"http://{display_host(host)}:{port}"


class HubServer(uvicorn.Server):
    """uvicorn server that leaves signals to the caller and reports a failed start as OSError.

    Plain uvicorn captures SIGINT/SIGTERM/SIGBREAK itself and re-raises them after it stopped, which with
    the default SIGTERM/SIGBREAK handlers kills the process before `swarm.stop()` can run; and it calls
    sys.exit() when the port is taken, which escapes the event loop from a background task."""

    def __init__(self, config: uvicorn.Config, on_started: Callable[[], Any] | None = None):
        super().__init__(config)
        self._on_started = on_started

    @contextlib.contextmanager
    def capture_signals(self):
        yield

    async def startup(self, sockets=None) -> None:
        try:
            await super().startup(sockets=sockets)
        except SystemExit:
            raise OSError(f"Cannot serve on {self.config.host}:{self.config.port} "
                          "(is the port already in use?)") from None
        if self._on_started is not None and not self.should_exit:
            self._on_started()


async def serve(swarm: "Swarm", host: str, port: int, log_level: str = "warning", *,
                on_started: Callable[[], Any] | None = None) -> None:
    """Run the web server until the task is cancelled; then shut it down gracefully (listening socket
    closed, connections ended, lifespan finished) before re-raising. Raises OSError if it cannot listen.
    `on_started` is called once the port is open."""
    app = create_app(swarm, host)
    config = uvicorn.Config(app, host=host, port=port, log_level=log_level, access_log=False,
                            log_config=None, lifespan="on", ws="auto", timeout_graceful_shutdown=GRACEFUL_SECONDS)
    server = HubServer(config, on_started)
    swarm.hub_url = public_base(host, port) + "/mcp"
    task = asyncio.ensure_future(server.serve())
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        server.should_exit = True
        try:
            await asyncio.wait({task}, timeout=GRACEFUL_SECONDS + 3)
        finally:
            if not task.done():
                server.force_exit = True
                task.cancel()
        raise
    finally:
        swarm.hub_url = None
