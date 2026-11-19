# HTTP API

**English** · [Русский](../ru/http-api.md) · [All docs](README.md)

The HTTP API lets a program use the swarm as a service. A site's server uploads a document (or names a folder the
owner listed), starts a session with a goal, shows its timeline, answers the agents if they ask, and gets the result
as JSON and files. It is the HTTP form of `dreamteam -p` ([Terminal UI](terminal.md)): everything is said in terms of
a session, its goal, events, requests to the user, the result and files.

The API lives under `/api/v1` on every hub of a home: `dreamteam serve`, the TUI's own hub, `dreamteam mcp` in a home
and `dreamteam sandbox` of a home. `dreamteam run` and the sandbox of a single project have none. The board's own
routes (`/api/...`, the WebSocket, `/mcp`) are not part of it and may change in any release; `/api/v1` changes only as
[Compatibility](#compatibility) says. The full contract is section 21 of the
[module specification](../SPEC.md).

## Turning it on

The API has a token of its own, apart from the hub token: `DREAMTEAM_API_TOKEN`, set in the environment of the
command that starts the hub. A client sends it in the header `Authorization: Bearer <token>`.

```bash
export DREAMTEAM_API_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
export DREAMTEAM_HUB_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")   # recommended, see below
dreamteam serve --no-browser
```

`serve` then prints the API's address with the others:

```
DreamTeam is running.
  Sessions: http://127.0.0.1:8765/
  MCP:      http://127.0.0.1:8765/mcp
  API:      http://127.0.0.1:8765/api/v1
  Home:     /home/you/DreamTeam
```

`dreamteam status` prints `API: <hub>/api/v1` too while such a hub runs; neither ever prints the token.

- Instead of the token the hub can get its SHA-256 (64 hex digits) in `DREAMTEAM_API_TOKEN_SHA256`, as with the hub
  token. A value that is no SHA-256 is refused with a warning in the log, and then no API token matches it (the hub
  token still works, as the owner).
- The hub token (and the hub's own clients) works on `/api/v1` too, as the owner: it sees every session. The API token
  sees only the sessions created through the API ([Security](#security)). A hub with a hub token and no API token
  serves the API to its owner alone.
- A hub with neither token answers every `/api/v1` request 403 `api_disabled`, on `127.0.0.1` too.
- A hub whose API token equals its hub token does not start: whoever holds the API token would open every route of
  the hub.
- A hub with an API token and no hub token starts with a warning in its log (and in `serve`'s output): the board, the
  settings and `/mcp` still answer whoever reaches the port without a token, so a bug of a site on the same computer
  could reach them. Set `DREAMTEAM_HUB_TOKEN` too, or run the hub in
  [the sandbox](sandbox.md#the-http-api-in-the-sandbox). In the Docker image there is no such warning: the hub there
  listens on `0.0.0.0` and needs a hub token anyway.
- DreamTeam removes both variables from its environment at start: they never reach the agents' commands, MCP servers
  or DreamTeam's own git, whatever `[shell] pass_env` says.

In the [Docker sandbox](sandbox.md#the-http-api-in-the-sandbox), set `DREAMTEAM_API_TOKEN` on the host before
`dreamteam sandbox ~/DreamTeam`: the container gets only its SHA-256, and the start prints
`API:      http://127.0.0.1:8765/api/v1  (header Authorization: Bearer <your DREAMTEAM_API_TOKEN>)`.

The limits of the API are in the `[api]` section of `dreamteam.toml`, edited by hand only
([HTTP API settings](configuration.md#http-api-settings)); a change counts at the next request.

## Quick start with curl

```bash
API=http://127.0.0.1:8765/api/v1
AUTH="Authorization: Bearer $DREAMTEAM_API_TOKEN"
JSON="Content-Type: application/json"

# what a client may choose: languages, teams, roles, models, skills, folders, limits
curl -s -H "$AUTH" $API/options

# 1. upload a file (the raw body); the answer's "upload" is its token for an hour
curl -s -H "$AUTH" -H "Content-Type: application/octet-stream" --data-binary @report.pdf \
     "$API/uploads?filename=report.pdf"
# {"upload": "q1mH...", "filename": "report.pdf", "bytes": 48213, "unpack": false, "expires_in": 3600}

# 2. start a session with that file
curl -s -H "$AUTH" -H "$JSON" $API/sessions -d '{
  "goal": "Summarize the report\nOne page, in plain words, as summary.md",
  "files": ["q1mH..."], "language": "en", "max_cost_usd": 2, "answers": "manual",
  "client_ref": "job-42"}'
# 202 {"session": {"id": "5f0c...", "name": "007-summarize-the-report", "state": "starting", ...}}

# 3. follow its events: long polls from the last id until "final" is true and "more" false
curl -s -H "$AUTH" "$API/sessions/5f0c.../events?after=0&wait=30"
# {"events": [{"id": 41, "type": "task.updated", "text": "◐ #3 Summarize — Anna", ...}],
#  "last": 41, "more": false, "state": "running", "final": false}

# 4. what waits for the user ("answers": "manual" leaves the answers to the client), and an answer
curl -s -H "$AUTH" $API/sessions/5f0c.../requests
curl -s -H "$AUTH" -H "$JSON" $API/sessions/5f0c.../requests/q12 -d '{"text": "Keep the tables"}'

# 5. the result, the files, a zip of them
curl -s -H "$AUTH" $API/sessions/5f0c.../result
curl -s -H "$AUTH" $API/sessions/5f0c.../files
curl -s -H "$AUTH" -o summary.md $API/sessions/5f0c.../files/summary.md
curl -s -H "$AUTH" -o result.zip $API/sessions/5f0c.../archive

# 6. the next instruction to the same session, then delete it
curl -s -H "$AUTH" -H "$JSON" $API/sessions/5f0c.../continue -d '{"text": "Add a list of the open questions"}'
curl -s -H "$AUTH" -X DELETE "$API/sessions/5f0c...?confirm=5f0c..."
```

In PowerShell call `curl.exe` (not the `curl` alias) and put a JSON body into a file: `-d "@body.json"`.

The same loop in Python, with the standard library only:

```python
import json, os, urllib.request

API = "http://127.0.0.1:8765/api/v1"
AUTH = {"Authorization": "Bearer " + os.environ["DREAMTEAM_API_TOKEN"]}

def call(method, path, body=None):
    headers = dict(AUTH, **({"Content-Type": "application/json"} if body is not None else {}))
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(API + path, data=data, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=90) as answer:
        return json.load(answer)

session = call("POST", "/sessions", {"goal": "Write a haiku about the sea", "client_ref": "haiku-1"})["session"]
after = 0
while True:
    page = call("GET", f"/sessions/{session['id']}/events?after={after}&wait=30")
    for event in page["events"]:
        if event["text"]:
            print(event["text"])
    after = page["last"]
    if page["final"] and not page["more"]:
        break
result = call("GET", f"/sessions/{session['id']}/result")
print(result["status"], result["summary"])
```

## Format

- Bodies and answers are JSON, except uploads (the raw file) and downloads (the file's bytes). Times are Unix seconds,
  money is USD.
- Every POST and PATCH sends `Content-Type: application/json` (a charset is fine), the body-less `stop` too; anything
  else is 415 `unsupported_media_type`. Uploads take `application/octet-stream` or `application/zip` instead, and
  never JSON. A body is at most 1 MiB (413 `too_large`), an empty body is `{}`, and a body that is not a JSON object,
  or holds `NaN`, `Infinity` or a number too large for a float, is 400 `bad_request`. A field that is `null` counts as
  not given.
- A field the server does not know, in a body or in a query, is 400 `unknown_field`, so a typo never passes silently.
  A query field given twice is 400 `bad_request`.
- Errors are `{"error": text, "code": code}`, the guard's refusals included; some codes add fields of their own. The
  text is English and for people, and may be reworded: programs check `code` ([Errors](#errors)).
- On `/api/v1` only the `Authorization` header counts: the board's cookie and `?token=` do nothing there.

## Routes

| method and path | answer |
|---|---|
| GET `/api/v1/options` | what a client may choose (below) |
| POST `/api/v1/uploads?filename=<name>[&unpack=true]` | 201 `{"upload", "filename", "bytes", "unpack", "expires_in": 3600}` ([Uploads](#uploads)) |
| DELETE `/api/v1/uploads/{token}` | `{"ok": true}`: an upload no create used is deleted, its place under the cap free at once |
| POST `/api/v1/sessions` | 202 `{"session": summary}`; 200 with the existing session for a repeated `client_ref` ([Creating a session](#creating-a-session)) |
| GET `/api/v1/sessions?state=&limit=` | `{"sessions": [summary]}`, newest first; `limit` 1–200, default 50; `state` keeps one [state](#states) |
| GET `/api/v1/sessions/{id}` | `{"session": summary}` |
| PATCH `/api/v1/sessions/{id}` | `{"max_cost_usd"?, "max_agents"?, "wrap_up"?}` → `{"session"}` ([Limits](#limits-stop-and-delete)) |
| POST `/api/v1/sessions/{id}/continue` | `{"text"}` → `{"session"}` ([The next instruction](#the-next-instruction-continue)) |
| POST `/api/v1/sessions/{id}/stop` | `{"session"}`; the agents' background jobs stop with it |
| DELETE `/api/v1/sessions/{id}?confirm={id}` | `{"ok": true}`, plus `"folder_kept": {"root", "path"}` for a folder worked in place |
| GET `/api/v1/sessions/{id}/events?after=&wait=&limit=&detail=&lang=` | `{"events", "last", "more", "state", "final"}` ([Events](#events)) |
| GET `/api/v1/sessions/{id}/events/stream?after=&detail=&lang=` | Server-Sent Events |
| GET `/api/v1/sessions/{id}/requests` | `{"requests": [request]}`, oldest first ([Requests](#requests-and-messages)) |
| POST `/api/v1/sessions/{id}/requests/{rid}` | the answer → `{"ok": true, "session"}` |
| GET `/api/v1/sessions/{id}/messages?after=&limit=` | `{"messages": [...], "last"}`: the user's conversation with the members, oldest first |
| POST `/api/v1/sessions/{id}/messages` | `{"to", "text", "reply_to"?}` → 201 `{"message"}` |
| GET `/api/v1/sessions/{id}/result` | the [result](#the-result), also while the session runs (`"final": false`) |
| GET `/api/v1/sessions/{id}/files?path=&scratch=` | `{"path", "entries": [...], "truncated"}`: one folder of the session's files ([Files](#files)) |
| GET `/api/v1/sessions/{id}/files/{path}?scratch=` | the file's bytes |
| GET `/api/v1/sessions/{id}/archive?path=&scratch=` | a zip of the session's folder or of one folder in it |

`{id}` is the session's `id` from its summary: 32 hex digits, never reused. The summary's `name` is the folder name
the board shows (`/s/<name>/`); folder numbers come back after a delete, so a client always keeps the `id`.

`GET /api/v1/options` answers `{"api": 1, "version", "languages", "language", "teams": [{name, roles}], "roles":
[{key, name, description}], "models": [{name, provider, model}], "default_model", "skills": [{name, description}],
"folders": [{name, in_place}], "limits": {...}}`. It never holds a key, an address, a variable name or a path. The
`limits` are `upload_max_mb` (one upload, and everything one create copies in), `upload_max_files` (the files one
create copies in), `files_per_session` (20), `uploads_pending`, `max_running` and `archive_max_mb`.

## Uploads

`POST /api/v1/uploads?filename=<name>` stores the body as one file (any file: a PDF, a CSV, a zip, an empty file too)
and answers its token. The token goes into `files` of a create, which puts the file into the session's folder under
that name before the first start.

- `unpack=true` takes a zip (400 `bad_request` for anything else, an empty body included) and unpacks it into the
  session's folder instead, by the rules of [a copy of files](projects.md#working-on-a-copy-of-files): a zip of one
  folder gives that folder's files; `.git`, `node_modules`, `.venv`, `venv`, `__pycache__`, links and, at the top,
  the session's own names (`.scratch`, `.gitignore`, `.dreamteam`, `mcp`) are left out. Without it a zip is one file
  like any other.
- The name is refused (400 `bad_name`), never cleaned up, when it is empty, `.` or `..`; holds `/`, `\`, `:`, a NUL or
  another character that does not print (controls, bidirectional overrides, zero-width characters, private use, the
  line and paragraph separators); is longer than 200 characters, 255 bytes of UTF-8 or 255 UTF-16 units; is a name
  Windows cannot hold, on every OS (`< > " | ? *`, `CON`, `aux.txt`, a trailing dot or space); has the form of a
  Windows short name (`GIT~1`); or is, in any letter case, `.git`, `node_modules`, `.venv`, `venv`, `__pycache__`,
  `.scratch`, `.gitignore`, `.dreamteam` or `mcp`.
- `[workspace] copy_max_mb` caps one upload (413 `too_large`) and the sum one create copies in; `copy_max_files` caps
  the files one create copies in (an unpacked zip counts its files). At most `[api] uploads_pending` (32) uploads wait
  at once, the owner's and the API token's together: one more is 429 `too_many_uploads`, whose `Retry-After` header
  says in how many seconds the first of them is past its hour.
- A token lives an hour and is used by one successful create. A create that is refused (any 4xx) leaves every token it
  named usable for the next try, and so does a create that fails on the hub's side or is cancelled.
- `DELETE /api/v1/uploads/{token}` deletes an upload that no create used (a form left unsent, a create given up), so
  its place under `uploads_pending` is free at once: `{"ok": true}`. A token that is unknown, used, past its hour or
  uploaded with the other token is 404 `not_found`.
- The API's uploads are its own: a token is never usable on the board's "New session" dialog, nor the reverse, and a
  token uploaded with the API token is unknown to the hub token's owner (404), and the reverse.

## Creating a session

`POST /api/v1/sessions`:

```json
{"goal": "text, required: the first line is the title, the rest the description",
 "title": "one line instead of the goal's first line",
 "description": "more description, after the goal's own",
 "language": "ru | en",
 "team": "name of a saved team | [\"role\", ...]",
 "model": "profile name",
 "start_agents": 3, "max_agents": 8, "max_cost_usd": 5.0,
 "network": "on | off",
 "files": ["upload token", "..."],
 "folder": {"root": "jobs", "path": "acme/site", "mode": "copy | in_place"},
 "skills": ["library skill name", "..."],
 "answers": "auto | manual",
 "clarify": false,
 "stop_when_done": true,
 "client_ref": "your own id of this job"}
```

Only `goal` is required; anything not given comes from the settings, as for the board. The answer is 202 with the
summary in state `starting`: the session starts in the background, and a start that fails shows as state `failed`
with its `error`.

- **Goal and title.** `goal` must hold something other than whitespace. Without `title` the goal's first line is the
  title and the rest the description. With `title` (one line, at most 200 characters), the title is `title` and the
  whole goal text becomes the description, unless the goal's own first line already equals `title`. A `description`
  is appended after a blank line. The title must not hold control characters (a tab, an escape sequence) or invisible
  format characters such as a bidirectional override (400 `bad_request`): it reaches the owner's terminal and pages as
  it is. The session's folder name is made from the title.
- **Team, model, limits.** `team` is the name of a team saved in the settings (400 `unknown_team`, the text lists the
  names) or a list of roles in order (400 `unknown_role` for a role the catalog does not have). A list sets
  `start_agents` unless it is given; a saved team sets it to its size, so `team` as a name with `start_agents` is 400.
  `model` is a profile name (400 `unknown_model`). `start_agents` and `max_agents` are whole numbers of at least 1, and
  `start_agents` at most `max_agents`, also when a team sets it; `max_cost_usd` is at least 0. No provider, key or
  address is ever taken from a request: those are the settings of the hub's computer.
- **`files`**: at most 20 upload tokens, none twice ([Uploads](#uploads)). Two files with one name are 409
  `file_conflict`.
- **`folder`**: a folder of the hub's computer, named by a root the owner listed
  ([Folders](#folders-on-the-hubs-computer)). There is no other way to name a path of the hub's computer.
- **`skills`**: names of skills in the hub's library (`GET options` lists them; in the sandbox the host's library too;
  400 `unknown_skill` for another name), at most `[skills] max_attached` (400 `bad_request`). No skill comes from a
  path, a Git URL or Claude Code.
- **`answers`**: who answers what the agents ask the user. `auto` (the default): the hub itself, as `dreamteam -p`
  does ([Automatic answers](#automatic-answers)). `manual`: the client, through [requests](#requests-and-messages), or
  the owner on the board. `clarify: true` (the planner asks first, [clarify mode](features.md)) needs `manual`.
- **`stop_when_done`** (default true): once the goal is completed, or the budget pauses the session, the session stops
  (its background jobs too), with the reason `done` or `budget` in its `session.stopped` event. `continue` starts it
  again. With false it keeps running like a board session.
- **`client_ref`** (at most 128 printable characters): a retry after a timeout never makes a second job. A create with
  the same `client_ref` and the same body as a session the API still has answers 200 with that session and creates
  nothing; one still being created is waited for. The same `client_ref` with another body is 409 `conflict`, whose
  `session` names the session made with it. The upload tokens of `files` are part of the body: a retry sends the same
  tokens again (a token the create used up is fine there), not new uploads of the same files, which are another body.
- **`[api] max_running`** (default 3, 0 for no limit) counts the running and starting sessions created through the
  API: a create or a `continue` past it is 429 `too_many_sessions`. The owner's own starts (the board, the terminal,
  MCP) and the resume when the hub starts are never refused, but they count.

The checks run before anything is copied or claimed, so a refused create costs nothing: the body, `client_ref`, the
settings, the skills, the folder, the upload tokens, then `max_running`.

A session created through the API shows on the sessions page with the badge "API", in `dreamteam sessions` and
`dreamteam status` with `| HTTP API`, and in the terminal's session list with "HTTP API". `dreamteam -c` never picks it
([Terminal UI](terminal.md)): the owner opens it with `dreamteam -r <name>` (the summary's `name`, not its `id`).

## States

A summary's `state` is the first that holds:

| state | when |
|---|---|
| `starting` | a create or `continue` is starting it, or the hub is about to resume it at its start |
| `failed` | its last start failed (the summary's `error` says why) |
| `created` | it never started |
| `done` | the goal is completed |
| `budget` | the budget paused it, or that pause stopped it |
| `waiting` | it runs, and a request that blocks the work waits for an answer |
| `paused` | it runs, paused by the user |
| `running` | it runs |
| `stopped` | stopped otherwise (by the user, a hub shutdown) |

`final` is true when the session is stopped and will not come back by itself: it is not running, not starting, not
about to be resumed, and no stop or start is under way. With `stop_when_done`, the end of a job is `done` or `budget`
with `final: true`. A session made by an earlier version of DreamTeam is `stopped`, final, with `"earlier_version":
true`; every route that would need to open it answers 409 `earlier_version`.

The summary: `{"id", "name", "title", "state", "final", "error", "goal": {title, description, status, summary} | null,
"language", "model", "cost_usd", "max_cost_usd", "max_agents", "created_at", "started_at", "finished_at", "agents",
"tasks": {status: n}, "requests", "answers", "stop_when_done", "client_ref", "via", "folder", "board_url",
"earlier_version"}`. It never holds an absolute path. `board_url` is the session's board, given only when the hub knows
its public address (`DREAMTEAM_PUBLIC_URL`, or a hub on `127.0.0.1`), else null; opening it takes the hub token, not
the API token.

## Events

An event is a row of the session's journal with its id and a line for people:

```json
{"id": 41, "type": "task.updated", "ts": 1794463609.8, "actor": "Anna Smith", "data": {...},
 "text": "◐ #3 Parse the document — Anna"}
```

- `data` is the event's own data ([event types](../SPEC.md), section 9) and may gain fields. A number that is not
  finite reads `null`.
- `text` is the line the terminal's verbose feed shows, without colours, in the session's language unless `lang=ru`
  or `lang=en` names another. It is null only for the events no feed shows (`agent.usage`, `agent.compacted`).
  DreamTeam's own texts in it name no absolute path; what the agents wrote (messages, tool calls and their results)
  is quoted as written. The wording may change between releases.
- `detail=timeline` (the default) gives the events a timeline shows: tasks, comments, messages, notifications to the
  user, the goal, hires, pauses, the session's start and end (`session.started`, `session.stopped` with its
  `reason`: `done`, `budget`, `user`, `failed`, `shutdown`, `restart`), background jobs, MCP servers, skills and file
  changes. `detail=all` gives every journaled event, the agents' activity (tool calls, thoughts) and usage included.

**Polling.** `GET .../events?after=<last id>` answers the events after that id, oldest first, at most `limit` (1–500,
default 200), with `last` (the id to ask after next time), `more` (more events are already there) and the session's
`state` and `final`. Start with `after=0` and ask again with `after=last` until `final` is true and `more` is false:
then nothing more comes unless the session is started again. A page may hold no events while `more` is true (the
timeline left out what it read): ask again at once. `wait` (0–60 seconds, digits only, `2.5` allowed) holds a request
that has nothing to answer yet until an event comes, the state changes, or the time is up.

**Stream.** `GET .../events/stream` sends the same events as Server-Sent Events: `id: <id>`, `event: <type>`, `data:
<the event's JSON>`. It starts after `after`, or after the `Last-Event-ID` header, which wins (an `EventSource` sends
it when it reconnects). A `: ping` comment comes after 15 seconds without a line. When the session is final and every
event was sent, one `event: result` with [the result](#the-result) ends the stream. A delete ends it without a result,
and so does the hub stopping: reconnect with `Last-Event-ID`.

A journal that cannot be read now (a stopped session whose file another program holds, an unplugged share) is 500
`internal` with the text "The session's journal cannot be read now (the hub's log says why): ask again later."; it is
never an empty page that would look like the end. Ask again later; a stream ends without a result.

## Requests and messages

A request is something only the user can give. Its id names the exact thing asked, so a retried answer never lands on a
newer one:

```json
{"id": "q12", "kind": "question", "ts", "from": "Anna Smith", "text", "pauses_swarm": true}
{"id": "m15", "kind": "message",  "ts", "from": "Anna Smith", "text"}
{"id": "r7-418", "kind": "review", "ts", "from": "Anna Smith", "task": {"id", "title", "result"}}
{"id": "s-web-3f9a0c1b2d4e", "kind": "mcp", "ts", "from": "Anna Smith", "server": {"name", "description", "tools"}}
```

- `question`: a question that pauses the swarm ([clarify mode](features.md)), or the questions of a planner that waits
  for the user's answers before it plans. Answer `{"text"}`; it ends the pause.
- `message`: the last message of a member to the user that the user has not answered yet, one per member. It does not
  block the work: reports land here too.
- `review`: a task waiting for the user's review. A new hand-in is a new id. Answer `{"approve": true|false,
  "comment"?}`; the comment is at most 20 000 characters.
- `mcp`: an MCP server an agent wrote, waiting for approval (`[mcp] require_approval`). Answer `{"approve":
  true|false}`; a server the agent built again since the listing has a new id, and the old one is 404.

`question`, `review` and `mcp` requests block the work: the state is then `waiting`. `GET .../requests` lists what
waits now, oldest first; the summary's `requests` counts them. A session that does not run lists only those that block
its work: a `message` can be answered only while the session runs, so a job that stopped with its member's last report
unanswered lists nothing (the report stays in `GET .../messages`), and lists it again if it runs again.

`POST .../requests/{rid}` answers one, as the user, and answers `{"ok": true, "session"}`. A field of another kind's
answer is 400 `unknown_field`, a missing one or one of the wrong type 400 `bad_request`. A request answered or
replaced meanwhile is 404 `not_found` (list the requests again). The session must run: a stopped one is 409
`not_running` (`continue` starts it with a text; a completed goal is reopened with it, and the agents work, and spend,
again). A second answer to the same MCP server while the first one is still
being checked is 409 `conflict`; so is anything the swarm refuses, with its text (a path of the hub's computer in it
rewritten, as in every error, see [Errors](#errors)). A text (of a question's or a
message's answer, of a message, of `continue`) has its surrounding whitespace dropped and must then hold something,
at most 50 000 characters, and no unpaired surrogate (a lone `\ud800` in the JSON). Only the message a request named is
marked read on the board: earlier messages that no request listed stay unread there.

**Messages.** `GET .../messages?after=&limit=` reads the user's conversation with the members, oldest first: what the
user sent and what reached the user (`{id, ts, from, to, text, reply_to}`; `limit` 1–500, default 200; `last` is the
newest id given, else `after`). `POST .../messages` sends one as the user: `{"to": ["all"] or 1 to 100 member names,
"text", "reply_to"?: a message id}` → 201 `{"message"}`. What the mailbox refuses (an unknown or retired member) is 400
`bad_request`; a stopped session is 409 `not_running`. A stopped session whose database cannot be read (a damaged
file) answers no requests and no messages.

### Automatic answers

With `"answers": "auto"` the hub answers for the user, as `dreamteam -p` does, on every start of the session, from any
surface:

- each message of a member to the user gets "The user is not available (non-interactive mode): decide yourself and
  continue." (in the session's language), at most 50 per start;
- a task waiting for the user's review is approved with a note that nobody checked it, once per hand-in;
- a question that pauses the swarm is answered; the planner of a clarifying session plans without asking.

Past 50 answers, messages are no longer answered: they wait as `message` requests, and the session tells the user once
per run (a notification). Reviews and question pauses are still answered. MCP servers are never approved automatically:
with `[mcp] require_approval = true` an `auto` session can stay `waiting` until the client or the owner answers.
`dreamteam -p -r <name>` on such a session sends nothing of its own, so nothing is answered twice.

## The next instruction (`continue`)

`POST .../continue {"text"}` does what `dreamteam -p -r <name> "<text>"` does with its prompt: without a goal the text
becomes the goal; a completed goal is reopened with the text; a pending question is answered by it; otherwise it is a
message to the coordinator (the member who holds the planning task, else the first agent). A session the user paused
is resumed as well.

- On a running session the text is given at once. 409 `budget_exhausted` while the budget is spent (raise it with
  PATCH first), 409 `conflict` for what the swarm refuses.
- A stopped, failed or starting session answers 200 with state `starting`: it is started in the background and the
  text is given right after its start. `max_running` and the budget are checked first (429, 409). Several `continue`s
  are given in the order they came. A `continue` sent after a stop runs the session again with its text; one sent
  while a start fails tries the start again.
- A text that could not be given after such a start shows in the summary's `error`, from the session's `api.error`
  `{code, text}`: `budget_exhausted`, `conflict`, the code of another refusal, `internal`, or `not_running` when a stop
  or a delete sent after the `continue` came first ("send the text again"). The next text given clears it. A start
  that failed shows its own error instead.

## Limits, stop and delete

- `PATCH /api/v1/sessions/{id}` with `max_cost_usd`, `max_agents` or `wrap_up` changes them as the board's ⚙ does,
  also for a stopped session. A budget that covers what was spent lifts a budget pause. `wrap_up: true` asks the team
  to finish with what it has (409 `conflict` on a session that never started). A value that cannot be a limit
  (`max_agents` below 1 or past 64 bits, a negative budget, a number too large for a float) is 400 `bad_request`, and
  nothing changes. PATCH checks its own `Content-Type` (415).
- `POST .../stop` stops the session and its background jobs (reason `user`); `continue` starts it again.
- `DELETE .../{id}?confirm={id}` deletes the session with its folder. A folder the session worked in in place is kept,
  with its files, and named: `"folder_kept": {"root", "path"}`. A download or an archive of the session still being
  sent is cut first (its client gets it short); one asked while the delete runs is 409 `conflict`.

## The result

`GET .../result`, and the stream's last event:

```json
{"type": "result", "status": "done", "session": "5f0c...", "goal": {...}, "summary": "...",
 "tasks": [{"id", "title", "status", "assignee", "parent_id", "crew"}], "cost_usd": 0.42, "duration_s": 180.5,
 "agents": ["Anna Smith"], "jobs": [], "error": null, "final": true, "board_url": null,
 "changed_files": [{"path": "summary.md", "change": "added"}], "changed_files_error": null,
 "changed_files_truncated": false}
```

- `status` is the session's state, `summary` the goal's summary when it is done, `duration_s` the time from the first
  start until the session last became final, or until now (null when it never started), `jobs` the background jobs
  still running.
- `changed_files` lists what changed in the session's folder since its base commit (the folder as it was at the first
  start, the copied files included): `added`, `modified` or `deleted`. Untracked files count; files the folder's
  `.gitignore` ignores and `.scratch` do not. At most 10 000 entries, the first ones by path; then
  `changed_files_truncated` is true.
- `changed_files` is null, with `changed_files_error` saying why, when git is off for the session, git is not installed,
  the session has no base commit, that commit is gone, the folder has no repository of its own, the settings cannot be
  read, git fails, or an in-place folder no longer passes its checks.
- A final result is kept and answered again as it was, with its `error` and `board_url` as the session's summary says
  them now, unless its `changed_files_error` may pass (git not installed, something that cannot be read, git failing,
  an in-place folder refused): then it is computed again at the next request. An edit of the hub's settings (such as
  `[api] folders` or `[workspace] git`) has every kept result computed again.

## Files

- `GET .../files?path=` lists one folder of the session's files (the session's folder itself without `path`):
  `{"path", "entries": [{name, path, type: "file" | "dir", size, modified}], "truncated"}`, in name order. A
  listing reads at most the first 10 000 names of the folder; `truncated` is true when it holds more (there is no
  paging: list a folder inside it, or ask for its archive).
- `GET .../files/{path}` downloads one file: `Content-Type: application/octet-stream` whatever the name (the agents
  wrote it: a site that passes it on must not have it rendered), `Content-Disposition: attachment` with the name,
  `X-Content-Type-Options: nosniff`, `Cache-Control: no-store` and `Content-Length`, the size when it was opened.
  Exactly that many bytes are sent: a file that grew meanwhile is cut there; one that shrank, or a read that fails,
  ends the answer early, so a client that compares the bytes with `Content-Length` never takes a short file for whole.
- `GET .../archive?path=` streams a zip of the session's folder, or of one folder in it, named `<folder>.zip`: every
  folder an entry (empty ones too), files deflated with their times, modes 0644 (0755 for folders and executable
  files). It is measured first: over `[api] archive_max_mb` (default 1024), or over 100 000 files and folders (the
  names it leaves out count too), it is 413 `too_large` before any byte, and a
  folder that cannot be read is 409 `conflict`, as is one more than 64 levels deep. Each file is taken at its size when
  it is opened. A file or folder that goes, is replaced or shrinks while the zip is written cuts the answer before the
  zip's end, so the client gets an invalid zip, never a valid one that lacks files. A hub sends at most 4 archives at
  once (from the measure until the answer ends, however slowly its client reads): one more is 429
  `too_many_archives`; ask again later, or download the files one by one. A client that leaves during the measure
  gives its place back at once. Each archive keeps open files on the hub while it is sent, so a hub whose limit of
  open files is below 1024 sends fewer (one at macOS's default of 256). The hub raises that limit to 1024 when it
  starts, as far as the system allows, and its log says so when it cannot (`ulimit -n 1024` before
  `dreamteam serve` helps then).
- Paths are relative to the session's folder, with `/`. An absolute path, `..`, `.`, an empty name, a NUL, a backslash,
  a `.git` component or a Windows short name (`GIT~1`) is 400 `bad_name`; so is a path in `.scratch` without
  `scratch=true`, and one in `.scratch/_skills` ever; on a Windows hub so are `:`, `< > " | ? *`, reserved names
  (`CON`) and a trailing dot or space.
- Never listed, served or archived: links; files with more than one hard link (so an environment installed with hard
  links, such as uv's `.venv` or pnpm's `node_modules`, is left out); `.git` at any depth; `.scratch` (the members' own
  files) unless `scratch=true`; `.scratch/_skills` ever.
- Nothing is followed: a link, or a folder swapped for one while it is read, ends the request instead of reading outside
  the session's folder.
- Codes: 404 for a missing path (a name longer than a folder can hold too), a link, a hard-linked or special file;
  400 `bad_request` for a folder asked as a file or the reverse; 409 `conflict` for a name replaced while it was
  opened or a file the hub may not read; 409 `folder_not_ready` when the session's folder is gone or cannot be read;
  403 `forbidden_folder` when it is reached through a link, an in-place folder's root no longer allows it, or it is
  a folder no session may work in (DreamTeam's own data, another DreamTeam home, a system folder).

## Folders on the hub's computer

A client whose files already lie on the hub's computer names a folder there instead of uploading it. Only folders
under a root the owner lists in `[api] folders` can be named, by the root's name and a path inside it
([HTTP API settings](configuration.md#http-api-settings)):

```json
"folder": {"root": "jobs", "path": "acme/site", "mode": "copy"}
```

- `path` is relative to the root, with `/`; empty or absent is the root itself. It follows the rules of a name a client
  creates (400 `bad_name`). An unknown root is 400 `unknown_folder`; a root that is missing is 404; a link under the
  root that leads out of it, or a root that is not allowed (a drive root, your home folder, the DreamTeam home or its
  own folders, another DreamTeam home, `/proc`, `/sys`, `/dev`, `/run`), is 403 `forbidden_folder`.
- **`copy`** (the default): the folder, or a `.zip` under the root, is copied into the session's own folder, as
  [a copy of files](projects.md#working-on-a-copy-of-files) is: `.git`, `node_modules`, `.venv`, `venv`, `__pycache__`,
  links, special files and files with more than one hard link are left out, and so are the session's own names at the
  top. The original is never written, and several sessions may copy one folder at once. `files` can go with it; a
  top-level name both have is 409 `file_conflict`.
- **`in_place`** (only under a root with `in_place = true`, else 403): the session works in that folder, as
  [your project](projects.md) does: a missing or empty folder is created, a folder with files is adopted, and the
  agents commit to its current branch. With git on, the folder must be a repository's own top folder, on a branch, with
  nothing uncommitted, whose `.git` is a folder inside it (not a linked worktree or a link), or in no repository at all
  (it then gets `git init` and a first commit); otherwise 409 `folder_not_ready`. Every start of the session checks the
  root and the folder again. `files` cannot go with it (400): put the files into the folder yourself.
- **One folder, one session.** A folder another session works in, or a folder inside or around it, is 409
  `folder_busy`; its `session` names that session's id when the token sees it. Send the next instruction there with
  `continue`; DELETE frees the folder and keeps its files.
- Summaries name the folder as `{"root", "path", "mode"}`, and error texts as `<root>/<path>`: never a path of the
  hub's computer.

## Errors

| code | status | when |
|---|---|---|
| `token_required` | 401 | no valid `Authorization: Bearer` (with `WWW-Authenticate: Bearer realm="DreamTeam"`) |
| `api_disabled` | 403 | the hub has neither an API token nor a hub token |
| `forbidden_host`, `forbidden_origin` | 403 | a `Host` the hub does not take, or a page of another origin |
| `unsupported_media_type` | 415 | a POST or PATCH that is not `application/json`, an upload of another type |
| `bad_request` | 400 | a body or value the API cannot take |
| `unknown_field` | 400 | a field the API does not know |
| `unknown_team`, `unknown_model`, `unknown_role`, `unknown_skill`, `unknown_folder` | 400 | a name the hub does not have; the text lists the names |
| `bad_name` | 400 | a file name or path the hub refuses |
| `forbidden_folder` | 403 | a folder outside what `[api] folders` allows |
| `not_found` | 404 | no such session, file, request, upload, root or route (for the API token, a session not created through the API too) |
| `method_not_allowed` | 405 | a method the route does not take (`Allow` names those it does) |
| `conflict` | 409 | the state does not allow it now, or the swarm refused it |
| `earlier_version` | 409 | a session made by an earlier version of DreamTeam |
| `file_conflict` | 409 | two files with one name in one create |
| `folder_busy` | 409 | another session works in that folder |
| `folder_not_ready` | 409 | a folder that is missing, or a repository that is not ready to work in |
| `not_running` | 409 | an answer or a message to a stopped session |
| `budget_exhausted` | 409 | the budget is spent: raise it with PATCH |
| `too_large` | 413 | a body, an upload, a copy or an archive over its limit |
| `too_many_uploads` | 429 | `[api] uploads_pending` uploads already wait (with `Retry-After`; delete the unused ones) |
| `too_many_sessions` | 429 | `[api] max_running` sessions already run |
| `too_many_archives` | 429 | the hub already sends as many archives as it sends at once (4) |
| `internal` | 500 | the hub failed, or is shutting down; its log says why. Ask again later |

An error text never names a path of the hub's computer for the API token: a server folder reads as `<root>/<path>`,
the home and the session's folders by their names, any other path by its last part.

## Compatibility

Within v1, fields, codes, states and event types are only added, never renamed, removed or given another meaning, so a
client ignores what it does not know. The `text` of events and errors is for people and may be reworded. Anything else
becomes `/api/v2`, and v1 then stays for at least one minor release. `GET /api/v1/options` names the DreamTeam
version.

## Security

- **What the API token reaches.** Only `/api/v1`: no route of the board, no `/mcp`, no WebSocket. With it a client
  cannot change the settings, read the model profiles' keys, reach a session not created through the API, or name a
  folder outside `[api] folders`. That limits what a client can name, not what the agents can reach: the API token
  is the power to run a goal. The agents run commands with the hub user's rights (in the sandbox, the container's),
  so they can read the home, its settings (a `dreamteam.toml` may hold a key) and the keys there, and an uploaded
  document may carry instructions to them. Every root of `[api] folders` is a further grant to whoever holds the
  token: with `in_place`, the agents write there.
- **Who sees what.** The API token sees only the sessions created through the API (every other session is 404 for
  it), whoever created them: a session you create on `/api/v1` with the hub token is the API token's too, which can
  read, continue, stop and delete it. Keep a job of your own away from it by starting it on the board, in the
  terminal or through `/mcp`. The hub token sees every session. The `main` session of an old project is never
  reachable.
- **A hub for a site** runs in [`dreamteam sandbox`](sandbox.md#the-http-api-in-the-sandbox):
  the agents run commands and an uploaded document may carry instructions to them, and a hub on the same computer as
  the site without a hub token exposes its other routes to the site's bugs (a request forgery reaches `127.0.0.1`).
  Outside the sandbox give the hub a hub token too.
- **Another computer.** A hub that listens on an address other than loopback needs the hub token anyway
  ([Security](security.md)); list the name the client uses in `DREAMTEAM_ALLOWED_HOSTS`. The hub speaks plain
  HTTP, so across a network the token and the uploaded files travel unencrypted: put the hub behind a reverse
  proxy that ends TLS, and set `DREAMTEAM_PUBLIC_URL` ([Security](security.md)).
- **No CORS.** A page of another origin is refused, as on the board: a site calls the API from its server, never from
  its visitors' browsers, and never shows them the token.
- **Files are the agents'.** Downloads are `application/octet-stream` with `nosniff`, so a browser never renders them,
  and event texts, task results, messages and files hold whatever the agents wrote, paths of the hub's computer
  included: the API does not scrub them.
