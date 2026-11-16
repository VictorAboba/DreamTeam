"""The external HTTP API v1 (docs/SPEC.md §21): `ApiV1(manager, host)`.

The launcher dispatches every path `web.is_api_path` takes to `ApiV1.app` (§21.1 "Dispatch"), behind the guard's v1
branch, which has already said who calls (`scope[web.API_CALLER]`: "owner" or "api"). The app is a Starlette app of
its own: its routes, `redirect_slashes` off (a bare `/api/v1` is a JSON 404, never a 307) and an `HTTPException`
handler, so that a wrong path or method is JSON too. Every answer under `/api/v1/` is JSON; an error is
`{"error": text, "code": code}` with the code's status (CODES). This module also holds what every route uses: the JSON
body read up to JSON_MAX, the check of a body's or a query's fields (`unknown_field`), the mapping of the manager's
errors to codes with the redaction of paths for the API caller, the one resolver of a session's `{id}`, the summary
and its state (§21.7), and `aclose()`, which waits for the starts in flight and never cancels them. It uses the
manager in process, never the internal routes over HTTP, and never imports dreamteam.tui."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Coroutine, Iterable, Mapping

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response
from starlette.routing import Route

from . import __version__, roles
from .autoreply import blocking_requests, requests_of
from .config import _joins_text, native_absolute
from .copyfrom import API_UPLOAD_PATH, CopyError, CopyLimits, Source, Uploads
from .goal_text import split_goal
from .httpjson import JSONResponse
from .instruct import continue_step
from .mailbox import MAX_TEXT
from .models import USER, ToolError
from .sessionopts import OptionError, overrides_from
from .sessions import API_ANSWERS, API_GIVEN, LEGACY_ID, TITLE_LIMIT, SessionError, api_answers, api_meta, session_uid
from .skills import NAME_MAX, NAME_RE, Library, SkillError
from .uitext import LANGS
from .web import API_CALLER, API_CALLERS, API_TOKEN_TEXT, BEARER_REALM, JSON_TYPE_TEXT, PUBLIC_URL_ENV, _loopback_bind

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
CLIENT_REF_MAX = 128        # characters of a create's client_ref
API_SID = "dreamteam.api_sid"   # the scope key of the session a route resolved ({id}): its error texts name its folders
UPLOAD_TYPES = ("application/octet-stream", "application/zip")   # what POST /api/v1/uploads takes (§21.3)
# the states of a session (docs/SPEC.md §21.7), in the order they are tried: the first that holds
STATES = ("starting", "failed", "created", "done", "budget", "waiting", "paused", "running", "stopped")
# the fields of POST /api/v1/sessions (docs/SPEC.md §21.4); `folder` comes with the server folders (§21.10)
CREATE_FIELDS = ("goal", "title", "description", "language", "team", "model", "start_agents", "max_agents",
                 "max_cost_usd", "network", "files", "skills", "answers", "clarify", "stop_when_done", "client_ref")
PATCH_FIELDS = ("max_cost_usd", "max_agents", "wrap_up")   # PATCH /api/v1/sessions/{id} (docs/SPEC.md §21.2)
CONTINUE_FIELDS = ("text",)                                # POST /api/v1/sessions/{id}/continue
SHUTTING_DOWN_TEXT = "DreamTeam is shutting down."
# a continue's `api.error` when its start started nothing: a stop sent after it took the session's lock first
NOT_DELIVERED_TEXT = "The session stopped before the text of a continue reached it: send the text again."
NO_SESSION_TEXT = "No such session: GET /api/v1/sessions lists the sessions this token sees."
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

    @classmethod
    def parse(cls, body: Mapping[str, Any]) -> "CreateRequest":
        """400 `bad_request` for a value of the wrong kind; `goal` required, with a character that is not
        whitespace; `title` one such line of at most TITLE_LIMIT characters; the session's title (`title`, else the
        goal's first line) without a control or invisible format character (_hidden_chars: it reaches the owner's
        terminal and pages); `clarify: true` only with `answers: "manual"`; `client_ref` at most CLIENT_REF_MAX
        printable characters; `files` at most FILES_PER_SESSION tokens. The kinds of the team, model and limits
        are sessionopts' (overrides_from), after client_ref (§21.4 "Order" 3). The goal and title combine as §21.4
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
        if title is None:
            title, goal_text = "", goal
        elif " ".join(title.split()) == split_goal(goal)[0]:
            goal_text = goal
        else:
            goal_text = f"{title}\n{goal}"
        return cls(title=title, goal=goal_text, description=description, answers=answers,
                   stop_when_done=API_GIVEN["stop_when_done"] if stop_when_done is None else stop_when_done,
                   client_ref=client_ref, files=files, skills=skills)


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


def query_count(value: str | None, name: str, default: int, most: int) -> int:
    """A whole number from 1 to `most` given in a query (400 `bad_request` otherwise); absent is `default`."""
    if value is None:
        return default
    if not (value.isascii() and value.isdigit()) or not 1 <= int(value) <= most:
        raise ApiError("bad_request", f"'{name}' must be a whole number from 1 to {most}.")
    return int(value)


def check_json_type(request: Request) -> None:
    """415 `unsupported_media_type` unless the request says `Content-Type: application/json` (parameters such as a
    charset aside): PATCH checks its type itself, since the guard's 415 rule covers POST only (docs/SPEC.md §21.1
    "Format"). A body-less one sends it too."""
    kind = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if kind != "application/json":
        raise ApiError("unsupported_media_type", f"{JSON_TYPE_TEXT}.")


def continue_text(body: Mapping[str, Any]) -> str:
    """The `text` of a `continue` (docs/SPEC.md §21.2) without the whitespace around it, as every action that takes
    it stores it (Mailbox.send strips a message, split_goal a goal; reopen_goal would keep it): a string with a
    character that is not whitespace, at most MAX_TEXT characters so (the most a message holds: the text may become
    one); 400 `bad_request` otherwise."""
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ApiError("bad_request", "'text' is required: the next instruction for the session (its goal when it "
                                      "has none, the answer to a pending question, else a message to the team).")
    text = text.strip()
    if len(text) > MAX_TEXT:
        raise ApiError("bad_request", f"'text' takes at most {MAX_TEXT} characters.")
    return text


def budget_text(spent: float, limit: float) -> str:
    """The text of `budget_exhausted` (docs/SPEC.md §21.2 `continue`)."""
    return (f"The session's budget is spent (${spent:.2f} of ${limit:.2f}): raise max_cost_usd with "
            "PATCH /api/v1/sessions/{id} first.")


async def claim_uploads(uploads: Uploads, tokens: list[str], caller: str) -> list[Source]:
    """`uploads.claim_many(tokens, caller=caller)` in a thread that a cancellation never leaves holding claims: a
    cancel waits for the claims to end and gives back what they took (docs/SPEC.md §21.3 "Life": nothing stays
    claimed after the create)."""
    work = asyncio.ensure_future(asyncio.to_thread(uploads.claim_many, tokens, caller=caller))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        while not work.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait({work})
        if not work.cancelled() and work.exception() is None:
            uploads.settle(work.result(), used=False)
        raise


@dataclass
class _Ref:
    """A create in flight that holds its client_ref (docs/SPEC.md §21.4 `client_ref`): a retry with the same body
    waits for `done`, one with another body is 409 at once."""
    digest: str
    done: asyncio.Event = field(default_factory=asyncio.Event)


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
        routes = [
            self._route("/api/v1/options", {"GET": self._options}),
            self._route(API_UPLOAD_PATH, {"POST": self._upload}),
            self._route("/api/v1/sessions", {"GET": self._list, "POST": self._create}),
            self._route("/api/v1/sessions/{id}", {"GET": self._get, "PATCH": self._patch, "DELETE": self._delete}),
            self._route("/api/v1/sessions/{id}/continue", {"POST": self._continue}),
            self._route("/api/v1/sessions/{id}/stop", {"POST": self._stop}),
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
        folder = api.get("folder") if api is not None else None
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
            "via": board.get("via"), "folder": folder if isinstance(folder, dict) else None,
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
        before anything is given while the budget is spent; 409 `conflict` for what the swarm refuses (a ToolError).
        A delivery that succeeds clears the session's `api.error` (a failed one of an earlier `continue`)."""
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
            raise ApiError("conflict", str(exc)) from None
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
        a refusal becomes the session's `api.error` with its code (a manager error's text through _public_text, as a
        start's error), any other exception `internal`, logged with its traceback."""
        try:
            self._deliver(sid, swarm, text)
        except ApiError as exc:
            self._record_error(sid, exc.code, exc.text)
        except CODED_ERRORS as exc:
            self._record_error(sid, error_code(exc), self.manager._public_text(sid, str(exc)))
        except Exception as exc:  # noqa: BLE001 - recorded in api.error; the session runs on
            log.exception("API v1: could not give the text of a continue to session %s", sid)
            self._record_error(sid, "internal", f"Internal error ({type(exc).__name__}): the hub's log has the "
                                                "details.")

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
        nothing: the body; client_ref; the overrides; the skills; the uploads checked; max_running checked and
        reserved; the uploads claimed; manager.create(start=False, mark=...); the claims settled; 202, the start in
        the background (spawn). 200 with the existing session for a repeated client_ref."""
        if self.closing:
            raise SessionError(SHUTTING_DOWN_TEXT, 503)
        query_fields(request)
        body = await read_json(request)
        check_fields(body, CREATE_FIELDS)
        ask = CreateRequest.parse(body)
        digest = request_digest(body)
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
            uploads = manager.api_uploads
            if ask.files:   # every token checked, none claimed yet: a refusal here uses nothing up
                await asyncio.to_thread(uploads.peek_many, ask.files, caller=caller)
            self._check_running(config)
            self._reserved += 1   # (no await since the check)
            try:
                sources = await claim_uploads(uploads, ask.files, caller) if ask.files else []
                mark = object()
                created = False
                try:
                    made = await manager.create(
                        ask.title, ask.goal, ask.description, overrides=overrides, start=False, via="api",
                        skills=skills or None, sources=sources or None, mark=mark,
                        api={"answers": ask.answers, "stop_when_done": ask.stop_when_done,
                             "client_ref": ask.client_ref, "request_digest": digest})
                    created = True
                finally:   # (synchronous: any outcome settles every claim, docs/SPEC.md §21.3 "Life")
                    uploads.settle(sources, used=created)
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
        session's folder with it."""
        query = query_fields(request, ("confirm",))
        sid = self._session(request, caller)
        if query.get("confirm") != request.path_params.get("id"):
            raise ApiError("bad_request", "Deleting a session takes ?confirm=<its id>.")
        await self.manager.delete(sid, skills="discard", via="api")
        return JSONResponse({"ok": True})


__all__ = ["API_SID", "API_VERSION", "CLIENT_REF_MAX", "CODED_ERRORS", "CODES", "CONTINUE_FIELDS", "CREATE_FIELDS",
           "FILES_PER_SESSION", "FOLDER_BUSY_TEXT", "JSON_MAX", "NOT_DELIVERED_TEXT", "PATCH_FIELDS",
           "SESSIONS_DEFAULT", "SESSIONS_MAX", "START_WAIT", "STATES", "STATUS_CODES", "UPLOAD_TYPES", "ApiError",
           "ApiV1", "CreateRequest", "budget_text", "check_fields", "check_json_type", "claim_uploads", "continue_text",
           "error_code", "fail", "library_skills", "options_view", "query_count", "query_fields", "query_flag",
           "read_json", "request_digest", "root_pairs", "skill_refs", "state_of"]
