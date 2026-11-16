"""The external HTTP API v1 (docs/SPEC.md §21): `ApiV1(manager, host)`.

The launcher dispatches every path `web.is_api_path` takes to `ApiV1.app` (§21.1 "Dispatch"), behind the guard's v1
branch, which has already said who calls (`scope[web.API_CALLER]`: "owner" or "api"). The app is a Starlette app of
its own: its routes, `redirect_slashes` off (a bare `/api/v1` is a JSON 404, never a 307) and an `HTTPException`
handler, so that a wrong path or method is JSON too. Every answer under `/api/v1/` is JSON; an error is
`{"error": text, "code": code}` with the code's status (CODES). This module also holds what every route uses: the JSON
body read up to JSON_MAX, the check of a body's or a query's fields (`unknown_field`), the mapping of the manager's
errors to codes with the redaction of paths for the API caller, and `aclose()`, which waits for the starts in flight
and never cancels them. It uses the manager in process, never the internal routes over HTTP, and never imports
dreamteam.tui."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Coroutine, Iterable, Mapping

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response
from starlette.routing import Route

from . import __version__, roles
from .config import native_absolute
from .copyfrom import CopyError
from .httpjson import JSONResponse
from .sessions import SessionError
from .skills import Library, SkillError
from .uitext import LANGS
from .web import API_CALLER, API_CALLERS, API_TOKEN_TEXT, BEARER_REALM

if TYPE_CHECKING:
    from .config import Config
    from .sessions import SessionManager

log = logging.getLogger(__name__)

API_VERSION = 1
JSON_MAX = 1 << 20          # bytes of a JSON body (413 too_large beyond them)
FILES_PER_SESSION = 20      # uploads one create takes (docs/SPEC.md §21.2 "Options": limits)
START_WAIT = 30.0           # seconds aclose() waits for the background tasks (starts) in flight; it never cancels them
FIELDS_SHOWN = 10           # unknown fields named in an unknown_field text
FIELD_CHARS = 60            # characters of each

# every v1 error code and its HTTP status (docs/SPEC.md §21.1 "Errors"): the guard's, then ApiV1's
CODES: dict[str, int] = {
    "token_required": 401, "api_disabled": 403, "forbidden_host": 403, "forbidden_origin": 403,
    "unsupported_media_type": 415,
    "bad_request": 400, "unknown_field": 400, "unknown_team": 400, "unknown_model": 400, "unknown_role": 400,
    "unknown_skill": 400, "unknown_folder": 400, "bad_name": 400,
    "forbidden_folder": 403,
    "not_found": 404,
    "method_not_allowed": 405,
    "conflict": 409, "earlier_version": 409, "file_conflict": 409, "folder_busy": 409, "folder_not_ready": 409,
    "not_running": 409, "budget_exhausted": 409,
    "too_large": 413,
    "too_many_uploads": 429, "too_many_sessions": 429,
    "internal": 500,
}
# the code of an error that has no code of its own, by its status; another 4xx is bad_request, anything else internal
STATUS_CODES = {400: "bad_request", 401: "token_required", 404: "not_found", 405: "method_not_allowed",
                409: "conflict", 413: "too_large", 415: "unsupported_media_type", 500: "internal"}
NOT_FOUND_TEXT = "No such route in the API (/api/v1): GET /api/v1/options lists what a client may choose."
# the errors whose texts and codes a route answers as they are (error_code; for the API caller through redact()): the
# manager's and the stores'. Any other exception is a 500 `internal` with no text of its own, whatever it carries.
CODED_ERRORS: tuple[type[Exception], ...] = (SessionError, CopyError, SkillError)


class ApiError(Exception):
    """A refusal of a v1 route (a helper's or the route's own): the route wrapper answers it as
    `{"error": text, "code": code, **extra}` with the code's status (CODES). `extra`: a code's own fields, named where
    docs/SPEC.md §21 describes that code."""

    def __init__(self, code: str, text: str, *, headers: Mapping[str, str] | None = None, **extra: Any):
        if code not in CODES:
            raise ValueError(f"not an API v1 code: {code!r}")
        super().__init__(text)
        self.code = code
        self.text = text
        self.headers = dict(headers or {})
        self.extra = extra

    def response(self) -> JSONResponse:
        return fail(self.code, self.text, headers=self.headers, **self.extra)


def fail(code: str, text: str, *, headers: Mapping[str, str] | None = None, **extra: Any) -> JSONResponse:
    """The JSON answer of an error: `{"error": text, "code": code, **extra}` with the status CODES gives the code."""
    return JSONResponse({"error": text, "code": code, **extra}, status_code=CODES[code], headers=dict(headers or {}))


def error_code(exc: BaseException) -> str:
    """The v1 code of an error the manager raised (docs/SPEC.md §21.1; one of CODED_ERRORS): its own `code` when v1
    knows it (SessionError, CopyError), `earlier_version` for a session made by an earlier version, else by its
    `status` (STATUS_CODES; another 4xx is `bad_request`); `internal` for anything without a status. A code decides the
    answer's status, which may differ from the error's own (`folder_not_ready` is 409 also where the internal routes
    answer 400)."""
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code in CODES:
        return code
    if isinstance(exc, SessionError) and exc.extra.get("earlier_version"):
        return "earlier_version"
    status = getattr(exc, "status", None)
    if isinstance(status, int) and not isinstance(status, bool):
        return STATUS_CODES.get(status) or ("bad_request" if 400 <= status < 500 else "internal")
    return "internal"


def _shown(name: Any) -> str:
    text = str(name)
    return repr(text if len(text) <= FIELD_CHARS else text[:FIELD_CHARS] + "…")


def check_fields(given: Iterable[Any], known: Iterable[str], *, where: str = "body") -> None:
    """400 `unknown_field` when `given` (a body's keys, a query's names) holds a field the route does not know
    (docs/SPEC.md §21.1 "Unknown fields"): a typo never passes silently. `where` names it in the text ("body",
    "query")."""
    allowed = set(known)
    unknown = sorted({str(name) for name in given if name not in allowed})
    if not unknown:
        return
    named = ", ".join(_shown(name) for name in unknown[:FIELDS_SHOWN]) + (", …" if len(unknown) > FIELDS_SHOWN else "")
    fields = ", ".join(sorted(allowed)) or "none"
    raise ApiError("unknown_field", f"Unknown {'fields' if len(unknown) > 1 else 'field'} in the {where}: {named}. "
                                    f"Known: {fields}.")


def query_fields(request: Request, known: Iterable[str] = ()) -> dict[str, str]:
    """The query of a request as {name: value}: 400 `unknown_field` for a name the route does not know, 400
    `bad_request` for a name given twice (which one would count?)."""
    items = request.query_params.multi_items()
    check_fields((name for name, _ in items), known, where="query")
    out: dict[str, str] = {}
    for name, value in items:
        if name in out:
            raise ApiError("bad_request", f"The query gives {_shown(name)} more than once.")
        out[name] = value
    return out


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not a JSON number")


def _finite(text: str) -> float:
    """A JSON number with a fraction or an exponent, refused when it overflows to ±inf (`1e999`): httpjson cannot
    write inf back, so a stored one would break every answer that shows it."""
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"{text[:20]} is out of range")
    return value


async def read_json(request: Request, *, limit: int = JSON_MAX) -> dict[str, Any]:
    """A request's JSON body as an object: {} for an empty body (a body-less POST such as /stop); 413 `too_large` over
    `limit` bytes (before any byte is read when the Content-Length says more: the launcher's own body helper has no
    bound); 400 `bad_request` for a body that is not JSON (NaN, Infinity and a number that overflows to it included)
    or not an object, or a request the client broke off."""
    length = request.headers.get("content-length", "").strip()
    if length.isdigit() and int(length) > limit:
        raise ApiError("too_large", f"The body is larger than {limit // 1024} KiB.")
    chunks: list[bytes] = []
    size = 0
    try:
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                raise ApiError("too_large", f"The body is larger than {limit // 1024} KiB.")
            chunks.append(chunk)
    except ClientDisconnect:
        raise ApiError("bad_request", "The request was interrupted.") from None
    raw = b"".join(chunks)
    if not raw.strip():
        return {}
    try:
        body = json.loads(raw, parse_constant=_no_constant, parse_float=_finite)
    except (ValueError, RecursionError):
        raise ApiError("bad_request", "The body is not valid JSON.") from None
    if not isinstance(body, dict):
        raise ApiError("bad_request", "The body must be a JSON object.")
    return body


def root_pairs(config: "Config") -> list[tuple[Path, str]]:
    """The roots of `[api] folders` as (folder, name) pairs for the redaction of error texts (docs/SPEC.md §21.1
    "Paths in answers"): a path below a root then reads `<root>/<path>`. Each as written and resolved; a root that is
    not absolute on this computer's own system (config.native_absolute) is never resolved, nor named."""
    pairs: list[tuple[Path, str]] = []
    for folder in config.api.folders.values():
        if not native_absolute(folder.path):
            continue
        path = Path(folder.path)
        pairs.append((path, folder.name))
        with contextlib.suppress(OSError, RuntimeError, ValueError):
            resolved = path.resolve()
            if resolved != path:
                pairs.append((resolved, folder.name))
    return pairs


def library_skills(config: "Config") -> list[dict[str, str]]:
    """The skills a create may name (docs/SPEC.md §21.2 "Options", §21.4): the library's and, in the Docker sandbox,
    the host library's (a name in both is the library's), sorted by name; never a broken entry, never a path. Blocking
    (it reads every skill's folder): run it in a thread."""
    found: dict[str, str] = {}
    libraries = [Library.from_config(config)]
    host = Library.host(config)
    if host is not None:
        libraries.append(host)
    for library in libraries:
        for entry in library.list():
            if "error" not in entry:
                found.setdefault(entry["name"], str(entry.get("description") or ""))
    return [{"name": name, "description": description} for name, description in sorted(found.items())]


def options_view(config: "Config", skills: list[dict[str, str]]) -> dict[str, Any]:
    """GET /api/v1/options (docs/SPEC.md §21.2): what a client may choose. Never a key, a `base_url`, an env name or a
    path: the models by name, provider and model; the roots of `[api] folders` by name and `in_place`."""
    language = config.swarm.language
    return {
        "api": API_VERSION, "version": __version__, "languages": list(LANGS), "language": language,
        "teams": [{"name": team.name, "roles": list(team.roles)} for team in config.teams],
        "roles": [{"key": role.key, "name": role.name, "description": role.description}
                  for role in roles.catalog(config.roles, language)],
        "models": [{"name": name, "provider": profile.provider, "model": profile.model}
                   for name, profile in config.models.items()],
        "default_model": config.agents.default_model,
        "skills": skills,
        "folders": [{"name": name, "in_place": folder.in_place} for name, folder in sorted(config.api.folders.items())],
        "limits": {"upload_max_mb": config.workspace.copy_max_mb, "upload_max_files": config.workspace.copy_max_files,
                   "files_per_session": FILES_PER_SESSION, "uploads_pending": config.api.uploads_pending,
                   "max_running": config.api.max_running, "archive_max_mb": config.api.archive_max_mb},
    }


Handler = Callable[[Request, str], Awaitable[Response]]


class ApiV1:
    """The external API of a launcher (docs/SPEC.md §21): `.app` (the Starlette app the launcher dispatches /api/v1
    to), the routes, the error mapping and `aclose()`. `host`: the address the launcher is bound to."""

    def __init__(self, manager: "SessionManager", host: str = "127.0.0.1"):
        self.manager = manager
        self.host = host
        self._tasks: set[asyncio.Task] = set()
        self.closing = False
        routes = [self._route("/api/v1/options", {"GET": self._options})]
        self.app = Starlette(routes=routes, exception_handlers={HTTPException: self._http_error})
        self.app.router.redirect_slashes = False

    # ---- plumbing -------------------------------------------------------------------------------------------------
    def _route(self, path: str, handlers: Mapping[str, Handler]) -> Route:
        """The one route of a path: its handlers by method, each given (request, caller), every error JSON. It takes
        only those methods (Starlette adds HEAD to a GET route: here HEAD is 405 like any other method). One route
        per path, never one per method: Starlette answers a wrong method with the first partial match alone, so the
        405's Allow would name one route's methods, not the path's."""
        table = {method.upper(): handler for method, handler in handlers.items()}

        async def endpoint(request: Request) -> Response:
            return await self._call(request, table[request.method])

        route = Route(path, endpoint, methods=list(table), name=path)
        route.methods = set(table)
        return route

    async def _call(self, request: Request, handler: Handler) -> Response:
        caller = request.scope.get(API_CALLER)
        if caller not in API_CALLERS:   # (the app reached without the guard's v1 branch)
            return fail("token_required", API_TOKEN_TEXT, headers={"WWW-Authenticate": BEARER_REALM})
        try:
            return await handler(request, caller)
        except ApiError as exc:
            return exc.response()
        except CODED_ERRORS as exc:
            return await self.error_response(exc, caller)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - every answer under /api/v1 is JSON
            log.exception("API v1: %s %s failed", request.method, request.url.path)
            return fail("internal", f"Internal error ({type(exc).__name__}): the hub's log has the details.")

    async def _http_error(self, request: Request, exc: Exception) -> Response:
        """Starlette's own refusals (no route, a wrong method) as JSON: 404 `not_found`, 405 `method_not_allowed`
        with its Allow header (sorted: Starlette joins a set)."""
        status = getattr(exc, "status_code", 500)
        if status == 404:
            return fail("not_found", NOT_FOUND_TEXT)
        if status == 405:
            given = (getattr(exc, "headers", None) or {}).get("Allow", "")
            allow = ", ".join(sorted({method.strip() for method in given.split(",") if method.strip()}))
            return fail("method_not_allowed", f"{request.method} is not allowed here"
                        + (f": use {allow}." if allow else "."), headers={"Allow": allow} if allow else None)
        code = STATUS_CODES.get(status) or ("bad_request" if 400 <= status < 500 else "internal")
        return fail(code, str(getattr(exc, "detail", "") or code))

    def redact(self, text: str, sid: str | None = None) -> str:
        """An error text for the API caller (docs/SPEC.md §21.1 "Paths in answers"): a path below a root of
        `[api] folders` as `<root>/<path>`, the user's home as "~", the home, and with `sid` that session's folders, by
        their names, a path in another session's folder as SessionManager.OTHER_SESSION (SessionManager._public_text),
        any other absolute path by its last component. Blocking (it resolves the roots, which may lie on a network
        share, and reads the session's files): a route calls it through error_response, in a thread."""
        return self.manager._public_text(sid, text, named=root_pairs(self.manager.config))

    async def error_response(self, exc: BaseException, caller: str, sid: str | None = None) -> JSONResponse:
        """One of CODED_ERRORS as `{"error", "code"}` (error_code); for the API caller its text through redact(), in a
        thread, so that a root on an unplugged share never stalls the hub. An `internal` one is logged, with its
        traceback unless it is the 503 of a hub shutting down (no fault of anyone's)."""
        code = error_code(exc)
        if code == "internal":
            if getattr(exc, "status", None) == 503:
                log.info("API v1: %s", exc)
            else:
                log.error("API v1: %s", exc, exc_info=exc)
        text = str(exc)
        return fail(code, await asyncio.to_thread(self.redact, text, sid) if caller == "api" else text)

    def spawn(self, coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task:
        """A background task of a route (a session's start after its create or `continue`), which aclose() waits for
        and never cancels; its failure is logged."""
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("API v1: %s failed", task.get_name(), exc_info=task.exception())

    async def aclose(self) -> None:
        """The launcher's shutdown, before the session router's (docs/SPEC.md §21.9): wait for the background tasks in
        flight, at most START_WAIT seconds, never cancelling one: SessionManager._start_locked catches only Exception,
        so a cancel inside swarm.start() would skip its cleanup and leave running what the start launched (the MCP
        servers, the skills' tasks). A start still running then holds its session's lock, which manager.shutdown()
        waits for. A cancel of aclose() itself leaves the tasks running too: launcher.serve_launcher, cancelled
        (Ctrl+C), gives the server GRACEFUL_SECONDS + 3 seconds before it cancels it, lifespan and this wait included,
        so the bound is then the smaller of the two. A task spawned while this waits is waited for too, up to the
        deadline."""
        self.closing = True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + START_WAIT
        while True:
            pending = {task for task in self._tasks if not task.done()}
            left = deadline - loop.time()
            if not pending or left <= 0:
                break
            await asyncio.wait(pending, timeout=left)
        if pending:
            log.warning("API v1: %d background task(s) still running at shutdown: the session manager waits for them",
                        len(pending))

    # ---- routes ---------------------------------------------------------------------------------------------------
    async def _options(self, request: Request, caller: str) -> Response:
        """GET /api/v1/options (docs/SPEC.md §21.2): the hub's global settings, a hand edit picked up first."""
        query_fields(request)
        config = self.manager.global_config()
        skills = await asyncio.to_thread(library_skills, config)
        return JSONResponse(options_view(config, skills))


__all__ = ["API_VERSION", "CODED_ERRORS", "CODES", "FILES_PER_SESSION", "JSON_MAX", "START_WAIT", "STATUS_CODES",
           "ApiError", "ApiV1", "check_fields", "error_code", "fail", "library_skills", "options_view", "query_fields",
           "read_json", "root_pairs"]
