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
    async def create_session(self, title: str, goal: str, description: str = "", overrides: dict | None = None,
                             start: bool = True) -> dict           # POST /api/sessions -> ["session"]
    async def start_session(self, sid: str) -> dict                # POST /api/sessions/{sid}/start -> ["session"]
    async def stop_session(self, sid: str) -> dict
    async def settings(self) -> dict                               # GET /api/settings (whole body)
    async def update_settings(self, settings: dict) -> dict        # PUT /api/settings {"settings": ...}
    # session level: sid=None means the hub itself is one swarm (mode "swarm")
    def session_base(self, sid: str | None) -> str                 # "" or "/s/{sid}"
    async def state(self, sid) -> dict                             # GET {base}/api/state
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
                    lang: str | None = None, out=sys.stdout, err=sys.stderr, on_session=None) -> int
    # on_session(sid): called with the session the run works in, once it is created or started
```

- The prompt is used as follows:
  - Without `session` / `continue_last`, it creates a session with `prompt` as the goal (title = `title` or
    the first 60 chars) and starts it.
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
  `dreamteam` / `dreamteam serve` (`dreamteam -c` can still continue it).
- A reader of `out` / `err` that goes away (`| head -1`) does not crash the run: the output is dropped and
  the exit code is returned as usual.
- `auto_reply`: every message an agent sends to the user is answered automatically:
  - ru: «Пользователь недоступен (неинтерактивный режим): прими решение сам и продолжай работу.»
  - en: "The user is not available (non-interactive mode): decide yourself and continue."
- Output:
  - `text`: on success, the goal summary is printed to `out` (plain text); on failure, a one-line reason goes
    to `err`. With `verbose`, a compact progress feed goes to `err`: the plain-text rendering of the
    `render.event()` lines at verbosity "normal".
  - `json`: one object on `out` at the end:
    `{"type":"result","status":"done|budget|timeout|error|interrupted","session":id,"goal":{...},
    "summary":str,"tasks":[{id,title,status,assignee}],"cost_usd":float,"duration_s":float,"agents":[names]}`.
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
  - review: `◑ #3 … на ревью у Бориса`;
  - done: `☒ #3 Title — Анна` + `⎿ result (first line)` (verbose: more);
  - failed or cancelled: `✗ #3 …` (red or dim);
  - discussion post: `💬 #3 Анна: text`.
- Goal: `goal.set` renders as a rounded panel titled «Цель» / "Goal". `goal.completed` renders as a green
  rounded panel «✓ Цель достигнута» with the summary as markdown.
- Thinking (verbose only): `✻ Анна думает…` with the text dim italic, max 12 lines.
- `wake` (verbose only): `⏰ Анна проснулась: <first line>`. Nudge and compaction lines appear only in
  verbose. Errors are always shown in red with `⚠`.
- Notifications to the user: `🔔 text`. MCP pending → handled by the approval dialog (plus an info line).
- Hires: `+ Анна Смирнова (Аналитик) присоединилась` (gendered verb not required: use «в команде»).
  Retire: `− Анна покинула рой`.
- Pause/resume renders as `⏸ Рой на паузе (причина)` / `▶ Рой продолжает работу`.
- Quiet shows only messages to/from the user, the goal, errors, approvals and pause/budget lines.
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
def agents_table(ctx, agents: list[dict]) -> RenderableType
def sessions_table(ctx, sessions: list[dict], current: str | None = None) -> RenderableType
def cost_table(ctx, state: dict) -> RenderableType       # per agent tokens in/out, cost; total vs budget
def status_report(ctx, info: dict) -> RenderableType     # version, home, web, session, goal, models, language, agents, tracing
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
                hint: str | None) -> tuple[list, list]   # (left, right) parts
    # left: "? — горячие клавиши" (or the hint / mode); right: "Калькулятор · → Анна · 3 агента · $0.42/$5.00 · claude-opus-5-5"
def to_ansi(renderable, width: int) -> str
def to_plain(renderable, width: int) -> str
```

## 4. The application (app.py, commands.py, input.py, dialogs.py)

```python
async def run_tui(conn: Connection, *, lang: str | None = None, session: str | None = None,
                  continue_last: bool = False, initial_prompt: str | None = None, verbosity: str = "normal",
                  input=None, output=None, printer=None) -> int
    # `printer(renderable)` overrides how output is printed (tests capture to_plain()).
    # `input`/`output` are prompt_toolkit Input/Output (tests: create_pipe_input / DummyOutput).
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
  - without a session: «Опишите цель — DreamTeam соберёт команду и начнёт работу (например: "Сделай игру
    змейка на Python")»;
  - with one: «Напишите сообщение… (@имя — кому, / — команды, ! — консоль)».
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
    start a session with the text as the goal; attach to it.
  - with a session → a message to the current **addressee**.
- `@Имя Фамилия текст` / `@all текст` sends to that member and makes them the sticky addressee. A first
  name alone is enough when unique, as in console.match_addressee.
- The default addressee is the agent who last messaged the user, else the planning task's assignee, else
  `all`. It is shown in the box title.
- `!command` is bash mode. It runs `command` in the session workspace directory when the hub is local
  (`conn.owned` or the hub's home == conn.home) with a 120 s timeout, using the shell from settings
  (`shell.resolve_shell`), and prints `bash_result`. A remote hub gets an error line. The command runs as a
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
| `/new [цель]` | new session (asks for the goal when omitted: the next input is the goal) |
| `/sessions`, `/resume [id]` | session picker (↑↓, Enter = attach and start if stopped, Esc) / attach by id |
| `/goal [текст]` | show the goal panel / set a new goal (`set_goal`; the whole text is the title) |
| `/board` | the board (`board`) |
| `/task N` | task with discussion |
| `/agents` | team table |
| `/to Имя\|all` | set the addressee |
| `/hire Роль \| инструкции` | `hire_agent` |
| `/trace [Имя\|N]`, `/span N [ctx]` | traces list / trace N / span / its context |
| `/files [путь]`, `/cat путь` | workspace listing / file with syntax highlighting |
| `/cost` | spending per agent |
| `/status` | setup and session status |
| `/model [профиль]` | list model profiles (✓ default); with an argument set the default profile in the settings (new sessions and new hires) |
| `/pause` | pause / resume the swarm (toggle, `swarm_control`) |
| `/budget N`, `/max N` | limits |
| `/approve имя`, `/reject имя [почему]` | MCP approval |
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
| Esc | close suggestions or a dialog; else interrupt a running `!command`; else, while agents work: pause the swarm (prints `⏸ …`) |
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
- `agent.status` and `agent.usage` events update the spinner. "Working" means the set of agents with status
  `working`. Elapsed time counts since the set became non-empty. Tokens and cost are summed over
  `agent.usage` since then. The spinner frame advances every 120 ms (refresh_interval); the verb changes
  every ~8 s.
- The state snapshot (`/api/state`) is refreshed at most every 2 s, and after events that change agents,
  limits or the goal. It feeds the status line, `@`/`#` suggestions and the addressee logic.
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
- Shared options: `--home`, `--lang ru|en`, `--model PROFILE`, `--agents N`, `--max-agents N`,
  `--budget USD`, `--title T`, `--host`, `--port`.
- `dreamteam tui` (alias `chat`) is the explicit subcommand with the same options.
- `serve` stays the headless web server; `run` stays the legacy single-swarm mode.
