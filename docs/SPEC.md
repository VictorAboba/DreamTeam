# DreamTeam — internal module specification

This is the contract between modules. Every public name below must exist with exactly this
signature/behaviour. Implementation details not covered here are up to the implementer, but
must not leak into other modules. Language of code, comments and docstrings: English.
User-visible texts for agents live in `prompts.py` (ru/en). Python >= 3.11, asyncio.

## 0. Conventions

* Time: `time.time()` floats everywhere (DB, events, messages).
* Actors: a member name (`"John Smith"`), or the reserved `USER = "user"` (the human).
  `"all"` is a pseudo-recipient for messages. Names are unique, case-insensitive, stored canonically.
* Errors meant for the caller (agent, user, MCP client) are raised as `models.ToolError(msg)`.
  Messages are short, in English, and tell the caller how to fix the call.
* JSON in DB: `db.dumps` / `db.loads` (ensure_ascii=False; a value holding a lone surrogate, e.g. from LLM
  tool arguments, is written with ASCII escapes instead, so it is storable and loads back identical).
  Plain TEXT columns cannot hold lone surrogates: `db.utf8_safe(text)` replaces them with U+FFFD; a tool
  whose arguments hit one returns an "invalid Unicode" error instead of an internal error.
* Every state change emits exactly one event via `swarm.bus.emit(type, actor, **data)` (see §9).
* No module prints to stdout (stdio MCP mode uses it). Use `logging.getLogger(__name__)`.
* Event loop: everything runs in one asyncio loop; the DB is used synchronously from it.
  Blocking work (subprocess, git, big file IO) goes through `asyncio.to_thread`.

## 1. Layout

```
src/dreamteam/
  __init__.py  __main__.py
  config.py      Config / ModelProfile / templates                  (exists)
  db.py          Database (sqlite)                                   (exists)
  models.py      USER, ALL, ToolError, Status, Task, AgentKind...    (exists)
  events.py      EventBus                                            (exists)
  names.py       generate_name                                       (exists)
  prompts.py     system_prompt(), t(), PLANNING_TASK, DEFAULT_ROLE   (exists)
  board.py       Board          — tasks, goal field, discussion
  mailbox.py     Mailbox        — direct messages
  workspace.py   Workspace      — shared files, git, locks
  shell.py       run_shell, scrubbed_env
  triggers.py    TriggerEngine + condition DSL
  llm/__init__.py  create_llm(profile)
  llm/base.py      ToolCall, Usage, LLMResponse, LLMError, LLMClient, estimate_tokens
  llm/anthropic_llm.py  AnthropicLLM
  llm/openai_llm.py     OpenAILLM
  llm/scripted.py       ScriptedLLM (tests / demos)
  mcp_forge.py   MCPForge, MCPConnection — agent-built MCP servers + library
  tools/__init__.py  registry: ToolSpec, ToolContext, tool(), tools_for(), schemas_for(), call_tool()
  tools/board_tools.py comm_tools.py file_tools.py swarm_tools.py trigger_tools.py mcp_tools.py
  agent.py       AgentRuntime   — ReAct loop of one LLM agent
  swarm.py       Swarm          — composition root
  hub_mcp.py     the swarm as an MCP server (stdio / streamable HTTP / stdio->HTTP proxy)
  web.py         Starlette app of one swarm: REST + WebSocket + /mcp + static board UI
  httpjson.py    JSONResponse / json_text for HTTP and WebSocket (lone surrogates escaped, never a 500)
  tracing.py     Tracer / Trace — agent traces (§16)
  trace_api.py   REST routes of the traces, mounted by web.create_app (§16.3)
  tools/trace_tools.py  the `agent_traces` tool (§16.2)
  settings.py    global settings of a home: read/write/validate, public view, updates, endpoint checks (§17.1)
  sessions.py    SessionManager — the sessions of a home (§17.2)
  launcher.py    launcher web app: sessions page, settings, /s/{sid}/ routing (§18.1)
  launcher_mcp.py  the launcher as one MCP server (§18.2)
  static/        index.html app.js traces.js style.css traces.css (board), home.html home.js home.css (launcher);
                 no build step
  console.py     terminal chat client (talks to the REST API)
  tui/           terminal UI client (docs/TUI.md)
  docker.py      sandbox: build & run DreamTeam in Docker
  cli.py         `dreamteam` command
tests/  conftest.py (FakeSwarm fixture) + test_*.py
```

A **home** (launcher, §17–18; `~/DreamTeam` by default) on disk:
```
<home>/dreamteam.toml               global settings (§17.1); dreamteam.toml.bak = the previous version
<home>/sessions/<id>/session.json   {"title", "created_at", "overrides", "autostart", "pending_goal"?}
<home>/sessions/<id>/workspace/     the session's shared files
<home>/sessions/<id>/.dreamteam/    the session's swarm.db and logs/ (a project directory as below)
<home>/sessions/.deleted-<id>-<ns>/ a session being deleted (renamed away first, then removed)
<home>/.dreamteam/hub.json          the running launcher (same format as below; "project" = the home)
<home>/.dreamteam/logs/dreamteam.log
<home>/.dreamteam/swarm.db          only in a home that was a single-swarm project before: session `main`
```

Project (one swarm of `dreamteam run`; a session directory has the same layout without `dreamteam.toml` and
`hub.json`) on disk:
```
<project>/dreamteam.toml
<project>/workspace/            shared files (git repo), workspace/mcp/<name>/server.py
<project>/.dreamteam/swarm.db   sqlite
<project>/.dreamteam/hub.json   {"base_url": "...", "mcp_url": "...", "pid": 123, "project": "<abs path>"}
                                written once the server listens; other commands trust it only if GET /api/hub
                                confirms the same pid and project
<project>/.dreamteam/logs/      dreamteam.log, mcp-<name>.log
```

## 2. Data model (db.py SCHEMA is authoritative)

* `tasks`: id, title, **goal** (why the task exists / success criterion), description, status,
  assignee, reviewer, created_by, parent_id, depends_on (json int list), priority (0 low,1 normal,
  2 high,3 critical), tags (json str list), result, created_at, updated_at.
* `comments`: the per-task **discussion**: id, task_id, author, text, reply_to (comment id | null), ts.
* `agents`: name, kind (`llm`|`external`), role, instructions, model (profile name), language,
  status (`idle`|`working`|`paused`|`error`|`retired`), hired_by, created_at, last_active,
  cost_usd, tokens_in, tokens_out, notes, system_prompt (frozen at creation), context_tokens.
* `history`: agent, seq, msg (json neutral message, §6.1) — append-only per agent except compaction.
* `messages` + `inbox(message_id, recipient, read_at)`.
* `notifications`: id, ts, recipient, kind (prompt key), text (rendered), data (json), delivered_at.
* `triggers`, `mcp_servers`, `file_locks`, `kv`, `events`.
* `traces`, `spans`, `history_archive(agent, epoch, seq, msg, archived_at)`: tracing.py SCHEMA (§16).
* kv keys: `goal` → `{"title","description","status":"active"|"done","summary","set_by","updated_at"}`,
  `limits` → `{"max_cost_usd": float, "max_agents": int}` (overrides config), `paused` → `{"paused": bool, "reason": str}`.

## 3. Swarm (swarm.py) — what other modules may use

```python
class Swarm:
    config: Config; db: Database; bus: EventBus
    board: Board; mail: Mailbox; workspace: Workspace; triggers: TriggerEngine; forge: MCPForge
    runtimes: dict[str, AgentRuntime]        # running LLM agents
    language: str                            # config.swarm.language
    llm_semaphore: asyncio.Semaphore
    tracer: Tracer                           # §16
    session_id: str | None; session_title: str | None   # set by SessionManager (§17.2)
    hub_url: str | None                      # MCP URL of this swarm while a web server runs (snapshot mcp_url)
    stopping: bool                           # set by stop()/close(): long polls (wait_for_events) end at once

    def __init__(self, project_dir, config: Config | None = None,
                 llm_factory: Callable[[ModelProfile, str], LLMClient] | None = None): ...
        # a failure after the database was opened (a corrupt swarm.db...) closes it before re-raising
    async def start(self, run_agents: bool = True) -> None
        # tracer.recover() and prune first (§16.1); LLM agents with open tasks (goal active) or whose last
        # activation was cut short (the stored history ends mid-activation) get `n_restart`; hire() respects run_agents
    async def stop(self) -> None
        # wakes every wait_signal() waiter (stopping=True), closes external trace sessions, stops the runtimes
    def close(self) -> None                              # closes the database
    def reconfigure(self, config: Config) -> None        # hot-apply new settings (§16.1)

    # members
    def member(self, name: str) -> dict | None          # agents row as dict (any status), exact canonical name
    def members(self, include_retired: bool = False) -> list[dict]
    def is_member(self, name: str) -> bool              # exists and not retired (llm or external)
    def kind_of(self, name: str) -> str | None          # "user" | "llm" | "external" | None
    def resolve_member(self, name: str, allow_user: bool = True, allow_retired: bool = False) -> str
        # case-insensitive / whitespace-tolerant, a leading "@" ignored, a unique first name alone accepted;
        # USER_ALIASES ("user", "пользователь", "the user", "human", "человек") -> USER when allowed, else
        # ToolError("The user cannot be used here..."); "" -> ToolError("Empty member name.");
        # raises ToolError("Unknown member 'x'. Members: ...") with close-match suggestions.
        # The rules live in module-level `resolve_member_in(pool, name, allow_user)`, shared with FakeSwarm.
    def language_of(self, name: str) -> str             # member language, else swarm language

    # notifications / waking
    def notify(self, recipient: str, key: str, *, wake: bool = True, **kw) -> None
        # text = prompts.t(language_of(recipient), key, **kw); insert notifications row (kind=key,
        # data=kw); emit "notification" {recipient, kind, text}; if wake: signal(recipient).
        # wake=False for a member: data also gets "_quiet": true; such a row is news for the member's
        # next wake-up (render_wake delivers it) but does not count in has_pending
    def signal(self, name: str) -> None                 # wakes LLM runtime / releases wait_signal waiters
    async def wait_signal(self, name: str, timeout: float) -> bool
    def has_pending(self, name: str) -> bool            # unread inbox or undelivered non-quiet notifications
    def render_wake(self, name: str, incoming: bool = False) -> str | None
        # drains (marks read/delivered) and renders the wake-up text (§6.3); None if nothing pending
        # and incoming=True
    def notes_shown(self, name: str, notes: str) -> None   # the agent's context now holds these notes

    # goal
    def goal(self) -> dict | None
    def set_goal(self, actor: str, title: str, description: str = "") -> dict
        # empty title -> ToolError; an empty board gets a planning task; agents are kicked off staggered
    def complete_goal(self, actor: str, summary: str) -> dict
        # ToolError when there is no goal, it is already done, or the summary is empty; notifies USER u_goal_done
    def reopen_goal(self, actor: str, comment: str = "") -> dict
        # only a done goal (else ToolError); notifies ONE LLM member n_goal_reopened: the one who completed
        # the goal, else the most recently active (the others are offered new tasks by the scheduler)

    # team
    async def hire(self, by: str, role: str, instructions: str = "", model: str | None = None,
                   name: str | None = None, language: str | None = None, kind: str = "llm") -> str
    async def retire(self, by: str, name: str, reason: str = "") -> list[int]   # returns released task ids

    # budget / control
    def limits(self) -> dict                             # {"max_cost_usd", "max_agents"}
    def set_limits(self, actor: str, max_cost_usd: float | None = None, max_agents: int | None = None) -> dict
    def spent_usd(self) -> float
    def record_usage(self, agent: str, usage: Usage, profile: ModelProfile, model: str | None = None) -> float
    def budget_exceeded(self) -> bool
    paused: bool; pause_reason: str
    def pause(self, actor: str, reason: str) -> None
    def resume(self, actor: str) -> None
        # n_resumed to LLM agents with open tasks, pending events or an activation the pause cut short
        # (runtime.interrupted); a reply owed to the user by that activation is still forwarded
    def make_llm(self, profile_name: str | None, agent: str) -> LLMClient
        # a member's profile (agents.model) that no longer exists in the settings (renamed or deleted) falls back
        # to the default profile with a warning, instead of crashing that agent at every activation
    def snapshot(self) -> dict                           # §14.1
```

Tests use `tests/conftest.py::FakeSwarm`, which implements the subset: `config, db, bus, language,
member, members, is_member, kind_of, resolve_member, language_of, notify, signal, has_pending,
board, mail, workspace, triggers, goal, set_goal, complete_goal` (attributes may be None until a test
wires them), and records notifications in `fake.notified: list[tuple[recipient, key, kw]]`. Its
resolve_member is the real `resolve_member_in`; set_goal / complete_goal validate and notify like the
real ones (the planning task is created when a real Board is wired).

Scheduler (every 3 s): expires file locks, prunes old traces (every 6 h) and closes idle external trace
sessions (§16); unless paused, offers ready unassigned tasks to idle LLM agents (`n_ready`), never more agents considering offers than there are ready tasks, and the kickoff
counts as everyone's offer of the planning task; wakes once (`n_stalled`) an idle agent whose
activation ended with plain text while holding in_progress tasks even after the nudge (§6.2); sends
`n_all_done` to one idle agent when every task is finished but the goal is open. `reconfigure()` applies
new settings to a running swarm, lifts a budget pause the new budget allows and retries agents in ERROR.

## 4. Board (board.py)

```python
UNSET = object()   # sentinel for "not passed" on nullable fields

class Board:
    def __init__(self, swarm): ...
    def get(self, task_id: int) -> Task          # with .blocked_by and .comments (count) filled; ToolError if missing
    def list(self, *, status: str | list[str] | None = None, assignee: str | None = None,
             tag: str | None = None, parent_id: int | None = None, include_closed: bool = False) -> list[Task]
        # ordered: priority desc, id asc. include_closed=False hides done/failed/cancelled unless
        # `status` explicitly asks for them.
    def blockers(self, task: Task) -> list[int]  # dependency ids whose status != done
    def create(self, actor: str, title: str, *, goal: str = "", description: str = "",
               assignee: str | None = None, reviewer: str | None = None, parent_id: int | None = None,
               depends_on: list[int] | None = None, priority: int = 1, tags: list[str] | None = None) -> Task
    def claim(self, actor: str, task_id: int) -> Task
    def update(self, actor: str, task_id: int, *, title=None, goal=None, description=None, status=None,
               assignee=UNSET, reviewer=UNSET, parent_id=UNSET, priority=None, tags=None,
               depends_on=None) -> Task
    def complete(self, actor: str, task_id: int, result: str) -> Task
    def review(self, actor: str, task_id: int, approve: bool, comment: str = "") -> Task
    def post(self, actor: str, task_id: int, text: str, reply_to: int | None = None) -> dict
    def discussion(self, task_id: int, since_id: int = 0, limit: int = 200) -> list[dict]
        # [{"id","task_id","author","text","reply_to","ts"}] ascending by id
    def participants(self, task_id: int) -> set[str]   # creator, assignee, reviewer, posters (no USER filtering)
    def ready_unassigned(self) -> list[Task]           # todo, no assignee, no blockers
    def open_tasks_of(self, name: str) -> list[Task]   # assignee == name and status open
    def release_all(self, name: str, actor: str) -> list[int]   # open tasks of name -> todo, assignee None
        # (their ids are returned); open tasks name was to review lose the reviewer, and one in `review`
        # goes back to in_progress (todo without assignee) with `n_reviewer_left` {reviewer} to its assignee
    def counts(self) -> dict[str, int]                 # per status
```

Rules (USER may do anything; "member" = agent of any kind):
* `create`: title non-empty (<= 200 chars). `assignee`/`reviewer` resolved via `swarm.resolve_member`
  (USER allowed). `depends_on` ids must exist, no self/cycles. `parent_id` must exist. priority 0..3.
  Status `todo`. Emits `task.created`. If assignee given and != actor: notify assignee `n_assigned`.
* `claim`: status must be `todo` (if already assigned to actor and in_progress/waiting: return as is).
  assignee must be None or actor (else ToolError naming the holder). No blockers (ToolError lists them).
  Sets assignee=actor, status=in_progress. Atomic (DB transaction re-reads the row).
* `update` permissions for members: title/goal/description/priority/tags/depends_on/reviewer/parent_id —
  creator or assignee. assignee — anyone if task is unassigned; otherwise current assignee or creator.
  Reassignment of an in_progress/waiting task resets status to `todo`. Notify new assignee `n_assigned`
  and removed assignee `n_unassigned` (never notify the actor themself).
  status transitions for members: assignee may set `in_progress` (only without blockers), `waiting`,
  `todo` (= release: assignee cleared), `failed`; creator or assignee may set `cancelled`; creator may
  reopen a terminal task to `todo`. `done`/`review` only via `complete`/`review` (USER may set `done`).
* `complete`: actor must be the assignee (or USER); status must be open and not `review`.
  result non-empty. If reviewer set and reviewer != actor → status `review`, notify reviewer
  `n_review_requested`; else `done`.
* `review`: actor is reviewer (or USER), status `review`. approve → `done`, notify assignee
  `n_review_approved`; reject → `in_progress` (or `todo` when the task has no assignee), notify assignee
  `n_review_rejected`. A non-empty comment is validated first and also posted to the discussion.
  Changing the reviewer of a task in review notifies the new reviewer (`n_review_requested`).
* Side effects whenever a task becomes `done`: each open task that depends on it and now has no
  blockers → notify its assignee `n_unblocked` (if assignee and != actor). Whenever a task becomes
  terminal: notify creator `n_task_closed` (if creator is a member, != actor); it wakes the creator only
  when the task failed / was cancelled or the creator has a `waiting` task, otherwise wake=False. USER is never notified
  about tasks (the UI shows them) except via discussion mentions/posts.
* `post` (discussion): text non-empty (<= 20000 chars); reply_to must be a post of the same task.
  Recipients = participants ∪ {members mentioned as `@Full Name` (case-insensitive, match against
  member names)} − {actor}. Mentioned members get `n_mention`, other recipients `n_discussion`.
  USER is notified only when mentioned (`@user`) or when USER is the task creator/assignee.
  Emits `task.comment` {task_id, post}. Text in notifications truncated to 1500 chars.
* Events: `task.created` {task}, `task.updated` {task, changes: {field: [old, new]}}, `task.comment`.
  `task` payloads are `Task.to_dict()`.

## 5. Mailbox (mailbox.py)

```python
class Mailbox:
    def __init__(self, swarm): ...
    def send(self, sender: str, to: str | list[str], text: str, reply_to: int | None = None) -> dict
        # to: name, list of names, "all" (all active members except sender; USER not included) or USER.
        # Resolves via swarm.resolve_member; duplicates removed; sender cannot be the only recipient.
        # text non-empty, <= 50000 chars. Inserts message + inbox rows; emits "message.sent" {message};
        # swarm.signal(each recipient).
    def get(self, message_id: int) -> dict
    def unread(self, name: str, mark_read: bool = True, limit: int = 100) -> list[dict]
    def mark_read(self, name: str, message_ids: list[int]) -> int   # returns how many were unread
        # read_messages marks every message it shows (with=..., include_read) read for its caller
    def unread_count(self, name: str) -> int
    def history(self, name: str, with_name: str | None = None, limit: int = 50, before_id: int | None = None) -> list[dict]
    def conversation(self, a: str, b: str, limit: int = 200, since_id: int = 0) -> list[dict]
        # messages where (sender a and b in recipients) or (sender b and a in recipients), ascending
    def recent(self, limit: int = 100, since_id: int = 0) -> list[dict]
# message dict: {"id","ts","sender","recipients":[...],"text","reply_to"}
```

## 6. Agent runtime (agent.py)

### 6.1 Neutral message format (history rows, LLM input)
```python
{"role": "user", "content": str}
{"role": "assistant", "content": str, "tool_calls": [{"id": str, "name": str, "arguments": dict}],
 "raw": {"provider": "anthropic", "model": str, "content": [block dicts]} | None}
{"role": "tool", "tool_call_id": str, "name": str, "content": str, "is_error": bool}
```
History is **append-only** (Anthropic binds thinking blocks to the exact prefix). The only rewrite is
*simple compaction*: when the context is too large, the agent asks its LLM (same system prompt and
tools, `compaction_request` appended as a user message) for a summary and replaces the whole
history with one user message `compacted` (+ the agent's `notes`, if any) + `continue`, followed by the
newest trailing user-side texts kept verbatim (the wake-up that started the activation, new events;
at most 40000 chars, never an earlier summary). The old history is archived for traces in the same
transaction. Without a usable LLM summary (a context-overflow error or another non-retryable 400 for
the compaction request, or an overflow of the main call) the agent compacts without the LLM: notes +
a digest of its latest steps + the kept texts. The system prompt is frozen in `agents.system_prompt` at
creation; the tool list for an agent kind is constant (agent-built MCP tools go through `mcp_call`).

### 6.2 Loop
```python
class AgentRuntime:
    def __init__(self, swarm, name: str): ...
    name: str; state: str
    def start(self) -> None            # creates the asyncio task
    async def stop(self) -> None
    def wake(self) -> None
    def end_turn(self) -> None         # called by the `wait` tool
    def note_sent_to_user(self) -> None  # called by send_message when USER is a recipient
```
* Waits for `wake()`. Skips if swarm paused or nothing pending (unless the last activation was cut
  short: `interrupted`). Activation: `render_wake(name)` →
  user message; loop up to `max_steps_per_turn`: budget check (exceeded → `swarm.pause`), compaction
  check: last prompt tokens + its output tokens (or, without reported usage, an estimate of the history
  + system prompt + tool schemas) + estimate of appended messages > budget, where budget = context
  limit (`profile.context_tokens` or `agents.context_tokens`) − min(max_tokens, limit/4) − the
  compaction request, so the summary call still fits when the limit is the model's real window; when a
  fresh compaction already fills more than 3/4 of the budget, the next one waits until the history grew
  by half a budget (no thrashing). Then `async with
  swarm.llm_semaphore: llm.complete(system, history, tools)`, record usage, append assistant message,
  emit `agent.activity` events (thinking summary / text / tool call / tool result, truncated 2000
  chars), execute tool calls sequentially via `tools.call_tool`, append results; after tools, if
  `render_wake(name, incoming=True)` returns text, append it as a user message. Stops when: `wait` was
  called, no tool calls (a `nudge` once per in_progress task the agent holds; after plain text even
  then, `stalled` records those tasks and the scheduler wakes it once with `n_stalled`), refusal, or
  step limit (then `notify(name, "n_step_limit")`). A reply cut off by max_tokens gets `cut_off` and
  the loop continues. After an activation, the agent re-wakes itself if events are still pending.
* If the activation was caused by a USER message and the agent finished with plain text without
  messaging USER, the text (all parts, truncated to the mailbox limit) is forwarded to USER as a
  message from the agent; an activation cut short owes that reply to its continuation.
* LLM errors: retryable → backoff 2,4,8,16,32 s (5 tries, stopped by pause/stop); a context overflow →
  compaction without the LLM and one retry; otherwise state `error`, notify USER (kind `u_agent_error`,
  once per streak of the same error), end the activation. Retryable failures and crashes are retried
  automatically after `RECOVERY_DELAYS` (30, 60, 120, 300, 600 s); non-retryable ones wait for the next
  event (or `reconfigure()`). A crash (any exception, or a CancelledError the task was not asked for)
  repairs dangling tool calls, sets `error` and schedules a recovery. History is repaired on start
  (dangling tool calls get `"interrupted"` tool results; only results after the last assistant message
  count). A history row is inserted before the in-memory history is updated.
* `stop()` cancels the task without swallowing the caller's cancellation, resets WORKING to IDLE and
  closes the LLM client (`aclose`). `reset_llm()` drops the cached client after a settings change.
* State changes emit `agent.status` {name, status}; persisted in `agents.status`.

### 6.3 Wake-up text (render_wake)
`wake_header` (or `incoming_header`), `reasons:` then one bullet per undelivered notification text and
per unread message (`msg` template, recipients list with the agent shown as `you`), then (not for
incoming) goal line, `my_tasks`, `ready` (up to 8), notes: in full only when they changed since they
were last put into the agent's context (a wake-up or a compaction; once more after a restart),
otherwise the one-line `notes_unchanged`, since the history is append-only.

## 7. Workspace (workspace.py) and shell (shell.py)

```python
class Workspace:
    root: Path                                   # config.workspace_dir
    def __init__(self, swarm): ...
    async def init(self) -> None                 # mkdir; if git enabled+available: init repo, .gitignore, initial commit
    def resolve(self, path: str) -> Path         # inside root (no escape, no .git); ToolError otherwise
    def rel(self, p: Path) -> str                # posix relative path
    def read(self, path: str, offset: int = 1, limit: int = 400) -> str   # lines offset..offset+limit-1, header "[path: lines a-b of N]"; binary -> ToolError
    async def write(self, actor: str, path: str, content: str) -> str
    async def edit(self, actor: str, path: str, old: str, new: str, replace_all: bool = False) -> str
        # old must occur exactly once unless replace_all; ToolError with hints otherwise
    def list(self, path: str = ".", depth: int = 2) -> str   # tree text, dirs end with "/", skips .git, sizes
    def tree(self, path: str = ".", depth: int = 4) -> list[dict]   # for the web UI: [{"path","type","size","mtime"}]
    def read_raw(self, path: str, max_bytes: int = 2_000_000) -> tuple[str, bool]  # (text, truncated) for UI
    def lock(self, actor: str, path: str, minutes: float | None = None) -> str
    def unlock(self, actor: str, path: str, force: bool = False) -> str   # force for USER
    def locks(self) -> list[dict]                # [{"path","owner","acquired_at","expires_at"}]
    def check_writable(self, actor: str, path: str) -> None
    def release_all(self, actor: str) -> int
    def expire_locks(self) -> int
    async def autocommit(self, actor: str, message: str) -> str | None   # commit hash or None; author = actor
    def mtime(self, path: str) -> float | None
    def exists(self, path: str) -> bool
```
Writes/edits check locks, create parent dirs, write UTF-8, emit `file.changed` {path, op} (the actor is
the event's top-level `actor`), then commit that path. Lock events: `file.locked` {path, owner, expires_at,
refreshed}, `file.unlocked` {path, owner, forced} or {path, owner, expired: true}. `init()` also writes
repo-local git settings (core.autocrlf=false, a default identity) so agents can run git themselves.

```python
async def run_shell(command: str, *, cwd: Path, timeout: float, program: str = "auto",
                    env: dict[str, str] | None = None, max_output: int = 12000) -> tuple[int | None, str]
    # returns (exit_code | None on timeout, combined stdout+stderr, decoded, head+tail truncated)
def resolve_shell(program: str = "auto") -> tuple[str, list[str]]   # (display name, argv prefix)
def scrubbed_env(config: Config, extra: dict[str, str] | None = None) -> dict[str, str]
    # os.environ minus secrets (model api_key_env vars, *_API_KEY, *_TOKEN, *SECRET*, *PASSWORD*)
    # except config.shell.pass_env; plus PYTHONIOENCODING=utf-8, PYTHONUTF8=1 and `extra`
```
Kills the whole process tree on timeout (Windows: `taskkill /T /F`; POSIX: process group).

## 8. Triggers (triggers.py)

Condition DSL: a Python *expression* parsed with `ast` and evaluated by a whitelist interpreter
(never `eval`). Allowed: `and or not`, comparisons `== != < <= > >= in not in`, literals (str, int,
float, bool, None, tuple/list/set of literals), bare status names (`todo in_progress waiting review
done failed cancelled`) evaluate to strings, function calls below (positional + keyword). Max 500
chars / 200 AST nodes.

| function | value |
|---|---|
| `status(id)` | status string, `"missing"` if no task |
| `done(*ids)` | all done |
| `any_done(*ids)` | at least one done |
| `finished(*ids)` | all terminal (done/failed/cancelled) |
| `failed(*ids)` | at least one failed |
| `assignee(id)` | assignee or None |
| `count(status=None, tag=None, assignee=None, parent=None)` | number of matching tasks (status may be a tuple) |
| `all_done(tag=None, parent=None)` | ≥1 matching task and all done |
| `message(sender=None, contains=None)` | a message to the owner arrived after creation/last fire |
| `comment(task=None, sender=None, contains=None)` | a discussion post (not by the owner) after creation/last fire |
| `file_exists(path)` / `file_changed(path)` | workspace file exists / mtime > creation/last fire |
| `after("2026-10-09 18:00")` | local time reached (ISO) |
| `elapsed(seconds)` | seconds since creation |
| `every(seconds)` | ≥ seconds since last fire (min 10); makes the trigger periodic |
| `agent_status(name)` | member status |
| `goal_done()` | global goal status is done |

```python
class TriggerEngine:
    tick_seconds: float = 2.0
    def __init__(self, swarm): ...
    def validate(self, condition: str) -> None              # ToolError with the grammar summary on error
    def create(self, owner: str, condition: str, *, note: str = "", actions: list[dict] | None = None,
               once: bool = True) -> dict
    def delete(self, actor: str, trigger_id: int) -> None    # owner or USER
    def list(self, owner: str | None = None, include_inactive: bool = False) -> list[dict]
    def mark_dirty(self) -> None
    def evaluate_all(self) -> int                            # returns number fired
    async def run(self) -> None                              # loop: wait dirty-or-tick, evaluate_all
```
`create` resolves member names in the condition (`sender=` of message/comment, `assignee=` of count,
`agent_status(name)`) with `swarm.resolve_member(allow_retired=True)` (a unique first name, `@Name`, any
case; agent_status does not accept the user) and stores the condition rewritten (`ast.unparse`) with the
canonical names; an unknown member is a ToolError at creation. Evaluation compares names exactly
(case/whitespace-insensitive).
Fires on false→true edge (and on every evaluation where it is true if it uses `every`); a trigger
true at creation fires on the first evaluation. `once` → deactivated after firing. Actions
(default `[{"type": "wake"}]`): `wake` → `notify(owner, "n_trigger", id, condition, note)`;
`message` {to, text} → `mail.send(owner, to, text)`; `set_status` {task_id, status} →
`board.update(owner, ...)`; `create_task` {title, goal?, description?, assignee?, tags?, priority?}.
Action errors are stored in `last_error` and reported to the owner via `n_trigger` note. Evaluation
errors are stored in `last_error`, not fired. After a fire, `message()`/`comment()`/`file_changed()`/`every()`
are measured from that fire. A repeating trigger with message/set_status/create_task actions that fires 20
times within 10 s is deactivated and its owner is told why (flood guard). The engine listens to the bus: `task.*`, `message.*`,
`file.*`, `goal.*`, `agent.*` mark it dirty. Events: `trigger.created`, `trigger.fired`, `trigger.deleted`.
Trigger dict: {"id","owner","condition","note","actions","once","active","created_at","last_fired","fire_count","last_error"}.

## 9. Event types (bus)

`task.created task.updated task.comment message.sent notification goal.set goal.completed
goal.reopened agent.hired agent.retired agent.status agent.activity agent.compacted agent.usage
file.changed file.locked file.unlocked trigger.created trigger.fired trigger.deleted
mcp.created mcp.status swarm.paused swarm.resumed swarm.limits`

Live-only events (sent with the EventBus method `publish(type, actor, **data)` instead of `emit`: delivered to
listeners and WebSocket subscribers with id None, not journaled): `trace.started`, `trace.span`,
`trace.finished` (§16).

## 10. LLM (llm/)

```python
@dataclass class ToolCall: id: str; name: str; arguments: dict; parse_error: str | None = None
@dataclass class Usage: input_tokens: int = 0; output_tokens: int = 0; cache_read_tokens: int = 0;
                        cache_write_tokens: int = 0; cost_usd: float | None = None   # provider-reported
@dataclass class LLMResponse: text: str; tool_calls: list[ToolCall]; usage: Usage; stop_reason: str;
                              model: str; raw: dict | None = None; thinking: str = ""
class LLMError(Exception): message: str; retryable: bool; kind: str  # rate_limit auth bad_request server connection refusal other
class LLMClient(Protocol):
    profile: ModelProfile
    async def complete(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse
        # tools: [{"name","description","parameters"}]; stop_reason normalised to
        # "end_turn" | "tool_use" | "max_tokens" | "refusal" | "other"
def estimate_tokens(obj) -> int    # ~ len(json)/3.5
def create_llm(profile: ModelProfile) -> LLMClient   # llm/__init__.py
# clients may also expose `async aclose()` to release their HTTP pool (called when an agent stops)
# stop_reason "other" covers pause_turn / stop_sequence and unknown values
def cost_of(profile: ModelProfile, usage: Usage, model: str | None = None) -> float
```
* **AnthropicLLM** (`anthropic.AsyncAnthropic`, SDK 1.x): converts neutral → Messages API (consecutive
  user-side items merged into one user message, tool_result blocks first; assistant uses `raw.content`
  verbatim when `raw.provider == "anthropic"`), top-level `cache_control={"type":"ephemeral"}`,
  `thinking={"type":"adaptive","display":...}` unless profile.thinking == "off", `output_config.effort`,
  `max_tokens`. Beta features (`fallbacks`, `thinking_mismatch` → `thinking.block_binding.prefix_mismatch_behavior`)
  switch to `client.beta.messages.create(..., betas=[...])`; if the API rejects a beta parameter, the
  client disables betas for itself and retries once. On a 400 about an invalid thinking `signature`,
  retries once with all thinking blocks stripped. `raw.content` = `[b.to_dict(mode="json", exclude_none=True)]`.
  stop_reason `refusal` → LLMResponse with stop_reason "refusal" and text describing the category.
* **OpenAILLM** (`openai.AsyncOpenAI`, chat.completions, any compatible base_url): tool calls
  `function.arguments` JSON-parsed (failure → `parse_error`), `reasoning_effort` from profile.effort,
  max tokens param name from profile (default `max_completion_tokens` for api.openai.com, else
  `max_tokens`), `usage.cost` used when the provider reports it (OpenRouter).
* **ScriptedLLM(handler)**: `handler(system, messages, tools) -> LLMResponse | dict | str` (sync or
  async); dict shorthand `{"text": ..., "tool_calls": [{"name","arguments"}]}`; str = text only.

## 11. MCP forge (mcp_forge.py)

```python
class MCPConnection:      # one running stdio MCP server, owned by a dedicated asyncio task
    def __init__(self, name: str, command: list[str], cwd: Path, env: dict, log_path: Path): ...
    async def start(self, timeout: float) -> list[dict]      # tool list [{"name","description","input_schema"}]
    async def call(self, tool: str, arguments: dict, timeout: float) -> tuple[str, bool]
    async def close(self) -> None
    alive: bool

class MCPForge:
    def __init__(self, swarm): ...
    async def start(self) -> None     # register config.mcp.external servers (origin "config"); no processes yet
    async def stop(self) -> None
    async def create(self, actor: str, name: str, description: str, code: str, *, scope: str = "swarm",
                     task_id: int | None = None, requirements: list[str] | None = None,
                     save_to_library: bool = True) -> str
    def list(self, actor: str, query: str = "") -> list[dict]
    async def tools(self, actor: str, name: str) -> list[dict]
    async def call(self, actor: str, server: str, tool: str, arguments: dict | None = None,
                   timeout: float | None = None, max_chars: int | None = None) -> tuple[str, bool]
        # result cut to max_chars (default agents.tool_output_chars); mcp_call passes ToolContext.output_cap
    async def approve(self, actor: str, name: str, approve: bool, comment: str = "") -> str
    async def remove(self, actor: str, name: str) -> str
    def get(self, name: str) -> dict | None
```
* Server code uses the mcp 2.x high-level API: `from mcp.server.mcpserver import MCPServer`,
  `mcp = MCPServer("name")`, `@mcp.tool()`, `mcp.run()` (stdio). Files: `workspace/mcp/<name>/server.py`,
  `meta.json`. Name: `^[a-z][a-z0-9_]{1,39}$`.
* Command: `[sys.executable, "server.py"]`, or with requirements and `uv` on PATH:
  `["uv", "run", "--no-project", "--with", "mcp>=2.3,<3", "--with", req..., "python", "server.py"]`.
  Env = `shell.scrubbed_env` (plus `COLUMNS=200` unless set: MCPServer logs through rich, whose 80-column boxes
  would wrap the error lines). stderr → `.dreamteam/logs/mcp-<name>.log`; the log tails shown to agents drop
  the padding and right borders of rich's boxed tracebacks.
* Client: `mcp.Client(mcp.stdio_client(StdioServerParameters(...), errlog=file))`; enter/exit inside the
  connection's own task (anyio requirement); `list_tools().tools[i].input_schema`; `call_tool(name, args)`
  → `CallToolResult(content, structured_content, is_error)`; text from TextContent, placeholders for
  other content, structured_content JSON when there is no text; truncated by MCPForge.call.
* `create`: write files, smoke test (start + list tools, startup_timeout), on failure status `failed`
  and return the error plus the log tail so the agent can fix and call create again (same name
  overwrites, only by owner or USER). Success → status `active` (or `pending` if
  `config.mcp.require_approval` and actor != USER → notify USER kind `u_mcp_pending`). Statuses:
  `active`, `pending`, `failed`, `rejected`, `removed`, and `available` for library entries not yet
  imported. Origins: `workspace` (built here), `library`, `config`. Importing from the library also honours
  `require_approval`. Swarm-scoped servers
  are copied to the library (`config.library_dir/<name>/` server.py + meta.json) when save_to_library.
* Scopes: `private` (owner only), `task` (owner + creator/assignee/reviewer of task_id), `swarm` (all).
  USER sees all. `list` merges project servers and library entries not in the project (`origin: "library"`);
  using a library server imports it into the project first.
* Events: `mcp.created` {server}, `mcp.status` {name, status}. Server dict: {"name","owner","scope",
  "task_id","description","status","origin","tools":[names],"error","requirements","command","created_at"}
  (`command`: a configured server's argv; `[]` for a forged one, which runs its `server.py`).

## 12. Tools (tools/)

```python
@dataclass
class ToolSpec:
    name: str; description: dict[str, str]  # {"en","ru"}
    parameters: dict                        # JSON schema {"type":"object","properties":...,"required":[...]}
    handler: Callable[[ToolContext, dict], Awaitable[str]]
    audience: frozenset[str]                # subset of {"llm","external","user"}
    allow_empty: frozenset[str]             # required string args that may be "" (write_file content...)
    order: tuple[int, int]                  # (module rank, definition order): registry order
@dataclass
class ToolContext:
    swarm: Swarm; actor: str; runtime: AgentRuntime | None = None
    @property kind -> str                    # swarm.kind_of(actor), "unknown" for a non-member
    @property output_cap -> int              # max chars of big outputs (shell, MCP results): with a runtime
                                             # (it cuts results for the model and traces them in full)
                                             # max(tool_output_chars, tracing.max_payload_chars), else
                                             # agents.tool_output_chars
REGISTRY: dict[str, ToolSpec]               # sorted by `order` once all modules are loaded (import-order free)
def tool(name, en, ru, properties, required=(), audience=("llm","external","user"), allow_empty=()): decorator
def tools_for(kind: str) -> list[ToolSpec]
def schemas_for(kind: str, language: str) -> list[dict]   # [{"name","description","parameters"}], deterministic copies
async def call_tool(ctx: ToolContext, name: str, args: dict) -> tuple[str, bool]
    # rejects actors that are neither USER nor an active member; unknown tool -> close matches + the
    # available tools; audience errors say how to fix the call (join_swarm/as_agent); checks required args
    # (whitespace-only = missing unless allow_empty) and types with coercion ("5"/"#5" -> 5, JSON strings for
    # arrays/objects, 0/1 -> bool, numbers -> str, case-insensitive enums, finite numbers); validates enums;
    # unknown args are ignored but reported in the result; ToolError -> ("Error: msg", True);
    # UnicodeEncodeError (lone surrogate) -> an "invalid Unicode" error; other exception ->
    # ("Internal error in <tool>: ...", True) + log
```
Tool catalogue (audience: L = llm agents, E = external MCP agents, U = user via UI/MCP):

| tool | args | aud |
|---|---|---|
| `board` | status?, assignee? ("me" ok), tag?, include_closed? | LEU |
| `task_get` | task_id | LEU |
| `task_create` | title, goal, description?, assignee?, reviewer?, depends_on?, parent_id?, priority?, tags? | LEU |
| `task_claim` | task_id | LE |
| `task_update` | task_id, title?, goal?, description?, status?, assignee? ("" = unassign), reviewer?, parent_id? (0 = none), priority?, tags?, depends_on?, comment? | LEU |
| `task_complete` | task_id, result | LEU |
| `task_review` | task_id, approve, comment? | LEU |
| `task_comment` | task_id, text, reply_to? | LEU |
| `task_discussion` | task_id, since_id? | LEU |
| `send_message` | to (string or array), text, reply_to? | LEU |
| `read_messages` | with?, limit?, include_read? | LEU |
| `agents` | — | LEU |
| `hire_agent` | role, instructions, model?, name? | LEU |
| `retire_agent` | name?, reason? | LEU |
| `update_notes` | content | L |
| `wait` | reason? | L |
| `goal_complete` | summary | LE |
| `trigger_create` | condition, note?, once?, actions? | LEU |
| `trigger_list` | all? | LEU |
| `trigger_delete` | trigger_id | LEU |
| `read_file` | path, offset?, limit? | LEU |
| `write_file` | path, content | LE |
| `edit_file` | path, old_text, new_text, replace_all? | LE |
| `list_files` | path?, depth? | LEU |
| `lock_file` | path, minutes?, unlock? | LE |
| `shell` | command, timeout? | LE |
| `mcp_create` | name, description, code, scope?, task_id?, requirements? | LE |
| `mcp_list` | query? | LEU |
| `mcp_tools` | server | LEU |
| `mcp_call` | server, tool, arguments? | LE |
| `join_swarm` | role, name?, instructions? | EU (the user via MCP only, before joining; creates kind "external") |
| `wait_for_events` | timeout? (s, default 50, max 600; an error at once when the swarm stops meanwhile) | EU |
| `set_goal` | title, description? | U |
| `swarm_status` | — | EU |
| `swarm_control` | action (pause/resume), max_cost_usd?, max_agents?, reopen_goal_comment? | U |
| `mcp_approve` | name, approve, comment? | U |
| `agent_traces` | agent?, trace_id?, span_id?, context?, status?, task?, limit?, offset? | EU (§16.2) |

Tool results are plain text (compact, line-oriented), language = English for data, ToolError texts in
English. The `wait` tool calls `ctx.runtime.end_turn()`. `send_message` calls
`ctx.runtime.note_sent_to_user()` when USER is a recipient.

## 13. Hub MCP (hub_mcp.py)

```python
def build_hub_server(swarm) -> mcp.server.lowlevel.Server
    # on_list_tools: tools for audiences E∪U, each schema extended with optional "as_agent"
    # (string: name returned by join_swarm; omit to act as the user). on_call_tool: actor =
    # as_agent (must be an active external member) or USER; returns
    # CallToolResult(content=[TextContent], is_error). `instructions=` explains the swarm.
async def hub_call(swarm, name: str, args: dict) -> tuple[str, bool]
    # one MCP tool call with the actor rules above (also used by the launcher MCP, §18.2); calls of external
    # members are traced (tracer.external_call), except UNTRACED_TOOLS = {"wait_for_events"}
async def serve_stdio(swarm) -> None
async def serve_stdio_proxy(url: str, instructions: str | None = None) -> None
    # stdio server forwarding to a running hub's /mcp (a swarm's or the launcher's); the client gets the
    # upstream server's instructions unless `instructions` is given
class MCPHttpEndpoint:   # ASGI app for /mcp backed by StreamableHTTPSessionManager
    def __init__(self, swarm, host: str, extra_hosts=()): ...   # extra_hosts: DREAMTEAM_ALLOWED_HOSTS
    manager: StreamableHTTPSessionManager
```
In mcp 2.x the request context is passed to handlers (`ctx.session` is per request), so identity
is explicit via `as_agent`. Stdio servers read stdin with `_StdinLines` (a daemon reader thread), so a stop
signal ends them even while the client keeps stdin open.

## 14. Web (web.py) — REST for the UI and the console

`create_app(swarm, host) -> Starlette` is the app of ONE swarm: the whole server of `dreamteam run`, and the
per-session app the launcher serves under `/s/{sid}/` (§18.1). All JSON (`httpjson.JSONResponse`; the
WebSocket sends `httpjson.json_text`). POST /api/* bodies require `Content-Type: application/json`. Guard
(`GuardMiddleware`, every bind, also 0.0.0.0 in Docker): Host must be localhost/127.0.0.1/[::1]; for
non-loopback binds also an IP literal or the bind address; plus names in `DREAMTEAM_ALLOWED_HOSTS`
(comma-separated). Origin must be loopback, an allowed name, or same-origin (WebSockets too).
`DREAMTEAM_PUBLIC_URL` is the base URL users reach the hub at (shown as the MCP URL and written to
hub.json; `dreamteam sandbox` sets it). `serve(swarm, host, port, ..., on_started=)` sets
`swarm.hub_url = public_base(host, port) + "/mcp"`, calls back once listening and shuts down gracefully when
cancelled. Pages (`/`, launcher `/`) are sent with `Cache-Control: no-store`; `/static` is `UIStaticFiles`
(`Cache-Control: no-cache`: browsers revalidate, so an upgraded server never runs a stale app.js).

| method path | response |
|---|---|
| GET `/` | static/index.html |
| GET `/static/{file}` | static files |
| GET `/api/state` | snapshot (§14.1 below) |
| GET `/api/hub` | {pid, project, name, version}: identity of this hub process (checked against hub.json) |
| GET `/api/tasks/{id}` | {task, discussion: [...], events: [...]} |
| GET `/api/messages?with=Name&limit=&since=` | conversation USER↔Name; without `with`: all recent messages |
| GET `/api/inbox?since_message=&since_notification=` | `{"messages","notifications"}` to USER: with since_* > 0 the ones with a larger id (oldest first, ≤ 500); without, the newest 500 (ascending). `since` = alias of since_message. Messages carry `read` |
| POST `/api/read` | body `{"with"?: name}`: marks USER's messages (from that member) read → {"ok", "unread"} |
| GET `/api/events?since=&limit=&types=` | events |
| GET `/api/agents/{name}/activity?limit=` | `agent.activity` events of that agent |
| GET `/api/files?path=` | dir → {"type":"dir","entries":[...]}; file → {"type":"file","content","truncated"} |
| POST `/api/act/{tool}` | run tool as USER: body = args → {"ok": bool, "result": str} |
| `/api/traces...`, `/api/spans/...`, `/api/agents/{name}/traces.jsonl` | traces (§16.3) |
| WS `/ws` | pushes every bus event as JSON (also the live-only `trace.*` events) |
| `/mcp` | hub MCP over streamable HTTP |

### 14.1 snapshot
```json
{"swarm": {"name","language","paused","pause_reason","limits":{"max_cost_usd","max_agents"},
           "spent_usd","goal": {...}|null, "counts": {status: n}, "mcp_url", "models": [profile names],
           "session": {"id","title","status": "running"|"stopped"}|null,   // null outside a SessionManager
           "tracing": bool},
 "tasks": [Task.to_dict() + {"blocked_by","comments"}],
 "agents": [{"name","kind","role","status","model","language","cost_usd","tokens_in","tokens_out","hired_by",
             "created_at","last_active","instructions","context_tokens","open_tasks":[ids],"unread"}],
 "triggers": [...], "mcp": [...], "locks": [...],
 "user": {"unread": n}}
```

## 15. CLI (cli.py)

Commands of a single-swarm **project** (a directory with `dreamteam.toml`, made by `init`); the launcher
commands of a **home** are in §18.3.
```
dreamteam init [DIR] [--lang ru|en] [--provider anthropic|openai|openrouter|ollama|vllm] [--name N] [--goal G] [--force]
dreamteam run [DIR] [--goal G] [--host H] [--port P] [--no-console] [--open]
    # one swarm, its board at / (web.serve); refuses when a hub already runs for DIR
```
`console`, `mcp`, `status` and `sandbox` work with a project as well as with a home (§18.3).

## 16. Traces (tracing.py)

Each activation (wake-up) of an LLM agent is one **trace** (`kind = "llm"`). An external member (hub MCP
`as_agent`) gets **session traces** (`kind = "external"`) that group its tool calls. A trace holds **spans**:

| kind | name | input | output | notes |
|---|---|---|---|---|
| input | `wake` / `incoming` / `nudge` / `continue` | – | text added to the context | `record()` |
| llm | `step` / `compaction` (the purpose) while running; the model id when finished | `{"epoch","upto","messages","extra"}` | `{"stop_reason","thinking","text","tool_calls":[{id,name,arguments,parse_error?}]}` | usage, cost; meta `{purpose, attempt}` + `{stop_reason}`; a refusal ends the span with status `error`; every failed attempt is its own span with status `error`, `error` text and meta `{error_kind, retry_in?}`, named after the configured model; a call cut off by a stop is `interrupted` (error_kind `cancelled`, not counted as an error) |
| tool | tool name | arguments (dict) | full result text (before truncation for the model) | meta `{call_id, chars, shown_chars?}`; status `error` if is_error; task ids in `task_id` / `parent_id` / `depends_on` are added to the trace's `tasks` |
| compaction | `compaction` | `{"messages","prompt_tokens"}` | summary text | parent of its llm span; meta `{epoch_archived, fallback?}`; status `error` when the summary call failed |
| event | `reply_to_user` / `refusal` / `budget_pause` / `step_limit` / `stopped` / `crash` | – | text | `refusal` and `crash` have status `error` |

- Span statuses: `running`, `ok`, `error`, `interrupted` (cut off: a stop, or a finished/recovered trace).
- Trace statuses (`TRACE_STATUSES`): `running`, `done`, `error`, `step_limit`, `refusal`, `paused`, `stopped`,
  `interrupted`. The list filter `status=failed` matches error, step_limit, refusal and interrupted.
- Trace row: `{id, agent, kind, started_at, ended_at, status, outcome, trigger, from_user, steps, llm_calls,
  tool_calls, errors, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_usd, tasks: [ids],
  model, duration_ms}` (a running trace's duration counts to now). Summaries (lists, events) cut `trigger` to
  300 chars. Span row: `{id, trace_id, agent, parent_id, step, kind, name, started_at, ended_at, duration_ms,
  status, input, output, meta, usage, cost_usd, error}`.
- `trace.outcome` is a short human sentence.
- Payload strings are capped at `tracing.max_payload_chars` (middle cut `…[N chars cut]…`); NaN / Infinity in
  payloads are stored as strings (the HTTP JSON encoder rejects them); plain TEXT columns go through
  `db.utf8_safe`, so model output with lone surrogates never breaks the writer.
- Live events are published with `bus.publish` (§9): `trace.started {trace}`, `trace.span {trace, span}` and
  `trace.finished {trace}`. Here `trace` is a trace summary and `span` is
  `{id, kind, name, step, status, duration_ms, cost_usd, usage, parent_id}`.
- Compaction archive: `AgentRuntime._replace_history` calls `swarm.tracer.archive_history(db.conn, name)` inside
  its `db.transaction()`, before deleting the history. The epoch number comes from the `history_archive` table (a
  rolled-back transaction leaves no gap); every archived epoch has a marker row (`seq = -1`, msg NULL), and with
  tracing off only the marker is written (plus the messages when the agent's newest LLM trace may still need
  them). `Tracer.span_context(span_id)` then rebuilds `{span_id, agent, epoch, model, system, tools, messages,
  complete, note}` for any LLM span, reading the live epoch from the table (a second reader such as the CLI
  gets it right too).
- External members: `tracer.external_call(agent, tool, args_without_as_agent, result_text, is_error, started_at)`
  for every hub tool call by an external member except `wait_for_events`. A session trace closes (`done`) at
  its last call after `EXTERNAL_IDLE_SECONDS` (15 min) without calls (scheduler: `close_external(idle_seconds)`),
  when the swarm stops (`close_external()`), or at the next start (`recover()`).
- API (all on `Tracer(db, bus=None, *, enabled=True, max_payload_chars=200_000)`):
  `begin(agent, trigger="", *, kind="llm", from_user=False, started_at=None) -> Trace`,
  `finish(trace_id, status="done", outcome="", *, ended_at=None)`, `recover() -> int` (open traces of a previous
  process: llm → `interrupted`, external → `done`), `external_call`, `close_external(idle_seconds=None)`,
  `current_epoch(agent)`, `archive_history(conn, agent) -> epoch`,
  `list_traces(agent=None, *, limit=50 (max 500), before=None, status=None, task=None, kind=None)` (newest
  first), `trace_summary(id)`, `get_trace(id, *, payload_chars=0)` (with `spans`), `get_span(id, *,
  payload_chars=0)`, `span_context(id, tools=None)`, `stats(agent=None, since=None)`, `export(id, *,
  contexts=False)` and `prune(older_than_days) -> int`.
  - `stats()` → `{agent, traces, llm_calls, tool_calls, errors, input_tokens, output_tokens, cache_read_tokens,
    cache_write_tokens, cost_usd, statuses: {status: n}, tools: [{name, calls, errors, avg_ms, max_ms}],
    models: [{name, calls, errors, avg_ms, cost_usd, output_tokens}]}` (a covering index keeps it fast).
  - `export()` → the trace with spans plus `member {name, role, model, language, system_prompt}`; with
    contexts=True every LLM span carries `context {messages, complete}` (each history epoch read once).
  - `prune(days)` deletes finished traces (with spans) older than that and history archived before then,
    keeping the newest epoch marker of every agent.
- `Trace` (the writer of one trace; every method is a no-op when tracing is off, and `llm_start` then runs no
  SQL at all): `start`, `end`, `record`, `note_tasks`, `llm_start(messages, *, purpose, extra, attempt,
  parent)`, `llm_end(span, response, cost)`, `llm_error(span, error, *, kind, retry_in=None, model=None,
  status="error")`, `tool_start(call_id, name, arguments)`, `tool_end(span, result, is_error, shown_chars=None)`,
  `finish(status, outcome)`.
- Text renderers (CLI, console, the tool; UTF-8-safe): `trace_line(t)`, `render_trace(trace, detail_chars=300)`,
  `render_span(span, limit=20000)`, `render_context(ctx, offset=0, limit=30, chars=4000)`.

### 16.1 Runtime integration (swarm.py, agent.py)
- `Swarm.__init__`: `self.tracer = Tracer(self.db, self.bus, enabled=cfg.tracing.enabled,
  max_payload_chars=cfg.tracing.max_payload_chars)`; `session_id` and `session_title` are None until a
  SessionManager sets them.
- `Swarm.start`: first `self.tracer.recover()`, then `self.tracer.prune(retention_days)` if it is > 0. The
  scheduler prunes again every 6 h.
- `Swarm.stop`: wakes the long polls, `self.tracer.close_external()`, then stops the runtimes (each closes its
  LLM client with `aclose()`).
- `Swarm.reconfigure(config: Config)`: hot-apply new settings to a loaded swarm.
  - Keep `project_dir` (data_dir and workspace stay), `self.language` and the agents' frozen system prompts.
  - Replace `self.config`; update the tracer `enabled` / `max_payload_chars`.
  - Replace `llm_semaphore` with a new one if `max_parallel_llm_calls` changed.
  - For each runtime, `runtime.reset_llm()` (the next call builds a client from the new profile); an agent in
    ERROR whose activation was cut short is woken to retry it.
  - Lift a budget pause the new budget allows.
- `AgentRuntime`:
  - `self.trace` is the current `Trace`, or None (`_tr` is a disabled writer outside an activation).
  - `_activate` finishes a trace left open (`interrupted`), begins the trace after a non-empty wake text and
    records `input/wake`. It sets `trace.step = step` at each loop iteration. It records `input/incoming`,
    `input/nudge`, `input/continue`, `event/reply_to_user` (auto-forward), `event/refusal` and
    `event/step_limit`; `event/budget_pause` finishes with `paused`.
  - Finish statuses: `stopped` (the agent stopped or left, or the swarm was paused), `paused` (budget),
    `step_limit`, `refusal`, `error` (the LLM failed after retries), `done` otherwise.
  - Outcome: `"Turn ended (wait)"`, `"Answered without tool calls"`, `"Step limit reached (N steps)."`,
    `"The model declined the request."`, the stop or pause reason, or `"{kind}: {message}"` of the LLM error.
  - A crash inside `_activate` is caught by `_run`: it records `event/crash` and finishes the trace with `error`;
    a stop records `event/stopped` and finishes with `stopped`.
  - `_call_llm(messages, *, purpose="step", extra=None, parent=None)` opens one llm span per attempt:
    success → `llm_end(span, response, cost)` (cost = `swarm.record_usage(...)`); retryable error →
    `llm_error(..., retry_in=delay)`; final error → `llm_error(...)`; cancelled → `llm_error(...,
    kind="cancelled", status="interrupted")`. Failed attempts are named after `_model_id()` (the configured
    model of the cached client; never builds one).
  - `_run_tools` uses `tool_start` / `tool_end` with the full text and `shown_chars` when truncated. A parse
    error becomes a tool span with status `error`.
  - `_compact` opens a `compaction` span, calls `_call_llm(..., purpose="compaction", extra=[request],
    parent=span)` and ends the span with the summary (`_compact_without_llm` ends it with meta `fallback`).
  - `reset_llm()` closes (`aclose`, scheduled as a task when a loop runs) and drops the cached client.
  - `agent.activity` events stay as they are.

### 16.2 Tool `agent_traces` (tools/trace_tools.py, audience user + external)
Arguments: `agent` (member name, retired ones too; optional = all members), `trace_id`, `span_id`, `context`
(bool, with span_id), `status` (a trace status or `"failed"`), `task` (int), `limit` (traces: default 10, max
50; context messages: default 20, max 100), `offset` (first context message, default 0).

Behaviour:
- No ids: a header and one `trace_line` per trace of `list_traces` (newest first).
- `trace_id`: `render_trace(get_trace(id, payload_chars=600))`.
- `span_id`: `render_span(get_span(id))`.
- `span_id` + `context=true`: `render_context(span_context(id), offset, limit)`.
- Unknown ids (or a context of a non-LLM span) → ToolError.

The descriptions (en, ru) say that it shows what agents did: LLM calls (thinking, text, tool calls, tokens,
cost, latency) and tool executions with full arguments and results.

### 16.3 REST (trace_api.py; per swarm, so `/s/{sid}/api/...` in the launcher)
| method path | response |
|---|---|
| GET `/api/traces?agent=&status=&task=&kind=&limit=50&before=` | `{"traces": [summary...]}` (limit 1..500; `before` = trace id for paging) |
| GET `/api/traces/stats?agent=` | `Tracer.stats()` |
| GET `/api/traces/{id}?payload=20000` | trace with spans; strings clipped to `payload` chars (0 = full); 404 `{"error"}` if missing |
| GET `/api/traces/{id}/export?contexts=0\|1` | JSON attachment `trace-{id}-{agent}.json` (`Tracer.export`) |
| GET `/api/spans/{id}` | full span; 404 if missing |
| GET `/api/spans/{id}/context` | `span_context`; 404 for a missing or non-llm span |
| GET `/api/agents/{name}/traces.jsonl` | `application/x-ndjson` attachment `traces-{name}.jsonl`, streamed: the agent's newest 500 traces (oldest first), one export (no contexts) per line |

Attachment names with characters outside `[A-Za-z0-9-_.]` (e.g. Cyrillic member names) are sent as an RFC 5987
`filename*=UTF-8''...` with an ASCII `filename` fallback (`trace-{id}.json`, `traces.jsonl`); downloads are
`Cache-Control: no-store`.

## 17. Sessions and settings

### 17.1 Settings (settings.py, config.py)
- The **home** is `$DREAMTEAM_HOME`, else `~/DreamTeam` (`default_home()`); the CLI flags `--home` / DIR
  override both. Its settings file is `<home>/dreamteam.toml` (`settings_path(home)`) with the sections
  `SECTIONS = swarm, agents, server, shell, workspace, mcp, tracing, models` (same format as a project's
  `dreamteam.toml`).
- `ensure_home(home, language="ru", provider="anthropic")` creates the directory, `sessions/` and the settings
  file from the template; `read_raw(home)`; `validate(home, raw) -> Config` (ValueError with a readable text);
  `dump_toml(data)`; `deep_merge(base, override)`.
- `config.Config.from_dict` checks every field against its type (bool / int / float / str / list[str] /
  tables; integral floats become ints, bools are not numbers, no NaN/inf, counts and sizes >= 1, port <= 65535;
  None = the default) with messages like `[agents] max_steps_per_turn must be ...`; API keys are never echoed.
  `make_profile(name, spec)` builds a `ModelProfile`; `env_var_name(value)` normalizes `api_key_env`
  (`$NAME`, `${NAME}`, `%NAME%` → `NAME`), and a value that cannot be a variable name is treated as a pasted
  key (`api_key`). `toml_string()` writes TOML basic strings (used by `render_template` for name and goal).
- `write_raw(home, raw)`: validates, refuses data that would not read back identically, keeps the previous
  file as `dreamteam.toml.bak`, writes atomically (temp file + replace, never left behind) and makes the
  settings and the backup owner-only on POSIX (they hold API keys).
- `public_view(raw)`:
  - every section with defaults filled in; no `swarm.goal` / `swarm.goal_description`;
  - in model profiles, `api_key` is null and comes with `api_key_set`, `api_key_hint` (`…abcd`) and
    `api_key_env_set` (true/false, or null when there is no env name); `api_key_env` is a variable name or
    null (a key pasted there is never shown);
  - values of `mcp.external.<server>.env` are masked as `SECRET_MASK + hint` (`••••…abcd`).
- `apply_update(raw, update)` (a dict of sections; ValueError for bad input):
  - each section present replaces the stored one (null values dropped); a stored `swarm.goal` stays;
  - masked `mcp.external.*.env` values get their stored value back (same server first, then a renamed one);
    an unknown mask is a ValueError;
  - `models` is the complete set of profiles (missing ones are deleted; names `[A-Za-z0-9_-]+`; at least one);
    per profile, an `api_key` that is null or absent keeps the stored key (of the same profile, or of
    `renamed_from`), `""` removes it, and any other string sets it (whitespace stripped); a key pasted into
    `api_key_env` becomes the `api_key`;
  - renaming the default profile (`renamed_from`) moves `agents.default_model` along (`profile_renames(update)`
    gives `{old: new}`).
- `profile_from_view(raw, data, name=None) -> ModelProfile` (an unsaved UI profile; a missing key falls back to
  the stored key of profile `name`; ValueError for unknown fields or wrong types).
- `async list_models(profile, timeout=20)` (GET /models, free; ValueError with a readable message).
- `async check_profile(profile, *, live=False, timeout=30)` → `{ok, models, models_error, model_found, error,
  reply, latency_ms, cost_usd}`. `live=True` sends one tiny request (max_tokens <= 512; effort `low` only when
  the profile uses effort). Errors and replies pass through `scrub_keys()`: an endpoint echoing a rejected key
  never sends it to the browser.
- `PROVIDER_CHOICES`: presets for the UI (`anthropic`, `openai`, `openrouter`, `ollama`, `vllm`, `lmstudio`,
  `custom`), each `{id, label, provider, base_url, api_key_env | api_key, model, models, ...}`.

### 17.2 Sessions (sessions.py)
`SessionManager(home, *, llm_factory=None, language="ru", provider="anthropic")` (`ensure_home` + the
settings; ValueError when they are invalid):
- Attributes: `home`, `raw` / `config` (the global settings), `swarms` (loaded Swarm objects by sid),
  `running` (the set of started ones), `hub_url` (set by `launcher.serve_launcher`; a session swarm's `hub_url`
  is `f"{hub_url}/s/{sid}/mcp"`), `sessions_dir`.
- Methods: `listen(fn)` / `unlisten(fn)`, `settings_view()`, `async update_settings(update)`, `ids()`,
  `exists(sid)`, `dir_of(sid)`, `meta(sid)`, `config_for(sid)`, `config_view(sid)`, `summary(sid)`, `list()`,
  `default_session()`, `async create(title, goal="", description="", *, overrides=None, start=True)`,
  `async load(sid)`, `async start(sid)`, `async stop(sid, *, remember=False)`, `async unload_idle(sid)`,
  `update(sid, *, title=None, overrides=None)`, `async delete(sid)`, `async resume()`, `async shutdown()`,
  `copy_overrides(sid)`; module functions `read_stats(db_path, conn=None)` and `slugify(title)`.
- `listen` callbacks receive `{"type": "sessions.changed", "session": sid | None, "change", "ts"}`. The change is
  one of `created`, `started`, `stopped`, `updated`, `deleted` or `settings` (session None).
- `SessionError(message, status)` (a ValueError) carries an HTTP status: 400 bad input or invalid settings,
  404 unknown session, 409 conflict (deleting `main`, files in use), 500 damaged or cannot be opened.
- Session ids are `NNN-slug` from the title (Cyrillic and Ukrainian transliterated, accents dropped; e.g.
  `001-snake-game`) and match `^[a-z0-9][a-z0-9-]{0,63}\Z`. `main` is the legacy session: a home that was a
  single-swarm project (`<home>/.dreamteam/swarm.db`) lives in the home itself, uses the global settings
  directly (including a `swarm.goal`), cannot get overrides and cannot be deleted from the UI;
  `sessions/main/` is ignored. Titles are one line, at most 200 chars (default: the goal's first 60 chars,
  else "Session").
- Effective config of a session = the global settings without `swarm.goal` / `goal_description`, deep-merged
  with its `overrides`. Overrides may only touch the sections swarm, agents, shell, workspace, mcp and tracing
  (not models or server); they are validated at create and update time (`update` applies them to a loaded
  swarm at once via `reconfigure`). `config_view(sid)` is the public view of the effective settings (the
  global ones when the overrides are invalid now).
- `create` stores the goal as `pending_goal`; the first `start` sets it once the members exist (a failure
  keeps it). `start` marks `autostart`; an explicit `stop` clears it (`remember=True`, used at shutdown, keeps
  it), and `resume()` starts the marked sessions. `stop` unloads the swarm (stop + close its database).
- `load(sid)` constructs the swarm without starting it (a stopped session is viewable) and closes the traces a
  crashed process left running (`tracer.recover()`). Load, start, stop and delete run under a per-session lock
  and re-check that the session still exists (404).
- `delete(sid)` renames the directory to `sessions/.deleted-<sid>-<ns>` first (a session whose files are open
  stays intact: 409), then removes it in a thread; leftovers are purged at the next delete.
- `update_settings(update)`: `apply_update` + validation; every session must still produce a valid config,
  except damaged sessions and sessions whose overrides were already invalid (skipped); a session override that
  pins a renamed profile follows the rename, one whose profile was deleted falls back to the default (an
  `updated` event); then the file is written and every loaded swarm is reconfigured (a failure is logged per
  session). Returns `{"settings": public view, "applied_to": [sids]}`. Edits of the settings file made by hand
  while the server runs are picked up (mtime/size) before reads and updates; an invalid hand edit is ignored
  with a warning.
- `default_session()`: the running session (the most recently active one when several run), else the only
  session, else None.
- Summary (`list()` sorts running first, then by last activity or creation, newest first; a session that
  cannot be summarized appears as `{"id", "title", "status": "stopped", "error"}`):
  ```json
  {"id","title","legacy","dir","created_at","status":"running|stopped","loaded","autostart","language",
   "model_profile","model","max_cost_usd","max_agents","overrides","config_error","url":"/s/{sid}/",
   "goal":{"title","description","status":"active|done|pending","summary"}|null,
   "tasks":{"total","done","open","by_status"},"agents":{"total","working","llm","external"},
   "spent_usd","paused","pause_reason","last_activity"}
  ```

## 18. Launcher: web, MCP, CLI, UI

### 18.1 Web (launcher.py)
`create_launcher_app(manager, host="127.0.0.1")` returns the app wrapped in the same `GuardMiddleware`
(host/origin, JSON POSTs). A `SessionRouter` in front sends `/s/{sid}/...` to that session's app
(`web.create_app(swarm)`, §14, served with `root_path = /s/{sid}`) and everything else to the launcher routes.

| method path | response |
|---|---|
| GET `/` | `static/home.html` (launcher page, `no-store`) |
| GET `/static/{file}` | static files (`UIStaticFiles`, `no-cache`) |
| GET `/api/hub` | `{"pid","project": str(home),"home","name":"DreamTeam","version","mode":"launcher"}` |
| GET `/api/sessions` | `{"sessions": [summary...]}` |
| POST `/api/sessions` | body `{title?, goal?, description?, overrides?, start?=true}` → `{"session": summary}` (201) |
| GET `/api/sessions/{sid}` | `{"session": summary, "config": manager.config_view(sid)}` |
| PATCH `/api/sessions/{sid}` | body `{title?, overrides?}` → `{"session"}` |
| DELETE `/api/sessions/{sid}?confirm={sid}` | `{"ok": true}`; 400 without a matching confirm |
| POST `/api/sessions/{sid}/start` / `/stop` | `{"session"}`; another action → 404 |
| GET `/api/settings` | `{"settings": public view, "providers": PROVIDER_CHOICES, "path": str(settings file), "home"}` |
| PUT `/api/settings` | body `{"settings": {...sections}}` → `{"settings","applied_to"}` |
| POST `/api/settings/check` | body `{"profile": {...}, "name"?: stored profile name for the key fallback, "live"?: bool}` → `check_profile` result |
| WS `/ws` | pushes `sessions.changed` events (manager.listen) |
| `/mcp` | launcher MCP (§18.2) |
| GET `/s/{sid}` | 307 redirect to `/s/{sid}/` (query kept) |
| GET `/s/{sid}/` | the session's `index.html` (board UI) |
| `/s/{sid}/api/*`, `/s/{sid}/ws`, `/s/{sid}/mcp` | the session app; `manager.load(sid)` loads a stopped session for viewing |

- Errors are `{"error": text}` with SessionError.status; a body that is not JSON, not an object or has fields of
  the wrong type is 400; unexpected failures (e.g. the swarm failed to start, a locked file) are JSON 500s.
- `POST /s/{sid}/api/*` without `Content-Type: application/json` is 415 (the guard only knows `/api/...`).
- An unknown sid: 404 JSON for `/api` and `/mcp` paths, an HTML error page otherwise, WebSocket close 1008. A
  session that cannot be opened: its status as JSON for `/api` and `/mcp`, an HTML page for the board, close
  1011. After shutdown began, no new session app is built (503).
- Session apps are cached per sid and rebuilt when the manager's swarm object changes. Each one's lifespan (its
  MCP `StreamableHTTPSessionManager.run()`) runs in a task of its own, entered when the app is built and exited
  when the session unloads or the server stops (a request cancelled meanwhile stops it too). On stop and on
  start the session's WebSockets are closed with code 1012, so the browsers reconnect and reload the state.
- A **stopped** session (loaded but not running) is read-only:
  - GET routes work;
  - `POST /s/{sid}/api/act/*` and any session `/mcp` request return 409 `{"ok": false, "result": STOPPED_TEXT,
    "error": STOPPED_TEXT}` with `STOPPED_TEXT = "The session is stopped. Start it on the sessions page."`;
  - the session's `/api/state` snapshot has `swarm.session.status = "stopped"`.
- `serve_launcher(manager, host, port, log_level="warning", *, on_started=None, resume=None, on_listening=None)`
  sets `manager.hub_url` (and the loaded swarms' `hub_url`), starts uvicorn, calls `on_listening` as soon as
  the port is open (write hub.json there), then resumes the autostart sessions when `server.resume_sessions` is
  on (or `resume=True`) — only once the port is ours, so a second launcher losing the race never runs them —
  then calls `on_started`. On exit (cancelled, or a callback failed) it stops the web server and
  `manager.shutdown()` (sessions keep their autostart mark).

### 18.2 Launcher MCP (launcher_mcp.py)
`build_launcher_server(manager)`, `LauncherMCPEndpoint(manager, host, extra_hosts=())` (served at `/mcp`) and
`serve_stdio_launcher(manager)` (stdio, with hub_mcp's `_StdinLines`):
- Session tools (`LAUNCHER_TOOLS`): `sessions_list`, `session_create(goal, title?, description?, language?,
  start_agents?, max_agents?, max_cost_usd?, model?, start?=true)` (unset values come from the global settings;
  the board URL is shown only when a web server runs), `session_start(session)` and `session_stop(session)`.
- Every hub tool (§13) with an extra optional `session` property. Without it the call goes to
  `manager.default_session()`; when there is none, the error lists the sessions. An unknown or stopped session
  is an error result (`session_start` is suggested).
- Calls run `hub_mcp.hub_call` on the session's swarm (same actor rules: `as_agent`). A call still running when
  its session stops or is deleted ends with `Error: session X was stopped while <tool> was running.`
- `dispatch_launcher_call(manager, name, args)` answers a tool name nobody knows (close matches + the list);
  `call_launcher_tool` turns SessionError / ToolError / unexpected failures into error results.
- `INSTRUCTIONS` explain sessions, then the hub instructions.
- `/s/{sid}/mcp` keeps the per-session hub MCP (§13).

### 18.3 CLI (cli.py)
```
dreamteam [tui|chat] ["PROMPT"] [-c | -r [SID]] [-p [--output-format text|json|stream-json] [--timeout S]
          [--no-auto-reply]] [--verbose] [--home H] [--lang ru|en] [--model PROFILE] [--agents N]
          [--max-agents N] [--budget USD] [--title T] [--host H] [--port P]
dreamteam serve [DIR | --home H] [--host H] [--port P] [--goal G [--title T]] [--no-console] [--no-browser]
dreamteam sessions [DIR | --home H]
dreamteam traces [AGENT] [--home H] [--session SID] [--id N] [--span N [--context]] [--status S] [--limit N] [--json]
dreamteam console [DIR | --home H | --url URL] [--session SID] [--lang ru|en]
dreamteam mcp [DIR | --home H] [--no-web]
dreamteam status [DIR | --home H]
dreamteam sandbox [DIR | --home H] [--build] [--port P] [--goal G]
```
- Default command (`_default_command`): `dreamteam` alone runs `tui` when stdin and stdout are terminals,
  else `serve`. Options only: `tui` when one of them is a TUI option (`-p -c -r --output-format --model
  --budget --agents --max-agents --verbose --timeout --no-auto-reply --lang`), `serve` when one is serve-only
  (`--no-console --no-browser --goal`) or there is no terminal, else `tui`. A first word that is not a command
  is the TUI prompt (`dreamteam "Сделай змейку"`), except a near-miss of a command name (difflib, 0.75), which
  argparse reports.
- `tui` (docs/TUI.md): `tui.main_tui` (interactive; refuses without a terminal, exit 2) or with `-p`
  `tui.main_print` (text piped into stdin — a pipe or a file, never a terminal or NUL — is appended to the
  prompt; no prompt → exit 2). `-r` without an id opens the session picker (`/sessions`). The flags `--model`,
  `--agents`, `--max-agents`, `--budget`, `--title` become the overrides of a session the TUI creates.
- The directory: `--home`, else DIR, else the current directory when it has `dreamteam.toml`, else the default
  home (`serve` never takes the current directory: `--home`, DIR or the default home). It is a launcher **home**
  when `--home` is given, when it has `sessions/` next to `dreamteam.toml`, or when it is the default home;
  otherwise it is a single-swarm project (§15). `serve`, `mcp` and `sandbox` create a home on demand; the
  listing commands (`sessions`, `status`, `traces`) never create one.
- `hub.json` lives in `<home>/.dreamteam/hub.json`, written once the port listens (before the resume) and
  removed at exit (only by the process that wrote it). `running_hub(dir)` trusts it only when `GET /api/hub`
  confirms the pid and the directory.
- `serve`: refuses when a hub already runs for the home (opening that one in the browser unless
  `--no-browser`) or when the port is busy;
  `--goal` creates and starts a session and opens its board; the terminal console attaches to the running
  session when stdin is a real terminal (on Windows the NUL device is not one). Settings errors and unusable
  homes are messages, not tracebacks.
- `console`: with a launcher hub it works against `{base}/s/{sid}` (`--session`, else the running one); it
  refuses a stopped session and a URL where no hub answers. Commands include `/trace [Name|#id]`,
  `/span N [ctx]` and `/session` (the current session; another one is `dreamteam console --session ID`).
- `mcp`: a running hub (for a home: the launcher) → a stdio proxy to its `/mcp` that hands on its
  instructions; otherwise a home gets the in-process launcher (SessionManager, the web UI when its port is free,
  resumed sessions) with the stdio launcher MCP, and a project the in-process swarm (§13).
- `traces`: with a hub, the session's REST (§16.3; `--session`, else the running one); without one, the
  session database is read directly (default: the most recently active session, printed on stderr; a session
  that never started prints "No traces." without creating its database).
- `status` of a home lists the sessions (from the hub, or read from the home); of a project, `swarm_status`.
- `sandbox` refuses when a hub runs for the directory; the container runs `serve /swarm` for a home and
  `run /swarm` for a project.
- `run [DIR]` stays the single-swarm server of a project (§15); it does not serve the launcher.

### 18.4 UI
- **home.html + home.js + home.css** (launcher page, `/`): header (DreamTeam, tabs «Сессии» / «Настройки»,
  RU/EN, theme, connection state); live updates from `/ws` plus a 5 s poll; texts in ru and en.
  - Sessions tab: a big «Начать новую сессию» button, search and the filters all / running / stopped, and a
    card per session.
    - Card: title (link to the board), goal (or «ждёт запуска» for a pending goal), status badge (running /
      paused / stopped / goal done), task progress bar, agents (working), spent/budget, model, last activity,
      the error of a damaged session.
    - Card buttons: «Открыть», «Запустить» / «Остановить» (disabled while the request runs), delete (not for
      the legacy `main`) with a confirmation dialog naming the session.
  - New-session modal: title; goal (required); description; model profile (from the saved settings);
    language, agents at start, max agents, budget; starting roles (comma list); under «Дополнительно» steps
    per wake-up and MCP approval; «Запустить сразу» with «Создать и открыть» (or «Создать»). Only values that
    differ from the saved defaults are sent as overrides. A stray click or Escape asks before discarding a
    typed goal.
  - Settings tab: sections «Модели и эндпоинты», «Новые сессии по умолчанию», «Агенты», «Консоль»,
    «Workspace», «MCP», «Трейсы», «Сервер», with a section navigation.
    - Model profiles: one card per profile. Fields: name, provider, base_url, key (mode «Переменная
      окружения» with its state on the server, or «Ключ»: a password input that is never filled from the
      server, «сохранён …abcd», a button to remove it), model (input + datalist filled by «Загрузить список»),
      max_tokens, context window, effort, thinking (Anthropic) or the max-tokens parameter (OpenAI), and under
      «Дополнительно» thinking display, prefix-mismatch handling and fallbacks (Anthropic), prices and extra
      request parameters (JSON). Buttons «Проверить» (models list) and «Тестовый запрос» (live); key-like text
      in the results is masked. Default profile (★, cannot be deleted), add from a preset, delete.
    - The external MCP servers are a JSON field; their `env` values come masked (`••••…abcd`) and are kept
      when sent back unchanged.
    - A save bar with «Сохранить» (PUT) and «Отменить изменения»; client-side checks (profile names,
      duplicates, a model is required, JSON fields) and server errors are shown there. Leaving the tab or the
      page with unsaved edits asks first.
    - Models and limits apply to running sessions at once; server settings after a restart of `dreamteam serve`.
- **index.html + app.js + traces.js** (session board, `/s/{sid}/`):
  - All API/WS/MCP URLs are relative to the session base `/s/{sid}` (`BASE`, the same id pattern as the
    server), with `""` when the page is served at `/` (`dreamteam run`).
  - Topbar: «← Все сессии» link to `/` and the session title; a stopped banner with «Запустить сессию»
    (POST `/api/sessions/{sid}/start`, then reload). Actions on a stopped session show the stopped message.
  - Tab «Трейсы»: agent filter, status filter (all statuses, «Только с ошибками» = failed), refresh and
    «Статистика»; the list of traces on the left (newest first, «Показать ещё» pages with `before`) and the
    trace detail on the right.
    - Trace detail: header (status, duration, steps, LLM calls, tools, errors, tokens, cost, tasks, what woke
      the agent, outcome); a waterfall timeline of spans (bars scaled to the trace duration, coloured by kind).
    - Clicking a span expands it: thinking, text, tool calls, arguments and the result (marked when the
      model saw a shortened result, with «Загрузить полностью»); «Контекст запроса» opens
      `/api/spans/{id}/context` in a wide modal.
    - Buttons «Экспорт JSON», «JSON с контекстами» and JSONL of the agent.
    - Live updates come from the `trace.*` WS events (a running trace reloads; opened details stay open).
  - Agent drawer: the button «Трейсы» opens the tab filtered to that agent.
