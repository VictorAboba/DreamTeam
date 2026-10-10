"""Launcher web server (docs/SPEC.md §18.1).

`/` is the sessions page with the global settings. Each session's board UI and API (the per-swarm app of
web.create_app) is served under `/s/{sid}/`. `/mcp` is the launcher MCP. A stopped session can be
viewed (its swarm is loaded without starting) but not changed.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import os
import re
from typing import TYPE_CHECKING, Any, Callable

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import __version__, roles
from .httpjson import JSONResponse, json_text
from .launcher_mcp import LauncherMCPEndpoint
from .sessions import SessionError
from .settings import PROVIDER_CHOICES, check_profile, profile_from_view, settings_path
from .web import (
    GRACEFUL_SECONDS, STATIC_DIR, GuardMiddleware, HubServer, UIStaticFiles, create_app, env_allowed_hosts,
    public_base,
)

if TYPE_CHECKING:
    from .sessions import SessionManager
    from .swarm import Swarm

log = logging.getLogger(__name__)

SESSION_PATH = re.compile(r"^/s/([a-z0-9][a-z0-9-]{0,63})(/.*)?$")
STOPPED_TEXT = "The session is stopped. Start it on the sessions page."
ERROR_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>DreamTeam</title>
<style>body{font-family:system-ui,sans-serif;background:#0f1115;color:#e6e6e6;display:grid;place-items:center;
height:100vh;margin:0}a{color:#7aa2ff}</style></head><body><div><h2>{title}</h2>
<p>{text} <a href="/">Back to sessions</a></p></div></body></html>"""


def _error_page(title: str, text: str, status: int) -> HTMLResponse:
    return HTMLResponse(ERROR_HTML.replace("{title}", html.escape(title)).replace("{text}", html.escape(text)),
                        status_code=status)


def _is_json_request(scope) -> bool:
    for key, value in scope.get("headers", []):
        if key.lower() == b"content-type":
            return value.decode("latin-1").strip().lower().startswith("application/json")
    return False


class _SessionApp:
    """A per-session app (web.create_app) with its lifespan (the session MCP manager) running in its own
    task, so that it is entered and exited in the same task (anyio requirement)."""

    def __init__(self, swarm: "Swarm", app: Starlette):
        self.swarm = swarm
        self.app = app
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.sockets: set[Callable] = set()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="session-app")
        waiter = asyncio.ensure_future(self._ready.wait())
        try:
            await asyncio.wait({self._task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        except BaseException:  # the request was cancelled meanwhile: the lifespan must not outlive it
            self._stop.set()
            self._task.cancel()
            raise
        finally:
            waiter.cancel()
        if self._task.done():
            self._task.result()  # raise the startup error

    async def _run(self) -> None:
        async with self.app.router.lifespan_context(self.app):
            self._ready.set()
            await self._stop.wait()

    async def close(self) -> None:
        for send in list(self.sockets):  # ask the browsers to reconnect (to the new swarm object)
            with contextlib.suppress(Exception):
                await send({"type": "websocket.close", "code": 1012})
        self.sockets.clear()
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError, Exception):
                await asyncio.wait_for(self._task, timeout=GRACEFUL_SECONDS + 2)


class SessionRouter:
    """ASGI dispatcher: `/s/{sid}/...` goes to that session's app, everything else to `fallback`."""

    def __init__(self, manager: "SessionManager", host: str, fallback):
        self.manager = manager
        self.host = host
        self.fallback = fallback
        self.apps: dict[str, _SessionApp] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._closing: set[asyncio.Task] = set()
        self._closed = False
        manager.listen(self._on_change)

    def _on_change(self, event: dict[str, Any]) -> None:
        sid = event.get("session")
        if sid and event.get("change") in ("stopped", "deleted", "started"):
            entry = self.apps.get(sid)
            if entry is not None and self.manager.swarms.get(sid) is not entry.swarm:
                self.apps.pop(sid, None)
                self._close_later(entry)
            elif entry is not None and event.get("change") == "started":
                self._close_sockets_later(entry)  # same swarm object, now running: let browsers reload state

    def _close_later(self, entry: _SessionApp) -> None:
        try:
            task = asyncio.get_running_loop().create_task(entry.close())
        except RuntimeError:
            return
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    def _close_sockets_later(self, entry: _SessionApp) -> None:
        async def close_sockets():
            for send in list(entry.sockets):
                with contextlib.suppress(Exception):
                    await send({"type": "websocket.close", "code": 1012})
            entry.sockets.clear()

        with contextlib.suppress(RuntimeError):
            task = asyncio.get_running_loop().create_task(close_sockets())
            self._closing.add(task)
            task.add_done_callback(self._closing.discard)

    async def app_for(self, sid: str) -> _SessionApp:
        lock = self._locks.setdefault(sid, asyncio.Lock())
        async with lock:
            if self._closed:  # a request still in flight at shutdown: no new session app (nobody would close it)
                raise SessionError("DreamTeam is shutting down.", 503)
            swarm = await self.manager.load(sid)
            entry = self.apps.get(sid)
            if entry is None or entry.swarm is not swarm:
                if entry is not None:
                    self._close_later(entry)
                entry = _SessionApp(swarm, create_app(swarm, self.host))
                await entry.start()
                self.apps[sid] = entry
            return entry

    async def aclose(self) -> None:
        self._closed = True
        self.manager.unlisten(self._on_change)
        entries = list(self.apps.values())
        self.apps.clear()
        for entry in entries:
            await entry.close()
        if self._closing:
            await asyncio.gather(*self._closing, return_exceptions=True)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.fallback(scope, receive, send)
            return
        match = SESSION_PATH.match(scope.get("path", ""))
        if match is None:
            await self.fallback(scope, receive, send)
            return
        sid, rest = match.group(1), match.group(2)
        if scope["type"] == "http" and rest is None:
            query = scope.get("query_string", b"").decode("latin-1")
            await RedirectResponse(f"/s/{sid}/" + (f"?{query}" if query else ""), status_code=307)(scope, receive, send)
            return
        rest = rest or ""
        api = rest.startswith("/api/") or rest.startswith("/mcp")
        # GuardMiddleware (here and in the session app) only knows the paths `/api/...`: the JSON-only POST rule
        # (no cross-site form or text/plain posts) must hold for `/s/{sid}/api/...` as well
        if (scope["type"] == "http" and scope.get("method") == "POST" and rest.startswith("/api/")
                and not _is_json_request(scope)):
            await PlainTextResponse("Content-Type must be application/json", status_code=415)(scope, receive, send)
            return
        if not self.manager.exists(sid):
            if scope["type"] == "http":
                if api:
                    await JSONResponse({"error": f"Unknown session '{sid}'."}, status_code=404)(scope, receive, send)
                else:
                    await _error_page("Session not found", f"There is no session '{sid}'.",
                                      404)(scope, receive, send)
            else:
                await send({"type": "websocket.close", "code": 1008})
            return
        running = sid in self.manager.running
        if scope["type"] == "http" and not running and (
                (scope.get("method") == "POST" and rest.startswith("/api/act/")) or rest.startswith("/mcp")):
            await JSONResponse({"ok": False, "result": STOPPED_TEXT, "error": STOPPED_TEXT},
                               status_code=409)(scope, receive, send)
            return
        try:
            entry = await self.app_for(sid)
        except SessionError as exc:
            if scope["type"] != "http":
                await send({"type": "websocket.close", "code": 1011})
            elif api:
                await JSONResponse({"error": str(exc)}, status_code=exc.status)(scope, receive, send)
            else:  # the board page of a damaged session: say why instead of showing raw JSON
                await _error_page("Cannot open the session", str(exc), exc.status)(scope, receive, send)
            return
        child = dict(scope)
        child["root_path"] = scope.get("root_path", "") + f"/s/{sid}"
        if scope["type"] == "websocket":
            entry.sockets.add(send)
            try:
                await entry.app(child, receive, send)
            finally:
                entry.sockets.discard(send)
        else:
            await entry.app(child, receive, send)


def _error(exc: Exception) -> JSONResponse:
    status = getattr(exc, "status", 400)
    return JSONResponse({"error": str(exc)}, status_code=status)


_BAD_JSON = object()


async def _json_body(request: Request) -> Any:
    """The parsed JSON body; a body that is not JSON at all is `_BAD_JSON` (web._json would make it `{}`, and an
    empty object is a valid request here: POST /api/sessions would create and start an untitled session)."""
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        return _BAD_JSON


def _failure(what: str, exc: Exception) -> JSONResponse:
    """An unexpected error (the swarm failed to start, a file is locked...) as JSON, so the UI can show it."""
    log.error("%s failed", what, exc_info=exc)
    return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)


def _body_error(body: Any, *, strings: tuple[str, ...] = (), objects: tuple[str, ...] = (),
                flags: tuple[str, ...] = ()) -> str | None:
    """Why a JSON body has the wrong shape (None if it is fine); absent and null fields are fine."""
    if body is _BAD_JSON:
        return "The body is not valid JSON."
    if not isinstance(body, dict):
        return "Body must be a JSON object."
    rules = ((strings, str, "a string"), (objects, dict, "an object"), (flags, bool, "true or false"))
    for names, kind, label in rules:
        for name in names:
            if body.get(name) is not None and not isinstance(body[name], kind):
                return f"'{name}' must be {label}."
    return None


def create_launcher_app(manager: "SessionManager", host: str = "127.0.0.1") -> GuardMiddleware:
    extra_hosts = env_allowed_hosts()
    endpoint = LauncherMCPEndpoint(manager, host, extra_hosts=extra_hosts)
    holder: dict[str, SessionRouter] = {}

    async def home(request: Request):
        return FileResponse(STATIC_DIR / "home.html", headers={"Cache-Control": "no-store"})

    async def hub_info(request: Request):
        return JSONResponse({"pid": os.getpid(), "project": str(manager.home), "home": str(manager.home),
                             "name": "DreamTeam", "version": __version__, "mode": "launcher"})

    async def sessions(request: Request):
        if request.method == "GET":
            return JSONResponse({"sessions": manager.list()})
        body = await _json_body(request)
        problem = _body_error(body, strings=("title", "goal", "description", "workspace"), objects=("overrides",),
                              flags=("start",))
        if problem:
            return JSONResponse({"error": problem}, status_code=400)
        try:
            summary = await manager.create(body.get("title") or "", body.get("goal") or "",
                                           body.get("description") or "", overrides=body.get("overrides") or {},
                                           start=body.get("start") is not False,
                                           workspace=body.get("workspace") or None)
        except (SessionError, ValueError) as exc:
            return _error(exc)
        except Exception as exc:  # e.g. the swarm failed to start: show it in the UI
            return _failure("session create", exc)
        result = summary.pop("workspace_result", None)
        return JSONResponse({"session": summary, **({"result": result} if result else {})}, status_code=201)

    async def session_item(request: Request):
        sid = request.path_params["sid"]
        try:
            if request.method == "GET":
                return JSONResponse({"session": manager.summary(sid), "config": manager.config_view(sid)})
            if request.method == "PATCH":
                body = await _json_body(request)
                problem = _body_error(body, strings=("title",), objects=("overrides",))
                if problem:
                    return JSONResponse({"error": problem}, status_code=400)
                summary = manager.update(sid, title=body.get("title"), overrides=body.get("overrides"))
                return JSONResponse({"session": summary})
            if request.method == "DELETE":
                if request.query_params.get("confirm") != sid:
                    return JSONResponse({"error": "Pass ?confirm=<session id> to delete a session."}, status_code=400)
                # a folder outside the session (the user's project, or one it was moved to) is never deleted; delete
                # names it as it was under the session's lock (a /cd of the session may have run just before)
                kept = await manager.delete(sid)
                return JSONResponse({"ok": True, **({"workspace_kept": kept} if kept else {})})
        except (SessionError, ValueError) as exc:
            return _error(exc)
        except Exception as exc:
            return _failure(f"session {sid} {request.method}", exc)
        return JSONResponse({"error": "Method not allowed"}, status_code=405)

    async def session_workspace(request: Request):
        """Move the session to another folder or adopt one: {"path": "<absolute>"} or {"default": true}."""
        sid = request.path_params["sid"]
        body = await _json_body(request)
        problem = _body_error(body, strings=("path", "expect"), flags=("default",))
        if problem:
            return JSONResponse({"error": problem}, status_code=400)
        if body.get("expect") not in (None, "moved", "adopted"):
            return JSONResponse({"error": "\"expect\" must be \"moved\" or \"adopted\"."}, status_code=400)
        if body.get("default") is True:
            path = None
        elif body.get("path"):
            path = body["path"]
        else:
            return JSONResponse({"error": "Body must be {\"path\": \"<absolute folder>\"} or {\"default\": true}."},
                                status_code=400)
        try:
            result = await manager.set_workspace(sid, path, expect=body.get("expect"))
            return JSONResponse({"session": manager.summary(sid), "result": result})
        except (SessionError, ValueError) as exc:
            return _error(exc)
        except Exception as exc:
            return _failure(f"session {sid} workspace", exc)

    async def session_action(request: Request):
        sid, action = request.path_params["sid"], request.path_params["action"]
        try:
            if action == "start":
                await manager.start(sid)
            elif action == "stop":
                await manager.stop(sid)
            else:
                return JSONResponse({"error": f"Unknown action '{action}'."}, status_code=404)
            return JSONResponse({"session": manager.summary(sid)})
        except (SessionError, ValueError) as exc:
            return _error(exc)
        except Exception as exc:
            log.exception("session %s %s failed", sid, action)
            return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)

    def role_views() -> dict[str, Any]:
        # the built-in roles (both languages) and the catalog they make with the stored [[roles]] entries
        return {"role_presets": roles.presets_view(), "role_catalog": roles.catalog_view(manager.config.roles)}

    async def settings(request: Request):
        if request.method == "GET":
            view = manager.settings_view()  # first: it picks up a hand-edited file into manager.config
            return JSONResponse({"settings": view, "providers": PROVIDER_CHOICES, **role_views(),
                                 "path": str(settings_path(manager.home)), "home": str(manager.home)})
        body = await _json_body(request)
        update = body.get("settings") if isinstance(body, dict) else None
        if not isinstance(update, dict):
            return JSONResponse({"error": "Body must be {\"settings\": {...}}."}, status_code=400)
        try:
            return JSONResponse({**await manager.update_settings(update), **role_views()})
        except (SessionError, ValueError) as exc:
            return _error(exc)
        except Exception as exc:  # e.g. the settings file is locked by an editor
            return _failure("settings update", exc)

    async def settings_check(request: Request):
        body = await _json_body(request)
        if not isinstance(body, dict) or not isinstance(body.get("profile"), dict) or \
                not isinstance(body.get("name") or "", str):
            return JSONResponse({"error": "Body must be {\"profile\": {...}, \"name\": ..., \"live\": bool}."},
                                status_code=400)
        try:
            profile = profile_from_view(manager.raw, body["profile"], body.get("name") or None)
            return JSONResponse(await check_profile(profile, live=bool(body.get("live"))))
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:
            return _failure("settings check", exc)

    async def ws_endpoint(websocket: WebSocket):
        await websocket.accept()
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)

        def on_change(event: dict[str, Any]) -> None:
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)

        manager.listen(on_change)

        async def pump():
            while True:
                await websocket.send_text(json_text(await queue.get()))

        sender = asyncio.create_task(pump())
        try:
            while True:
                await websocket.receive_text()
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            manager.unlisten(on_change)
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sender

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with endpoint.manager.run():
            try:
                yield
            finally:
                router = holder.get("router")
                if router is not None:
                    await router.aclose()

    routes = [
        Route("/", home),
        Route("/api/hub", hub_info),
        Route("/api/sessions", sessions, methods=["GET", "POST"]),
        Route("/api/sessions/{sid}", session_item, methods=["GET", "PATCH", "DELETE"]),
        Route("/api/sessions/{sid}/workspace", session_workspace, methods=["POST"]),
        Route("/api/sessions/{sid}/{action}", session_action, methods=["POST"]),
        Route("/api/settings", settings, methods=["GET", "PUT"]),
        Route("/api/settings/check", settings_check, methods=["POST"]),
        WebSocketRoute("/ws", ws_endpoint),
        Route("/mcp", endpoint),
        Route("/mcp/", endpoint),
        Mount("/static", UIStaticFiles(directory=STATIC_DIR, check_dir=False), name="static"),
    ]
    inner = Starlette(routes=routes, lifespan=lifespan)
    router = SessionRouter(manager, host, inner)
    holder["router"] = router

    # The router forwards "lifespan" (and every non-session path) to `inner`, whose lifespan runs the
    # launcher MCP manager and closes the session apps at shutdown.
    app = GuardMiddleware(router, host=host, extra_hosts=extra_hosts)
    app.session_router = router  # type: ignore[attr-defined]
    app.manager = manager  # type: ignore[attr-defined]
    return app


async def serve_launcher(manager: "SessionManager", host: str, port: int, log_level: str = "warning", *,
                         on_started: Callable[[], Any] | None = None, resume: bool | None = None,
                         on_listening: Callable[[], Any] | None = None) -> None:
    """Run the launcher until the task is cancelled, then shut the web server and every session down.
    Sessions that were running when the server last stopped are resumed (server.resume_sessions) once the port
    is ours: a second launcher that loses the race for the port must not run them too, even for a moment.
    `on_listening` is called as soon as the port is open (write hub.json there: other `dreamteam` commands then
    find this hub during a long resume instead of starting a second one), `on_started` after the resume, so
    callers see the resumed sessions running."""
    app = create_launcher_app(manager, host)
    config = uvicorn.Config(app, host=host, port=port, log_level=log_level, access_log=False,
                            log_config=None, lifespan="on", ws="auto", timeout_graceful_shutdown=GRACEFUL_SECONDS)
    listening = asyncio.Event()
    server = HubServer(config, listening.set)
    manager.hub_url = public_base(host, port)
    for sid, swarm in manager.swarms.items():
        swarm.hub_url = f"{manager.hub_url}/s/{sid}/mcp"
    task = asyncio.ensure_future(server.serve())
    try:
        waiter = asyncio.ensure_future(listening.wait())
        try:
            await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
        if listening.is_set() and not task.done():
            if on_listening is not None:
                on_listening()
            if manager.config.server.resume_sessions if resume is None else resume:
                await manager.resume()
            if on_started is not None:
                on_started()
        await asyncio.shield(task)
    except BaseException:  # cancelled (Ctrl+C), or on_started failed: the web server must not outlive this call
        if not task.done():
            server.should_exit = True
            try:
                await asyncio.wait({task}, timeout=GRACEFUL_SECONDS + 3)
            finally:
                if not task.done():
                    server.force_exit = True
                    task.cancel()
        raise
    finally:
        try:
            await manager.shutdown()
        finally:
            manager.hub_url = None
