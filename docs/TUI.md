# DreamTeam TUI — terminal client in the style of Claude Code (contract)

`dreamteam` started in a terminal opens an interactive terminal UI that looks and feels like Claude Code:

- a welcome box;
- the conversation and the swarm's activity scrolling above;
- a rounded input box at the bottom, with the status line under it;
- slash-command and `@agent` suggestions listed under the box;
- an animated spinner while agents work ("✻ Совещаются… (12s · ↑ 4.2k ↓ 310 · $0.12 · esc — пауза)");
- permission dialogs (MCP approval);
- `!` bash mode;
- a non-interactive print mode `dreamteam -p "goal"`.

The TUI is a **client** of the launcher's HTTP/WebSocket API (docs/SPEC.md §14, §16–18). It never touches the
database directly. If no hub runs for the home, it starts the launcher **in its own process** (web UI included)
and talks to it over localhost exactly like to an external hub.

Libraries:

- `prompt_toolkit>=3.0.43`: the input box, key bindings, the live bottom area and printing above it
  (`patch_stdout`).
- `rich>=13.7`: rendering of everything printed above (markdown, tables, syntax, panels). It is rendered to an
  ANSI string at the current terminal width and printed through prompt_toolkit.

Package layout: `src/dreamteam/tui/`.

| module | owner | contents |
|---|---|---|
| `client.py` | client | `HubClient` (async HTTP + WebSocket), `Connection`, `connect()` |
| `printmode.py` | client | `run_print()` — `dreamteam -p` |
| `i18n.py` | render | `t(lang, key, **kw)`, `TEXTS = {"ru": {...}, "en": {...}}` (same keys in both) |
| `theme.py` | render | colours, symbols (unicode + ASCII fallback), `agent_style(name)` |
| `render.py` | render | every renderable, the spinner line, the status line, `to_ansi()`, `to_plain()` |
| `commands.py` | app | slash-command registry and handlers |
| `input.py` | app | suggestions (slash commands, `@agents`, `#tasks`), input modes, history |
| `dialogs.py` | app | session picker, MCP approval dialog, confirm dialog, select-list widget |
| `bash.py` | app | `ShellCommand`: the interruptible `!command` runner |
| `app.py` | app | `TUI` application: layout, key bindings, event loop, view state |
| `__init__.py` | app | `run_tui(...)`, `run_print_mode(...)` entry points used by `cli.py` |

## 1. client.py

```python
class HubError(Exception):
    status: int            # HTTP status (0 = connection error)
    message: str
class HubUnreachable(HubError)     # the hub did not answer at all (status 0); the TUI shows a translated text

class HubClient:
    """Async client of a DreamTeam hub (launcher or single-swarm `dreamteam run`)."""
    def __init__(self, base_url: str, *, timeout: float = 30.0, http=None): ...
    base_url: str
    mode: str                              # "launcher" | "swarm" (after hub_info())
    async def aclose(self) -> None
    async def hub_info(self) -> dict       # GET /api/hub; sets .mode ("launcher" if data["mode"] == "launcher")
    # launcher level (HubError(404/405) on a single-swarm hub)
    async def sessions(self) -> list[dict]                         # GET /api/sessions -> ["sessions"]
    async def session(self, sid: str) -> dict                      # GET /api/sessions/{sid}: {"session", "config"}
    async def create_session(self, title: str, goal: str, description: str = "", overrides: dict | None = None,
                             start: bool = True, workspace: str | None = None) -> dict
        # POST /api/sessions -> ["session"]; `workspace` (an absolute folder, SPEC §17.2): the summary then has
        # "workspace_result" (the hub's "result": mode "created" | "adopted", git, ...)
    async def start_session(self, sid: str) -> dict                # POST /api/sessions/{sid}/start -> ["session"]
    async def stop_session(self, sid: str) -> dict
    async def delete_session(self, sid: str) -> dict               # DELETE /api/sessions/{sid}?confirm={sid}
                                                                   # -> {"ok": true[, "workspace_kept": path]}
    async def set_workspace(self, sid: str, path: str | None) -> dict
        # POST /api/sessions/{sid}/workspace {"path": path} (None: {"default": true}) -> {"session": summary,
        # "result": {"mode", "from", "to", "git", "mcp_moved", "old_kept", "old_missing", "restarted",
        # "start_error"}} (timeout 900 s: a stop, the files, possibly copied across drives, and a start)
    async def settings(self) -> dict                               # GET /api/settings (whole body)
    async def update_settings(self, settings: dict) -> dict        # PUT /api/settings {"settings": ...}
    # session level: sid=None means the hub itself is one swarm (mode "swarm")
    def session_base(self, sid: str | None) -> str                 # "" or "/s/{sid}"
    async def state(self, sid) -> dict                             # GET {base}/api/state
    async def roles(self, sid) -> dict                             # GET {base}/api/roles: {"language", "roles":
                                                                   # [{key, name, description, instructions,
                                                                   # model, builtin, names}], "hidden": [...]}
                                                                   # (the visible catalog; SPEC §14)
    async def act(self, sid, tool: str, args: dict | None = None) -> tuple[bool, str]   # POST {base}/api/act/{tool}
    async def inbox(self, sid, since_message: int = 0, since_notification: int = 0) -> dict
    async def messages(self, sid, with_: str | None = None, limit: int = 50) -> list[dict]
    async def mark_read(self, sid, with_: str | None = None) -> None
    async def task(self, sid, task_id: int) -> dict                # {task, discussion, events}
    async def files(self, sid, path: str = ".") -> dict
    async def events(self, sid, since: int = 0, limit: int = 200, types: list[str] | None = None) -> list[dict]
    async def traces(self, sid, **filters) -> list[dict]           # agent, status, task, limit, before
    async def trace(self, sid, trace_id: int, payload: int = 20000) -> dict
    async def span(self, sid, span_id: int) -> dict
    async def span_context(self, sid, span_id: int) -> dict
    async def trace_stats(self, sid, agent: str | None = None) -> dict
    def ws_url(self, sid) -> str                                   # ws(s)://host/{base}/ws
    def stream(self, sid) -> AsyncIterator[dict]
        # Yields bus events (dicts as sent by the server, trace.* included), forever. It reconnects with
        # backoff (0.5 s .. 10 s). On every (re)connect and loss it yields pseudo events
        # {"type": "_connection", "data": {"state": "connected" | "lost"}}. When the hub answers the handshake
        # with an HTTP error instead (403: e.g. the session was deleted), {"state": "rejected", "status": N}
        # follows the "lost" (once per streak): consumers check over REST whether the session still exists.
        # It stops when the iterator is closed (aclose) or the client is closed. Server close codes 1012
        # (session restarted) / 1001 mean "reconnect now".

@dataclass
class Connection:
    client: HubClient
    base_url: str                 # e.g. http://127.0.0.1:8765
    home: Path
    owned: bool                   # True when this process started the launcher
    manager: "SessionManager | None"   # set when owned
    async def close(self) -> None # closes the client; if owned, stops the in-process web server and the
                                  # sessions (they stay marked for resume)

async def connect(home: Path, *, host: str | None = None, port: int | None = None, start: bool = True,
                  web: bool = True, llm_factory=None) -> Connection
    # 1. A hub already serving `home` (cli.running_hub, called in a thread) -> owned=False.
    # 2. Else, with start=True: SessionManager(home, llm_factory=llm_factory), then launcher.serve_launcher
    #    on host/port. The defaults come from the home settings. hub.json (cli._write_hub_file) is written
    #    from serve_launcher's on_listening, i.e. before the sessions resume, so that a second `dreamteam`
    #    finds this hub instead of resuming the same sessions. When the configured port is busy (or lost in
    #    a race), a hub of this home that shows up within ~2 s is used (owned=False); else a free ephemeral
    #    port on 127.0.0.1. Wait until it started -> owned=True. The server task runs in the current loop.
    # 3. Else raise HubError(0, ...).
    # web=False still runs the HTTP server (the TUI needs it) but binds 127.0.0.1 on an ephemeral port and
    # writes no hub.json.
```

## 2. printmode.py — `dreamteam -p`

```python
async def run_print(conn: Connection, prompt: str, *, session: str | None = None, continue_last: bool = False,
                    title: str | None = None, overrides: dict | None = None, output_format: str = "text",
                    verbose: bool = False, timeout: float | None = None, auto_reply: bool = True,
                    lang: str | None = None, out=sys.stdout, err=sys.stderr, on_session=None,
                    workspace: str | None = None) -> int
    # on_session(sid): called with the session the run works in, once it is created or started
    # workspace: the absolute folder a new session works in (`--workspace`; with --verbose the folder is named on
    # err); a refused folder (e.g. a repository with uncommitted changes) ends the run as an error
```

- The prompt is used as follows:
  - Without `session` / `continue_last`, it creates a session with `prompt` as the goal and starts it. The
    title is `title`, else `""`: the hub splits the goal (first line = title, the rest = description).
  - With `session` (or `continue_last`, which picks the most recently active session), the session is
    started if stopped. The prompt is then sent as a user message to the session's coordinator: the
    assignee of the planning task, else the first LLM agent. When that session has no goal yet, the prompt
    becomes its goal (`set_goal`). A session paused by the user (not by the budget) is resumed: the prompt
    is a new instruction to work on.
- It follows the session's WebSocket stream until it ends:
  - `goal.completed` → exit code 0;
  - `swarm.paused` with reason "budget" → 2;
  - timeout → 3;
  - an unrecoverable error (hub gone and not reconnecting for 60 s, or the session deleted) → 1;
  - Ctrl+C → 130. The session is left as it is (running, if the hub is external).
- When `dreamteam -p` runs the launcher in its own process (`main_print`, `conn.owned`), the session it used
  is stopped explicitly at the end, whatever the outcome: a one-shot run is not resumed by the next
  `dreamteam` / `dreamteam serve` (`dreamteam -c` can still continue it). Stopping the session stops the agents'
  background jobs with it (a dev server started for the goal does not outlive the run).
- A reader of `out` / `err` that goes away (`| head -1`) does not crash the run: the output is dropped and
  the exit code is returned as usual.
- `auto_reply`: every message an agent sends to the user is answered automatically (at most `AUTO_REPLY_MAX` = 50
  answers per run, a safety net against a question / answer ping-pong):
  - ru: «Пользователь недоступен (неинтерактивный режим): прими решение сам и продолжай работу.»
  - en: "The user is not available (non-interactive mode): decide yourself and continue."

  With auto-replies on, a task that waits for the user's review (`task.updated` with status `review` and
  reviewer `user`, also one already in review when the run attaches) is approved (`task_review` approve) once
  per update (approvals do not count against `AUTO_REPLY_MAX`: in a one-agent run every task goes to the user),
  with the comment:
  - ru: «Принято автоматически: пользователь недоступен (неинтерактивный режим), задачу никто не проверял.»
  - en: "Approved automatically: the user is not available (non-interactive mode); nobody reviewed the task."

  `--no-auto-reply` leaves such a task in review: the run then waits until `--timeout` (exit 3), and without a
  timeout until it is interrupted.
- Output:
  - `text`: on success, the goal summary is printed to `out` (plain text); on failure, a one-line reason goes
    to `err`. With `verbose`, a compact progress feed goes to `err`: the plain-text rendering of the
    `render.event()` lines at verbosity "normal".
  - `json`: one object on `out` at the end:
    `{"type":"result","status":"done|budget|timeout|error|interrupted","session":id,"goal":{...},
    "summary":str,"tasks":[{id,title,status,assignee}],"cost_usd":float,"duration_s":float,"agents":[names],
    "jobs":[{id,label,owner,command,origin}]}`. `jobs`: the agents' background jobs still running at the end.
  - `text` against a hub of another process (`conn.owned` false): when background jobs are still running at the
    end, one more line on `err` lists them (`label (owner: command)`, the command clipped to 60 characters):
    - ru: «Ещё работают фоновые процессы агентов: {jobs}. Они остановятся вместе с сессией (остановить раньше можно
      командой /jobs stop jN в dreamteam tui).»
    - en: "Background jobs of the agents are still running: {jobs}. They stop when the session stops (to stop one
      earlier: /jobs stop jN in dreamteam tui)."
  - `stream-json`: every received bus event as one JSON line `{"type": ev.type, "ts": ..., "actor": ...,
    "data": ...}` (trace.* and agent.activity included), then the same `result` object as the last line.

## 3. Rendering (i18n.py, theme.py, render.py)

All render functions are **pure** (input: dicts as returned by the API, plus a `RenderContext`) and return
rich renderables (or `None` when nothing is shown at the current verbosity). `to_ansi(renderable, width)`
renders through `rich.Console(file=StringIO, force_terminal=True, color_system="truecolor", width=width,
legacy_windows=False)`; `to_plain(renderable, width)` renders without styles (tests, `-p` text output).

```python
VERBOSITY = ("quiet", "normal", "verbose")
@dataclass
class RenderContext:
    lang: str = "ru"
    verbosity: str = "normal"
    width: int = 100
    unicode: bool = True          # False -> ASCII symbols (theme.SYMBOLS_ASCII)
    me: str = "user"
```

### 3.1 Style (Claude Code look)

- Activity lines start with `⏺` in the agent's colour (stable per name, `theme.agent_style`), then the agent
  name in bold and the action. Continuation lines are indented with `  ⎿  ` (dim).
- A tool call renders as `⏺ Анна Смирнова · task_claim(task_id=3)`: args as `k=v`, strings quoted and
  shortened, at most ~80 chars, in the normal style (only the name is bold and coloured). The tool result
  renders as `  ⎿  Claimed task #3 …`; "normal" shows the first line (max 1 line), "verbose" up to 20
  lines, then `… +N строк`. Calls and results of the housekeeping tools `wait` / `wait_for_events` show
  only in verbose (errors always). So do those of the tools whose effect has its own line
  (`render.ECHOED_TOOLS`: `send_message`, `task_claim`, `task_create`, `task_complete`, `task_comment`,
  `goal_complete`, `hire_agent`, `retire_agent`): in normal verbosity the message, the task line, the goal
  panel or the team line says it once; a failed call still shows (`⏺ Анна · send_message` + the error).
- An agent's step emits all its calls first and then the results, while other agents interleave, so the
  printed feeds (the TUI, `-p --verbose`) pair them with `ToolPairs`: a call is held until its result and
  both are printed together. A call still running after 1 s is printed alone; its result then gets a
  `⏺ Анна Смирнова · tool` header unless the call line is still the last line printed.
- Messages:
  - an agent's message to the user renders as `⏺ Анна Смирнова → вам` followed by the body rendered as
    markdown (indented 2);
  - an agent-to-agent message renders as `✉ Анна → Борис: text` (dim; shortened in normal, hidden in
    quiet);
  - the user's own sent message renders as `> text` on a grey background (the "you said" echo), with
    `→ Анна Смирнова` dim after it.
- Tasks render todo-style:
  - created: `☐ #3 Title` (dim · goal);
  - claimed or in progress: `◐ #3 Title — Анна`;
  - review: `◑ #3 … на ревью у Бориса`; review by you: `◑ #3 … ждёт вашего ревью` (bold warn); `task_detail` of
    such a task adds the hint «Принять: /approve 3 [комментарий] · вернуть: /reject 3 что исправить»;
  - done: `☒ #3 Title — Анна` + `⎿ result (first line)` (verbose: more);
  - failed or cancelled: `✗ #3 …` (red or dim);
  - discussion post: `💬 #3 Анна: text`.
- Goal: `goal.set` renders as a rounded panel titled «Цель» / "Goal". `goal.completed` renders as a green
  rounded panel «✓ Цель достигнута» with the summary as markdown.
- Thinking (verbose only): `✻ Анна думает…` with the text dim italic, max 12 lines.
- `wake` (verbose only): `⏰ Анна проснулась: <first line>`. Nudge and compaction lines appear only in
  verbose. Errors are always shown in red with `⚠`.
- Notifications to the user: `🔔 text`. MCP pending → handled by the approval dialog (plus an info line). The
  review notices (`u_review_requested`, `u_review_fallback`, `u_review_handed_over`, `u_reviews_waiting`) are one
  warning line, like `u_budget`.
- Hires: `+ Анна Смирнова (Аналитик) присоединилась` (gendered verb not required: use «в команде»).
  Retire: `− Анна покинула рой`.
- Pause/resume renders as `⏸ Рой на паузе (причина)` / `▶ Рой продолжает работу`.
- Wrap-up mode (`swarm.wrap_up`) renders as a warning line `⚠ вы: режим завершения — агенты завершают задачи с тем,
  что есть, без новых вычислений и фоновых процессов` / a dim `· вы: режим завершения выключен`.
- Quiet shows only messages to/from the user, the goal, errors, approvals and pause/budget/wrap-up lines.
- Unicode symbols have ASCII fallbacks (`*`, `|_`, `[ ]`, `[~]`, `[x]`, `x`, `>`), used when the terminal
  encoding cannot encode them.

### 3.2 Functions

```python
def welcome(ctx, *, version: str, home: str, web_url: str | None, session: dict | None) -> RenderableType
    # Rounded box: "✻ Добро пожаловать в DreamTeam!" (bold, accent), blank line,
    # "/help — справка · /sessions — сессии · ? — горячие клавиши", blank line,
    # "home: …", "web:  …" (dim). A session line if attached.
def tips(ctx, has_sessions: bool) -> RenderableType      # "Советы для начала:" numbered list (4 items)
def event(ctx, ev: dict) -> RenderableType | None        # every bus event type from SPEC §9 + trace.* (-> None)
def user_echo(ctx, text: str, to: str | None) -> RenderableType
def agent_message(ctx, msg: dict) -> RenderableType      # msg from /api/messages or message.sent data.message
def goal_panel(ctx, goal: dict | None) -> RenderableType
def board(ctx, tasks: list[dict]) -> RenderableType      # grouped by column, todo symbols, assignee, deps, 💬n
def task_detail(ctx, data: dict) -> RenderableType       # /api/tasks/{id}: header, goal, description, result, discussion
def agents_table(ctx, agents: list[dict]) -> RenderableType   # a member with running background jobs: `⚙N` after
                                                              # its tasks (narrow: after its status)
def jobs_table(ctx, jobs: list[dict]) -> RenderableType       # /jobs: the state's "jobs" (running, then ended in the
    # last hour): Процесс (j3), Агент, Состояние («работает» + how it went to the background / «завершился с кодом
    # 1» / «остановлен вами» / «потерян при сбое»), Время, Команда (+ its processes; the log in verbose); narrow
    # (< 80): no agent / time columns. Footer hint «/jobs jN — вывод процесса · /jobs stop jN — остановить · пауза
    # роя их не останавливает»; none: info «Фоновых процессов нет.»
def job_output(ctx, text: str) -> RenderableType              # /jobs jN: the hub's text (`jobs` tool), its first
                                                              # line bold after ⚙, the rest under ⎿ (200 lines;
                                                              # 1000 in verbose)
def roles_table(ctx, catalog: list[dict]) -> RenderableType   # /roles: role, model (≥ 80 columns), the first
                                                              # sentence of the description; hint line
def role_detail(ctx, role: dict) -> RenderableType            # /roles Роль: panel with the whole description,
                                                              # the role's instructions and model
def sessions_table(ctx, sessions: list[dict], current: str | None = None) -> RenderableType
def cost_table(ctx, state: dict) -> RenderableType       # per agent tokens in/out, cost; total vs budget
def status_report(ctx, info: dict) -> RenderableType     # version, home, web, session, goal, models, language, agents,
                                                         # jobs ({"running": N, "ids": ["j3", "j5"]}: «2 работают
                                                         # (j3, j5)»), tracing
def traces_list(ctx, traces: list[dict]) -> RenderableType
def trace_detail(ctx, trace: dict) -> RenderableType     # header + spans as ⏺/⎿ lines (llm: tokens, cost, stop, calls)
def span_detail(ctx, span: dict) -> RenderableType
def context_view(ctx, context: dict, offset=0, limit=30) -> RenderableType
def files_view(ctx, data: dict) -> RenderableType        # dir listing or syntax-highlighted file (rich Syntax by extension)
def help_view(ctx, commands: list) -> RenderableType     # command table (name, args, description)
def shortcuts_view(ctx) -> RenderableType                # the "?" help: keys in 2 columns
def info(ctx, text: str) -> RenderableType               # dim "  ⎿  text"
def error(ctx, text: str) -> RenderableType              # red "⚠ text"
def bash_result(ctx, command: str, code: int | None, output: str) -> RenderableType
def tool_header(ctx, agent, tool) -> RenderableType      # `⏺ Анна Смирнова · tool` above a result printed apart
class ToolPairs:                                         # the one stateful helper (see §3.1)
    def feed(self, ctx, ev, now=None) -> list[RenderableType] | None   # None: not agent.activity, use event()
    def due(self, ctx, now=None) -> list[RenderableType]               # calls held longer than 1 s
    def next_due(self) -> float | None
    def printed(self, renderable) -> None                              # every renderable the consumer prints
    def reset(self) -> None
def cells(text) -> int; def cut_cells(text, width) -> str   # widths as prompt_toolkit lays them out (wcwidth
                                                            # per code point); used for all bottom-area text
# bottom area (prompt_toolkit formatted text: list[tuple[str, str]])
SPINNER_FRAMES = ("·", "✢", "✳", "✶", "✻", "✽")  # ping-pong like Claude Code
def spinner_verbs(lang) -> list[str]   # ru: Совещаются, Планируют, Размышляют, Колдуют, Мозгуют, Кодят, Шуршат…;
                                       # en: Huddling, Scheming, Pondering, Tinkering, Swarming, Brewing…
def spinner_line(ctx, *, frame: int, verb: str, working: list[str], elapsed: float, tokens_in: int,
                 tokens_out: int, cost: float, paused: bool) -> list[tuple[str, str]]
    # "✻ Совещаются… (12s · ↑ 4.2k ↓ 310 · $0.12 · esc — пауза)  Анна, Борис"
def status_line(ctx, *, session: dict | None, addressee: str | None, agents_total: int, working: int,
                spent: float, budget: float, model: str | None, verbosity: str, connected: bool,
                hint: str | None, jobs: int = 0) -> tuple[list, list]   # (left, right) parts
    # left: "? — горячие клавиши" (or the hint / mode); right: "Калькулятор · → Анна · 3 агента · процессы 2 ·
    # $0.42/$5.00 · claude-opus-5-5" («процессы N» / "jobs N" only while background jobs run; when narrow, the model
    # goes first, then the jobs, then the agents count)
def to_ansi(renderable, width: int) -> str
def to_plain(renderable, width: int) -> str
```

## 4. The application (app.py, commands.py, input.py, dialogs.py)

```python
async def run_tui(conn: Connection, *, lang: str | None = None, session: str | None = None,
                  continue_last: bool = False, initial_prompt: str | None = None, verbosity: str = "normal",
                  input=None, output=None, printer=None, overrides=None, workspace: str | None = None) -> int
    # `printer(renderable)` overrides how output is printed (tests capture to_plain()).
    # `input`/`output` are prompt_toolkit Input/Output (tests: create_pipe_input / DummyOutput).
    # `workspace`: the absolute folder of the first session created here (`--workspace`; the welcome says so,
    # «Новая сессия будет работать в папке …»; a refused creation keeps it for the next try).
```

### 4.1 Screen (bottom area, re-rendered live; everything else printed above with `patch_stdout(raw=True)`)

```
✻ Совещаются… (12s · ↑ 4.2k ↓ 310 · $0.12 · esc — пауза)  Анна, Борис      <- only while agents work / paused
╭─ → Анна Смирнова ─────────────────────────────────────────────────────╮  <- title = addressee (or «новая сессия»)
│ > _                                                                   │  <- multi-line input; placeholder when empty
╰───────────────────────────────────────────────────────────────────────╯
  ? — горячие клавиши                      Калькулятор · 3 агента · $0.42/$5.00   <- status line
  /board      доска задач                                               <- suggestions (when typing / @ #)
  /bash …
```

- The border is accent-coloured. It is pink in bash mode (input starts with `!`) and yellow while a dialog is
  open.
- Placeholder (one line: cut with `…` to the free width of the box):
  - without a session: «Опишите цель (первая строка — заголовок, Alt+Enter — новая строка) — DreamTeam соберёт
    команду и начнёт работу»;
  - with one: «Напишите сообщение… (@имя — кому, / — команды, ! — консоль)»;
  - while `/new` or `/goal` waits for the goal: «Опишите цель: первая строка — заголовок, следующие (Alt+Enter)
    — подробности…».
- Suggestions appear under the status line, at most 8 rows: the matching item plus a description. Up/Down
  moves the selection, Tab completes, Enter runs the selected command. They come from:
  - `/` at the start — commands;
  - `@` — agents plus `all`;
  - `#` — tasks: `#3 Title` and the translated status.
- Submitting or clearing the input (Enter, Ctrl+C) also clears the suggestions: Enter on an empty box does
  nothing, and Up/Down browse the history.

### 4.2 Input semantics

- Plain text:
  - with no current session (or the current one is done and the user confirms) → `/new <text>`: create and
    start a session with the text as the goal; attach to it. The text goes whole to the hub, which splits it:
    its first line becomes the title, the rest the description (the TUI sends the title `""` unless `--title`
    gave one).
  - with a session → a message to the current **addressee**.
- `@Имя Фамилия текст` / `@all текст` sends to that member and makes them the sticky addressee. A first
  name alone is enough when unique, as in console.match_addressee.
- The default addressee is the agent who last messaged the user, else the planning task's assignee, else
  `all`. It is shown in the box title.
- `!command` is bash mode. It runs `command` in the session's folder when the hub is local
  (`conn.owned` or the hub's home == conn.home) with a 120 s timeout, using the shell from settings
  (`shell.resolve_shell`), and prints `bash_result`. The folder is the path of `swarm.workspace` in the state
  snapshot, else of `workspace` in the session summary (wherever `/cd` put it); a folder outside the session
  that does not exist now is an error («Папки сессии … нет — /cd, чтобы выбрать другую»), never a stand-in; the
  session's own folder before its first start falls back to the session directory. A remote hub gets an error
  line. The command runs as a
  task of its own (`bash.ShellCommand`), so other input is not held up: the status line shows «выполняется:
  … · esc — прервать», Esc or Ctrl+C kill its whole process tree, a second `!` meanwhile gets an error,
  and quitting kills it at once (its output is read by a daemon thread nothing waits for).
- The history file is best effort: one that cannot be read or written only costs the history.
- Multi-line input: a line ending with `\` + Enter, Alt+Enter, or Ctrl+J inserts a newline.
- History: Up/Down when no suggestions are shown. It is persisted in `<home>/.dreamteam/tui_history`
  (prompt_toolkit FileHistory).
- Every sent message is echoed with `user_echo`.

### 4.3 Commands (commands.py; descriptions ru/en; `/help` lists them)

| command | action |
|---|---|
| `/help` | commands table + key hints |
| `/new [цель]` | new session (asks for the goal when omitted: the next input is the goal); first line = title, the rest = description |
| `/team [роль*N, …]` | the starting team of the sessions this window creates next (like `--team`; `/team -` back to the settings); without an argument shows it; the settings stay as they are |
| `/sessions`, `/resume [id]` | session picker (↑↓, Enter = attach and start if stopped, Del = delete the selected session, Esc) / attach by id; a running session's status reads paused («пауза») or, in the user's wrap-up mode, «завершается» / "wrapping up" |
| `/delete [id]` | delete a session after a confirmation («Нет» preselected); without an id a picker chooses it |
| `/goal [текст]` | show the goal panel / set a new goal (`set_goal`; first line = title, the rest = description) |
| `/board` | the board (`board`) |
| `/task N` | task with discussion |
| `/agents` | team table |
| `/roles [роль]` | the role catalog (`roles_table`) / one role in full (`role_detail`) |
| `/jobs`, `/jobs jN`, `/jobs stop jN …` | the agents' background jobs (`jobs_table` from the state) / one job's output (the `jobs` tool as the user, `job_output`) / stop jobs and everything they started (`job_stop`, its result printed); `stop` also as `kill` / `стоп`; a job id as `j3`, `3`, `#3` or `%3`; anything else: the usage line |
| `/to Имя\|all` | set the addressee |
| `/hire Роль [\| инструкции]` | `hire_agent`; the instructions are optional for a catalog role (sent as `""`) |
| `/trace [Имя\|N]`, `/span N [ctx]` | traces list / trace N / span / its context |
| `/files [путь]`, `/cat путь` | workspace listing / file with syntax highlighting |
| `/cd [путь\|--default]` | the session folder (path; own / moved here / a project folder; missing) / move the session to another folder or adopt one, after a confirmation (§4.5); without a session: the folder of the next new session (as `--workspace`; `--default` clears it) |
| `/cost` | spending per agent |
| `/status` | setup and session status (with the session folder) |
| `/model [профиль]` | list model profiles (✓ default); with an argument set the default profile in the settings (new sessions and new hires) |
| `/pause` | pause / resume the swarm (toggle, `swarm_control`) |
| `/wrapup [off]` | wrap-up mode on (`on`/`вкл` or nothing) or off (`off`/`выкл`): the agents finish their tasks with what they have, start no new computations, and no new background jobs start (`swarm_control` wrap_up; the `swarm.wrap_up` event prints the change, a no-op prints the tool's answer) |
| `/budget N`, `/max N` | limits |
| `/approve имя`, `/approve N [комментарий]` | MCP approval; accept a task in review (`task_review`) |
| `/reject имя [почему]`, `/reject N что исправить` | MCP rejection; return a task in review (the comment is required; after N it is free text taken as typed, without one pair of quotes around all of it) |
| `/stop` | stop the session (agents stop; resumable) |
| `/web` | open the session board in the browser (`webbrowser.open`) |
| `/settings` | print the model profiles (key state, never the key) and open `/#settings` in the browser |
| `/verbose` | cycle verbosity (same as Shift+Tab) |
| `/lang ru\|en` | UI language (persisted in `<home>/.dreamteam/tui.json`) |
| `/clear` | clear the screen |
| `/exit`, `/quit` | quit |

Unknown command: `⚠ Неизвестная команда /x. /help — список.` with "did you mean" suggestions.

### 4.4 Keys

| key | action |
|---|---|
| Enter | send / run the selected suggestion |
| Alt+Enter, Ctrl+J, `\`+Enter | newline |
| Tab | complete the selected suggestion |
| ↑/↓ | move in suggestions, else history |
| Esc | close suggestions or a dialog; else interrupt a running `!command`; else, while agents work: pause the swarm (prints `⏸ …`). Neither Esc nor `/pause` stops the agents' background jobs (`/jobs stop jN` does) |
| Shift+Tab | cycle verbosity quiet → normal → verbose (shown in the status line) |
| Ctrl+O | toggle verbose |
| Ctrl+C | clear the input; else interrupt a running `!command`; on an empty input "Нажмите Ctrl+C ещё раз для выхода" (2 s), twice → exit |
| Ctrl+D | exit on an empty input |
| Ctrl+L | clear the screen |
| `?` on an empty input | `shortcuts_view` |

### 4.5 Live behaviour

- A session the TUI has just created (`new_session` → `attach(fresh=True)`) is followed from event 0: the
  catch-up replays its journal (the starting team joining, the goal panel, the first tasks and messages),
  so nothing that happened before the stream connected is lost and the goal is printed once.
- After attaching to an existing session:
  - print the goal panel, a one-line board summary (`Задачи: ☐ 3 · ◐ 2 · ☒ 5`) and the last 6 messages between the
    user and agents (the newest messages of everyone merged with the user's inbox, so an old question to the
    user is not lost behind agent-to-agent traffic);
  - then follow `client.stream(sid)`; every event goes through `render.event` at the current verbosity.
  - Attaching is transactional: the start, the event baseline, the state and the history are fetched first;
    only then is the current session left. A failed `/resume` or picker choice keeps the current session.
    Answers for a session the user has left meanwhile (a slow state refresh) are not applied.
  - The event baseline is taken before the history; a message printed as history is not printed again
    when its `message.sent` is replayed (by message id).
- Deleting a session (`/delete`, Del in the picker): the main session of a project home (`legacy`) is refused
  at once, as the hub would. Otherwise a `ConfirmDialog` («Удалить сессию «X»?», what is deleted, that a
  workspace folder outside the session stays, and that a running session's agents are stopped; «Нет»
  preselected), then `client.delete_session(sid)` (`DELETE /api/sessions/{sid}?confirm={sid}`) under a
  `BusyDialog` that takes the keys (nothing typed meanwhile is sent or becomes a goal). The picker, the
  confirmation and the busy notice are one `dialog_flow()`: an MCP approval waits until it ends instead of
  popping up between its steps. The picker comes back with the sessions left (`/delete` without an id closes
  after the deletion). Deleting the current session interrupts a running `!` command first (its folder could
  not be moved away on Windows) and leaves the session quietly: its stream ends while the hub deletes, and
  those connection states are not printed. A 404 means it was deleted meanwhile (success). Other refusals
  (files in use) are printed and keep the session; the hub stopped it before trying, so it is shown stopped,
  and the connection states held back meanwhile are handled then.
- The session folder, `/cd` (launcher sessions; SPEC §17.2 "Workspace folder"). Without a current session,
  `/cd PATH` only chooses the folder of the next session created here (the same as `--workspace`, checked by
  the hub when the session is created; «Новая сессия будет работать в папке …»), `/cd --default` forgets it.
  - `/cd` prints «Папка сессии: <path> (своя папка сессии | своя папка сессии, перенесена сюда | папка
    проекта: агенты коммитят в её текущую ветку)», a red line when that folder does not exist, and the hint
    «/cd <папка> — перенести сессию в другую папку, /cd --default — вернуть в её собственную».
  - `/cd PATH`: quotes around PATH are dropped. When the hub is local, `~` is expanded and a relative path is
    resolved against the TUI's current directory; for a remote hub only an absolute path is accepted («Хаб не на
    этом компьютере: укажите абсолютный путь к папке»). `/cd --default` targets `<session dir>/workspace`. The
    main session of a project home (`legacy`) is refused at once. An existing file is refused at once.
  - The current folder itself: nothing is asked, the hub answers "unchanged" («уже работает в …»).
  - Otherwise a `ConfirmDialog` asks first, its title «Перенести сессию «X» в <path>?» («Вернуть сессию «X» в её
    собственную папку <path>?» for `--default`). Its text is worded from what the TUI sees in the folder
    (`console.cd_preview`, shared with the console; the hub decides):
    - missing / empty: «Папки нет — она будет создана; файлы и история git переедут.» / «Папка пустая: файлы и
      история git переедут.» («Да» preselected). After a folder the session adopted: «…сессия начнёт в ней с
      чистого листа…; ваша папка … останется как есть, переедут только MCP-серверы сессии.» After a folder
      that is gone: «Прежней папки … нет — переносить нечего…».
    - with files: «Папка не пустая: агенты будут работать с её файлами и коммитить в текущую ветку (прежняя папка
      … останется как есть). Продолжить?» («Нет» preselected). `--default` with the session's old files still
      there: «…агенты вернутся к ним…».
    - a remote hub: both cases are explained. A running session adds «агенты остановятся на время переноса и
      продолжат работу». A running `!` command in the folder whose files leave it (a move into an empty or
      missing folder from a folder of the session's own: `console.cd_moves_files`) adds «Выполняющаяся команда !
      будет остановлена»; an adopt, or leaving the user's folder, moves none of the files there, and the command
      goes on.
  - Esc or «Нет»: «Папка сессии не изменилась». «Да»: that `!` command is interrupted first (Windows cannot move
    files in use, and it must not go on writing into the emptied folder), then `client.set_workspace(sid, path,
    expect)` runs under a `BusyDialog` («Переношу сессию…»), all in one `dialog_flow()`. `expect`
    (`console.cd_expect`) is the mode the question announced ("moved" for an empty or missing folder, "adopted"
    for one with files, none for a remote hub): the hub refuses (409) when the folder changed since. The session
    stops and starts again in its new folder: losses of its stream meanwhile are held back (a loss not made good
    when the move ends gets the usual grace, then «нет связи»), so a move prints no connection lines.
  - The result is printed line by line (`console.cd_result_lines`): «Сессия «X» теперь работает в <path>: файлы
    перенесены» (moved) / «…теперь работает в <path>» (nothing of the session's moved) / «…теперь работает с
    файлами папки <path>» (adopted); then the git outcome (history moved, the folder's repository and its
    current branch, a new repository, git off, no git), the MCP servers that moved, «Прежняя папка … осталась на
    месте» (after the user's folder whose repository had the session's MCP servers committed: «…MCP-серверы
    сессии (mcp/…) убраны из неё, удаление закоммичено там (<commit>)», or, when that commit could not be made,
    «…это удаление там не закоммичено (…): закоммитьте его или верните файлы (git checkout -- …)»), «Прежней
    папки … не было…», and in red «Сессия не запустилась снова: …» (`start_error`). A refusal (e.g. 409: a
    repository with uncommitted changes — «Commit or stash them first», a merge in progress, a folder that
    changed since the question) is printed as an error and nothing changes. The new summary replaces the
    session's (status line, `!`, `/status`).
- A session in a folder it adopted is checked again by the hub at every start (SPEC §17.2): when the user left
  uncommitted changes there meanwhile (or a detached HEAD, a merge halfway), the start is refused with the hub's
  409 text, printed as any start error; the user commits or stashes, and the next message starts it.
- A session whose folder outside it does not exist (deleted, an unplugged drive) cannot start (409). Attaching
  to it (`/resume`, the picker, `-r`, `-c`) attaches it stopped instead of failing, with «Сессия «X» не запущена:
  папки … нет. Верните её (например, подключите диск) или перенесите сессию: /cd <папка> или /cd --default»;
  a message to it (which starts it) gets the same text. `/cd` then moves it (nothing to move: «Прежней папки …
  не было…»), and the next message starts it.
- `agent.status` and `agent.usage` events update the spinner. "Working" means the set of agents with status
  `working`. Elapsed time counts since the set became non-empty. Tokens and cost are summed over
  `agent.usage` since then. The spinner frame advances every 120 ms (refresh_interval); the verb changes
  every ~8 s.
- The state snapshot (`/api/state`) is refreshed at most every 2 s, and after events that change agents,
  limits, the goal or background jobs (`job.*`). It feeds the status line, `@`/`#` suggestions and the addressee
  logic.
- Background jobs (SPEC §7): `job.started` / `job.ended` print one dim line each (`⚙ Анна Смирнова · фоновый
  процесс j3: npm run dev`, `⚙ Анна Смирнова · процесс j3 остановлен вами (25m 00s): npm run dev`; hidden in
  quiet). While jobs run the status line says «процессы N» (agents asleep while their jobs work: the session is
  not stalled), and `/status` lists them. Quitting the TUI with an owned launcher stops the sessions and so their
  jobs; with an external hub they go on until their session stops.
- Messages to the user are printed in full, are marked read (`mark_read` with the sender), and make the
  sender the addressee when the user has not chosen one explicitly in the last 60 s.
- MCP approval: on `mcp.*` events with status `pending` (or a notification `u_mcp_pending`), the input area
  is replaced by the approval dialog (dialogs.py). The dialog shows: server name, owner, scope (translated),
  description, command (the forge's public record has none yet: `python mcp/<name>/server.py` plus the
  requirements, and a «/cat mcp/<name>/server.py» hint), tools. Options: «1. Да, разрешить», «2. Нет,
  отклонить (с комментарием)». Esc keeps it pending for later (`/approve`). Several pending servers are
  queued. The dialog pops up by itself, so it waits while the user is typing (the status line says
  «MCP-сервер … ждёт одобрения — отправьте или очистите ввод»), and Enter / digits do not choose during its
  first 0.6 s.
- On connection loss the status line shows «нет связи…» and input stays usable (commands fail with a clear
  error; a hub that does not answer: «Хаб … недоступен»). On reconnect the TUI re-syncs: state, plus the
  journaled events of the gap (messages, notifications, goal, tasks, hires, swarm, MCP). The replay covers
  only those types, so it is no high-water mark: a live event is dropped only when the live stream or the
  replay already handled its id. A stream the hub refuses for a session that no longer exists (404 over
  REST) ends the attachment with «Сессии … больше нет».
- On exit, if `conn.owned`, print «Сессии остановлены — продолжить: dreamteam -c» and close the
  connection.

## 5. CLI wiring (cli.py)

- `dreamteam` with no arguments runs the TUI when stdin and stdout are TTYs, else `serve`.
- `dreamteam "текст"` (a first positional argument that is not a command) runs the TUI with an initial
  prompt.
- `dreamteam -p "текст" [--output-format text|json|stream-json] [--verbose] [--timeout S]
  [--no-auto-reply]` runs print mode.
- `-c/--continue` attaches to the most recently active session.
- `-r/--resume [ID]` opens the picker, or attaches to ID.
- Shared options: `--home`, `--lang ru|en`, `--model PROFILE`, `--agents N`, `--team "ROLE[*N],…"`,
  `--max-agents N`, `--budget USD`, `--title T`, `--workspace DIR`, `--host`, `--port`. `--team` is the starting
  team of the session this run creates (`starting_roles`; `start_agents` = its size unless `--agents` is given).
- `--workspace DIR` is the folder of the session this run creates (the TUI's first new session, or the
  session of `-p`): `~` and relative paths are resolved against the current directory; an empty or missing
  folder becomes its workspace, a folder with files (the user's project) is adopted (SPEC §17.2). With `-c` /
  `-r`, or for a file, it is refused (exit 2): an existing session moves with `/cd`.
- `dreamteam tui` (alias `chat`) is the explicit subcommand with the same options.
- `serve` stays the headless web server; `run` stays the legacy single-swarm mode.
