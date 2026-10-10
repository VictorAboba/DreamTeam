"""Async client of a DreamTeam hub (REST + WebSocket) and the connection the TUI works through
(docs/TUI.md §1).

`connect(home)` attaches to the hub already serving the home, or starts the launcher in this process (in the
current event loop) and talks to it over localhost exactly like to an external hub.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import json
import logging
import socket
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable

import httpx2
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

if TYPE_CHECKING:
    from ..sessions import SessionManager

log = logging.getLogger(__name__)

BACKOFF_MIN = 0.5          # first reconnect delay of stream(), doubled up to BACKOFF_MAX
BACKOFF_MAX = 10.0
RECONNECT_NOW = (1001, 1012)   # going away / session restarted: reconnect without waiting...
RECONNECT_NOW_DELAY = 0.05     # ...almost: a server that keeps closing must not spin the loop
ACT_TIMEOUT = 660.0        # tools may block (wait_for_events: up to 600 s)
SESSION_TIMEOUT = 120.0    # creating / starting / stopping a session starts or stops a whole swarm
MOVE_TIMEOUT = 900.0       # moving a session's folder: a stop, the files (copied across drives), a start
START_TIMEOUT = 120.0      # the in-process launcher must listen within this (it resumes sessions first)
HUB_WAIT = 2.0             # the configured port is busy: how long a hub of this home may take to show up
HUB_POLL = 0.2
WS_MAX_SIZE = 32 * 2**20
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
_WS_PROXY_OPTION = "proxy" in inspect.signature(ws_connect.__init__).parameters  # websockets >= 15


class HubError(Exception):
    """A failed hub call: `status` is the HTTP status (0 = the hub is not reachable), `message` the hub's
    `{"error": ...}` text when it sent one; `data` is the parsed JSON body, if any."""

    def __init__(self, status: int, message: str, data: Any = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.data = data

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.status}, {self.message!r})"


class HubUnreachable(HubError):
    """The hub did not answer at all (refused, reset, timed out): status 0."""


def _error_text(resp: httpx2.Response, data: Any) -> str:
    if isinstance(data, dict):
        for key in ("error", "result"):
            if isinstance(data.get(key), str) and data[key]:
                return data[key]
    text = (resp.text or "").strip()
    return text[:300] if text else f"HTTP {resp.status_code} {resp.reason_phrase}".strip()


def _connection_event(state: str) -> dict[str, Any]:
    return {"type": "_connection", "data": {"state": state}}


def _is_loopback(url: str) -> bool:
    try:
        return (urllib.parse.urlsplit(url).hostname or "") in _LOOPBACK
    except ValueError:
        return False


class HubClient:
    """Async client of a DreamTeam hub (launcher or single-swarm `dreamteam run`)."""

    def __init__(self, base_url: str, *, timeout: float = 30.0, http: httpx2.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.mode = ""   # "launcher" | "swarm" after hub_info()
        self._loopback = _is_loopback(self.base_url)
        self._own_http = http is None
        # a hub on this machine never goes through the proxy of the environment
        self.http = http or httpx2.AsyncClient(timeout=timeout, trust_env=not self._loopback)
        self._closed = False
        self._closed_event = asyncio.Event()
        self._sockets: set[Any] = set()

    @property
    def closed(self) -> bool:
        return self._closed

    async def aclose(self) -> None:
        """Close the HTTP pool and end every stream() of this client."""
        if self._closed:
            return
        self._closed = True
        self._closed_event.set()
        for ws in list(self._sockets):
            with contextlib.suppress(Exception):
                await ws.close()
        if self._own_http:
            await self.http.aclose()

    async def __aenter__(self) -> "HubClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # ---- plumbing ------------------------------------------------------------------------------
    async def _call(self, method: str, path: str, *, params: dict[str, Any] | None = None, body: Any = None,
                    timeout: float | None = None) -> Any:
        if self._closed:
            raise HubError(0, "The hub client is closed.")
        kwargs: dict[str, Any] = {}
        if params:
            kwargs["params"] = {k: v for k, v in params.items() if v is not None}
        if body is not None:
            kwargs["json"] = body  # also sets Content-Type: application/json (the hub requires it on POST)
        if timeout is not None:
            kwargs["timeout"] = max(timeout, self.timeout)
        try:
            resp = await self.http.request(method, self.base_url + path, **kwargs)
        except (httpx2.HTTPError, OSError) as exc:
            raise HubUnreachable(0, f"The hub at {self.base_url} is not reachable: "
                                    f"{str(exc) or type(exc).__name__}") from None
        try:
            data = resp.json()
        except ValueError:
            data = None
        if resp.status_code >= 400:
            raise HubError(resp.status_code, _error_text(resp, data), data)
        return data

    async def _get(self, url_path: str, /, **params: Any) -> Any:
        return await self._call("GET", url_path, params=params)

    def session_base(self, sid: str | None) -> str:
        return "" if sid is None else "/s/" + urllib.parse.quote(str(sid), safe="")

    # ---- hub / launcher level ------------------------------------------------------------------
    async def hub_info(self) -> dict:
        data = await self._get("/api/hub")
        if not isinstance(data, dict):
            raise HubError(0, f"{self.base_url} is not a DreamTeam hub.")
        self.mode = "launcher" if data.get("mode") == "launcher" else "swarm"
        return data

    async def sessions(self) -> list[dict]:
        return (await self._get("/api/sessions")).get("sessions", [])

    async def session(self, sid: str) -> dict:
        """GET /api/sessions/{sid}: {"session": summary, "config": public view of its settings}."""
        return await self._get(f"/api/sessions/{urllib.parse.quote(sid, safe='')}")

    async def create_session(self, title: str, goal: str, description: str = "", overrides: dict | None = None,
                             start: bool = True, workspace: str | None = None) -> dict:
        """POST /api/sessions -> the new session's summary. `workspace`: an absolute folder the session works in
        (docs/SPEC.md §17.2); the summary then has "workspace_result" (the hub's "result")."""
        body = {"title": title, "goal": goal, "description": description, "overrides": overrides or {},
                "start": start}
        if workspace:
            body["workspace"] = workspace
        data = await self._call("POST", "/api/sessions", body=body,
                                timeout=MOVE_TIMEOUT if workspace else SESSION_TIMEOUT)
        summary = data["session"]
        if isinstance(data.get("result"), dict):
            summary = {**summary, "workspace_result": data["result"]}
        return summary

    async def _session_action(self, sid: str, action: str) -> dict:
        path = f"/api/sessions/{urllib.parse.quote(sid, safe='')}/{action}"
        return (await self._call("POST", path, body={}, timeout=SESSION_TIMEOUT))["session"]

    async def start_session(self, sid: str) -> dict:
        return await self._session_action(sid, "start")

    async def stop_session(self, sid: str) -> dict:
        return await self._session_action(sid, "stop")

    async def delete_session(self, sid: str) -> Any:
        """DELETE /api/sessions/{sid}?confirm={sid}: stop the session and delete its directory. The hub's answer:
        {"ok": true} plus "workspace_kept" for a folder outside the session (it stays)."""
        return await self._call("DELETE", f"/api/sessions/{urllib.parse.quote(sid, safe='')}",
                                params={"confirm": sid}, timeout=SESSION_TIMEOUT)

    async def set_workspace(self, sid: str, path: str | None, expect: str | None = None) -> dict:
        """POST /api/sessions/{sid}/workspace: make the session work in the absolute folder `path` (None: back to
        its own folder). An empty or missing folder gets its files, one with files is adopted; a running session
        is stopped and started again. `expect` ("moved" | "adopted"): what the user agreed to (409 when the folder
        turned out otherwise). -> {"session": summary, "result": {"mode", "from", "to", "git", "mcp_moved",
        "old_kept", ...}} (docs/SPEC.md §17.2)."""
        body: dict = {"default": True} if path is None else {"path": path}
        if expect:
            body["expect"] = expect
        return await self._call("POST", f"/api/sessions/{urllib.parse.quote(sid, safe='')}/workspace", body=body,
                                timeout=MOVE_TIMEOUT)

    async def settings(self) -> dict:
        return await self._get("/api/settings")

    async def update_settings(self, settings: dict) -> dict:
        return await self._call("PUT", "/api/settings", body={"settings": settings})

    # ---- session level (sid=None: the hub itself is one swarm) ----------------------------------
    async def state(self, sid: str | None) -> dict:
        return await self._get(f"{self.session_base(sid)}/api/state")

    async def roles(self, sid: str | None) -> dict:
        """GET /api/roles: {"language", "roles": [{key, name, description, instructions, model, builtin}]}, the
        visible role catalog in the swarm's language (docs/SPEC.md §17.3)."""
        return await self._get(f"{self.session_base(sid)}/api/roles")

    async def act(self, sid: str | None, tool: str, args: dict | None = None) -> tuple[bool, str]:
        """Run a tool as the user: (ok, result text). A refused call that still answers in the act shape
        (a stopped session: 409, a non-object body: 400) is (False, text); other failures raise HubError."""
        path = f"{self.session_base(sid)}/api/act/{urllib.parse.quote(tool, safe='')}"
        try:
            data = await self._call("POST", path, body=args or {}, timeout=ACT_TIMEOUT)
        except HubError as exc:
            if isinstance(exc.data, dict) and "result" in exc.data:
                return False, str(exc.data.get("result") or exc.message)
            raise
        if not isinstance(data, dict):
            raise HubError(0, f"Unexpected answer to {tool}.")
        return bool(data.get("ok")), str(data.get("result", ""))

    async def inbox(self, sid: str | None, since_message: int = 0, since_notification: int = 0) -> dict:
        return await self._get(f"{self.session_base(sid)}/api/inbox", since_message=since_message or None,
                               since_notification=since_notification or None)

    async def messages(self, sid: str | None, with_: str | None = None, limit: int = 50) -> list[dict]:
        data = await self._get(f"{self.session_base(sid)}/api/messages", limit=limit, **{"with": with_ or None})
        return data.get("messages", [])

    async def mark_read(self, sid: str | None, with_: str | None = None) -> None:
        await self._call("POST", f"{self.session_base(sid)}/api/read", body={"with": with_} if with_ else {})

    async def task(self, sid: str | None, task_id: int) -> dict:
        return await self._get(f"{self.session_base(sid)}/api/tasks/{int(task_id)}")

    async def files(self, sid: str | None, path: str = ".") -> dict:
        return await self._get(f"{self.session_base(sid)}/api/files", path=path)

    async def events(self, sid: str | None, since: int = 0, limit: int = 200,
                     types: list[str] | None = None) -> list[dict]:
        data = await self._get(f"{self.session_base(sid)}/api/events", since=since or None, limit=limit,
                               types=",".join(types) if types else None)
        return data.get("events", [])

    async def traces(self, sid: str | None, **filters: Any) -> list[dict]:
        """Trace summaries; filters: agent, status, task, kind, limit, before."""
        return (await self._get(f"{self.session_base(sid)}/api/traces", **filters)).get("traces", [])

    async def trace(self, sid: str | None, trace_id: int, payload: int = 20000) -> dict:
        return await self._get(f"{self.session_base(sid)}/api/traces/{int(trace_id)}", payload=payload)

    async def span(self, sid: str | None, span_id: int) -> dict:
        return await self._get(f"{self.session_base(sid)}/api/spans/{int(span_id)}")

    async def span_context(self, sid: str | None, span_id: int) -> dict:
        return await self._get(f"{self.session_base(sid)}/api/spans/{int(span_id)}/context")

    async def trace_stats(self, sid: str | None, agent: str | None = None) -> dict:
        return await self._get(f"{self.session_base(sid)}/api/traces/stats", agent=agent)

    # ---- live events ---------------------------------------------------------------------------
    def ws_url(self, sid: str | None) -> str:
        parts = urllib.parse.urlsplit(self.base_url)
        scheme = "wss" if parts.scheme == "https" else "ws"
        return urllib.parse.urlunsplit((scheme, parts.netloc, parts.path.rstrip("/") + self.session_base(sid) + "/ws",
                                        "", ""))

    async def _pause(self, delay: float) -> None:
        """Sleep `delay` seconds, or less if the client gets closed."""
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._closed_event.wait(), delay)

    async def stream(self, sid: str | None) -> AsyncIterator[dict]:
        """Bus events of the session (or of a single-swarm hub) as the server sends them, forever.

        Reconnects with exponential backoff (BACKOFF_MIN .. BACKOFF_MAX); close codes 1012 (session
        restarted) and 1001 reconnect at once. Yields {"type": "_connection", "data": {"state": ...}}
        with "connected" on every (re)connect and "lost" when the connection goes (or cannot be made);
        "rejected" (with "status") follows when the hub answers the handshake with an HTTP error instead of
        a stream, e.g. 403 for a session that no longer exists: the caller may check over REST whether
        retrying makes sense. Ends when the iterator is closed or the client is closed."""
        url = self.ws_url(sid)
        options: dict[str, Any] = {"open_timeout": 10, "close_timeout": 2, "max_size": WS_MAX_SIZE}
        if self._loopback and _WS_PROXY_OPTION:
            options["proxy"] = None
        delay = BACKOFF_MIN
        state: str | None = None
        while not self._closed:
            connected = False
            code: int | None = None
            refused: int | None = None
            try:
                async with ws_connect(url, **options) as ws:
                    if self._closed:
                        return
                    self._sockets.add(ws)
                    try:
                        connected, state, delay = True, "connected", BACKOFF_MIN
                        yield _connection_event("connected")
                        async for raw in ws:
                            try:
                                event = json.loads(raw)
                            except ValueError:
                                continue
                            if isinstance(event, dict):
                                yield event
                    finally:
                        self._sockets.discard(ws)
                    code = ws.close_code
            except ConnectionClosed as exc:
                code = exc.rcvd.code if exc.rcvd is not None else None
            except InvalidStatus as exc:  # the hub answered, but not with a stream
                refused = exc.response.status_code
                log.debug("stream %s: HTTP %s", url, refused)
            except Exception as exc:  # refused, timeout, protocol error...
                log.debug("stream %s: %s", url, exc)
            if self._closed:
                return
            if state not in ("lost", "rejected"):
                state = "lost"
                yield _connection_event("lost")
            if refused is not None and state != "rejected":
                state = "rejected"
                yield {"type": "_connection", "data": {"state": "rejected", "status": refused}}
            if connected and code in RECONNECT_NOW:
                await self._pause(RECONNECT_NOW_DELAY)
                continue
            await self._pause(delay)
            delay = min(delay * 2, BACKOFF_MAX)


# ---- connection ---------------------------------------------------------------------------------
@dataclass
class Connection:
    client: HubClient
    base_url: str                     # e.g. http://127.0.0.1:8765
    home: Path
    owned: bool                       # True when this process started the launcher
    manager: "SessionManager | None" = None   # set when owned
    _server: asyncio.Task | None = field(default=None, repr=False)
    _hub_file: bool = field(default=False, repr=False)
    _marker: Path | None = field(default=None, repr=False)   # cli.write_launcher_marker of an owned launcher
    _closed: bool = field(default=False, repr=False)

    @property
    def server_running(self) -> bool:
        """For an owned connection: the in-process web server is still up."""
        return self._server is not None and not self._server.done()

    async def close(self) -> None:
        """Close the client; if owned, stop the in-process web server and the sessions (they stay marked
        for resume) and remove the hub.json this process wrote."""
        if self._closed:
            return
        self._closed = True
        try:
            await self.client.aclose()
        finally:
            server = self._server
            if server is not None:
                if not server.done():
                    server.cancel()  # serve_launcher shuts the web server and every session down
                await asyncio.gather(server, return_exceptions=True)
                from .. import cli

                if self._hub_file:
                    cli._remove_hub_file(self.home)
                cli.remove_launcher_marker(self._marker)


def _free_port(host: str = "127.0.0.1") -> int:
    with socket.socket() as s:
        s.bind((host, 0))
        return s.getsockname()[1]


async def _wait_started(server: asyncio.Task, started: asyncio.Event, timeout: float) -> bool:
    waiter = asyncio.ensure_future(started.wait())
    try:
        await asyncio.wait({server, waiter}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        waiter.cancel()
    return started.is_set() and not server.done()


async def _stop_task(task: asyncio.Task) -> BaseException | None:
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    return None if task.cancelled() else task.exception()


async def connect(home: Path | str, *, host: str | None = None, port: int | None = None, start: bool = True,
                  web: bool = True, llm_factory: Callable | None = None, resume: bool | None = None) -> Connection:
    """Connect to the hub of `home`, starting the launcher in this process when none runs.

    1. A hub already serving `home` (cli.running_hub) -> owned=False.
    2. Else, with start=True: SessionManager + launcher.serve_launcher as a task of the current loop, on
       host/port (defaults: the home settings) -> owned=True. hub.json is written as soon as the server
       listens, before the sessions resume, so that another `dreamteam` finds this hub meanwhile. When the
       configured port is busy (or is lost in a race), a hub of this home that shows up within HUB_WAIT is
       used instead (owned=False); else a free ephemeral port on 127.0.0.1. web=False binds 127.0.0.1 on an
       ephemeral port and writes no hub.json. `resume` overrides server.resume_sessions of the settings.
    3. Else HubError(0, ...)."""
    from .. import cli

    home = Path(home).expanduser().resolve()
    base = await asyncio.to_thread(cli.running_hub, home)
    if base:
        return await _attach(home, base)
    if not start:
        raise HubError(0, f"No DreamTeam hub is running for {home}.")
    return await _start_launcher(home, host=host, port=port, web=web, llm_factory=llm_factory, resume=resume)


async def _attach(home: Path, base: str) -> Connection:
    client = HubClient(base)
    try:
        await client.hub_info()
    except BaseException:
        await client.aclose()
        raise
    return Connection(client, base, home, owned=False)


async def _wait_for_hub(home: Path, timeout: float | None = None) -> str | None:
    """The configured port is taken: another `dreamteam` may be starting the launcher of this home right now
    (it writes hub.json as soon as it listens). Its hub, if one shows up within `timeout` (HUB_WAIT)."""
    from .. import cli

    loop = asyncio.get_running_loop()
    begin = loop.time()
    timeout = HUB_WAIT if timeout is None else timeout
    while True:
        base = await asyncio.to_thread(cli.running_hub, home)
        elapsed = loop.time() - begin
        if base or elapsed >= timeout:
            return base
        if elapsed >= 3 * HUB_POLL and not cli.hub_file(home).exists():
            return None  # no hub of this home is coming: the port belongs to something else
        await asyncio.sleep(HUB_POLL)


async def _start_launcher(home: Path, *, host: str | None, port: int | None, web: bool,
                          llm_factory: Callable | None, resume: bool | None) -> Connection:
    from .. import cli
    from ..launcher import serve_launcher
    from ..sessions import SessionManager
    from ..web import display_host, public_base

    try:
        manager = SessionManager(home, llm_factory=llm_factory)
    except (ValueError, OSError) as exc:
        raise HubError(0, f"Invalid settings in {home}: {exc}") from None
    marker = cli.write_launcher_marker(home)  # the sessions of this home run here (also without the web UI)
    configured: tuple[str, int] | None = None
    if web:
        bind_port = manager.config.server.port if port is None else port
        if bind_port:
            configured = (host or manager.config.server.host, bind_port)
    hub_written = False

    def listening(base: str) -> None:
        nonlocal hub_written
        hub_written = True  # first: whatever fails later removes the file
        cli._write_hub_file(home, base, manager.config)

    async def launch(bind_host: str, bind_port: int) -> tuple[asyncio.Task | None, BaseException | None]:
        nonlocal hub_written
        started = asyncio.Event()
        on_listening = functools.partial(listening, public_base(bind_host, bind_port)) if web else None
        task = asyncio.create_task(
            serve_launcher(manager, bind_host, bind_port, on_started=started.set, on_listening=on_listening,
                           resume=resume),
            name="dreamteam-launcher")
        if await _wait_started(task, started, START_TIMEOUT):
            return task, None
        error = await _stop_task(task) or TimeoutError("the web server did not start in time")
        if hub_written:
            cli._remove_hub_file(home)
            hub_written = False
        return None, error

    server: asyncio.Task | None = None
    error: BaseException | None = None
    chosen: tuple[str, int] | None = None
    try:
        if configured is not None:
            if cli._port_free(*configured):
                server, error = await launch(*configured)
                chosen = configured
            if server is None:  # busy, or lost the race for it
                other = await _wait_for_hub(home)
                if other:
                    cli.remove_launcher_marker(marker)
                    return await _attach(home, other)
        for _ in range(3):  # an ephemeral port can be taken between the probe and the bind: retry
            if server is not None:
                break
            chosen = ("127.0.0.1", _free_port())
            server, error = await launch(*chosen)
        if server is None or chosen is None:
            raise HubError(0, f"The DreamTeam web server could not start: {error}")
    except BaseException:
        if server is not None:
            await _stop_task(server)
        if hub_written:
            cli._remove_hub_file(home)
        cli.remove_launcher_marker(marker)
        raise

    bind_host, bind_port = chosen
    base = f"http://{display_host(bind_host)}:{bind_port}"
    conn = Connection(HubClient(base), base, home, owned=True, manager=manager, _server=server,
                      _hub_file=hub_written, _marker=marker)
    try:
        await conn.client.hub_info()
    except BaseException as exc:
        await conn.close()
        if isinstance(exc, Exception) and not isinstance(exc, HubError):
            raise HubError(0, f"The DreamTeam hub could not be set up: {exc}") from exc
        raise
    return conn


__all__ = ["HubError", "HubUnreachable", "HubClient", "Connection", "connect"]
