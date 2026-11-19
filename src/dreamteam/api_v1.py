"""The external HTTP API v1 (docs/SPEC.md §21): `ApiV1(manager, host)`.

The launcher dispatches every path `web.is_api_path` takes to `ApiV1.app` (§21.1 "Dispatch"), behind the guard's v1
branch, which has already said who calls (`scope[web.API_CALLER]`: "owner" or "api"). The app is a Starlette app of
its own: its routes, `redirect_slashes` off (a bare `/api/v1` is a JSON 404, never a 307) and an `HTTPException`
handler, so that a wrong path or method is JSON too. Every answer under `/api/v1/` is JSON; an error is
`{"error": text, "code": code}` with the code's status (CODES). This module also holds what every route uses: the JSON
body read up to JSON_MAX, the check of a body's or a query's fields (`unknown_field`), the mapping of the manager's
errors to codes with the redaction of paths for the API caller, the one resolver of a session's `{id}`, the summary
and its state (§21.7), the files and the archive of a session's folder, read without following a link (§21.8,
nofollow), and `aclose()`, which waits for the starts in flight and never cancels them. It uses the
manager in process, never the internal routes over HTTP, and never imports dreamteam.tui."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import functools
import hashlib
import io
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import stat
import threading
import time
import unicodedata
import urllib.parse
import zipfile
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import (TYPE_CHECKING, Any, AsyncIterator, Awaitable, BinaryIO, Callable, Coroutine, Iterable, Iterator,
                    Mapping)

import anyio
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from . import __version__, apifolders, netoff, nofollow, roles
from .apifolders import FOLDER_FIELDS, MODES, FolderRequest
from .autoreply import (ID_MAX, blocking_requests, journal_head, listed_requests, read_requests, request_key,
                        requests_of)
from .board import MAX_POST
from .config import _joins_text, native_absolute
from .copyfrom import API_UPLOAD_PATH, CopyError, CopyLimits, Source, Uploads, check_rel, printable_line
from .db import reading_file
from .eventtext import event_text
from .goal_text import split_goal
from .httpjson import JSONResponse, json_text
from .instruct import continue_step
from .jobs import COMMAND_CHARS
from .mailbox import MAX_TEXT, read_user_conversation
from .models import USER, ToolError
from .result import build_result
from .sessionopts import OptionError, overrides_from
from .sessions import (API_ANSWERS, API_GIVEN, LEGACY_ID, TITLE_LIMIT, SessionError, api_answers, api_meta,
                       read_lists, session_uid)
from .skills import NAME_MAX, NAME_RE, Library, SkillError
from .uitext import LANGS
from .web import API_CALLER, API_CALLERS, API_TOKEN_TEXT, BEARER_REALM, JSON_TYPE_TEXT, PUBLIC_URL_ENV, _loopback_bind
from .workspace import BaseCommitGone, changed_since, isolated_git

if TYPE_CHECKING:
    from .config import Config
    from .sessions import SessionManager
    from .swarm import Swarm

log = logging.getLogger(__name__)

API_VERSION = 1
JSON_MAX = 1 << 20          # bytes of a JSON body (413 too_large beyond them)
FILES_PER_SESSION = 20      # uploads one create takes (docs/SPEC.md §21.2 "Options": limits)
START_WAIT = 30.0           # seconds aclose() waits for the background tasks (starts) in flight; it never cancels them
FIELDS_SHOWN = 10           # unknown fields named in an unknown_field text
FIELD_CHARS = 60            # characters of each
SESSIONS_DEFAULT, SESSIONS_MAX = 50, 200   # GET /api/v1/sessions `limit`
MESSAGES_DEFAULT, MESSAGES_MAX = 200, 500  # GET /api/v1/sessions/{id}/messages `limit`
EVENTS_DEFAULT, EVENTS_MAX = 200, 500      # GET /api/v1/sessions/{id}/events `limit` (docs/SPEC.md §21.6)
WAIT_MAX = 60                              # ... and its `wait`, in seconds
STREAM_PAGE = 500                          # the events/stream's pages
PING_SECONDS = 15.0                        # a `: ping` comment of the stream after this long without a line
# after a wake-up, a long poll or a stream waits this long before it reads: a burst of events (an agent's tool calls)
# is read once, not once per event
SETTLE_SECONDS = 0.05
RESULTS_CACHED = 64                        # final results kept (ApiV1.result; docs/SPEC.md §21.7)
CHANGED_FILES_MAX = 10_000                 # entries of a result's `changed_files` (the first ones by path)
DETAILS = ("timeline", "all")              # the events' `detail`
# detail=timeline (docs/SPEC.md §21.6): the types a client's timeline shows, as an exact list (`type IN (...)`, never a
# LIKE pattern); a `notification` only when it is addressed to the user
TIMELINE = ("task.created", "task.updated", "task.comment", "message.sent", "notification", "goal.set",
            "goal.completed", "goal.reopened", "agent.hired", "agent.retired", "swarm.paused", "swarm.resumed",
            "swarm.wrap_up", "swarm.limits", "session.started", "session.stopped", "job.started", "job.ended",
            "mcp.created", "mcp.status", "skill.attached", "skill.detached", "skill.proposed", "skill.library",
            "skill.deps", "file.changed")
EVENTS_FIELDS = ("after", "wait", "limit", "detail", "lang")   # GET .../events
STREAM_FIELDS = ("after", "detail", "lang")                    # GET .../events/stream
FILES_FIELDS = ("path", "scratch")                             # GET .../files (docs/SPEC.md §21.8)
FILE_FIELDS = ("scratch",)                                     # GET .../files/{path}
ARCHIVE_FIELDS = ("path", "scratch")                           # GET .../archive
FILE_CHUNK = 1 << 20        # bytes of each read of a download or of an archived file
ZIP_SENT = 256 << 10        # bytes of zip an archive gathers before it sends them
ZIP_ENTRY_COST = 4 << 10    # what one entry of an archive counts for in a step, as bytes read (_zip_parts)
# ID_MAX (autoreply's, SQLite's largest integer): the largest id a query's `after`, a message's `reply_to` or a
# request's id names
TO_MAX = 100                # names in the `to` of a message the client sends
CLIENT_REF_MAX = 128        # characters of a create's client_ref
API_SID = "dreamteam.api_sid"   # the scope key of the session a route resolved ({id}): its error texts name its folders
UPLOAD_TYPES = ("application/octet-stream", "application/zip")   # what POST /api/v1/uploads takes (§21.3)
# the states of a session (docs/SPEC.md §21.7), in the order they are tried: the first that holds
STATES = ("starting", "failed", "created", "done", "budget", "waiting", "paused", "running", "stopped")
# the fields of POST /api/v1/sessions (docs/SPEC.md §21.4); `folder` is an object of FOLDER_FIELDS (§21.10)
CREATE_FIELDS = ("goal", "title", "description", "language", "team", "model", "start_agents", "max_agents",
                 "max_cost_usd", "network", "files", "folder", "skills", "answers", "clarify", "stop_when_done",
                 "client_ref")
PATCH_FIELDS = ("max_cost_usd", "max_agents", "wrap_up")   # PATCH /api/v1/sessions/{id} (docs/SPEC.md §21.2)
CONTINUE_FIELDS = ("text",)                                # POST /api/v1/sessions/{id}/continue
MESSAGE_FIELDS = ("to", "text", "reply_to")                # POST /api/v1/sessions/{id}/messages (docs/SPEC.md §21.5)
# the fields of an answer, by the kind of the request it answers (POST /api/v1/sessions/{id}/requests/{rid}, §21.5)
ANSWER_FIELDS = {"question": ("text",), "message": ("text",), "review": ("approve", "comment"), "mcp": ("approve",)}
SHUTTING_DOWN_TEXT = "DreamTeam is shutting down."
NO_REQUEST_TEXT = ("No such request: it was answered, or it waits no more. GET /api/v1/sessions/{id}/requests lists "
                   "what waits now.")
# `not_running` of an answer or a message (docs/SPEC.md §21.2): they need the session running
NOT_RUNNING_TEXT = ("The session is not running: POST /api/v1/sessions/{id}/continue starts it again with a text, the "
                    "answer to a pending question, else a message to its team; a completed goal is reopened with it, "
                    "and its agents work on it again.")
# a continue's `api.error` when its start started nothing: a stop sent after it took the session's lock first
NOT_DELIVERED_TEXT = "The session stopped before the text of a continue reached it: send the text again."
NO_SESSION_TEXT = "No such session: GET /api/v1/sessions lists the sessions this token sees."
# `changed_files_error` (docs/SPEC.md §21.7): why `changed_files` is null; DreamTeam's own words, never git's output
# (it may name the folder)
GIT_OFF_TEXT = "Git is off in this session ([workspace] git): its changes are not tracked."
NO_GIT_TEXT = "git is not installed on the hub's computer."
NO_BASE_TEXT = ("The session has no base commit: its first start found no git, git was off, or it was not created "
                "through the API.")
BASE_GONE_TEXT = "The session's base commit is no longer in its repository (its history was rewritten or replaced)."
NO_REPOSITORY_TEXT = "The session's folder has no repository of its own any more."
SETTINGS_TEXT = "The session's settings cannot be read, so its folder is not known."
# the reasons that stay as they are while the session stays final: a final result with one of them (or none) is kept;
# any other (git not installed, settings or a repository that cannot be read, git failing, an in-place folder refused)
# may pass, so it is computed again at the next request
LASTING_TEXTS = (None, GIT_OFF_TEXT, NO_BASE_TEXT, BASE_GONE_TEXT)
# the files of a session (docs/SPEC.md §21.8): its folder gone, or reached through a link now
FOLDER_GONE_TEXT = "The session's folder is not there (deleted or moved, or on a drive that is not connected)."
FOLDER_LINKED_TEXT = "The session's folder is reached through a link now: nothing in it is read."
FOLDER_UNREADABLE_TEXT = "The session's folder cannot be read on the hub's computer (its permissions)."
# the end of a server folder's start-check refusal (SessionManager._check_server_folder) when v1 reads its files
FILES_REFUSAL = "its files are not read"
# v1's own text of `folder_busy` (docs/SPEC.md §21.10): the manager's names the other session's folder, so its title
FOLDER_BUSY_TEXT = ("Another session works in this folder, in a folder inside it or in one containing it, or is being "
                    "created there: two sessions cannot share files. Send that session's next instruction with "
                    "POST /api/v1/sessions/{id}/continue, or DELETE it to free the folder (its files stay).")

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
# a route of v1 that an error text names ("GET /api/v1/options lists the roles"): lowercase words and `{id}`-like
# placeholders after /api/v1, starting a word and ending at a space or at the punctuation that ends a sentence. No
# path of the hub's computer: redact() keeps it as it is (the redaction would name it by its last component).
_ROUTE = re.compile(r"""(?<![^\s(\[{<=,;:'"`])/api/v1(?:/(?:[a-z_]+|\{[a-z_]+\}))*(?![^\s'"`<>|.,;:!?)\]}])""")
# the errors whose texts and codes a route answers as they are (error_code; for the API caller through redact()): the
# manager's, the stores' and the create options'. Any other exception is a 500 `internal` with no text of its own,
# whatever it carries.
CODED_ERRORS: tuple[type[Exception], ...] = (SessionError, CopyError, SkillError, OptionError)


class ApiError(Exception):
    """A refusal of a v1 route (a helper's or the route's own): the route wrapper answers it as
    `{"error": text, "code": code, **extra}` with the code's status (CODES). `extra`: a code's own fields, named where
    docs/SPEC.md §21 describes that code. `redact`: the text is not v1's own but what the swarm refused with (a
    ToolError's, which may name a path of the hub's computer, e.g. a failed smoke test's): for the API caller it goes
    through ApiV1.redact, as a manager error's does."""

    def __init__(self, code: str, text: str, *, headers: Mapping[str, str] | None = None, redact: bool = False,
                 **extra: Any):
        if code not in CODES:
            raise ValueError(f"not an API v1 code: {code!r}")
        super().__init__(text)
        self.code = code
        self.text = text
        self.headers = dict(headers or {})
        self.redact = redact
        self.extra = extra

    def response(self) -> JSONResponse:
        return fail(self.code, self.text, headers=self.headers, **self.extra)


def fail(code: str, text: str, *, headers: Mapping[str, str] | None = None, **extra: Any) -> JSONResponse:
    """The JSON answer of an error: `{"error": text, "code": code, **extra}` with the status CODES gives the code."""
    return JSONResponse({"error": text, "code": code, **extra}, status_code=CODES[code], headers=dict(headers or {}))


def error_code(exc: BaseException) -> str:
    """The v1 code of an error the manager raised (docs/SPEC.md §21.1; one of CODED_ERRORS): its own `code` when v1
    knows it (SessionError, CopyError, OptionError), `earlier_version` for a session made by an earlier version, else
    by its `status` (STATUS_CODES; another 4xx is `bad_request`); `internal` for anything without a status. A code
    decides the answer's status, which may differ from the error's own (`folder_not_ready` is 409 also where the
    internal routes answer 400)."""
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
    for folder in apifolders.roots(config).values():
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
        "folders": [{"name": name, "in_place": folder.in_place}
                    for name, folder in sorted(apifolders.roots(config).items())],
        "limits": {"upload_max_mb": config.workspace.copy_max_mb, "upload_max_files": config.workspace.copy_max_files,
                   "files_per_session": FILES_PER_SESSION, "uploads_pending": config.api.uploads_pending,
                   "max_running": config.api.max_running, "archive_max_mb": config.api.archive_max_mb},
    }


def state_of(*, starting: bool, earlier: bool, failed: bool, never_started: bool, done: bool, budget: bool,
             running: bool, paused: bool, blocking: Callable[[], bool]) -> str:
    """A session's state (docs/SPEC.md §21.7), the first that holds: `starting` (a create or `continue` starts it, or
    the hub's resume() is about to), then a session of an earlier version is `stopped`, `failed` (its last start
    failed), `created` (never started), `done` (its goal is completed), `budget` (the budget paused it, or that pause
    stopped it), `waiting` (running, a request that blocks the work waits: `blocking()`, asked only then), `paused`
    (running, paused by the user), `running`, else `stopped`."""
    if starting:
        return "starting"
    if earlier:
        return "stopped"
    if failed:
        return "failed"
    if never_started:
        return "created"
    if done:
        return "done"
    if budget:
        return "budget"
    if running:
        if blocking():
            return "waiting"
        return "paused" if paused else "running"
    return "stopped"


def _error_text(api: Mapping[str, Any] | None) -> str | None:
    """A v1 session's `error`: the text of `api.start_error`, else of `api.error` ({code, text} or a text)."""
    for key in ("start_error", "error"):
        value = (api or {}).get(key)
        text = value.get("text") if isinstance(value, dict) else value
        if isinstance(text, str) and text:
            return text
    return None


def request_digest(body: Mapping[str, Any]) -> str:
    """A create's `request_digest` (docs/SPEC.md §21.4 `client_ref`): the SHA-256 of the body's canonical JSON (keys
    sorted, no spaces), so a retry is told from another job with the same client_ref. A field that is null is left
    out (null counts as not given), and a whole number written with a fraction (5.0) is written as 5: a retry that
    spells the same request another way is still a repeat."""
    canonical = {key: int(value) if isinstance(value, float) and value.is_integer() else value
                 for key, value in body.items() if value is not None}
    text = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _one_line(text: str) -> bool:
    return "".join(text.splitlines()) == text


def _hidden_chars(text: str) -> bool:
    """Whether a title holds a character that must not reach a terminal or a page as it is: a control character (C0
    and C1: an escape sequence, NUL, a tab), an unpaired surrogate, or an invisible format character other than the
    joiners of ordinary text (config._joins_text: emoji sequences; a bidirectional override is refused), as a saved
    team's name may not."""
    for char in text:
        category = unicodedata.category(char)
        if category in ("Cc", "Cs") or (category == "Cf" and not _joins_text(char)):
            return True
    return False


@dataclass
class CreateRequest:
    """POST /api/v1/sessions after the body's own checks (docs/SPEC.md §21.4 "Order" 1): what `manager.create` is
    called with. The team, model and limits are sessionopts' (overrides_from); the skills and the uploads are checked
    against the library and the store later."""
    title: str
    goal: str
    description: str
    answers: str
    stop_when_done: bool
    client_ref: str | None
    files: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    # the server folder (docs/SPEC.md §21.10): {"root", "path" ("" for the root itself), "mode"}, checked as a shape
    # here, resolved later (apifolders.resolve_request, §21.4 "Order" 5)
    folder: dict[str, Any] | None = None

    @classmethod
    def parse(cls, body: Mapping[str, Any]) -> "CreateRequest":
        """400 `bad_request` for a value of the wrong kind; `goal` required, with a character that is not
        whitespace; `title` one such line of at most TITLE_LIMIT characters; the session's title (`title`, else the
        goal's first line) without a control or invisible format character (_hidden_chars: it reaches the owner's
        terminal and pages); `clarify: true` only with `answers: "manual"`; `client_ref` at most CLIENT_REF_MAX
        printable characters; `files` at most FILES_PER_SESSION tokens; `folder` an object of FOLDER_FIELDS
        (`unknown_field` for another; `root` a string, `path` a string, `mode` one of MODES, a null one not given),
        and `files` never with an `in_place` folder (the client puts its files into the folder itself). The kinds
        of the team, model and limits are sessionopts' (overrides_from), after client_ref (§21.4 "Order" 3); the
        folder is resolved after the skills (apifolders.resolve_request, "Order" 5). The goal and title combine as §21.4
        says: without a title the goal's first line is its title
        (create("", goal, description)); with one, the goal's title is the title and the whole goal its description
        (create(title, title + "\\n" + goal, description)), unless the goal's own title, as split_goal reads it,
        already is the title (create(title, goal, description))."""
        def text(key: str) -> str | None:
            value = body.get(key)
            if value is not None and not isinstance(value, str):
                raise ApiError("bad_request", f"'{key}' must be a string.")
            return value

        def flag(key: str) -> bool | None:
            value = body.get(key)
            if value is not None and not isinstance(value, bool):
                raise ApiError("bad_request", f"'{key}' must be true or false.")
            return value

        def names(key: str, what: str) -> list[str]:
            value = body.get(key)
            if value is None:
                return []
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ApiError("bad_request", f"'{key}' must be a list of {what}.")
            return list(value)

        goal = text("goal")
        if goal is None or not goal.strip():
            raise ApiError("bad_request", "'goal' is required: what the session is to achieve (its first line is the "
                                          "title).")
        title = text("title")
        if title is not None and (not title.strip() or not _one_line(title) or len(title) > TITLE_LIMIT):
            raise ApiError("bad_request", f"'title' must be one line with a character that is not a space, at most "
                                          f"{TITLE_LIMIT} characters.")
        if title is not None and _hidden_chars(title):
            raise ApiError("bad_request", "'title' cannot hold control characters (a tab, an escape sequence) or "
                                          "invisible format characters.")
        if title is None and _hidden_chars(split_goal(goal)[0]):
            raise ApiError("bad_request", "The first line of 'goal' is the session's title: it cannot hold control "
                                          "characters (an escape sequence) or invisible format characters.")
        description = text("description") or ""
        answers = body.get("answers")
        answers = API_GIVEN["answers"] if answers is None else answers
        if answers not in API_ANSWERS:
            raise ApiError("bad_request", f"'answers' must be one of {', '.join(API_ANSWERS)}.")
        if flag("clarify") and answers != "manual":
            raise ApiError("bad_request", "'clarify': true needs 'answers': \"manual\": the planner's questions wait "
                                          "for the client's answers (GET /api/v1/sessions/{id}/requests).")
        stop_when_done = flag("stop_when_done")
        client_ref = text("client_ref")
        if client_ref is not None and (not client_ref or len(client_ref) > CLIENT_REF_MAX
                                       or not client_ref.isprintable()):
            raise ApiError("bad_request", f"'client_ref' must be 1 to {CLIENT_REF_MAX} printable characters.")
        files = names("files", f"upload tokens from POST {API_UPLOAD_PATH}")
        if len(files) > FILES_PER_SESSION:
            raise ApiError("bad_request", f"'files' takes at most {FILES_PER_SESSION} uploads.")
        skills = names("skills", "library skill names")
        folder = cls._folder(body.get("folder"))
        if folder is not None and folder["mode"] == "in_place" and files:
            raise ApiError("bad_request", "'files' cannot go with an in_place folder: put the files into the folder "
                                          "itself.")
        if title is None:
            title, goal_text = "", goal
        elif " ".join(title.split()) == split_goal(goal)[0]:
            goal_text = goal
        else:
            goal_text = f"{title}\n{goal}"
        return cls(title=title, goal=goal_text, description=description, answers=answers,
                   stop_when_done=API_GIVEN["stop_when_done"] if stop_when_done is None else stop_when_done,
                   client_ref=client_ref, files=files, skills=skills, folder=folder)

    @staticmethod
    def _folder(value: Any) -> dict[str, Any] | None:
        """A create's `folder` (docs/SPEC.md §21.10 "Request") as {"root", "path", "mode"}: None when not given; 400
        `unknown_field` for a field of it v1 does not know, `bad_request` for a value of the wrong kind."""
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ApiError("bad_request", "'folder' must be an object: {\"root\", \"path\", \"mode\"}.")
        check_fields(value, FOLDER_FIELDS, where="folder")
        root, path, mode = value.get("root"), value.get("path"), value.get("mode")
        if not isinstance(root, str) or not root:
            raise ApiError("bad_request", "'folder' needs 'root': the name of a root of [api] folders (GET "
                                          "/api/v1/options lists them).")
        if path is not None and not isinstance(path, str):
            raise ApiError("bad_request", "The folder's 'path' must be a string, relative to its root, with '/'.")
        mode = "copy" if mode is None else mode
        if mode not in MODES:
            raise ApiError("bad_request", f"The folder's 'mode' must be one of {', '.join(MODES)}.")
        return {"root": root, "path": path or "", "mode": mode}


def skill_refs(names: list[str], config: "Config") -> list[str]:
    """The skills a create names (docs/SPEC.md §21.4 `skills`) as references `create` takes: `library:<name>`, or
    `host-library:<name>` for one only the host library has (the Docker sandbox); never a bare name (it would also
    search ~/.claude/skills) nor a source. 400 `unknown_skill` for a name that is no skill name (skills.NAME_RE) or in
    neither library, `bad_request` past `[skills] max_attached`. Blocking (it reads the libraries)."""
    unique = list(dict.fromkeys(names))
    if len(unique) > config.skills.max_attached:
        raise ApiError("bad_request", f"At most {config.skills.max_attached} skills can be attached to a session "
                                      "([skills] max_attached).")
    library, host = Library.from_config(config), Library.host(config)
    refs = []
    for name in unique:
        if len(name) <= NAME_MAX and NAME_RE.fullmatch(name):
            if library.path(name) is not None:
                refs.append(f"library:{name}")
                continue
            if host is not None and host.path(name) is not None:
                refs.append(f"host-library:{name}")
                continue
        raise ApiError("unknown_skill", f"No skill {_shown(name)} in the library: GET /api/v1/options lists the "
                                        "skills a session can take.")
    return refs


def query_flag(value: str | None, name: str) -> bool:
    """A true/false query field: "true" or "false", nothing else (400 `bad_request`); absent is false."""
    if value is None:
        return False
    if value not in ("true", "false"):
        raise ApiError("bad_request", f"'{name}' must be true or false.")
    return value == "true"


def _whole(value: str, most: int) -> int | None:
    """A whole number written in a query (ASCII digits, leading zeros allowed) when it is at most `most`, else None.
    Never int() of more digits than `most` has: Python refuses to convert a text of over 4300 digits (a ValueError,
    which would answer 500)."""
    if not (value.isascii() and value.isdigit()):
        return None
    digits = value.lstrip("0") or "0"
    return int(digits) if len(digits) <= len(str(most)) and int(digits) <= most else None


def query_count(value: str | None, name: str, default: int, most: int) -> int:
    """A whole number from 1 to `most` given in a query (400 `bad_request` otherwise); absent is `default`."""
    if value is None:
        return default
    number = _whole(value, most)
    if number is None or number < 1:
        raise ApiError("bad_request", f"'{name}' must be a whole number from 1 to {most}.")
    return number


def query_after(value: str | None, name: str = "after") -> int:
    """An id given in a query (`after`): a whole number from 0 up to ID_MAX, the largest SQLite holds (400
    `bad_request` otherwise); absent is 0."""
    if value is None:
        return 0
    number = _whole(value, ID_MAX)
    if number is None:
        raise ApiError("bad_request", f"'{name}' must be a whole number from 0 to {ID_MAX} (an id this route "
                                      "answered).")
    return number


_SECONDS = re.compile(r"[0-9]{1,6}(?:\.[0-9]{1,6})?")


def query_wait(value: str | None, name: str = "wait") -> float:
    """A long poll's `wait` (docs/SPEC.md §21.6): seconds from 0 to WAIT_MAX, a whole or decimal number written with
    digits (no exponent, no sign, no "inf"); absent is 0. 400 `bad_request` otherwise."""
    if value is None:
        return 0.0
    if not _SECONDS.fullmatch(value) or float(value) > WAIT_MAX:
        raise ApiError("bad_request", f"'{name}' must be a number of seconds from 0 to {WAIT_MAX}.")
    return float(value)


def query_choice(value: str | None, name: str, choices: tuple[str, ...], default: str | None) -> str | None:
    """A query field that names one of `choices` (400 `bad_request` otherwise); absent is `default`."""
    if value is None:
        return default
    if value not in choices:
        raise ApiError("bad_request", f"'{name}' must be one of {', '.join(choices)}.")
    return value


def session_lang(summary: Mapping[str, Any]) -> str:
    """The language of a session's event lines when the client names none (docs/SPEC.md §21.6 `lang`): its own, from
    its summary; "en" for one v1 does not know."""
    language = summary.get("language")
    return language if language in LANGS else "en"


def event_view(row: Mapping[str, Any], lang: str) -> dict[str, Any]:
    """A journaled event as v1 shows it (docs/SPEC.md §21.6): {id, type, ts, actor, data, text}, `text` the line of
    eventtext.event_text in `lang` as the user sees it (null for agent.usage and agent.compacted)."""
    return {"id": row["id"], "type": row["type"], "ts": row["ts"], "actor": row["actor"], "data": row["data"],
            "text": event_text(row, lang, me=USER)}


def _without_inf(value: Any) -> Any:
    """`value` with every float that is not finite as null: an event's data may hold one (a model's `NaN` in a tool
    argument, journaled as it was), which no JSON answer can carry."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _without_inf(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_without_inf(item) for item in value]
    return value


def safe_json(value: Any) -> str:
    """httpjson.json_text of `value`, its non-finite floats as null when it has any (_without_inf): an answer that
    shows journaled data never fails on them (a stream would fail again at every reconnect)."""
    try:
        return json_text(value)
    except ValueError:
        return json_text(_without_inf(value))


def sse_event(event: Mapping[str, Any]) -> str:
    """One event of the stream (docs/SPEC.md §21.6 "Stream"): `id: <id>`, `event: <type>`, `data: <the event JSON>`,
    then a blank line. The JSON is one line (json_text escapes line breaks); the type line never holds one."""
    kind = re.sub(r"[\r\n]", " ", str(event["type"]))
    return f"id: {event['id']}\nevent: {kind}\ndata: {safe_json(event)}\n\n"


class SafeJSONResponse(JSONResponse):
    """httpjson.JSONResponse that answers journaled data whatever floats it holds (safe_json)."""

    def render(self, content: Any) -> bytes:
        return safe_json(content).encode("utf-8")


def check_json_type(request: Request) -> None:
    """415 `unsupported_media_type` unless the request says `Content-Type: application/json` (parameters such as a
    charset aside): PATCH checks its type itself, since the guard's 415 rule covers POST only (docs/SPEC.md §21.1
    "Format"). A body-less one sends it too."""
    kind = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if kind != "application/json":
        raise ApiError("unsupported_media_type", f"{JSON_TYPE_TEXT}.")


def _text_value(value: str, key: str, most: int) -> str:
    """A body's text `value` (a string) without the whitespace around it, at most `most` characters so, and without
    an unpaired surrogate ("\\ud800" in the JSON: SQLite stores UTF-8, so a message or a post holding one could not be
    kept, a 500); 400 `bad_request` otherwise."""
    text = value.strip()
    if len(text) > most:
        raise ApiError("bad_request", f"'{key}' takes at most {most} characters.")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise ApiError("bad_request", f"'{key}' holds an unpaired surrogate (\\ud800 to \\udfff): it is no "
                                      "text.") from None
    return text


def required_text(body: Mapping[str, Any], key: str, hint: str, most: int = MAX_TEXT) -> str:
    """The text field `key` of a body (_text_value): a string with a character that is not whitespace (400
    `bad_request`, "'<key>' is required: <hint>.", otherwise; null is not given)."""
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ApiError("bad_request", f"'{key}' is required: {hint}.")
    return _text_value(value, key, most)


def continue_text(body: Mapping[str, Any]) -> str:
    """The `text` of a `continue` (docs/SPEC.md §21.2) without the whitespace around it, as every action that takes
    it stores it (Mailbox.send strips a message, split_goal a goal; reopen_goal would keep it): a string with a
    character that is not whitespace, at most MAX_TEXT characters so (the most a message holds: the text may become
    one), no unpaired surrogate; 400 `bad_request` otherwise."""
    return required_text(body, "text", "the next instruction for the session (its goal when it has none, the answer "
                                       "to a pending question, else a message to the team)")


def answer_of(kind: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """The answer to a request of `kind` (docs/SPEC.md §21.5) in a POST's body: `{"text"}` for a question or a message
    (required_text: it becomes the user's message, at most MAX_TEXT characters), `{"approve": bool, "comment"?}` for a
    review (the comment at most board.MAX_POST characters: it becomes a post of the task), `{"approve": bool}` for an
    MCP server. 400 `unknown_field` for a field another kind's answer has (ANSWER_FIELDS), `bad_request` for a missing
    one or a value of the wrong kind; null is not given."""
    check_fields(body, ANSWER_FIELDS[kind])
    if kind in ("question", "message"):
        return {"text": required_text(body, "text", f"the answer to the {kind}, sent to its member as your message")}
    approve = body.get("approve")
    if not isinstance(approve, bool):
        raise ApiError("bad_request", "'approve' is required: true or false.")
    if kind == "mcp":
        return {"approve": approve}
    comment = body.get("comment")
    if comment is not None and not isinstance(comment, str):
        raise ApiError("bad_request", "'comment' must be a string.")
    return {"approve": approve, "comment": _text_value(comment or "", "comment", MAX_POST)}


def message_of(body: Mapping[str, Any]) -> tuple[list[str], str, int | None]:
    """(to, text, reply_to) of a message the client sends (POST messages, docs/SPEC.md §21.5): `to` a list of 1 to
    TO_MAX member names or "all", `text` as required_text, `reply_to` a message id or null. 400 `unknown_field` for
    another field, `bad_request` for a missing or wrong value."""
    check_fields(body, MESSAGE_FIELDS)
    to = body.get("to")
    if (not isinstance(to, list) or not 1 <= len(to) <= TO_MAX
            or not all(isinstance(name, str) and name.strip() for name in to)):
        raise ApiError("bad_request", f"'to' is required: a list of 1 to {TO_MAX} member names, or [\"all\"] for "
                                      "every member.")
    text = required_text(body, "text", "the message")
    reply_to = body.get("reply_to")
    if reply_to is not None and (isinstance(reply_to, bool) or not isinstance(reply_to, int)
                                 or not 1 <= reply_to <= ID_MAX):
        raise ApiError("bad_request", "'reply_to' must be the id of a message (GET /api/v1/sessions/{id}/messages "
                                      "lists them).")
    return list(to), text, reply_to


def message_view(message: Mapping[str, Any]) -> dict[str, Any]:
    """A message as v1 shows it (docs/SPEC.md §21.2 `messages`): {id, ts, from, to, text, reply_to}."""
    return {"id": message["id"], "ts": message["ts"], "from": message["sender"],
            "to": list(message.get("recipients") or []), "text": message["text"], "reply_to": message.get("reply_to")}


def budget_text(spent: float, limit: float) -> str:
    """The text of `budget_exhausted` (docs/SPEC.md §21.2 `continue`)."""
    return (f"The session's budget is spent (${spent:.2f} of ${limit:.2f}): raise max_cost_usd with "
            "PATCH /api/v1/sessions/{id} first.")


async def _outlive(work: asyncio.Future[Any]) -> None:
    """Wait for `work` (a thread's) to end, once the caller was cancelled, without keeping the event loop busy: an
    anyio cancel scope (Starlette cancels a streamed answer through one when its client goes away) cancels a task
    that is still waiting again at every pass of the loop, so the wait is shielded from it; a cancel of the task
    itself (the hub shutting down) is waited through as well."""
    while not work.done():
        with anyio.CancelScope(shield=True), contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait({work})


async def _thread_step(fn: Callable[..., Any], *args: Any, release: Callable[[Any], None] | None = None) -> Any:
    """fn(*args) in a thread that a cancellation (the client gone) never leaves running behind the caller: a cancel
    waits for it to end (_outlive), hands what it gave to `release` (a file it opened, to close; claims it took, to
    give back), then goes on. So a download or an archive closes its file or its walk only once no thread reads it,
    and a create holds no claim after it."""
    work = asyncio.ensure_future(asyncio.to_thread(fn, *args))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        await _outlive(work)
        if not work.cancelled() and work.exception() is None and release is not None:
            release(work.result())
        raise


async def claim_uploads(uploads: Uploads, tokens: list[str], caller: str) -> list[Source]:
    """`uploads.claim_many(tokens, caller=caller)` in a thread that a cancellation never leaves holding claims: a
    cancel waits for the claims to end and gives back what they took (docs/SPEC.md §21.3 "Life": nothing stays
    claimed after the create; _thread_step)."""
    return await _thread_step(functools.partial(uploads.claim_many, tokens, caller=caller),
                              release=lambda sources: uploads.settle(sources, used=False))


@dataclass
class _Ref:
    """A create in flight that holds its client_ref (docs/SPEC.md §21.4 `client_ref`): a retry with the same body
    waits for `done`, one with another body is 409 at once."""
    digest: str
    done: asyncio.Event = field(default_factory=asyncio.Event)


class _Watch:
    """The waiter of a long poll or a stream (SessionManager.watch, docs/SPEC.md §21.6): keyed by the session's folder
    name, so it follows `{id}` from pass to pass; when the id resolves to another folder name, the waiter moves there
    (follow) and the caller reads again, since a wake-up may have come before. Enter, move and close it on the event
    loop."""

    def __init__(self, manager: "SessionManager"):
        self.manager = manager
        self.sid: str | None = None
        self.changed = asyncio.Event()
        self._stack = contextlib.ExitStack()

    def follow(self, sid: str) -> bool:
        """Watch session `sid`; True when that is a change."""
        if sid == self.sid:
            return False
        self._stack.close()
        self._stack = contextlib.ExitStack()
        self.changed = self._stack.enter_context(self.manager.watch(sid))
        self.sid = sid
        return True

    async def wait(self, timeout: float) -> bool:
        """True when a change came within `timeout` seconds (clear `changed` before the read that precedes this)."""
        try:
            await asyncio.wait_for(self.changed.wait(), max(0.0, timeout))
        except TimeoutError:
            return False
        return True

    def close(self) -> None:
        self._stack.close()


def job_view(job: Any) -> dict[str, Any]:
    """A running background job as a result names it (result.build_result keeps `{id, label, owner, command,
    origin}` of the running ones), from the Job itself: its public() lists its processes, which a result never
    shows."""
    return {"id": job.id, "label": job.label, "owner": job.owner, "command": str(job.command)[:COMMAND_CHARS],
            "origin": job.origin, "status": job.status}


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


# ---- files and archive (docs/SPEC.md §21.8) -------------------------------------------------------------------------
def served(parts: tuple[str, ...], scratch: bool = False) -> bool:
    """Whether v1 lists, serves and archives the path `parts` of a session's folder (docs/SPEC.md §21.8): exactly the
    paths copyfrom.check_rel takes, which `files/{path}` checks, so the three agree: never `.git` at any depth, never
    `.scratch/_skills`, `.scratch` only with `scratch`, never the form of a short name, on a Windows hub never a name
    NTFS reads as another, never a name that is no UTF-8 text."""
    try:
        check_rel("/".join(parts), scratch=scratch)
    except CopyError:
        return False
    return True


def _skipped(asked: tuple[str, ...], scratch: bool) -> Callable[[tuple[str, ...]], bool]:
    """nofollow.walk_inside's `skip` for a walk of the folder `asked`: what `served` leaves out (never opened, a
    folder never entered)."""
    return lambda sub: not served(asked + sub, scratch)


def attachment(name: str) -> str:
    """The `Content-Disposition` of a download (docs/SPEC.md §21.8): an attachment, its name as `filename*` (RFC 5987,
    UTF-8: any name), after an ASCII `filename` for older clients (its other characters as '_')."""
    fallback = "".join(ch if ch.isascii() and (ch.isalnum() or ch in "-_.") else "_" for ch in name)[-100:]
    return f"attachment; filename=\"{fallback or 'download'}\"; filename*=UTF-8''{urllib.parse.quote(name, safe='')}"


def _download_headers(name: str, size: int | None = None) -> dict[str, str]:
    """The headers of a download or an archive (docs/SPEC.md §21.8): an attachment the client never renders."""
    headers = {"Content-Disposition": attachment(name), "X-Content-Type-Options": "nosniff",
               "Cache-Control": "no-store"}
    if size is not None:
        headers["Content-Length"] = str(size)
    return headers


def _shown_path(parts: tuple[str, ...]) -> str:
    return printable_line("/".join(parts), 200) or "."


def _no_file_text(shown: str) -> str:
    return f"No such file or folder in the session's folder: '{shown}'."


def _not_served_text(shown: str) -> str:
    return (f"'{shown}' is not served: only folders and regular files with one name are (never a link, a file with "
            "more than one name, or a special file).")


def _changed_text(shown: str) -> str:
    return (f"'{shown}' changed while it was read (a file or a folder replaced, or a link put in its place): try "
            "again.")


def _is_folder_text(shown: str) -> str:
    return (f"'{shown}' is a folder: GET /api/v1/sessions/{{id}}/files?path=<path> lists it, GET "
            "/api/v1/sessions/{id}/archive?path=<path> gives it as a zip.")


def _is_file_text(shown: str) -> str:
    return f"'{shown}' is a file: GET /api/v1/sessions/{{id}}/files/{{path}} gives its bytes."


@dataclass(frozen=True)
class _Where:
    """Where v1 reads a session's files (docs/SPEC.md §21.8): nofollow's root and the names of the session's folder
    below it (none: the root is the folder; a session that works in a server folder in place: its names below the
    resolved root of `[api] folders`, so that no link put on the way after the check is followed)."""

    root: Path
    base: tuple[str, ...] = ()


def _unreadable_text(shown: str, error: BaseException | None, then: str = "") -> str:
    """The text of a path that cannot be read: the system's words for why (never the error's own text, which may
    name a path)."""
    why = getattr(error, "strerror", None) or (type(error).__name__ if error is not None else "unknown")
    return f"'{shown}' cannot be read on the hub's computer ({str(why).rstrip('.')}){f': {then}' if then else ''}."


def _read_error(exc: OSError, where: _Where, asked: tuple[str, ...]) -> "ApiError | None":
    """The answer to an OSError nofollow raised while it read the path `asked` of a session's folder (docs/SPEC.md
    §21.8), by where it met it (its path relative to nofollow's root, which nofollow's errors name): a name replaced
    while it was opened, 409 `conflict` (try again); the session's folder itself (or a folder above it, below an
    `[api] folders` root) gone, 409 `folder_not_ready`, not readable (its permissions), 409 `folder_not_ready`, or
    reached through a link, 403 `forbidden_folder`; below it, one that cannot be read (its permissions), 409
    `conflict`; the path asked or a folder on its way missing, 404 `not_found`, a link, a file with another name or a
    special file, 404 (never served), of the other kind, 400 `bad_request`; below it (what a listing or a walk met), a
    link put in place of a folder or a file, or a path that now leads out, 409 `conflict`. A conflict quotes the path
    where it was met, from the session's folder (the client's own names). None for anything else (a 500, logged)."""
    refused = isinstance(exc, nofollow.Refused)
    rel = exc.rel if isinstance(exc, nofollow.Refused) else exc.filename
    if not isinstance(rel, str):
        return None
    met = () if rel in ("", ".") else tuple(rel.split("/"))
    depth, base, full = len(met), len(where.base), len(where.base) + len(asked)
    shown, here = _shown_path(asked), _shown_path(met[base:])
    if refused and exc.reason == "replaced":
        return ApiError("conflict", _changed_text(here))
    if isinstance(exc, PermissionError):
        if depth <= base:
            return ApiError("folder_not_ready", FOLDER_UNREADABLE_TEXT)
        return ApiError("conflict", _unreadable_text(here, exc))
    if depth <= base:
        if refused:
            return ApiError("forbidden_folder", FOLDER_LINKED_TEXT)
        if isinstance(exc, (FileNotFoundError, NotADirectoryError)):
            return ApiError("folder_not_ready", FOLDER_GONE_TEXT)
        return None
    if depth <= full:
        if refused:
            return ApiError("not_found", _not_served_text(shown))
        if isinstance(exc, FileNotFoundError) or (isinstance(exc, NotADirectoryError) and depth < full):
            return ApiError("not_found", _no_file_text(shown))
        if isinstance(exc, NotADirectoryError):
            return ApiError("bad_request", _is_file_text(shown))
        if isinstance(exc, IsADirectoryError):
            return ApiError("bad_request", _is_folder_text(shown))
        return None
    return ApiError("conflict", _changed_text(here)) if refused else None


class _Streamed(StreamingResponse):
    """A download or an archive, streamed as it is read (docs/SPEC.md §21.8): whatever ends the answer (its end, an
    error, the client gone, before the first byte too), its body is closed and `closing()` called, so no descriptor
    and no walk stays open until a garbage collection."""

    def __init__(self, content: AsyncIterator[bytes], *, closing: Callable[[], None], **kwargs: Any):
        super().__init__(content, **kwargs)
        self.closing = closing

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            body = self.body_iterator
            try:
                if hasattr(body, "aclose"):
                    await body.aclose()
            finally:
                try:
                    self.closing()
                except Exception:  # noqa: BLE001 - the answer's own outcome goes on
                    log.exception("API v1: a download could not close what it read")


async def _file_bytes(file: BinaryIO, size: int, label: str) -> AsyncIterator[bytes]:
    """The bytes of an opened file (`files/{path}`), FILE_CHUNK at a time in a thread, exactly `size` of them (the
    Content-Length sent): a file that grew meanwhile is cut there; one that shrank, or a read that fails, ends the
    answer with an error (logged), so the client sees it cut short, never a whole file that is not."""
    sent = 0
    try:
        while sent < size:
            chunk = await _thread_step(file.read, min(FILE_CHUNK, size - sent))
            if not chunk:
                raise OSError(errno.EIO, f"the file ended after {sent} of its {size} bytes")
            sent += len(chunk)
            yield chunk
    except Exception as exc:
        log.warning("API v1: the download of %s was cut: %s", label, exc)
        raise
    finally:
        file.close()


class _ZipSink:
    """Where zipfile writes an archive being streamed: no seek, no tell (zipfile then writes each file's sizes after
    its data, as a data descriptor); `take()` gives what was written since the last call."""

    def __init__(self) -> None:
        self._parts: list[bytes] = []
        self.size = 0

    def write(self, data: bytes) -> int:
        self._parts.append(bytes(data))
        self.size += len(data)
        return len(data)

    def flush(self) -> None:
        pass

    def take(self) -> bytes:
        data = b"".join(self._parts)
        self._parts.clear()
        self.size = 0
        return data


class _Cut(Exception):
    """An archive that cannot be finished whole once its answer has started (docs/SPEC.md §21.8): it is cut there."""


def _zip_info(rel: str, entry: nofollow.Entry, *, folder: bool) -> zipfile.ZipInfo:
    """The zip entry of a folder or a file of the walk: its name relative to the folder archived ('/'), its time
    (clamped to what a zip holds, 1980-2107), a Unix mode (0o755 for a folder and for a file someone may execute,
    else 0o644: never writable by others, never setuid, whatever the hub's computer says; a Windows hub says 0o666),
    deflated when it is a file."""
    try:
        when = tuple(time.localtime(entry.mtime_ns / 1e9)[:6])
    except (OverflowError, OSError, ValueError):
        when = (1980, 1, 1, 0, 0, 0)
    info = zipfile.ZipInfo(f"{rel}/" if folder else rel, min(max(when, (1980, 1, 1, 0, 0, 0)),
                                                              (2107, 12, 31, 23, 59, 58)))
    info.create_system = 3   # (Unix: unzip reads the mode below)
    mode = 0o755 if folder or entry.mode & 0o111 else 0o644
    info.external_attr = ((stat.S_IFDIR if folder else stat.S_IFREG) | mode) << 16 | (0x10 if folder else 0)
    info.compress_type = zipfile.ZIP_STORED if folder else zipfile.ZIP_DEFLATED
    info.file_size = 0 if folder else entry.size
    info.CRC = 0
    return info


def _zip_parts(where: _Where, asked: tuple[str, ...], scratch: bool, measured: Mapping[str, str],
               limit: int) -> Iterator[bytes]:
    """The zip of the folder `asked` of a session's folder (docs/SPEC.md §21.8 `archive`), in parts: the second walk
    (nofollow, what `served` takes), each file read through the walk's own open file before the walk goes on,
    exactly the size it had when it was opened (more written meanwhile is left for the next archive), deflated, each
    folder an entry of its own. What the first walk `measured` (rel -> kind) must be there still: a file or a folder
    gone, one that became a link or another kind, a file that shrank while it was read, an entry that cannot be
    read, a link swapped in (nofollow.Refused), or more than `limit` bytes, raise (_Cut, OSError) before the central
    directory is written, so the client's zip is never a valid one that lacks files or holds a torn one. What came
    meanwhile is taken. Blocking: one step at a time in a thread (_zip_bytes); a step ends (a part, b"" when there
    is no zip to send yet) once ZIP_SENT bytes of zip are ready or about FILE_CHUNK bytes were read (an entry counts
    as ZIP_ENTRY_COST), so that a client gone waits for one short step only."""
    sink = _ZipSink()
    archive = zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True)
    walk = nofollow.walk_inside(where.root, where.base + asked, skip=_skipped(asked, scratch))
    seen: set[str] = set()
    total = work = 0   # (bytes of files so far; bytes read in this step, an entry counted as ZIP_ENTRY_COST)
    try:
        for entry in walk:
            rel, was = entry.rel, measured.get(entry.rel)
            work += ZIP_ENTRY_COST
            if entry.kind == nofollow.EXCLUDED:
                continue
            if entry.kind == nofollow.ERROR:
                if was is None and isinstance(entry.error, FileNotFoundError):
                    continue   # (came and went between the two walks)
                raise _Cut(f"{rel} cannot be read any more: {entry.error}")
            if entry.kind not in (nofollow.DIR, nofollow.FILE):   # (a link, a file with another name, a special file)
                if was is not None:
                    raise _Cut(f"{rel} is no {was} any more")
                continue
            if was not in (None, entry.kind):
                raise _Cut(f"{rel} is no {was} any more")
            seen.add(rel)
            if entry.kind == nofollow.DIR:
                archive.mkdir(_zip_info(rel, entry, folder=True))
            else:
                reader = entry.reader()
                # (no force_zip64: exactly entry.size bytes are written, so zipfile's own rule from file_size holds)
                with archive.open(_zip_info(rel, entry, folder=False), "w") as out:
                    left = entry.size
                    while left:
                        chunk = reader.read(min(FILE_CHUNK, left))
                        if not chunk:
                            raise _Cut(f"{rel} shrank while it was read ({entry.size - left} of its {entry.size} "
                                       "bytes)")
                        left -= len(chunk)
                        total += len(chunk)
                        if total > limit:
                            raise _Cut(f"more than {limit} bytes ([api] archive_max_mb)")
                        out.write(chunk)
                        work += len(chunk)
                        if work >= FILE_CHUNK or sink.size >= ZIP_SENT:
                            work = 0
                            yield sink.take()
            if work >= FILE_CHUNK or sink.size >= ZIP_SENT:
                work = 0
                yield sink.take()
        missing = sorted(set(measured) - seen)
        if missing:
            raise _Cut(f"{len(missing)} file(s) or folder(s) gone meanwhile, the first {missing[0]}")
        archive.close()   # (the central directory: only for a whole archive)
        yield sink.take()
    finally:
        walk.close()


async def _zip_bytes(parts: Iterator[bytes], label: str) -> AsyncIterator[bytes]:
    """The parts of an archive (_zip_parts), each step in a thread (_thread_step: the walk is closed only once no
    thread reads it); an error ends the answer (logged), so the client's zip has no central directory."""
    try:
        while True:
            chunk = await _thread_step(next, parts, None)
            if chunk is None:
                return
            if chunk:
                yield chunk
    except Exception as exc:
        log.warning("API v1: the archive of %s was cut: %s", label, exc)
        raise
    finally:
        parts.close()


Handler = Callable[[Request, str], Awaitable[Response]]


class ApiV1:
    """The external API of a launcher (docs/SPEC.md §21): `.app` (the Starlette app the launcher dispatches /api/v1
    to), the routes, the error mapping and `aclose()`. `host`: the address the launcher is bound to."""

    def __init__(self, manager: "SessionManager", host: str = "127.0.0.1"):
        self.manager = manager
        self.host = host
        self._tasks: set[asyncio.Task] = set()
        self.closing = False    # aclose() runs: no new create starts
        self._closed = False    # aclose() ended: nothing more is spawned (manager.shutdown() may run)
        self._reserved = 0      # creates past the max_running check whose session is not marked starting yet
        self._refs: dict[str, _Ref] = {}   # the client_refs of the creates in flight
        # the results of final sessions (docs/SPEC.md §21.7), newest last, at most RESULTS_CACHED: key (_result_key)
        # -> the result without its board_url
        self._results: OrderedDict[tuple[Any, ...], dict[str, Any]] = OrderedDict()
        self._results_lock = threading.Lock()   # (results are built in threads)
        routes = [
            self._route("/api/v1/options", {"GET": self._options}),
            self._route(API_UPLOAD_PATH, {"POST": self._upload}),
            self._route("/api/v1/sessions", {"GET": self._list, "POST": self._create}),
            self._route("/api/v1/sessions/{id}", {"GET": self._get, "PATCH": self._patch, "DELETE": self._delete}),
            self._route("/api/v1/sessions/{id}/continue", {"POST": self._continue}),
            self._route("/api/v1/sessions/{id}/stop", {"POST": self._stop}),
            self._route("/api/v1/sessions/{id}/requests", {"GET": self._requests}),
            self._route("/api/v1/sessions/{id}/requests/{rid}", {"POST": self._answer}),
            self._route("/api/v1/sessions/{id}/messages", {"GET": self._messages, "POST": self._send}),
            self._route("/api/v1/sessions/{id}/events", {"GET": self._events}),
            self._route("/api/v1/sessions/{id}/events/stream", {"GET": self._stream}),
            self._route("/api/v1/sessions/{id}/result", {"GET": self._result}),
            self._route("/api/v1/sessions/{id}/files", {"GET": self._files}),
            self._route("/api/v1/sessions/{id}/files/{path:path}", {"GET": self._file}),
            self._route("/api/v1/sessions/{id}/archive", {"GET": self._archive}),
        ]
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
            if exc.redact and caller == "api":   # (the swarm's own text: in a thread, as error_response's)
                text = await asyncio.to_thread(self.redact, exc.text, request.scope.get(API_SID))
                return fail(exc.code, text, headers=exc.headers, **exc.extra)
            return exc.response()
        except CODED_ERRORS as exc:   # (the session the route resolved, if any: its folders are named in the text)
            return await self.error_response(exc, caller, request.scope.get(API_SID))
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
        any other absolute path by its last component. A route of v1 the text names (_ROUTE: "GET /api/v1/options")
        is kept as it is. Blocking (it resolves the roots, which may lie on a network share, and reads the session's
        files): a route calls it through error_response, in a thread."""
        routes: list[str] = []
        marker = "APIROUTE"
        while marker in text:
            marker += "X"

        def keep(match: re.Match) -> str:
            routes.append(match.group(0))
            return f"{marker}{len(routes) - 1}{marker}"

        def back(match: re.Match) -> str:   # (a folder name the redaction put in may spell a marker too)
            number = int(match.group(1))
            return routes[number] if number < len(routes) else match.group(0)

        public = self.manager._public_text(sid, _ROUTE.sub(keep, text), named=root_pairs(self.manager.config))
        return re.sub(f"{marker}([0-9]+){marker}", back, public) if routes else public

    async def error_response(self, exc: BaseException, caller: str, sid: str | None = None) -> JSONResponse:
        """One of CODED_ERRORS as `{"error", "code"}` (error_code); for the API caller its text through redact(), in a
        thread, so that a root on an unplugged share never stalls the hub. An `internal` one is logged, with its
        traceback unless it is the 503 of a hub shutting down (no fault of anyone's). `sid`: the session the route
        is about (its folders are named by their names). `folder_busy` is v1's own text, whoever calls: the manager's
        names the other session's folder, so its title; the answer's `session` names that session's id when the
        caller sees it (docs/SPEC.md §21.10 "One folder, one session")."""
        code = error_code(exc)
        if code == "internal":
            if getattr(exc, "status", None) == 503:
                log.info("API v1: %s", exc)
            else:
                log.error("API v1: %s", exc, exc_info=exc)
        if code == "folder_busy":
            holder = getattr(exc, "holder", None)
            uid = await asyncio.to_thread(self._uid_for, holder, caller) if isinstance(holder, str) else None
            return fail(code, FOLDER_BUSY_TEXT, **({"session": uid} if uid else {}))
        text = str(exc)
        return fail(code, await asyncio.to_thread(self.redact, text, sid) if caller == "api" else text)

    def _uid_for(self, sid: str, caller: str) -> str | None:
        """The uid of session `sid` when `caller` sees it (resolve's scope), else None. Blocking (session.json)."""
        try:
            meta = self.manager.meta(sid)
        except SessionError:
            return None
        uid = session_uid(meta)
        return uid if uid is not None and (caller == "owner" or api_meta(meta) is not None) else None

    def spawn(self, coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task:
        """A background task of a route (a session's start after its create or `continue`), which aclose() waits for
        and never cancels; its failure is logged. Once aclose() has ended nothing more is spawned (the manager's
        shutdown may be running, and nobody would wait for the task): SessionError 503, and `coro` is closed, so the
        route gives back what it reserved for it (a create's start mark)."""
        if self._closed:
            coro.close()
            raise SessionError(SHUTTING_DOWN_TEXT, 503)
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
        deadline; none is spawned once it has ended (spawn). While it runs no new create starts (503)."""
        self.closing = True
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + START_WAIT
            while True:
                pending = {task for task in self._tasks if not task.done()}
                left = deadline - loop.time()
                if not pending or left <= 0:
                    break
                await asyncio.wait(pending, timeout=left)
            if pending:
                log.warning("API v1: %d background task(s) still running at shutdown: the session manager waits for "
                            "them", len(pending))
        finally:
            self._closed = True

    # ---- sessions: the one resolver, the summary ------------------------------------------------------------------
    def resolve(self, uid: Any, caller: str) -> str | None:
        """The folder name of the session `{id}` names for `caller` (docs/SPEC.md §21.2 `{id}`), else None: the uid of
        a session (sid_of_uid: never the legacy main session, never a deleted one whose folder number came back), and
        for the API caller one created through v1 (§21.1 "Scope"). A long poll or a stream calls it again at every
        pass. Blocking (session.json)."""
        sid = self.manager.sid_of_uid(uid)
        if sid is None or sid == LEGACY_ID:
            return None
        if caller == "owner":
            return sid
        try:
            return sid if api_meta(self.manager.meta(sid)) is not None else None
        except SessionError:
            return None

    def _session(self, request: Request, caller: str) -> str:
        """The session of a route's `{id}` (resolve), else 404 `not_found`; kept in the scope, so an error text of the
        route names its folders by their names (error_response)."""
        sid = self.resolve(request.path_params.get("id"), caller)
        if sid is None:
            raise ApiError("not_found", NO_SESSION_TEXT)
        request.scope[API_SID] = sid
        return sid

    def board_url(self, sid: str) -> str | None:
        """The board of the session for people (docs/SPEC.md §21.7): the hub's address + /s/<name>/, only when the hub
        knows its public address (DREAMTEAM_PUBLIC_URL, or a loopback bind); manager.hub_url alone says nothing (it is
        set for a 0.0.0.0 bind too)."""
        base = self.manager.hub_url
        if not base or not (os.environ.get(PUBLIC_URL_ENV, "").strip() or _loopback_bind(self.host)):
            return None
        return f"{base.rstrip('/')}/s/{sid}/"

    def summary(self, sid: str) -> dict[str, Any]:
        """A session as v1 shows it (docs/SPEC.md §21.7 "Summary"), for any caller: never an absolute path (no `dir`,
        `workspace` or `copied_from` of the board's summary, whose figures it takes). SessionError 404 for a session
        that is gone. Blocking (it reads session.json and the database), as the board's summary."""
        manager = self.manager
        meta = manager.meta(sid)
        board = manager.summary(sid)
        api = api_meta(meta)
        running = sid in manager.running
        goal = board.get("goal")
        stats_goal = goal if isinstance(goal, dict) else None
        agents = (board.get("agents") or {}).get("total", 0)

        def blocking() -> bool:   # (a request that cannot be read blocks nothing, as the summary's count skips it)
            swarm = manager.swarms.get(sid)
            if swarm is None:
                return False
            try:
                return bool(blocking_requests(requests_of(swarm)))
            except (sqlite3.Error, ValueError, TypeError, KeyError, IndexError) as exc:
                log.debug("API v1: cannot read the requests of session %s: %s", sid, exc)
                return False

        state = state_of(
            starting=sid in manager.starting or sid in manager.resume_pending,
            earlier=bool(board.get("earlier_version")),
            failed=bool(api and api.get("start_error")),
            never_started=(api.get("started_at") is None) if api is not None else (not running and not agents),
            done=(stats_goal or {}).get("status") == "done",
            budget=bool(board.get("paused")) and board.get("pause_reason") == "budget",
            running=running, paused=bool(board.get("paused")), blocking=blocking)
        folder = apifolders.folder_meta(api.get("folder")) if api is not None else None
        return {
            "id": session_uid(meta), "name": sid, "title": board.get("title") or sid, "state": state,
            "final": manager.is_final(sid), "error": _error_text(api),
            "goal": {key: stats_goal.get(key, "") for key in ("title", "description", "status", "summary")}
            if stats_goal is not None else None,
            "language": board.get("language"), "model": board.get("model_profile"),
            "cost_usd": float(board.get("spent_usd") or 0.0), "max_cost_usd": board.get("max_cost_usd"),
            "max_agents": board.get("max_agents"), "created_at": meta.get("created_at"),
            "started_at": api.get("started_at") if api is not None else None,
            "finished_at": api.get("finished_at") if api is not None else None,
            "agents": agents, "tasks": dict((board.get("tasks") or {}).get("by_status") or {}),
            "requests": board.get("requests", 0),
            "answers": api_answers(api) if api is not None else None,
            "stop_when_done": api.get("stop_when_done", API_GIVEN["stop_when_done"]) if api is not None else None,
            "client_ref": api.get("client_ref") if api is not None else None,
            "via": board.get("via"), "folder": folder,
            "board_url": self.board_url(sid), "earlier_version": bool(board.get("earlier_version")),
        }

    def running_count(self) -> int:
        """How many sessions created through v1 run or start now (docs/SPEC.md §21.4 `[api] max_running`): running,
        `starting` (a create or a `continue`) or about to be resumed, whoever started them, plus the creates past the
        check whose session is not marked yet."""
        busy = self.manager.running | self.manager.starting | self.manager.resume_pending
        return len(busy & set(self.manager.api_sids())) + self._reserved

    def _check_running(self, config: "Config") -> None:
        """429 `too_many_sessions` when `[api] max_running` (0: no limit) sessions of v1 run or start already. The
        caller reserves its place right after, with no await between."""
        limit = config.api.max_running
        count = self.running_count() if limit else 0
        if limit and count >= limit:
            raise ApiError("too_many_sessions", f"{count} sessions of the API run or start already ([api] max_running "
                                                f"= {limit}): wait for one to end, or stop one.")

    def _stored_ref(self, ref: str) -> tuple[str, Any] | None:
        """(sid, request_digest) of the v1 session created with client_ref `ref`, else None: the manager's index
        (SessionManager.sid_of_client_ref), then that one session.json. Every caller sees such a session (it was made
        through v1: §21.1 "Scope"). On the loop, with no await: a create in flight cannot finish between this read and
        the reservation that follows it."""
        sid = self.manager.sid_of_client_ref(ref)
        if sid is None:
            return None
        try:
            api = api_meta(self.manager.meta(sid))
        except SessionError:
            return None
        return (sid, api.get("request_digest")) if api is not None and api.get("client_ref") == ref else None

    def _never_started(self, sid: str, exc: BaseException) -> None:
        """A created session whose start could not be spawned (the hub stopped: spawn's 503) records it as a failed
        start (`api.start_error`, `api.finished_at`; state `failed`, so a repeat of its client_ref says why), before
        its mark goes. Only while no operation holds the session's lock (a read-modify-write of session.json), with no
        await between that check and the write; the error is logged, never raised (_api_write)."""
        if isinstance(exc, SessionError) and not self.manager._lock(sid).locked():
            self.manager._api_write(sid, start_error={"code": "internal", "text": str(exc)}, finished_at=time.time())

    async def _hold_ref(self, ref: str, digest: str) -> tuple[dict[str, Any] | None, _Ref | None]:
        """A create's `client_ref` (docs/SPEC.md §21.4): (the summary of the session it already made, None) for a
        repeat, which creates nothing; (None, the reservation this create now holds) otherwise. A create with the
        same client_ref in flight: the same body waits for it, then is a repeat or goes on when it failed; another
        body is 409 `conflict` at once, and so is a stored session of another body."""
        conflict = (f"client_ref {_shown(ref)} names another job: a session was created with it from another "
                    "request body.")
        while True:
            held = self._refs.get(ref)
            if held is not None:
                if held.digest != digest:
                    raise ApiError("conflict", conflict)
                await held.done.wait()
                continue
            stored = self._stored_ref(ref)
            if stored is None:
                mine = self._refs[ref] = _Ref(digest)
                return None, mine
            sid, stored_digest = stored
            if stored_digest != digest:
                raise ApiError("conflict", conflict)
            try:
                return self.summary(sid), None
            except SessionError as exc:
                if exc.status != 404:
                    raise
                # (deleted meanwhile: look again)

    @staticmethod
    def _no_start(sid: str, exc: Exception) -> None:
        """A start in the background that raised: it is recorded in the session's `api.start_error` (state `failed`)
        by the manager, which logged a failed `swarm.start()` with its traceback: logged here at info only, any other
        exception with its traceback (the manager records a failed check before `swarm.start()` without logging it)."""
        if isinstance(exc, SessionError):
            log.info("API v1: session %s did not start: %s", sid, exc)
        else:
            log.info("API v1: session %s did not start: %s: %s", sid, type(exc).__name__, exc, exc_info=exc)

    async def _start_later(self, sid: str, mark: object) -> None:
        """A created session's start in the background (docs/SPEC.md §21.4 "Order" 11): nothing when a stop or a
        delete came first (start(marked=...)); a refused or failed start is recorded in its `api.start_error`
        (_no_start)."""
        try:
            await self.manager.start(sid, via="api", marked=mark)
        except Exception as exc:  # noqa: BLE001 - recorded in api.start_error; never an error log of its own
            self._no_start(sid, exc)

    async def _continue_later(self, sid: str, uid: str | None, mark: object, text: str) -> None:
        """A `continue` of a session that does not run, in the background (docs/SPEC.md §21.2): its own start
        (start(marked=...), a no-op when another start in flight had the session running first), which gives the text
        under the same hold of the lock (_deliver_recorded as manager.start's `then`). A start that fails shows its own
        error (`api.start_error`); one that started nothing (a stop or a delete sent after this `continue` took the
        lock first) leaves `not_running` (_not_delivered)."""
        try:
            swarm = await self.manager.start(sid, via="api", marked=mark,
                                             then=lambda swarm: self._deliver_recorded(sid, swarm, text))
        except Exception as exc:  # noqa: BLE001 - recorded in api.start_error; never an error log of its own
            self._no_start(sid, exc)
            return
        if swarm is None:
            await self._not_delivered(sid, uid)

    async def _not_delivered(self, sid: str, uid: str | None) -> None:
        """A `continue` whose start started nothing: its text never reached the session, which its `api.error` says
        (`not_running`, NOT_DELIVERED_TEXT) under the session's lock; nothing when the session is gone, or when the
        folder name is another session's now (a delete gives the folder number to the next one: `uid` tells them
        apart)."""
        async with self.manager._lock(sid):
            try:
                same = session_uid(self.manager.meta(sid)) == uid
            except SessionError:
                return
            if same:
                self._record_error(sid, "not_running", NOT_DELIVERED_TEXT)

    # ---- continue: the text delivered (docs/SPEC.md §21.2) ---------------------------------------------------------
    def _deliver(self, sid: str, swarm: "Swarm", text: str) -> None:
        """A `continue`'s text given to the running session, as the user, in process, the session's lock held (the
        caller's): what instruct.continue_step decides from swarm.snapshot(), as `dreamteam -p -r` does with its
        prompt (the goal set when there is none, a completed goal reopened with the text as the comment, a pending
        question answered with a message to the asker, else a message to the coordinator or "all"), then a resume
        when the user paused the session or a budget pause the budget now covers holds. 409 `budget_exhausted`
        before anything is given while the budget is spent; 409 `conflict` for what the swarm refuses (a ToolError,
        its text redacted for the API caller). A delivery that succeeds clears the session's `api.error` (a failed one
        of an earlier `continue`)."""
        if swarm.budget_exceeded():
            raise ApiError("budget_exhausted", budget_text(swarm.spent_usd(), swarm.limits()["max_cost_usd"]))
        step = continue_step(swarm.snapshot())
        try:
            if step.action == "set_goal":
                swarm.set_goal(USER, text)
            elif step.action == "reopen_goal":
                swarm.reopen_goal(USER, text)
            else:   # (to the asker of a pending question: Mailbox.send makes it the answer, which ends the pause)
                swarm.mail.send(USER, [step.to], text)
            if step.resume or (swarm.paused and swarm.pause_reason == "budget"):
                swarm.resume(USER)
        except ToolError as exc:
            raise ApiError("conflict", str(exc), redact=True) from None
        try:
            failed = (api_meta(self.manager.meta(sid)) or {}).get("error")
        except SessionError:
            failed = None
        if failed:
            self.manager._api_write(sid, error=None)

    def _record_error(self, sid: str, code: str, text: str) -> None:
        """A `continue` whose text did not reach the session after a start: `api.error` {code, text}, shown in the
        summary's `error` (docs/SPEC.md §21.4 session.json), logged at information. The caller holds the session's
        lock (a read-modify-write of session.json); nothing for a session not made through v1, which has no `api`."""
        log.info("API v1: the text of a continue did not reach session %s: %s", sid, text)
        self.manager._api_write(sid, error={"code": code, "text": text})

    def _deliver_recorded(self, sid: str, swarm: "Swarm", text: str) -> None:
        """_deliver right after a `continue`'s start (manager.start's `then`, the session's lock held), never raising:
        a refusal becomes the session's `api.error` with its code (a manager error's text and the swarm's own refusal
        through _public_text, as a start's error), any other exception `internal`, logged with its traceback."""
        try:
            self._deliver(sid, swarm, text)
        except ApiError as exc:
            self._record_error(sid, exc.code, self.manager._public_text(sid, exc.text) if exc.redact else exc.text)
        except CODED_ERRORS as exc:
            self._record_error(sid, error_code(exc), self.manager._public_text(sid, str(exc)))
        except Exception as exc:  # noqa: BLE001 - recorded in api.error; the session runs on
            log.exception("API v1: could not give the text of a continue to session %s", sid)
            self._record_error(sid, "internal", f"Internal error ({type(exc).__name__}): the hub's log has the "
                                                "details.")

    # ---- requests and messages (docs/SPEC.md §21.5) ----------------------------------------------------------------
    def _read_db(self, sid: str, loaded: Callable[["Swarm"], Any], stored: Callable[[Path], Any]) -> Any:
        """What a route reads from session `sid`'s database: `loaded(swarm)` for a loaded session (its own connection,
        under its Database's lock), else `stored(path)` from its file, read-only, never through manager.load (which
        would build and keep a whole Swarm); from the file also when an unload closed the connection meanwhile
        (sqlite3.ProgrammingError). Blocking: a route calls it in a thread."""
        swarm = self.manager.swarms.get(sid)
        if swarm is not None:
            try:
                return loaded(swarm)
            except sqlite3.ProgrammingError as exc:   # (unloaded meanwhile: its connection is closed)
                log.debug("API v1: session %s was unloaded while it was read (%s): read from its file", sid, exc)
        return stored(self.manager._data_dir(sid) / "swarm.db")

    def _clarify(self, sid: str) -> bool:
        """A session's [swarm] clarify (a clarify planner that waits is a request only with it on), as its summary
        reads it: its overrides on the global settings, the global settings alone when its overrides fail."""
        try:
            return bool(self.manager.config_for(sid).swarm.clarify)
        except (ValueError, SessionError):
            return bool(self.manager.config.swarm.clarify)

    @staticmethod
    def _stored_conversation(path: Path, after: int, limit: int) -> list[dict]:
        """read_user_conversation of a session's file; [] for a file it cannot read (a damaged one), as
        read_requests gives no requests and the summary counts none then (logged at debug level: clients poll)."""
        try:
            return read_user_conversation(path, after, limit)
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, IndexError) as exc:   # (a damaged file)
            log.debug("API v1: cannot read the messages of %s: %s", path, exc)
            return []

    def _give_answer(self, swarm: "Swarm", rid: str, key: tuple[Any, ...], answer: Mapping[str, Any]) -> None:
        """The answer to request `rid` (`key`: request_key's reading of it) of the running session, given as the
        user, in process, the session's lock held (the caller's). The request must wait still (requests_of, read on
        the loop with no await before the action, which the agents, on the same loop, cannot come between): an
        answered or vanished one is 404 `not_found`. A question or a message: the user's message to its member in
        reply to it (to the asker of a question pause, Mailbox.send makes it the answer, which ends the pause), and
        that message (only it: the client was shown it) read in the user's inbox; a review: Board.review. 409
        `conflict` for what the swarm refuses (a ToolError). An MCP server's request is only checked here:
        _approve_server answers it, after the lock."""
        current = next((item for item in requests_of(swarm) if item["id"] == rid), None)
        if current is None:
            raise ApiError("not_found", NO_REQUEST_TEXT)
        try:
            if key[0] in ("question", "message"):
                swarm.mail.send(USER, [current["from"]], answer["text"], reply_to=key[1])
                swarm.mail.mark_read(USER, [key[1]])
            elif key[0] == "review":
                swarm.board.review(USER, key[1], answer["approve"], answer["comment"])
        except ToolError as exc:
            raise ApiError("conflict", str(exc), redact=True) from None

    async def _approve_server(self, sid: str, swarm: "Swarm", key: tuple[Any, ...], approve: bool) -> None:
        """An MCP server's request answered (MCPForge.approve as the user, with the request's digest), right after
        _give_answer checked it under the session's lock: leaving that lock and taking the forge's free lock of the
        server never yield, so nothing comes between; while another call holds the forge's lock (an agent building
        the server again), the forge's own digest check, on an approval and on a rejection, refuses a server built
        again meanwhile. Not under the session's lock, as the board's approval: one whose working copy changed builds
        and smoke-tests it again first (up to [mcp] startup_timeout), and a stop, a delete or the hub's shutdown would
        wait for that. 409 `conflict` for what the forge refuses (a ToolError: also a second answer to the same server
        given while this one ran), `not_running` when the session stopped meanwhile (its database closed)."""
        try:
            await swarm.forge.approve(USER, key[1], approve, digest=key[2])
        except ToolError as exc:
            raise ApiError("conflict", str(exc), redact=True) from None
        except sqlite3.ProgrammingError:   # (the session stopped while the forge worked: its connection is closed)
            if self.manager.swarms.get(sid) is swarm:
                raise
            raise ApiError("not_running", NOT_RUNNING_TEXT) from None

    # ---- events and the result (docs/SPEC.md §21.6, §21.7) ----------------------------------------------------------
    def current(self, uid: Any, caller: str) -> tuple[str, dict[str, Any]] | None:
        """(sid, summary) of the session `uid` names for `caller` now (resolve, then summary), None when it names none
        (any more: a delete gives the folder number to the next session). On the loop, as the other routes build a
        summary: it reads the manager's and the swarm's state in memory, which only the loop changes."""
        sid = self.resolve(uid, caller)
        if sid is None:
            return None
        summary = self._summary_of(sid)
        return None if summary is None else (sid, summary)

    def _summary_of(self, sid: str) -> dict[str, Any] | None:
        """summary(sid), None for a session that is gone (deleted meanwhile). On the loop (current)."""
        try:
            return self.summary(sid)
        except SessionError as exc:
            if exc.status == 404:
                return None
            raise

    def events_page(self, sid: str, after: int, limit: int, detail: str, lang: str) -> dict[str, Any]:
        """A page of a poll or a stream, read after its state and `final` (docs/SPEC.md §21.6 "Polling"): the
        journal's head, then its rows (SessionManager.journal: a loaded session through its connection, any other from
        its file; SessionError 503 when the file cannot be read now), so a page read after a `final` that says no more
        holds every event of the run. {"events", "last", "more"}, each event with its line in `lang`. `detail`
        "timeline": TIMELINE's types, a notification only when it is the user's; "all": every journaled event.
        Blocking (the database, the lines): in a thread."""
        timeline = detail == "timeline"
        events, last, more = self.manager.journal(sid, after, limit, TIMELINE if timeline else None,
                                                  recipient=USER if timeline else None)
        return {"events": [event_view(event, lang) for event in events], "last": last, "more": more}

    async def read_events(self, uid: Any, caller: str, after: int, limit: int, detail: str,
                          lang: str | None) -> tuple[str, dict[str, Any], dict[str, Any]] | None:
        """One pass of a poll (docs/SPEC.md §21.6), in this order: `{id}` resolved again, the state and `final`
        (current, on the loop), then the journal's head and rows (events_page, in a thread; the lines in `lang`, else
        the session's language). (sid, summary, page: {"events", "last", "more", "state", "final"}); None when the
        id names no session for `caller` any more."""
        found = self.current(uid, caller)
        if found is None:
            return None
        sid, summary = found
        page = await asyncio.to_thread(self.events_page, sid, after, limit, detail, lang or session_lang(summary))
        return sid, summary, {**page, "state": summary["state"], "final": summary["final"]}

    def _file_head(self, path: Path) -> int | None:
        """The newest event id of a session database file (0 without one), None when it cannot be read."""
        if not path.is_file():
            return 0
        try:
            with reading_file(path, timeout=2.0) as (conn, _immutable):
                row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()
        except (sqlite3.Error, OSError) as exc:
            log.debug("API v1: cannot read the journal of %s: %s", path, exc)
            return None
        return int(row[0] or 0)

    @staticmethod
    def _lists(swarm: "Swarm") -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        with swarm.db._lock:   # (a loaded session's connection: read_lists leaves its row factory alone)
            return read_lists(swarm.db.path, swarm.db.conn)

    def changed_files(self, sid: str, meta: dict[str, Any], api: Mapping[str, Any]) -> tuple[list | None, str | None]:
        """(changed_files, changed_files_error) of a session's result (docs/SPEC.md §21.7): its working tree against
        `api.base_commit` (workspace.changed_since, with the session's own settings: git, its folder, adopted or not,
        its network setting), or null with the reason (git off or not installed, no base commit, the commit gone,
        git failed; DreamTeam's words, any path in them redacted: a result holds none). A session working in a server
        folder in place is checked first as a start checks it (SessionManager._check_server_folder, then
        apifolders.git_dir_refusal): git never reads a repository whose database lies outside the folder. Blocking
        (git): in a thread."""
        manager = self.manager
        swarm = manager.swarms.get(sid)
        try:
            config = swarm.config if swarm is not None else manager.config_for(sid)
        except (ValueError, SessionError) as exc:
            log.debug("API v1: no changed files for session %s: %s", sid, exc)
            return None, SETTINGS_TEXT
        if not config.workspace.git:
            return None, GIT_OFF_TEXT
        base = api.get("base_commit")
        if not isinstance(base, str) or not base:
            return None, NO_BASE_TEXT
        git = shutil.which("git")
        if git is None:
            return None, NO_GIT_TEXT
        folder = config.workspace_dir
        network_off = netoff.is_off(config)
        if apifolders.in_place_folder(api.get("folder")) is not None:   # (as every start checks it, §21.10)
            try:
                manager._check_server_folder(sid, meta, refusal="its changed files are not listed")
            except SessionError as exc:
                return None, self.redact(str(exc), sid)
            problem = apifolders.git_dir_refusal(folder, git, offline=isolated_git(network_off))
            if problem is not None:
                if problem.startswith(apifolders.GIT_UNREADABLE):   # (never git's own output: logged instead)
                    log.info("API v1: no changed files for session %s: %s", sid, problem)
                    problem = apifolders.GIT_UNREADABLE
                return None, self.redact(f"Its folder's repository is not read: {problem}.", sid)
        secrets = [profile.api_key_env for profile in config.models.values() if getattr(profile, "api_key_env", None)]
        try:
            files = changed_since(folder, base, adopted=config.workspace_adopted, network_off=network_off, git=git,
                                  secret_names=secrets)
        except ValueError:   # (no commit id: a hand edit of session.json)
            return None, NO_BASE_TEXT
        except BaseCommitGone:
            return None, BASE_GONE_TEXT
        except OSError as exc:
            log.info("API v1: no changed files for session %s: %s", sid, exc)
            if not os.path.lexists(folder / ".git"):
                return None, NO_REPOSITORY_TEXT
            return None, self.redact(f"git could not compare the files: {exc}.", sid)
        return files, None

    def _result_key(self, sid: str, summary: Mapping[str, Any], api: Mapping[str, Any]) -> tuple[Any, ...] | None:
        """The key a final result is cached by (docs/SPEC.md §21.7): its uid and `api.finished_at`, with its state and
        the journal's head (a PATCH of a stopped session, which writes no finished_at, may lift a budget pause, and
        a change made while the session was loaded for the board leaves an event); None for a result that is not
        cached (not final, no finished_at, a journal that cannot be read). Blocking."""
        finished = api.get("finished_at")
        if not summary["final"] or summary["id"] is None or not _number(finished):
            return None
        head = self._read_db(sid, journal_head, self._file_head)
        return None if head is None else (summary["id"], finished, summary["state"], head)

    def result(self, sid: str, summary: Mapping[str, Any], jobs: list[dict[str, Any]]) -> dict[str, Any]:
        """The result of a session (docs/SPEC.md §21.7), also while it runs (`final` false), from its `summary` and its
        running `jobs`, which the caller read on the loop (current, job_view): result.build_result with v1's state as
        `status`, the uid as `session`, the summary's goal (the goal's summary as `summary` when the state is done),
        its tasks (with `parent_id` and `crew`) and members read as the snapshot lists them (sessions.read_lists: a
        stopped session from its file, never loaded) and `duration_s` from `api.started_at` to `api.finished_at` or
        now; then `error` (always, null when none), `final`, `board_url`, `changed_files` (the first CHANGED_FILES_MAX
        by path), `changed_files_error` and `changed_files_truncated` (true when the list stops there), in that order.
        A final result is kept (RESULTS_CACHED, _result_key) unless its `changed_files_error` may pass (LASTING_TEXTS).
        SessionError 404 for a session that is gone. Blocking (the database, git): in a thread."""
        meta = self.manager.meta(sid)
        api = api_meta(meta) or {}
        key = self._result_key(sid, summary, api)
        if key is not None:
            with self._results_lock:
                cached = self._results.get(key)
                if cached is not None:
                    self._results.move_to_end(key)
            if cached is not None:
                return {**cached, "board_url": summary["board_url"]}
        tasks, agents = self._read_db(sid, self._lists, read_lists)
        files, files_error = self.changed_files(sid, meta, api)
        started, finished = api.get("started_at"), api.get("finished_at")
        duration = (round(max(0.0, (finished if _number(finished) else time.time()) - started), 3)
                    if _number(started) else None)
        goal, state = summary["goal"], summary["state"]
        result = build_result(status=state, session=summary["id"], goal=goal,
                              summary=str((goal or {}).get("summary") or "") if state == "done" else "",
                              tasks=tasks, agents=agents, jobs=jobs, cost_usd=summary["cost_usd"],
                              duration_s=duration)
        for shown, task in zip(result["tasks"], tasks):
            shown.update(parent_id=task.get("parent_id"), crew=task.get("crew"))
        truncated = files is not None and len(files) > CHANGED_FILES_MAX
        result.update(error=summary["error"], final=summary["final"], board_url=summary["board_url"],
                      changed_files=files[:CHANGED_FILES_MAX] if truncated else files,
                      changed_files_error=files_error, changed_files_truncated=truncated)
        if key is not None and files_error in LASTING_TEXTS:
            with self._results_lock:
                self._results[key] = result
                self._results.move_to_end(key)
                while len(self._results) > RESULTS_CACHED:
                    self._results.popitem(last=False)
        return result

    async def _sse(self, uid: Any, caller: str, sid: str, after: int, detail: str,
                   lang: str | None) -> AsyncIterator[str]:
        """The events of a stream (docs/SPEC.md §21.6 "Stream"), read by id as a poll reads them, window after window
        while `more` holds. Each pass reads, in this order: `{id}` resolved again (resolve), `final` (manager.is_final,
        in memory), then the journal's head and rows (events_page, in a thread); the lines are in `lang`, else in the
        session's language, read from its summary once per folder name. At the head of a session that is not final it
        waits for a wake-up of the session (manager.watch: keyed by session, never by a bus, so an unload that replaces
        the Swarm loses nothing) or the next `: ping` (PING_SECONDS without a line). Once `final` holds and nothing
        more is there: the summary, then the result built on it (in a thread, the hub's settings read again first, as
        GET result does), sent as one `event: result` (no id) only when nothing changed since that pass began (no
        wake-up, still final, `{id}` still there): else the stream reads on, so a session started again meanwhile
        never gets a result that says final. Then the end. A session that is gone (a delete) ends it without a result,
        and so do the hub's shutdown and a journal that cannot be read now (SessionError: the client reconnects with
        Last-Event-ID). It holds no Swarm, bus or connection across a wait."""
        loop = asyncio.get_running_loop()
        watch = _Watch(self.manager)
        ping_at = loop.time() + PING_SECONDS
        language = lang
        try:
            watch.follow(sid)
            while not self.closing:
                watch.changed.clear()
                now = self.resolve(uid, caller)
                if now is None:
                    return
                if watch.follow(now):   # (another folder name: the waiter moved; a wake-up may have come before)
                    sid, language = now, lang
                    continue
                if language is None:
                    summary = self._summary_of(sid)
                    if summary is None:
                        return
                    language = session_lang(summary)
                final = self.manager.is_final(sid)
                page = await asyncio.to_thread(self.events_page, sid, after, STREAM_PAGE, detail, language)
                after = page["last"]
                if page["events"]:
                    yield "".join(sse_event(event) for event in page["events"])
                    ping_at = loop.time() + PING_SECONDS
                if page["more"]:
                    continue
                if final:
                    self.manager._refresh_settings()
                    summary = self._summary_of(sid)
                    if summary is None:
                        return
                    result = await asyncio.to_thread(self.result, sid, summary, [])
                    if (watch.changed.is_set() or not summary["final"] or not self.manager.is_final(sid)
                            or self.resolve(uid, caller) != sid):
                        continue
                    yield f"event: result\ndata: {safe_json(result)}\n\n"
                    return
                if await watch.wait(ping_at - loop.time()):
                    await asyncio.sleep(SETTLE_SECONDS)
                    continue
                yield ": ping\n\n"
                ping_at = loop.time() + PING_SECONDS
        except SessionError as exc:   # (gone, or the journal cannot be read now: the client reconnects)
            log.info("API v1: the event stream of session %s ended: %s", sid, exc)
        except Exception:   # (the answer has started: the client sees the stream end and reconnects)
            log.exception("API v1: the event stream of session %s failed", sid)
        finally:
            watch.close()

    # ---- files and archive (docs/SPEC.md §21.8) ---------------------------------------------------------------------
    def _where(self, sid: str) -> _Where:
        """Where the files of session `sid` are read from (docs/SPEC.md §21.8): its folder, which nofollow opens
        without following its own name; for a session that works in a server folder in place, the folder checked as
        a start checks it (SessionManager._check_server_folder, with the settings the caller read again on the loop:
        403 `forbidden_folder`, 409 `folder_not_ready`; its texts end in FILES_REFUSAL), then reached from its resolved
        root name by name, the names as the resolved folder spells them (a folder on the way renamed in letter case
        only is still read), so that a link put on the way after the check is met, never followed. A folder inside
        its root by file identity only (a bind mount, another spelling) is 403 `forbidden_folder`: it is never read
        by its own path after the check. 409 `folder_not_ready` for a folder that is not there. Blocking
        (session.json, the folder): in a thread."""
        manager = self.manager
        meta = manager.meta(sid)
        info = manager.workspace_of(sid, meta)
        checked = manager._check_server_folder(sid, meta, refusal=FILES_REFUSAL)
        if checked is not None:
            root, real = checked
            try:
                return _Where(root, real.relative_to(root).parts)
            except ValueError:
                raise ApiError("forbidden_folder", FOLDER_LINKED_TEXT) from None
        if not info["exists"]:
            raise ApiError("folder_not_ready", FOLDER_GONE_TEXT)
        return _Where(Path(info["path"]))

    def _listing(self, sid: str, asked: tuple[str, ...], scratch: bool) -> list[dict[str, Any]]:
        """GET files: the folders and the regular files with one name of the folder `asked` (nofollow.list_inside: no
        link, no file with another name, no special file), what `served` takes, in name order, as
        {name, path, type: "file" | "dir", size (0 for a folder), modified (Unix seconds)}. Blocking: in a thread."""
        where = self._where(sid)
        try:
            found = nofollow.list_inside(where.root, where.base + asked)
        except OSError as exc:
            raise (_read_error(exc, where, asked) or exc) from None
        entries = []
        for entry in found:
            parts = asked + entry.parts
            if served(parts, scratch):
                folder = entry.kind == nofollow.DIR
                entries.append({"name": entry.name, "path": "/".join(parts), "type": "dir" if folder else "file",
                                "size": 0 if folder else entry.size, "modified": entry.mtime_ns / 1e9})
        return entries

    def _open_file(self, sid: str, asked: tuple[str, ...]) -> tuple[BinaryIO, int]:
        """GET files/{path}: the file `asked` opened for reading below the session's folder without following any
        link (nofollow.open_inside: never a link, a file with another name or a special file), and its size then.
        The caller closes it. Blocking: in a thread (_thread_step, which closes it when the request is cancelled)."""
        where = self._where(sid)
        try:
            fd = nofollow.open_inside(where.root, where.base + asked)
        except OSError as exc:
            raise (_read_error(exc, where, asked) or exc) from None
        try:   # (fstat first: a FileIO made is the descriptor's only owner, which closes it)
            size = os.fstat(fd).st_size
            file = io.FileIO(fd, "rb", closefd=True)
        except BaseException:
            os.close(fd)
            raise
        return file, size

    def _measure(self, sid: str, asked: tuple[str, ...], scratch: bool,
                 limit_mb: int) -> tuple[_Where, dict[str, str]]:
        """An archive's first walk (docs/SPEC.md §21.8 `archive`), before any byte is sent: what `served` takes below
        the folder `asked`, measured without opening a file (nofollow.walk_inside, open_files False). 413
        `too_large` past `limit_mb` MiB of files ([api] archive_max_mb); 409 `conflict` for an entry that cannot be
        read (the archive would lack it). (where it read, {rel: "dir" | "file"} of what the second walk must find
        again). Blocking: in a thread."""
        where = self._where(sid)
        measured: dict[str, str] = {}
        total = 0
        walk = nofollow.walk_inside(where.root, where.base + asked, skip=_skipped(asked, scratch), open_files=False)
        try:
            for entry in walk:
                if entry.kind == nofollow.FILE:
                    total += entry.size
                    if total > limit_mb << 20:
                        raise ApiError("too_large", f"The folder holds more than [api] archive_max_mb "
                                                    f"({limit_mb} MB) of files: ask for a folder inside it "
                                                    "(archive?path=), or for its files one by one (files/{path}).")
                if entry.kind in (nofollow.DIR, nofollow.FILE):
                    measured[entry.rel] = entry.kind
                elif entry.kind == nofollow.ERROR and not isinstance(entry.error, FileNotFoundError):
                    raise ApiError("conflict", _unreadable_text(_shown_path(asked + entry.parts), entry.error,
                                                                "an archive would lack it"))
        except OSError as exc:
            raise (_read_error(exc, where, asked) or exc) from None
        finally:
            walk.close()
        return where, measured

    # ---- routes ---------------------------------------------------------------------------------------------------
    async def _options(self, request: Request, caller: str) -> Response:
        """GET /api/v1/options (docs/SPEC.md §21.2): the hub's global settings, a hand edit picked up first."""
        query_fields(request)
        config = self.manager.global_config()
        skills = await asyncio.to_thread(library_skills, config)
        return JSONResponse(options_view(config, skills))

    async def _upload(self, request: Request, caller: str) -> Response:
        """POST /api/v1/uploads?filename=<name>[&unpack=true] (docs/SPEC.md §21.3): the raw body kept as one file of
        this caller's for an hour, in v1's own store, under `[workspace] copy_max_mb` and `[api] uploads_pending`."""
        query = query_fields(request, ("filename", "unpack"))
        unpack = query_flag(query.get("unpack"), "unpack")
        kind = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if kind not in UPLOAD_TYPES:   # (the guard says so too; this holds wherever the app is mounted)
            raise ApiError("unsupported_media_type", f"Content-Type must be {' or '.join(UPLOAD_TYPES)}.")
        config = self.manager.global_config()   # (a hand edit of the limits is picked up)
        try:
            data = await self.manager.api_uploads.receive(
                request.stream(), filename=query.get("filename"), length=request.headers.get("content-length"),
                limits=CopyLimits.from_config(config), kind="file", unpack=unpack,
                max_pending=config.api.uploads_pending, caller=caller)
        except ClientDisconnect:   # (receive removed the partial file already)
            raise ApiError("bad_request", "The upload was interrupted.") from None
        return JSONResponse(data, status_code=201)

    async def _list(self, request: Request, caller: str) -> Response:
        """GET /api/v1/sessions?state=&limit= (docs/SPEC.md §21.2): the sessions the caller sees, newest first by
        created_at; `state` keeps those in that state (§21.7)."""
        query = query_fields(request, ("state", "limit"))
        state = query.get("state")
        if state is not None and state not in STATES:
            raise ApiError("bad_request", f"'state' must be one of {', '.join(STATES)}.")
        limit = query_count(query.get("limit"), "limit", SESSIONS_DEFAULT, SESSIONS_MAX)
        manager = self.manager
        sids = manager.api_sids() if caller == "api" else [sid for sid in manager.ids() if sid != LEGACY_ID]
        dated = []
        for sid in sids:
            try:
                created = manager.meta(sid).get("created_at")
            except SessionError:   # (gone meanwhile, or damaged: no uid to name it by)
                continue
            dated.append((created if isinstance(created, (int, float)) and not isinstance(created, bool) else 0.0,
                          sid))
        found: list[dict[str, Any]] = []
        for _created, sid in sorted(dated, reverse=True):
            try:
                item = self.summary(sid)
            except SessionError as exc:
                log.debug("API v1: session %s is not listed: %s", sid, exc)
                continue
            except Exception:  # noqa: BLE001 - one broken session must not break the list (as manager.list())
                log.exception("API v1: cannot summarize session %s: it is not listed", sid)
                continue
            if item["id"] is not None and (state is None or item["state"] == state):
                found.append(item)
                if len(found) >= limit:
                    break
        return JSONResponse({"sessions": found})

    async def _create(self, request: Request, caller: str) -> Response:
        """POST /api/v1/sessions (docs/SPEC.md §21.4), in the order of §21.4 "Order", so that a refused create costs
        nothing: the body; client_ref; the overrides; the skills; the server folder resolved (apifolders,
        §21.10); the uploads checked; max_running checked and reserved; the uploads claimed; manager.create(start=False,
        mark=...) with the folder to copy among the sources (read below its root without following a link), or the
        folder to work in in place as its workspace, checked again inside its root (`within`); the claims settled;
        202, the start in the background (spawn). 200 with the existing session for a repeated client_ref."""
        if self.closing:
            raise SessionError(SHUTTING_DOWN_TEXT, 503)
        query_fields(request)
        body = await read_json(request)
        check_fields(body, CREATE_FIELDS)
        ask = CreateRequest.parse(body)
        # (the folder as parsed: {"root": "jobs"} and {"root": "jobs", "path": null, "mode": "copy"} are one request)
        digest = request_digest({**body, "folder": ask.folder} if ask.folder is not None else body)
        held: _Ref | None = None
        if ask.client_ref is not None:
            repeat, held = await self._hold_ref(ask.client_ref, digest)
            if repeat is not None:
                return JSONResponse({"session": repeat})
        try:
            manager = self.manager
            config = manager.global_config()
            overrides = manager.validated_overrides(overrides_from(body, config, strict=True))
            skills = await asyncio.to_thread(skill_refs, ask.skills, config) if ask.skills else []
            folder: FolderRequest | None = None
            if ask.folder is not None:   # resolved and checked (docs/SPEC.md §21.10), checked again by the create
                folder = await asyncio.to_thread(apifolders.resolve_request, ask.folder["root"], ask.folder["path"],
                                                 ask.folder["mode"], config=config, home=manager.home)
            uploads = manager.api_uploads
            if ask.files:   # every token checked, none claimed yet: a refusal here uses nothing up
                await asyncio.to_thread(uploads.peek_many, ask.files, caller=caller)
            copied = [folder.source()] if folder is not None and folder.mode == "copy" else []
            in_place = folder is not None and folder.mode == "in_place"
            self._check_running(config)
            self._reserved += 1   # (no await since the check)
            try:
                claimed = await claim_uploads(uploads, ask.files, caller) if ask.files else []
                mark = object()
                created = False
                try:
                    made = await manager.create(
                        ask.title, ask.goal, ask.description, overrides=overrides, start=False, via="api",
                        skills=skills or None, sources=(copied + claimed) or None, mark=mark,
                        workspace=str(folder.target) if in_place else None,
                        within=folder.root_real if in_place else None,
                        api={"answers": ask.answers, "stop_when_done": ask.stop_when_done,
                             "client_ref": ask.client_ref, "request_digest": digest,
                             "folder": folder.meta() if folder is not None else None})
                    created = True
                finally:   # (synchronous: any outcome settles every claim, docs/SPEC.md §21.3 "Life")
                    uploads.settle(claimed, used=created)
            finally:
                self._reserved -= 1   # (the session is `starting` now, with its mark)
            sid = made["id"]
            try:
                self.spawn(self._start_later(sid, mark), name=f"api-start-{sid}")
            except BaseException as exc:
                self._never_started(sid, exc)
                manager.unmark_starting(sid, mark)
                raise
            return JSONResponse({"session": self.summary(sid)}, status_code=202)
        finally:
            if held is not None:
                self._refs.pop(ask.client_ref, None)
                held.done.set()

    async def _get(self, request: Request, caller: str) -> Response:
        """GET /api/v1/sessions/{id}: its summary."""
        query_fields(request)
        return JSONResponse({"session": self.summary(self._session(request, caller))})

    async def _patch(self, request: Request, caller: str) -> Response:
        """PATCH /api/v1/sessions/{id} {max_cost_usd?, max_agents?, wrap_up?} (docs/SPEC.md §21.2): what
        `swarm_control` does with them, as the user (manager.control, under the session's lock; a stopped session is
        loaded to write them into its database, then unloaded again). Its own 415 first: the guard's covers POST
        only. A field that is null is not given. 400 for a value of the wrong kind or one the swarm refuses, 409
        `conflict` for `wrap_up: true` before the first start, `earlier_version` for a session of an earlier
        version."""
        check_json_type(request)
        query_fields(request)
        body = await read_json(request)
        check_fields(body, PATCH_FIELDS)
        sid = self._session(request, caller)
        await self.manager.control(sid, max_cost_usd=body.get("max_cost_usd"), max_agents=body.get("max_agents"),
                                   wrap_up=body.get("wrap_up"))
        return JSONResponse({"session": self.summary(sid)})

    async def _continue(self, request: Request, caller: str) -> Response:
        """POST /api/v1/sessions/{id}/continue {"text"} (docs/SPEC.md §21.2): the session's next instruction, as
        `dreamteam -p -r <id> "<text>"` gives its prompt (_deliver). The summary answers, whatever the path:
        - running: the text is given at once, under the session's lock (409 `budget_exhausted` or `conflict` when it
          cannot be, nothing given); a session that stopped while this request waited for the lock is taken as one
          that does not run (below);
        - not running (stopped, `starting`: a create or another `continue` in flight, or about to be resumed as the
          hub starts): 503 while the hub stops; then, with no await between, `[api] max_running` for a session made
          through v1 that holds no place in it yet (429), the budget (409 `budget_exhausted` while it is spent: raise
          it with PATCH first), the session marked `starting` with a mark of this request's own, and its start in the
          background (_continue_later), which gives the text under the same hold of the lock (manager.start's
          `then`): queued on the lock in the order the requests came, behind a start in flight (then a no-op), before
          a stop sent after it, and after a stop sent before it (which leaves this mark alone).
        In the background a text that cannot be given is the session's `api.error` (its summary's `error`): what the
        swarm refuses, a spent budget, or `not_running` when a stop sent after it took the lock first; a start that
        fails shows its own error instead. 409 `earlier_version` for a session of an earlier version."""
        query_fields(request)
        body = await read_json(request)
        check_fields(body, CONTINUE_FIELDS)
        text = continue_text(body)
        sid = self._session(request, caller)
        manager = self.manager
        manager._refuse_earlier(sid)
        if sid in manager.running:
            delivered = False
            async with manager._lock(sid):
                swarm = manager.swarms.get(sid)
                if sid in manager.running and swarm is not None:   # (else stopped meanwhile: as a stopped one, below)
                    self._deliver(sid, swarm, text)
                    delivered = True
            if delivered:
                return JSONResponse({"session": self.summary(sid)})
        if self.closing:
            raise SessionError(SHUTTING_DOWN_TEXT, 503)
        meta = manager.meta(sid)
        held = sid in manager.starting or sid in manager.resume_pending   # (its place in max_running is held already)
        if api_meta(meta) is not None and not held:
            self._check_running(manager.global_config())
        board = manager.summary(sid)
        limit, spent = float(board.get("max_cost_usd") or 0.0), float(board.get("spent_usd") or 0.0)
        if limit > 0 and spent >= limit:
            raise ApiError("budget_exhausted", budget_text(spent, limit))
        mark = manager.mark_starting(sid)   # (no await since the checks: max_running counts it from here)
        try:
            self.spawn(self._continue_later(sid, session_uid(meta), mark, text), name=f"api-continue-{sid}")
        except BaseException:
            manager.unmark_starting(sid, mark)
            raise
        return JSONResponse({"session": self.summary(sid)})

    async def _stop(self, request: Request, caller: str) -> Response:
        """POST /api/v1/sessions/{id}/stop (docs/SPEC.md §21.2): manager.stop(reason="user"), its background jobs with
        it; a body-less JSON POST (any field is unknown). 409 `earlier_version` for a session of an earlier version."""
        query_fields(request)
        check_fields(await read_json(request), ())
        sid = self._session(request, caller)
        await self.manager.stop(sid, reason="user")
        return JSONResponse({"session": self.summary(sid)})

    async def _delete(self, request: Request, caller: str) -> Response:
        """DELETE /api/v1/sessions/{id}?confirm={id} (docs/SPEC.md §21.2): manager.delete(skills="discard"), the
        session's folder with it, except a server folder it worked in in place (§21.10), which is kept and named:
        `folder_kept: {root, path}`."""
        query = query_fields(request, ("confirm",))
        sid = self._session(request, caller)
        if query.get("confirm") != request.path_params.get("id"):
            raise ApiError("bad_request", "Deleting a session takes ?confirm=<its id>.")
        try:
            folder = apifolders.in_place_folder((api_meta(self.manager.meta(sid)) or {}).get("folder"))
        except SessionError:
            folder = None
        kept = await self.manager.delete(sid, skills="discard", via="api")
        answer: dict[str, Any] = {"ok": True}
        if folder is not None and kept is not None:
            answer["folder_kept"] = {"root": folder["root"], "path": folder["path"]}
        return JSONResponse(answer)

    async def _requests(self, request: Request, caller: str) -> Response:
        """GET /api/v1/sessions/{id}/requests (docs/SPEC.md §21.5): what waits for the user now, oldest first; a loaded
        session's from its swarm (requests_of), any other's from its file (read_requests with its [swarm] clarify:
        nothing from a file it cannot read, as its summary's count); of a session that does not run only those that
        hold its work (listed_requests: a member's message can be answered only while it runs). In a thread: the file
        may be large."""
        query_fields(request)
        sid = self._session(request, caller)
        found = await asyncio.to_thread(self._read_db, sid, requests_of,
                                        lambda path: read_requests(path, clarify=self._clarify(sid)))
        return JSONResponse({"requests": listed_requests(found, running=sid in self.manager.running)})

    async def _answer(self, request: Request, caller: str) -> Response:
        """POST /api/v1/sessions/{id}/requests/{rid} (docs/SPEC.md §21.5): the user's answer to one request, `{"text"}`
        for a question or a message, `{"approve", "comment"?}` for a review, `{"approve"}` for an MCP server
        (answer_of), given by _give_answer under the session's lock (an MCP server's by _approve_server right after
        it); `{"ok": true, "session"}`. In this order: the query, the body, the id (an id that request_key cannot read
        names no request: 404 `not_found`), the body's fields for the kind it names (400), the session (404),
        `earlier_version` (409), then the session must run (409 `not_running`, also when it stopped while this waited
        for its lock) and the request must wait still (404)."""
        query_fields(request)
        body = await read_json(request)
        rid = request.path_params.get("rid")
        key = request_key(rid)
        if key is None:
            raise ApiError("not_found", NO_REQUEST_TEXT)
        answer = answer_of(key[0], body)
        sid = self._session(request, caller)
        manager = self.manager
        manager._refuse_earlier(sid)
        given: "Swarm | None" = None
        if sid in manager.running:
            async with manager._lock(sid):
                swarm = manager.swarms.get(sid)
                if sid in manager.running and swarm is not None:
                    self._give_answer(swarm, rid, key, answer)
                    given = swarm
        if given is None:
            raise ApiError("not_running", NOT_RUNNING_TEXT)
        if key[0] == "mcp":   # (no await since the check under the lock)
            await self._approve_server(sid, given, key, answer["approve"])
        return JSONResponse({"ok": True, "session": self.summary(sid)})

    async def _messages(self, request: Request, caller: str) -> Response:
        """GET /api/v1/sessions/{id}/messages?after=&limit= (docs/SPEC.md §21.5 "Messages"): the user's conversation
        with the members after message `after`, oldest first (Mailbox.user_conversation; a session that is not loaded
        from its file, read_user_conversation: nothing from a file it cannot read, as GET requests), at most `limit`
        (1 to MESSAGES_MAX, default MESSAGES_DEFAULT); `last` is the newest id it gives, else `after`. In a thread:
        the file may be large."""
        query = query_fields(request, ("after", "limit"))
        after = query_after(query.get("after"))
        limit = query_count(query.get("limit"), "limit", MESSAGES_DEFAULT, MESSAGES_MAX)
        sid = self._session(request, caller)
        found = await asyncio.to_thread(self._read_db, sid, lambda swarm: swarm.mail.user_conversation(after, limit),
                                        lambda path: self._stored_conversation(path, after, limit))
        messages = [message_view(item) for item in found]
        return JSONResponse({"messages": messages, "last": messages[-1]["id"] if messages else after})

    async def _send(self, request: Request, caller: str) -> Response:
        """POST /api/v1/sessions/{id}/messages {"to", "text", "reply_to"?} (docs/SPEC.md §21.5): a message of the user,
        as the board's user sends it (Mailbox.send: one to the asker of a question pause is its answer), under the
        session's lock; 201 `{"message"}`. Its checks come in the order of an answer's (_answer): 409 `not_running`
        for a session that does not run (`continue` gives a stopped one its next instruction); 400 `bad_request` for
        what the mailbox refuses (an unknown or retired member, a reply_to that names no message)."""
        query_fields(request)
        to, text, reply_to = message_of(await read_json(request))
        sid = self._session(request, caller)
        manager = self.manager
        manager._refuse_earlier(sid)
        if sid in manager.running:
            async with manager._lock(sid):
                swarm = manager.swarms.get(sid)
                if sid in manager.running and swarm is not None:
                    try:
                        sent = swarm.mail.send(USER, to, text, reply_to)
                    except ToolError as exc:
                        raise ApiError("bad_request", str(exc), redact=True) from None
                    return JSONResponse({"message": message_view(sent)}, status_code=201)
        raise ApiError("not_running", NOT_RUNNING_TEXT)

    async def _events(self, request: Request, caller: str) -> Response:
        """GET /api/v1/sessions/{id}/events?after=&wait=&limit=&detail=&lang= (docs/SPEC.md §21.6 "Polling"): the
        events after id `after` (0: from the oldest), at most `limit` (1 to EVENTS_MAX, default EVENTS_DEFAULT), with
        `last`, `more`, `state` and `final` (read_events). With `wait` (0 to WAIT_MAX seconds) it waits only when
        nothing can be answered yet: no row, the head reached (`more` false, `after` not past it) and the state not
        final; it answers on rows, on a change of the state or of `final`, or at the deadline, reading again (the id
        resolved again) at every wake-up of the session (manager.watch: a new event, a change, the end of an
        operation that held its lock). 404 once the id names no session (a delete meanwhile); a journal that cannot be
        read now is SessionError 503 (`internal`), never an empty page."""
        query = query_fields(request, EVENTS_FIELDS)
        after = query_after(query.get("after"))
        wait = query_wait(query.get("wait"))
        limit = query_count(query.get("limit"), "limit", EVENTS_DEFAULT, EVENTS_MAX)
        detail = query_choice(query.get("detail"), "detail", DETAILS, "timeline")
        lang = query_choice(query.get("lang"), "lang", tuple(LANGS), None)
        uid = request.path_params.get("id")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        watch = _Watch(self.manager)
        seen: tuple[Any, Any] | None = None
        try:
            watch.follow(self._session(request, caller))
            while True:
                watch.changed.clear()
                found = await self.read_events(uid, caller, after, limit, detail, lang)
                if found is None:
                    raise ApiError("not_found", NO_SESSION_TEXT)
                sid, _summary, page = found
                request.scope[API_SID] = sid
                if watch.follow(sid):
                    continue
                now = (page["state"], page["final"])
                left = deadline - loop.time()
                if (page["events"] or page["more"] or page["last"] < after or page["final"] or left <= 0
                        or (seen is not None and now != seen)):
                    return SafeJSONResponse(page)
                seen = seen or now
                if await watch.wait(left):
                    await asyncio.sleep(SETTLE_SECONDS)
        finally:
            watch.close()

    async def _stream(self, request: Request, caller: str) -> Response:
        """GET /api/v1/sessions/{id}/events/stream?after=&detail=&lang= (docs/SPEC.md §21.6 "Stream"): Server-Sent
        Events (_sse) after `after`, or after the `Last-Event-ID` header when it is there (a client's EventSource sends
        it as it reconnects, with the URL of the first connect: the header wins). Its checks and the session come
        before the answer starts (400, 404)."""
        query = query_fields(request, STREAM_FIELDS)
        after = query_after(query.get("after"))
        resumed = request.headers.get("last-event-id", "").strip()
        if resumed:
            after = query_after(resumed, "Last-Event-ID")
        detail = query_choice(query.get("detail"), "detail", DETAILS, "timeline")
        lang = query_choice(query.get("lang"), "lang", tuple(LANGS), None)
        sid = self._session(request, caller)
        return StreamingResponse(self._sse(request.path_params.get("id"), caller, sid, after, detail, lang),
                                 media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def _result(self, request: Request, caller: str) -> Response:
        """GET /api/v1/sessions/{id}/result (docs/SPEC.md §21.7): the result, also while the session runs (`final`
        false); the hub's settings read again first (a hand edit of `[api] folders` or `[workspace] git` counts at
        once, as for a start), its summary and its running jobs read here, on the loop, the rest in a thread
        (result)."""
        query_fields(request)
        sid = self._session(request, caller)
        self.manager._refresh_settings()
        summary = self.summary(sid)
        swarm = self.manager.swarms.get(sid)
        jobs = ([job_view(job) for job in swarm.jobs.running_of()]
                if swarm is not None and sid in self.manager.running else [])
        return SafeJSONResponse(await asyncio.to_thread(self.result, sid, summary, jobs))

    async def _files(self, request: Request, caller: str) -> Response:
        """GET /api/v1/sessions/{id}/files?path=&scratch= (docs/SPEC.md §21.8): `{"path", "entries"}` of a folder of
        the session's folder (_listing). In this order: the query, the path (copyfrom.check_rel: 400 `bad_name`), the
        session (404), then the folder read in a thread, the hub's settings read again first (an in-place folder is
        checked as a start checks it). A stopped session is never loaded."""
        query = query_fields(request, FILES_FIELDS)
        scratch = query_flag(query.get("scratch"), "scratch")
        asked = check_rel(query.get("path"), scratch=scratch)
        sid = self._session(request, caller)
        self.manager._refresh_settings()
        entries = await asyncio.to_thread(self._listing, sid, asked, scratch)
        return JSONResponse({"path": "/".join(asked), "entries": entries})

    async def _file(self, request: Request, caller: str) -> Response:
        """GET /api/v1/sessions/{id}/files/{path}?scratch= (docs/SPEC.md §21.8): the file's bytes, streamed FILE_CHUNK
        at a time, as an attachment that is never rendered (`application/octet-stream`, `nosniff`, `no-store`), with
        its Content-Length; a folder is 400 `bad_request` (list it, or ask for its archive). Checked in the order of
        GET files."""
        query = query_fields(request, FILE_FIELDS)
        scratch = query_flag(query.get("scratch"), "scratch")
        asked = check_rel(request.path_params.get("path"), scratch=scratch)
        if not asked:
            raise ApiError("bad_request", _is_folder_text("."))
        sid = self._session(request, caller)
        self.manager._refresh_settings()
        file, size = await _thread_step(self._open_file, sid, asked, release=lambda opened: opened[0].close())
        return _Streamed(_file_bytes(file, size, f"{'/'.join(asked)} of session {sid}"), closing=file.close,
                         media_type="application/octet-stream", headers=_download_headers(asked[-1], size))

    async def _archive(self, request: Request, caller: str) -> Response:
        """GET /api/v1/sessions/{id}/archive?path=&scratch= (docs/SPEC.md §21.8): a zip of the session's folder, or of
        the folder `path` in it, with what GET files lists at every level, streamed as it is built (_zip_parts). It is
        measured first (_measure: 413 `too_large` past `[api] archive_max_mb` before any byte); an error once the
        answer has started cuts it (the zip then lacks its central directory). Checked in the order of GET files."""
        query = query_fields(request, ARCHIVE_FIELDS)
        scratch = query_flag(query.get("scratch"), "scratch")
        asked = check_rel(query.get("path"), scratch=scratch)
        sid = self._session(request, caller)
        limit_mb = self.manager.global_config().api.archive_max_mb   # (a hand edit is picked up)
        where, measured = await asyncio.to_thread(self._measure, sid, asked, scratch, limit_mb)
        parts = _zip_parts(where, asked, scratch, measured, limit_mb << 20)
        return _Streamed(_zip_bytes(parts, f"{'/'.join(asked) or '.'} of session {sid}"), closing=parts.close,
                         media_type="application/zip", headers=_download_headers(f"{asked[-1] if asked else sid}.zip"))


__all__ = ["ANSWER_FIELDS", "API_SID", "API_VERSION", "ARCHIVE_FIELDS", "BASE_GONE_TEXT", "CHANGED_FILES_MAX",
           "CLIENT_REF_MAX", "CODED_ERRORS", "CODES", "CONTINUE_FIELDS", "CREATE_FIELDS", "DETAILS", "EVENTS_DEFAULT",
           "EVENTS_FIELDS", "EVENTS_MAX", "FILES_FIELDS", "FILES_PER_SESSION", "FILES_REFUSAL", "FILE_CHUNK",
           "FILE_FIELDS", "FOLDER_BUSY_TEXT", "FOLDER_FIELDS", "FOLDER_GONE_TEXT", "FOLDER_LINKED_TEXT",
           "FOLDER_UNREADABLE_TEXT", "GIT_OFF_TEXT", "ID_MAX", "JSON_MAX", "LASTING_TEXTS", "MESSAGES_DEFAULT",
           "MESSAGES_MAX", "MESSAGE_FIELDS", "NOT_DELIVERED_TEXT", "NOT_RUNNING_TEXT", "NO_BASE_TEXT", "NO_GIT_TEXT",
           "NO_REPOSITORY_TEXT", "NO_REQUEST_TEXT", "PATCH_FIELDS", "PING_SECONDS", "RESULTS_CACHED",
           "SESSIONS_DEFAULT", "SESSIONS_MAX", "SETTINGS_TEXT", "SETTLE_SECONDS", "START_WAIT", "STATES",
           "STATUS_CODES", "STREAM_FIELDS", "STREAM_PAGE", "TIMELINE", "TO_MAX", "UPLOAD_TYPES", "WAIT_MAX",
           "ZIP_ENTRY_COST", "ZIP_SENT", "ApiError", "ApiV1", "CreateRequest", "SafeJSONResponse", "answer_of",
           "attachment", "budget_text", "check_fields", "check_json_type", "claim_uploads", "continue_text",
           "error_code", "event_view", "fail", "job_view", "library_skills", "message_of", "message_view",
           "options_view", "query_after", "query_choice", "query_count", "query_fields", "query_flag", "query_wait",
           "read_json", "request_digest", "required_text", "root_pairs", "safe_json", "served", "session_lang",
           "skill_refs", "sse_event", "state_of"]
