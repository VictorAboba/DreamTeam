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
  Blocking work (git, big file IO) goes through `asyncio.to_thread`. Shell commands never use the loop's default
  executor (git does, and `asyncio.run` waits for its threads at exit): their spawn, output reader and waiter are
  daemon threads of their own (§7).

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
  roles.py       role catalog: presets, [[roles]] overlay, resolution, starting team (§17.3); imports nothing
                 from the package
  goal_text.py   split_goal, shorten_title: a goal typed as one text -> title + description (§3)
  board.py       Board          — tasks, goal field, discussion, independent review (§4)
  mailbox.py     Mailbox        — direct messages
  workspace.py   Workspace      — shared files, git, locks
  shell.py       run_shell, start_command, RunningCommand, Capture, scrubbed_env (§7)
  netoff.py      sessions without network: isolation, wrap, environment, sandbox_entry (§7.2);
                 netns_helper.py makes the offline commands' network namespace and joins a command to it
  procs.py       ProcessTree    — a command's contained process tree: Windows Job Object, POSIX process group with a
                 lifeline watchdog (§7); standard library only
  jobs.py        JobManager, ShellOutcome — background jobs of the members' shell commands (§7.1)
  triggers.py    TriggerEngine + condition DSL
  llm/__init__.py  create_llm(profile)
  llm/base.py      ToolCall, Usage, LLMResponse, LLMError, LLMClient, estimate_tokens
  llm/anthropic_llm.py  AnthropicLLM
  llm/openai_llm.py     OpenAILLM
  llm/scripted.py       ScriptedLLM (tests / demos)
  mcp_forge.py   MCPForge, MCPConnection — agent-built MCP servers + library
  skills.py      skill folders (SKILL.md), their sources (folder, zip, Git, Claude Code) and the shared library (§19)
  swarm_skills.py SessionSkills — the skills attached to one session, the agents' mirror, the wake-up index,
                 members' proposals and the user's library decision, the roles' skills (§19)
  skill_deps.py  skills' script dependencies: manifest detection, EnvManager — environments shared by all sessions
                 under ~/.dreamteam/skill-envs, builds, OS locks, Node views, cleanup (§19)
  tools/__init__.py  registry: ToolSpec, ToolContext, tool(), tools_for(), schemas_for(), call_tool()
  tools/board_tools.py comm_tools.py file_tools.py job_tools.py swarm_tools.py trigger_tools.py mcp_tools.py
  tools/skill_tools.py the skills tools: the user's `skills`, `skill_attach`, `skill_detach`, `skill_approve`,
                 `skill_reinstall`, and the members' `skill_propose` (frozen revision 3; §19)
  tools/frozen.py    frozen LLM tool sets: revisions, the kv record of each agent, `python -m dreamteam.tools.frozen`
                     (§6.1)
  tools/snapshots/   llm_r<N>.json: every released LLM tool set (revision N, en and ru), never edited
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
  static/        index.html app.js traces.js style.css traces.css (board), home.html home.js home.css (launcher),
                 markdown.js and icons.css (both); no build step
  console.py     terminal chat client (talks to the REST API)
  tui/           terminal UI client (docs/TUI.md)
  docker.py      sandbox: build & run DreamTeam in Docker
  cli.py         `dreamteam` command
tests/  conftest.py (FakeSwarm fixture) + test_*.py
```

A **home** (launcher, §17–18; `~/DreamTeam` by default) on disk:
```
<home>/dreamteam.toml               global settings (§17.1); dreamteam.toml.bak = the previous version
<home>/sessions/<id>/session.json   {"title", "created_at", "overrides", "autostart", "pending_goal"?,
                                     "workspace"?: {"path", "adopted", "in_home"?}, "workspace_notice"?} (§17.2)
<home>/sessions/<id>/workspace/     the session's shared files (unless "workspace" names another folder)
<home>/sessions/<id>/.dreamteam/workspace-move.json  only while a workspace move runs, or after one whose
                                    undo failed (§17.2 "Workspace folder")
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
<project>/workspace/.scratch/<slug>/  each member's temporary files, never committed (§7 "Scratch folder")
<project>/workspace/.scratch/<slug>/jobs/  j<N>.log (and j<N>.prev.log): the output of the member's background
                                jobs (§7.1)
<project>/.dreamteam/swarm.db   sqlite
<project>/.dreamteam/jobs/      job logs when `.scratch` is not a plain folder of the workspace (§7.1)
<project>/.dreamteam/hub.json   {"base_url": "...", "mcp_url": "...", "pid": 123, "project": "<abs path>"}
                                written once the server listens; other commands trust it only if GET /api/hub
                                confirms the same pid and project
<project>/.dreamteam/logs/      dreamteam.log, mcp-<name>.log
```

## 2. Data model (db.py SCHEMA is authoritative)

* `tasks`: id, title, **goal** (why the task exists / success criterion), description, status,
  assignee, reviewer, created_by, parent_id, depends_on (json int list), priority (0 low,1 normal,
  2 high,3 critical), tags (json str list), result, created_at, updated_at. A task in `review` keeps its
  executor as `assignee` (NULL only when the executor left the swarm, §4.1); there is no
  separate executor column.
* `comments`: the per-task **discussion**: id, task_id, author, text, reply_to (comment id | null), ts.
* `agents`: name, kind (`llm`|`external`), role (a catalog role by its canonical name in the member's language,
  §17.3), instructions (the hirer's own; a catalog role's instructions are only in the system prompt), model
  (profile name), language, status (`idle`|`working`|`paused`|`error`|`retired`), hired_by, created_at,
  last_active, cost_usd, tokens_in, tokens_out, notes, system_prompt (frozen at creation), context_tokens.
* `history`: agent, seq, msg (json neutral message, §6.1) — append-only per agent except compaction.
* `messages` + `inbox(message_id, recipient, read_at)`.
* `notifications`: id, ts, recipient, kind (prompt key), text (rendered), data (json), delivered_at.
* `triggers`, `mcp_servers`, `file_locks`, `kv`, `events`.
* `jobs` (background jobs, §7.1; index `jobs_owner(owner, status)`): id (AUTOINCREMENT: `j<id>`, unique for the
  session's life), owner, command, shell (display name), origin (`timeout`|`background`|`detached`), status
  (`running`|`ended`|`stopped`|`lost`), reason (stopped: `owner`|`user`|`lifetime`|`retired`|`session`; lost:
  `crash`), stopped_by (the owner's name or `user` for reason owner/user), exit_code (the shell's own code once it
  ended by itself, also while processes it started run on; NULL when the shell was killed), log (workspace-relative
  posix path, or absolute for the data_dir fallback), pid, pgid (POSIX), token (the random
  `DREAMTEAM_PROCESS_TOKEN`), hub_pid (the process that ran it), processes (json list of names), tail (the last
  2000 characters of the output, at the end), started_at, background_at, ended_at. `CREATE TABLE IF NOT EXISTS`:
  an older database gets it at its next open.
* `traces`, `spans`, `history_archive(agent, epoch, seq, msg, archived_at)`: tracing.py SCHEMA (§16).
* kv keys: `goal` → `{"title","description","status":"active"|"done","summary","set_by","updated_at"}`,
  `limits` → `{"max_cost_usd": float, "max_agents": int}` (overrides config), `paused` → `{"paused": bool, "reason": str}`,
  `tools:<agent name>` → `{"rev": int, "language": "en"|"ru", "tools": [{"name","description","parameters"}]}` (the
  agent's frozen tool set, §6.1; kept for retired agents, traces read it), `wait_progress:<actor>` → the last events
  id whose board progress `wait_for_events(progress=true)` returned to that actor (§12).

## 3. Swarm (swarm.py) — what other modules may use

```python
class Swarm:
    config: Config; db: Database; bus: EventBus
    board: Board; mail: Mailbox; workspace: Workspace; triggers: TriggerEngine; forge: MCPForge
    runtimes: dict[str, AgentRuntime]        # running LLM agents
    language: str                            # config.swarm.language
    llm_semaphore: asyncio.Semaphore
    tracer: Tracer                           # §16
    jobs: JobManager                         # background jobs of the members' shell commands (§7.1)
    skills: SessionSkills                    # the skills attached to this session and their mirror (§19)
    session_id: str | None; session_title: str | None   # set by SessionManager (§17.2)
    hub_url: str | None                      # MCP URL of this swarm while a web server runs (snapshot mcp_url)
    stopping: bool                           # set by stop()/close(): long polls (wait_for_events) end at once

    def __init__(self, project_dir, config: Config | None = None,
                 llm_factory: Callable[[ModelProfile, str], LLMClient] | None = None): ...
        # a failure after the database was opened (a corrupt swarm.db...) closes it before re-raising
    async def start(self, run_agents: bool = True) -> None
        # tracer.recover(), then jobs.recover() (rows a previous process left running become `lost`, §7.1 "Crash
        # recovery"), and the trace prune first (§16.1); the very first start creates the starting team:
        # roles.starting_team(swarm.starting_roles, swarm.start_agents), member i gets starting_roles[i], the rest
        # (and blank entries) are generalists (§17.3); LLM agents with open tasks, reviews waiting for them or
        # pending events (goal active), or whose last activation was cut short (the stored history ends
        # mid-activation), get `n_restart`; hire() respects run_agents. After forge.start(): skills.start() (the
        # session's skills read again, their script dependencies planned in a daemon thread and held in use, the
        # agents' mirror rebuilt from the session copies with its node_modules links, the owners of decisions made
        # while stopped told, §19), then skills.ensure_deps() as a `_background` task (builds never waited for);
        # after the starting team is created: skills.ensure_role_skills(announce=False)
        # (the skills the members' roles list; nobody is told: every first wake-up after a start shows the index).
        # The scheduler calls skills.check() (the mirror, at most once a minute) and skills.touch_envs() (hourly)
    async def stop(self) -> None
        # wakes every wait_signal() waiter (stopping=True), closes external trace sessions (a failure is logged:
        # tracing never keeps a session from stopping), stops the runtimes (an LLM agent's foreground command dies
        # with its cancellation), then jobs.stop_all() (every job and every foreground command, e.g. an external
        # member's call, is killed; every finish and call in flight has returned and every job log and handle is
        # closed, §7.1), then commits every member's held new files (workspace.commit_held(None, "when the session
        # stopped"), after a settle() when there are any; §7 "Held new files"), then forge.stop(), then waits for
        # git commands still running in threads
        # (workspace.settle(), §7): nothing touches git or the database afterwards. stop_all, forge.stop and
        # settle run in `finally`: also when an earlier step failed or the stop was cancelled (the caller closes
        # and drops the session after a failed stop, so no process may outlive it); then skills.stop_deps() (no
        # more build news, the view tasks cancelled, the session's in-use set released; builds go on in the
        # process); in an adopted folder (§17.2) skills.remove_mirror() last: no copy of a skill stays in the user's
        # project
    def close(self) -> None                              # closes the database
    def reconfigure(self, config: Config) -> None        # hot-apply new settings (§16.1)
        # when the roles' skills changed (swarm_skills.role_skill_pairs) under a running swarm (started, not
        # stopping): skills.ensure_role_skills() in a background task (`_role_skills_task`, in `_background`: stop()
        # cancels it; a newer change while it runs makes it run once more, never cancels it: its copy thread
        # would go on past the lock); a save that does not touch them starts nothing

    # members
    def member(self, name: str) -> dict | None          # agents row as dict (any status), exact canonical name
    def members(self, include_retired: bool = False) -> list[dict]
    def is_member(self, name: str) -> bool              # exists and not retired (llm or external)
    def agent_count(self) -> int                        # active LLM members: what max_agents limits (external
                                                        # members, MCP clients that joined, neither count nor are
                                                        # limited)
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
    def waits_for_job(self, name: str) -> bool
        # an AI agent asleep (runtime state idle) while a command of its own runs on as a job it waits for
        # (JobManager.busy) and whose end will wake it (JobManager.awaited): a member's broadcast reaches it without a
        # wake (§5 "Quiet broadcasts")
    def defer_wake(self, name: str) -> None              # such news reached `name` unwoken (_wake_deferred, below)
    def render_wake(self, name: str, incoming: bool = False, stamps: bool = False,
                    since: float | None = None) -> str | None
        # drains (marks read/delivered) and renders the wake-up text (§6.3); None if nothing pending
        # and incoming=True; stamps/since: only wait_for_events (§12), never an agent's wake-up
    def notes_shown(self, name: str, notes: str) -> None   # the agent's context now holds these notes
    def skills_shown(self, name: str, value: bool | None) -> None
        # what the agent's context holds of the Skills index (§6.3): True the full index, False "none attached",
        # None nothing known (the next wake-up shows the full index); in memory, like notes_shown

    # goal
    def goal(self) -> dict | None
    def set_goal(self, actor: str, title: str, description: str = "") -> dict
        # every goal comes through here (the set_goal tool, a session's pending goal, swarm.goal of
        # dreamteam.toml, `run --goal`): title/description pass through goal_text.split_goal (the first
        # non-empty line is the title, at most 200 chars; the other lines open the description, followed by
        # `description`; an empty title takes the text from the description), and the goal dict stores the split
        # values, so the goal line of every wake-up is one line; empty title -> ToolError("The goal title is
        # empty."); an empty board gets a planning task (PLANNING_TASK, body = its text + title + description);
        # agents are kicked off staggered: `n_kickoff` {by, title, description, team, task_id, task_title}, with
        # team = "Name — Role; ..." of all members, or `n_goal_updated` when the board was not empty.
        # PLANNING_TASK's steps: 1 look around (only as much as splitting the work needs: research, experiments,
        # reproducing a problem and environment checks are separate tasks for the others, the team waits for the
        # planner's tasks meanwhile; `hint_planner` reminds a planner that has created none); 2 independent work at once (work that needs no contracts:
        # literature and theory, the environment and tools, data, exploratory experiments, becomes tasks before
        # CONTRACTS.md, so the members waiting for the plan start now; only the planner creates the plan's tasks);
        # 3 then the contracts; 4 then the rest of the tasks; 5 no false dependencies (a final task: report,
        # summary, documentation, release, depends only on the tasks it cannot be written without; exploratory and
        # optional tasks do not block it); 6 tests by someone else; 7 reviews; 8 team; 9 finish quickly. n_kickoff
        # tells the others that the first tasks come before the plan is complete
    def complete_goal(self, actor: str, summary: str) -> dict
        # ToolError when there is no goal, it is already done, or the summary is empty; notifies USER u_goal_done;
        # background jobs keep running (a dev server may be for the user): when some run, USER also gets the quiet
        # u_jobs_running {jobs: "j3 (Anna Smith: npm run dev, 25 min); ..."}, and their later ends wake nobody
    def reopen_goal(self, actor: str, comment: str = "") -> dict
        # only a done goal (else ToolError); notifies ONE LLM member n_goal_reopened: the one who completed
        # the goal, else the most recently active (the others are offered new tasks by the scheduler).
        # set_goal and reopen_goal both end wrap-up mode (end_wrap_up, below)

    # team
    async def hire(self, by: str, role: str, instructions: str = "", model: str | None = None,
                   name: str | None = None, language: str | None = None, kind: str = "llm") -> str
        # the role is resolved in the role catalog (roles.find_role, §17.3) in the member's language: a catalog
        # role is stored by its canonical name ("tester" -> "Tester" / «Тестировщик») and brings its description,
        # instructions and model profile; any other name is a new role that only `instructions` describe.
        # Model profile: explicit `model` (unknown -> ToolError) > the role's `model` (a profile missing from
        # the settings logs a warning and falls back) > agents.default_model. The frozen system prompt
        # (LLM members only) is prompts.system_prompt(language, name, swarm, role, role_description,
        # instructions, shell, scratch, max_agents, foreground_max): role_description = the catalog description,
        # instructions = the catalog's then the hirer's (blank line between), scratch = workspace.scratch_rel(name)
        # (§7), foreground_max = [shell] foreground_max (§7.1). The agents row keeps only the hirer's instructions.
        # u_member_joined (quiet, when `by` is not USER) and n_hired carry the canonical role. The tool's guard (a
        # new role needs instructions) is in `hire_agent` (§12), not here: the starting team and programmatic hires
        # need none. An LLM hire beyond max_agents (agent_count()) is a ToolError: "The swarm already has the
        # maximum of N AI agents (members who joined through MCP do not count). Retire someone or ask the user to
        # raise max_agents."; external members are not limited. After the member is created and before n_hired:
        # skills.ensure_role_skills([name]) (its role's skills, §19; the other members are told, not the new one,
        # whose first wake-up lists them), the report kept for `hire_agent` (skills.note_hire / hire_report)
    async def retire(self, by: str, name: str, reason: str = "") -> list[int]   # returns released task ids
        # before the first await: the ids of its running jobs, and jobs.forbid(name) (none of its commands may
        # become a job while it leaves); then tasks and locks released, triggers deactivated, status retired,
        # `agent.retired` {name, reason, released, jobs}, n_member_retired; then its runtime stops; last
        # jobs.stop_owner(name, "retired"): its jobs and its foreground commands (an external member's call in
        # flight) are killed and awaited, committed under its name, with no notice to it; then its held new files
        # (workspace.commit_held(name, "when <name> left the team", leaving=True): it is no suspect any more, §7
        # "Held new files")

    # budget / control
    def limits(self) -> dict                             # {"max_cost_usd", "max_agents"}
    def set_limits(self, actor: str, max_cost_usd: float | None = None, max_agents: int | None = None) -> dict
    def spent_usd(self) -> float
    def record_usage(self, agent: str, usage: Usage, profile: ModelProfile, model: str | None = None) -> float
    def budget_exceeded(self) -> bool
    paused: bool; pause_reason: str
    def pause(self, actor: str, reason: str) -> None
    def resume(self, actor: str) -> None
        # n_resumed to LLM agents with open tasks, reviews waiting for them, pending events or an activation the
        # pause cut short (runtime.interrupted); a reply owed to the user by that activation is still forwarded
    wrapping_up: bool                                    # the user's wrap-up mode (kv "wrap_up" {"on", "by", "since"})
    def wrap_up(self, actor: str) -> bool
        # wrap-up mode on (False when it was on already): the team finishes with what it has. Emits `swarm.wrap_up`
        # {on: true} and notifies every LLM member once, waking it: `n_wrap_up` (start no new computations or
        # background jobs, not even one more quick pass; task_complete with the result so far and what is left; do
        # the reviews waiting for you; stop the jobs nobody needs; when every task is finished the goal is completed
        # with what was achieved and what is left, without new tasks). While on: every wake-up carries
        # `wrap_up_line` (§6.3), no command of a member becomes a new job (§7.1 "Wrap-up mode"), `_check_all_done`
        # sends `n_all_done_wrap_up` (goal_complete, no new tasks) instead of `n_all_done`. Nothing is paused; the
        # user's own actions are not restricted
    def end_wrap_up(self, actor: str) -> bool
        # off (False when it was not on): `swarm.wrap_up` {on: false} and a quiet `n_wrap_up_end` to every LLM member
        # (shown at its next wake-up); called by the user (swarm_control wrap_up=false), set_goal and reopen_goal
    def make_llm(self, profile_name: str | None, agent: str) -> LLMClient
        # a member's profile (agents.model) that no longer exists in the settings (renamed or deleted) falls back
        # to the default profile with a warning, instead of crashing that agent at every activation
    def snapshot(self) -> dict                           # §14.1
    def workspace_info(self) -> dict                     # {"path", "default", "adopted", "exists"} (§14.1, §17.2)
```

Tests use `tests/conftest.py::FakeSwarm`, which implements the subset: `config, db, bus, language,
member, members, is_member, kind_of, resolve_member, language_of, notify, signal, has_pending,
board, mail, workspace, triggers, goal, set_goal, complete_goal` (attributes may be None until a test
wires them), and records notifications in `fake.notified: list[tuple[recipient, key, kw]]`. Its
resolve_member is the real `resolve_member_in`; set_goal / complete_goal validate, split the goal and notify
like the real ones (the planning task is created when a real Board is wired).

Scheduler (every 3 s): expires file locks, compares the skills mirror with its manifest (`skills.check()`, at most
every `SKILLS_CHECK_SECONDS` = 60 s, §19), prunes old traces (every 6 h) and closes idle external trace
sessions (§16); unless paused, `_wake_deferred` first (an agent a broadcast reached without a wake, §5, that no longer
`waits_for_job` while the news is still unread is woken once: e.g. the busy window of its server ended), then in this
order. "Idle" (`_idle_llm_members`) is an LLM agent with status idle, a
runtime, nothing pending and not busy with a job (`JobManager.busy`, §7.1 "Liveness"):
1. `_nudge_idle`: offers ready unassigned tasks to idle LLM agents (`n_ready`), never more agents considering
   offers than there are ready tasks, and the kickoff counts as everyone's offer of the planning task; with
   independent review on, a member with reviews waiting for it is not offered more work (it is reminded of them
   instead);
2. `_wake_stalled`: wakes once (`n_stalled`) an idle agent whose activation ended with plain text while holding
   in_progress tasks even after the nudge (§6.2);
3. `_wake_idle_holders`: an idle agent holding in_progress tasks (e.g. after `wait` with nothing that will wake
   it) is woken once per such set of tasks with `n_stalled`, once it has been idle with that set for
   `agents.idle_nudge_seconds` (the clock starts when it was first seen idle with that set, or at its
   `last_active` when that is later: an activation in between restarts it); the woken sets are shared with
   `_wake_stalled`, so a set is never woken about twice. Neither 2 nor 3 wakes an agent while the end of one of
   its running jobs will wake it (`JobManager.awaited`, what its `wait` result promised): it waits for that job
   (a `detached` one, or a `background` one past its busy window), it has not stalled;
4. `hint_planner()` (planning hint; run 008-013: a planner researched for 40 minutes before creating any task while
   the team idled): an LLM member holding a task tagged `planning` in `in_progress` that has created no task (no
   task with `created_by` = it and an id above the planning task's) gets `n_planning_hint` {id, steps, elapsed} once
   it has made `agents.planning_hint_steps` (default 8) LLM steps (`AgentRuntime.llm_steps`) or
   `agents.planning_hint_minutes` (default 10) have passed since the swarm first saw it holding the task (a claim,
   another holder, or a restart starts the count again; a pause stops the clock: `resume` moves its start on by the
   pause's length); once more at twice either value, and only after at least one step since the first hint; never a
   third time for that member and task (the hints it already had are counted from the notifications table when the
   count starts, so neither a re-claim nor a restart brings more), never with fewer than two members, never while
   the goal is not active, the swarm is paused or in wrap-up mode (`wrapping_up`); 0 turns a trigger off. The agent
   also calls `swarm.hint_planner(name)` after the tools of each step (§6.2), so the step trigger lands in that
   activation's `incoming`; the scheduler pass catches the time trigger
   and a holder that went idle (the notification wakes it). No hard limit: the planner may go on researching;
   `_planning_watch` (in memory) holds the counts and forgets tasks nobody holds;
5. `_remind_reviewers` (independent review only): review reminder and hand-over (§4.1 "Liveness");
6. `_check_all_done`: sends `n_all_done` to one idle agent when every task is finished but the goal is open
   (`review` is an open status, so it waits for every review); not while any LLM member works or is busy with a
   job (with no open task only a `timeout` job can make it busy: its command may still change the result);
7. `_remind_user_reviews` (both modes): `u_reviews_waiting` when only the user's reviews keep the swarm going
   (§4.1 "Liveness").

`reconfigure()` applies new settings to a running swarm, lifts a budget pause the new budget allows and retries
agents in ERROR; a changed role catalog (§17.3) and `[swarm] independent_review` apply at once (new hires use the
new catalog; existing members keep their frozen prompts).

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
    def release_all(self, name: str, actor: str) -> list[int]   # name leaves; see §4.1
        # independent review off: open tasks of name -> todo, assignee None (their ids are returned); open tasks
        # name was to review lose the reviewer, and one in `review` goes back to in_progress (todo without
        # assignee) with `n_reviewer_left` {reviewer} to its assignee
    def counts(self) -> dict[str, int]                 # per status

    # independent review (below)
    independent_review: bool                           # property: [swarm] independent_review, read on every call
    def reviews_of(self, name: str) -> list[Task]      # status review and reviewer == name; priority desc, id asc
    def pick_reviewer(self, task: Task, exclude: Iterable[str] = ()) -> str   # a member name or USER
    def hand_over_review(self, task_id: int, exclude: Iterable[str]) -> Task | None
        # the reviewer did not review: the next pick (excluding `exclude` and the current reviewer), else USER;
        # None when the task is no longer in review
```
`Task.short()` (board listings, wake-ups) shows `review:<reviewer>` after `@assignee` while the task is in review.

Rules (USER may do anything; "member" = agent of any kind):
* `create`: title non-empty (<= 200 chars). `assignee`/`reviewer` resolved via `swarm.resolve_member`
  (USER allowed). `depends_on` ids must exist, no self/cycles. `parent_id` must exist. priority 0..3.
  Status `todo`. Emits `task.created`. If assignee given and != actor: notify assignee `n_assigned`.
* `claim`: status must be `todo` (if already assigned to actor and in_progress/waiting: return as is).
  assignee must be None or actor (else ToolError naming the holder). No blockers (ToolError lists them).
  Sets assignee=actor, status=in_progress. Atomic (DB transaction re-reads the row).
* `update` permissions for members: title/goal/description/priority/tags/reviewer/parent_id —
  creator or assignee. depends_on — the creator and the current assignee (any member kind); for the assignee a new
  order of the same ids is no change. Anyone else: ToolError `Only the creator (X) or the assignee (Y) of task #N
  can change its depends_on (the assignee may remove dependencies the task can do without). Suggest the change in
  the discussion.` When an assignee who did not create the task removes dependencies (done ones too: harmless), the
  discussion gets a record posted under its name
  (`post_deps_removed` {by, deps: "#3 [in_progress], #5 [todo]"} in the swarm language; `task.comment` is emitted,
  but no participant is notified) and the creator a quiet `n_deps_removed` {by, id, title, deps} (the assignee is
  told in the tool result to post why, and that post wakes the creator, a participant). A task whose last blocker is
  removed (by anyone) and that is open: its assignee gets `n_unblocked_removed` {id, title, by, deps} (if != actor;
  it wakes like `n_unblocked`, but says the removed blockers are not done, so their results do not exist); an
  unassigned one becomes ready (the scheduler offers it). A blocked-task ToolError for the current assignee (claim,
  status `in_progress`; not one becoming the assignee in the same call) adds `If the task can be done without some of them, remove those from its depends_on (task_update)
  and say why in its discussion.` assignee — anyone if task is unassigned; otherwise current assignee or creator.
  Reassignment of an in_progress/waiting task resets status to `todo`. Notify new assignee `n_assigned`
  and removed assignee `n_unassigned` (never notify the actor themself).
  status transitions for members: assignee may set `in_progress` (only without blockers), `waiting`,
  `todo` (= release: assignee cleared), `failed`; creator or assignee may set `cancelled`; creator may
  reopen a terminal task to `todo`. `done`/`review` only via `complete`/`review` (USER may set `done`; on a
  task in review that is an approval: the assignee gets `n_review_approved` with comment "").
  The review fields (`assignee` and `reviewer` of a task in review, a reviewer equal to the assignee) follow
  §4.1 below.
* `complete`: actor must be the assignee (or USER); status must be open and not `review`.
  result non-empty. With independent review on, an agent's completion always goes to `review` (reviewer
  chosen below). Otherwise (switch off, or USER completes): if reviewer set and reviewer != actor → status
  `review`, else `done`. A task entering review notifies its reviewer: `n_review_requested` {by, id, title},
  or USER `u_review_requested` / `u_review_fallback` (below).
* `review`: actor is reviewer (or USER), status `review`. approve → `done`, the assignee gets
  `n_review_approved`, which wakes it (`task_complete` told it that it will be told the verdict, so an
  executor that ended its turn with `wait` waits for exactly this); reject → `in_progress` (or `todo` when the task has no assignee), the
  assignee gets `n_review_rejected` (wakes it: it must fix the work). A non-empty comment is validated first and
  also posted to the discussion (the assignee is skipped there: the notification carries it). When someone
  other than the reviewer settles it (USER), the reviewer gets a quiet `n_review_cancelled` {by, id, title}.
  Changing the reviewer of a task in review (`update`) notifies the new reviewer (`n_review_requested`, or
  USER `u_review_requested` when the actor is not USER) and the old one quietly (`n_review_cancelled`).
* Side effects whenever a task becomes `done`: each open task that depends on it and now has no
  blockers → notify its assignee `n_unblocked` (if assignee and != actor). Whenever a task becomes
  terminal: notify creator `n_task_closed` (if creator is a member, != actor); it wakes the creator only
  when the task failed / was cancelled or the creator has a `waiting` task, otherwise wake=False. Task
  notifications go to active members only (never the actor). USER is not notified about tasks (the UI shows them)
  except via discussion mentions/posts and about the reviews it owns (`u_review_requested`, `u_review_fallback`,
  `u_review_handed_over`, `u_reviews_waiting`, below).
* `post` (discussion): text non-empty (<= 20000 chars); reply_to must be a post of the same task.
  Recipients = participants ∪ {members mentioned as `@Full Name` (case-insensitive, match against
  member names)} − {actor}. Mentioned members get `n_mention`, other recipients `n_discussion`.
  USER is notified only when mentioned (`@user`) or when USER is the task creator/assignee.
  Emits `task.comment` {task_id, post}. Text in notifications truncated to 1500 chars.
* Events: `task.created` {task}, `task.updated` {task, changes: {field: [old, new]}}, `task.comment`.
  `task` payloads are `Task.to_dict()`.

### 4.1 Independent review

With `[swarm] independent_review = true` (the default, §17.1) an agent never approves work it executed. The
**executor** of a task in `review` is its `assignee`: agents cannot change the assignee of a task in review (R2),
so it is the member that called `task_complete`; it is NULL only when the executor left the swarm. The creator
(e.g. the planner) may review, but members who neither created nor executed the task are preferred, and the user
reviews when no other member can. `false` keeps the legacy rules above (a task without a reviewer is done at once).
`Board.independent_review` reads `swarm.config.swarm.independent_review` on every call, so `reconfigure()` and
session overrides apply at once; turning it off leaves tasks already in review alone (their reviewers still review
them). Tool descriptions and system prompts are mode-neutral (§12 R0′), so no frozen prompt or tools array depends on
the switch. A resumed session follows the current setting too: its existing members' completions go to review, and
the tool results and notifications explain it.

- **Valid reviewer** of a task: not None, not its assignee, and USER or an active member (an external member only
  when designated explicitly).
- **Reviewer selection** `pick_reviewer(task, exclude=())`: the pool is the active LLM members other than
  `task.assignee` and `exclude` (external members are never picked: they may be offline). The first by the key
  `(is the task's creator, role rank: 0 for the catalog's reviewer role (roles.role_key == "reviewer",
  «Ревьюер»/Reviewer) else 1, status rank: idle 0 / working 1 / other 2, open tasks held + reviews pending,
  name)`; USER when the pool is empty. So the creator reviews only when no third member exists (in a two-agent
  team the planner reviews the other agent's tasks); testers are not preferred (their own test tasks would starve).
- **Operations** (the last column: the switch off):

  | operation | independent review on | off |
  |---|---|---|
  | `create(..., assignee, reviewer)` | reviewer == assignee != USER → R1 | unchanged |
  | `claim` by the designated reviewer | the claim also clears the reviewer (same mutation; another one is picked at completion; the tool result says so) | unchanged |
  | `update` reviewer | a reviewer equal to the (new) assignee, not USER → R1; in review: clearing it → R3, another valid reviewer is notified and the old one quietly cancelled | no R1 / R3; the notifications as on |
  | `update` assignee | in review: an agent → R2; USER may (reassigning to the reviewer re-picks the reviewer, excluding the new and the old assignee); not in review: a new assignee equal to the reviewer clears the reviewer | unchanged |
  | `update(USER, status=done)` in review | done; `n_review_approved` (comment "") to the assignee, quiet `n_review_cancelled` to the reviewer | same |
  | `complete` by an agent | always `review`: reviewer = the task's own if valid, else `pick_reviewer(task)` | `review` only with a reviewer other than the actor, else `done` |
  | `complete` by USER | as with the switch off | — |
  | `review` by an agent | the task's assignee → R4 (checked before "only the reviewer", also for a row written while the switch was off with reviewer == assignee) | no R4 |
  | `release_all(name)` | name's tasks in review: assignee NULL, they stay in review and are not in the returned ids (a reviewer equal to name, a legacy row, is re-picked); name's other open tasks: `todo`, unassigned (returned). Reviews name was to do: re-picked with `exclude={name}`, the new reviewer is notified (by = the executor, else the actor); open tasks not in review just lose the reviewer. No `n_reviewer_left` | as in the method comment above |

  A task in review whose executor left is approved or returned as usual; a return makes it `todo` (claimable). The
  executor can still withdraw its work while it waits (`task_update status=in_progress|todo|failed|cancelled`,
  allowed for the holder): the reviewer's review is then cancelled quietly.
- **Error texts** (English, like all board errors):
  - R1: `"{reviewer} is the assignee of task #{id}: a task is reviewed by someone other than its assignee. Choose
    another member or 'user', or leave the reviewer empty (a reviewer is picked when the task is completed)."`
    (`create` says "the task");
  - R2: `"Task #{id} is waiting for review, so its assignee cannot change now: the reviewer approves it or returns
    it to {assignee or 'the board'}."`;
  - R3: `"Task #{id} is waiting for review, so it needs a reviewer: pass another member or 'user' instead of
    clearing it."`;
  - R4: `"You are the assignee of task #{id}: your own work is reviewed by someone else ({reviewer or 'the
    user'}). Wait for the verdict; you will be notified."` ("the user" also when the stored reviewer is the
    actor itself).
- **Notifications** (texts in prompts.py, ru/en; "quiet" = `wake=False`, §3 `notify`):

  | when | to | key {placeholders} | wakes |
  |---|---|---|---|
  | a task enters review, or gets a new reviewer while in review; the reviewer is a member | the reviewer (never the actor) | `n_review_requested` {by, id, title} | yes |
  | same; the reviewer is USER, set explicitly (or kept from the task) | USER (unless USER is the actor) | `u_review_requested` {by, id, title} | yes |
  | same; the board chose USER because nobody else qualifies (a pick at `complete`, a re-pick in `release_all` while the executor is still a member) | USER | `u_review_fallback` {by, id, title} | yes |
  | same; the board chose USER at a hand-over, or at a re-pick in `release_all` when the executor has left too | USER | `u_review_handed_over` {id, title, reviewer} (`reviewer`: the member the review was taken from) | yes |
  | the review a member was asked for is settled or moved by someone else, or the task left review | the old reviewer | `n_review_cancelled` {by, id, title} | no |
  | a review handed over (Liveness) | the old reviewer | `n_review_reassigned` {id, title, reviewer} | no |
  | an idle reviewer's reviews wait (Liveness) | the reviewer | `n_review_reminder` {tasks} | yes |
  | only the user's reviews keep the swarm going (Liveness) | USER | `u_reviews_waiting` {tasks} | yes |
  | approved (`task_review`, or USER sets `done`) | the assignee | `n_review_approved` {by, id, title, comment} | yes |
  | returned | the assignee | `n_review_rejected` {by, id, title, comment} | yes |
  | switch off: the reviewer left | the assignee | `n_reviewer_left` {id, title, reviewer} | yes |

  `by` of a review request is the actor; for a re-pick or a hand-over it is the executor, or, when the executor
  has left, the member the review was taken from (never USER: the user did not send it).
- **Liveness** (scheduler, §3):
  - The wake-up text lists the reviews waiting for the member (`my_reviews`, §6.3); `start()` and `resume()` wake a
    member whose only pending work is a review; with the switch on, an idle member with reviews is not offered
    ready tasks.
  - `_remind_reviewers` (switch on): a member's reviews are *due* when their task was last updated at least
    `agents.idle_nudge_seconds` ago. An idle LLM member (status idle, a runtime, nothing pending) gets
    `n_review_reminder` once per set of due task ids. When the same set is still due `idle_nudge_seconds` after
    the reminder and the member is idle, or `2 × idle_nudge_seconds` after the set became due while the member is
    in `error` (no reminder: it cannot act), every due review is handed over:
    `Board.hand_over_review(task_id, exclude=lapsed)`, `lapsed` being every member the task's review was already
    taken from (kept in memory while the task is in review, `Swarm._review_lapsed`). A review therefore never
    returns to a member it was taken from, and goes to the user once every agent let it lapse; a stalled set costs
    at most a reminder and a hand-over. A working member is neither reminded nor handed over, nor is a member busy
    with a job (e.g. its test run went on in the background, §7.1): while it is busy its reminder time is renewed
    at every tick, so the hand-over clock restarts when the busy window ends.
  - `hand_over_review(task_id, exclude)`: reviewer = `pick_reviewer(task, exclude={*exclude, current reviewer})`,
    else USER; `task.updated` with actor None (scheduler-driven); the new reviewer is asked (`n_review_requested`
    by the executor, else by the old reviewer) or USER gets `u_review_handed_over` (the old reviewer did not review
    it and no other agent can take it over); the old reviewer gets the quiet `n_review_reassigned`.
  - `_remind_user_reviews` (both modes): the goal is active, at least one task waits for USER's review, no LLM
    member is working, busy with a job (§7.1) or has pending events, no `todo` task is actionable (not blocked and
    not assigned to USER), no task is `in_progress` and no review waits for anyone but USER → USER gets
    `u_reviews_waiting` {tasks:
    "#3, #5"} once per set of such tasks. Blocked `todo` tasks (the tests of an implementation that waits for the
    user) do not suppress it. The user never answering only leaves the session idle (no cost).
  - Goal completion: `review` is an open status, so `n_all_done` waits for every review, and `goal_complete`
    (§12) is refused while any task is in review (switch on).
  - The planning task is reviewed like any task; in a one-agent team that asks the user to approve the plan, which
    blocks nothing (no task depends on it).
  - Known limitation: an explicitly designated external (MCP) reviewer that went offline is neither reminded nor
    handed over (only LLM members are); the user can approve or return the task, or name another reviewer.
- **The user** can always approve or return a task in review (`task_review` as USER; the web card, `/approve N
  [comment]` and `/reject N what to fix` in the TUI and the console) or set it `done` (`task_update`); print mode
  (`-p`) approves the user's reviews itself while auto-replies are on (docs/TUI.md).

## 5. Mailbox (mailbox.py)

```python
class Mailbox:
    def __init__(self, swarm): ...
    def send(self, sender: str, to: str | list[str], text: str, reply_to: int | None = None) -> dict
        # to: name, list of names, "all" (all active members except sender; USER not included) or USER.
        # Resolves via swarm.resolve_member; duplicates removed; sender cannot be the only recipient.
        # text non-empty, <= 50000 chars. Inserts message + inbox rows; emits "message.sent" {message};
        # swarm.signal(each recipient), except quiet broadcasts (below). Inside `sent_as_seen_by(members, sender)` (a context manager over a
        # ContextVar, set by hub_mcp.hub_call with the caller as `sender`, §13) a message from that sender reaches
        # those recipients with their inbox row already read and no signal: the MCP client that sends acts as them
        # too, so the message is in their history but is no news to them. Only that sender's: a task started inside
        # the block inherits the context (an agent runtime starts in a context of its own anyway, §6.2), and the
        # messages of every other sender reach those members as news.
    def get(self, message_id: int) -> dict
    def unread(self, name: str, mark_read: bool = True, limit: int = 100) -> list[dict]
    def mark_read(self, name: str, message_ids: list[int]) -> int   # returns how many were unread
        # read_messages marks every message it shows (with=..., include_read) read for its caller
    def unread_count(self, name: str) -> int
    def history(self, name: str, with_name: str | None = None, limit: int = 50, before_id: int | None = None) -> list[dict]
    def conversation(self, a: str, b: str, limit: int = 200, since_id: int = 0) -> list[dict]
        # messages where (sender a and b in recipients) or (sender b and a in recipients), ascending
    def recent(self, limit: int = 100, since_id: int = 0) -> list[dict]
# message dict: {"id","ts","sender","recipients":[...],"text","reply_to","to_all"}
# to_all: sent to "all" (column messages.to_all; Database adds it to a file of an older version: db.ADDED_COLUMNS)
```

Quiet broadcasts: when a member (not the user) sends to "all", a recipient reached only through "all" (not also
named) that `swarm.waits_for_job` (an AI agent asleep while it waits for the end of its own job, §3) is not signalled:
its inbox row stays unread (`has_pending` is true; it is not idle for the scheduler, being busy anyway), and the
job's end wakes it and shows the message in the same wake-up instead of a wake-up of its own (which would re-read its
whole context to read "all" news). `swarm.defer_wake` records it; should its wait end without a wake (the busy window
of a server ended, its end became quiet), the scheduler's `_wake_deferred` wakes it. Personal messages (also to several
named members, or "all" plus its name), every message of the user (its "all" included), reviews, assignments and
notifications wake as always. The sender's result is unchanged.

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
creation, and the tools array is frozen per agent (below; agent-built MCP tools go through `mcp_call`). New rules
reach existing members through tool results and notifications, and new tools at their next compaction.

**Frozen tool sets** (tools/frozen.py). The tools array is part of the prompt prefix that thinking blocks are bound
to, so every LLM agent sends exactly the array it was given, and the array changes only where nothing is bound to
it:
- Every released LLM tool set is a **revision** shipped as `tools/snapshots/llm_r<N>.json` (`{"revision": N, "en":
  [...], "ru": [...]}`), never edited. `TOOLSET_REVISION` (now 3: `skill_propose`) is what `schemas_for("llm",
  language)` builds; `BASELINE_REVISION = 1` is the array agents sent before tool sets were frozen (sorted digest
  `e3c5cbc7…`).
- Record: kv `tools:<name>` = `{"rev", "language", "tools"}` (§2), stored through `db.set` (`db.dumps`: key and
  tool order round-trip exactly). `AgentRuntime.__init__` (`_load_tools`) reads it; an agent without one gets
  `frozen.initial_record(language, has_history)`, written at once: the baseline revision when it already has a
  history (it ran before tool sets were frozen), else the current one. View-only loads (no runtime) write nothing.
- `AgentRuntime`: `tools_rev`, `tools` (the record's list, passed unchanged to `llm.complete`), `tool_names`
  (frozenset), `tool_parameters(name) -> dict | None` (None: the agent lacks that tool).
- **Upgrade at compaction only**: `_replace_with_summary` moves an agent whose `tools_rev != TOOLSET_REVISION` to
  `frozen.current_record(language)` (`!=`: after a code downgrade it realigns too); the record is written in the same
  `db.transaction()` as the history rewrite (`_replace_history(messages, tools=)`), and the new history holds no
  assistant turn bound to the old array. The compaction request itself still sends the old array. When tools were
  added, the summary message gets `tools_added` {tools: "`jobs`, `job_stop`"} before `continue`. `agent.compacted`
  data and the compaction span meta carry `tools_revision`.
- Existing agents keep revision 1 until their next compaction: they get timer backgrounding, the adoption of
  processes a command leaves running, the console intercept, the job notices and the wake line at once (§7.1), and
  texts that point at a tool are tailored with `ToolContext.has_tool` (§12). Downgrading the code below a session's
  newest revision is unsupported.
- `frozen` API: `kv_key(name)`, `normalize(tools)`, `revisions()`, `snapshot(rev, language)` (a fresh copy; unknown
  language → en), `current(language)`, `record(rev, language, tools)`, `current_record(language)`,
  `initial_record(language, has_history)`, `read(db, name) -> dict | None` (an invalid record is logged and ignored),
  `write(db, name, rec)`, `names(tools)`, `for_span(db, name, language, revision)` (§16), `member_tool_names(swarm,
  member) -> frozenset` (a runtime's names, else the record or the set its first start will freeze, nothing
  written; the live registry for external members and the user). `python -m dreamteam.tools.frozen --write
  [--force]` writes `llm_r{TOOLSET_REVISION}.json` (exit 2 when a different file exists: a released revision is
  never edited), `--check` exits 1 when the code differs from it.

### 6.2 Loop
```python
class AgentRuntime:
    def __init__(self, swarm, name: str): ...
    name: str; state: str
    def start(self) -> None            # creates the asyncio task, in a fresh contextvars context: it never inherits
                                       # the request context of its starter (hub_call's sent_as_seen_by, §5)
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
  by half a budget (no thrashing), but, while a fresh compaction still gets under the limit − min(max_tokens,
  limit/4), never past that (the window may be real: a larger prompt would fail every request; a summary call
  that no longer fits there falls back to the compaction without the LLM; a compaction above it means the limit
  is not the real window). Then `async with
  swarm.llm_semaphore: llm.complete(system, history, tools)`, record usage, append assistant message,
  emit `agent.activity` events (thinking summary / text / tool call / tool result, truncated 2000
  chars), count the reply in `llm_steps`, execute tool calls sequentially via `tools.call_tool`, append results,
  call `swarm.hint_planner(name)` (§3 scheduler item 4; never raises); after tools, if
  `render_wake(name, incoming=True)` returns text, append it as a user message. Stops when: `wait` was
  called, no tool calls (a `nudge` once per in_progress task the agent holds, none while it is busy with a
  job, §7.1 "Liveness": the job's end wakes it; after plain text even then, `stalled` records those tasks and
  the scheduler wakes it once with `n_stalled`, also once the busy window ended), refusal, or
  step limit (then `notify(name, "n_step_limit")`). A reply cut off by max_tokens gets `cut_off` and
  the loop continues. At the end of an activation that is not stopping (any outcome), the new files its commands
  left are committed unless a command still running may write them (`workspace.commit_held(name, "at the end of
  <name>'s turn")`, §7 "Held new files"; never raises). After an activation, the agent re-wakes itself if events are still pending.
* Output limit after a cut-off reply (`_output_limit`): a reply cut off by max_tokens **without tool calls**
  (typically all thinking) makes the next step ask for twice the limit of the cut-off call, and it keeps doubling
  while replies keep being cut off, up to the profile's ceiling `max_tokens_ceiling` (None = 2 × max_tokens; at
  or below max_tokens = never raised; `ModelProfile.output_ceiling`) and never beyond the context room
  (`_context_limit() − _prompt_estimate()`: when context_tokens is the model's real window, prompt + max_tokens
  must fit; less room than the profile's own limit = the profile's limit). Any other reply (tool calls, a normal
  end) returns to the profile's max_tokens; so does a new activation, and so do compaction calls. The limit goes
  to the client as `complete(..., max_tokens=n)` only when raised (a client whose `complete` has no such keyword,
  `accepts_max_tokens`, is never raised). If a raised request fails with a non-retryable `bad_request` about the
  output limit (`is_output_limit_error`: "max_tokens", "max_completion_tokens", "max_output_tokens",
  "max_new_tokens", "maximum output", "output tokens", "completion tokens"), the same step is retried once with
  the profile's limit (not a counted attempt) and the runtime remembers for this client (`_limit_refused_by`,
  cleared when `reset_llm` / a stop drops the client) not to raise again; a context overflow of a raised request
  (`is_context_overflow`, checked first: vLLM's "maximum context length ... (32000 > 32768 − 1000)" and Anthropic's
  "input length and `max_tokens` exceed context limit" name max_tokens too) is retried the same way but not
  remembered. A raised limit also replaces a limit key in the profile's `extra` (the SDKs merge `extra_body` over
  the body, so the old value would silently win). Both are an `output_limit_fallback` event in the trace and an
  `error` activity; any other error of a raised request behaves as before. The `cut_off` text is unchanged.
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
`wake_header` (or `incoming_header`), then (not for incoming, only while the user's wrap-up mode is on, §3) the one-line
`wrap_up_line` ("Wrap-up mode (the user): start no new computations or background jobs; finish your tasks with what
you have."), `reasons:` then one bullet per undelivered notification text and
per unread message (`msg` template, recipients list with the agent shown as `you`), then, only when one of those
messages was sent to "all" (`to_all`, from the user or a member; incoming too), the one-line `all_hint` ("Before
creating a task for a message to all, check the board: one member creates it, the others take it or comment."), then
(not for incoming) goal line (one line: the goal is stored split, §3 `set_goal`), `my_tasks`, one `my_blocked` {id,
blockers: "#3, #4"} per open task of the agent with unfinished dependencies, except one in review whose result is
already produced (it may remove those it can do without, §4), `my_reviews` (only when
tasks wait for its review: `#id title (assignee or -)`), `my_jobs` (only while it has running background jobs,
§7.1: `my_job` items `j3 npm run dev (running 25 min)` joined with "; ", the command cut to 60 characters, the
duration in the member's language; a silent job, §7.1, is `my_job_silent` `j3 npm run dev (running 50 min, no output
for 40 min)`), `ready` (up to 8), for an LLM member the Skills index (below), notes: in full only when they
changed
since they were last put into the agent's context (a wake-up or a compaction; once more after a restart),
otherwise the one-line `notes_unchanged`, since the history is append-only.

The **Skills index** (§19 "Delivery to agents"; never for external members, never in an incoming render): with skills
attached, the full `skills_index` (or `skills_index_own` when the member's role lists one of them) once per agent
memory: when `Swarm._skills_shown[name]` is not True (never shown, after a restart, or after a compaction whose text
had none); every later wake-up shows the one-line `skills_names` (the names with their marks) instead. With none
attached, nothing, except one `skills_none` line for a member that had seen an index (`_skills_shown` False then; a
later attach shows the full index again).

`stamps=True` (only `wait_for_events`, whose one result may hold hours of news; an agent's wake-up stays as above so
its history does not grow): when the oldest undelivered notification or unread message, or `since` (the oldest
progress line shown with it), is more than `fmt.STAMP_SPAN` (60 s) older than now, each notification and message
bullet starts with its time (`fmt.item_time`: `HH:MM`, with the date when not today's) and the header shows the range
(`fmt.stamp_range`: `2026-10-11 10:50–12:50`) instead of one stamp.

## 7. Workspace (workspace.py) and shell (shell.py)

```python
class Workspace:
    root: Path                                   # config.workspace_dir
    def __init__(self, swarm): ...
    async def init(self) -> None                 # mkdir; ensure_scratch() (with or without git); if git
                                                 # enabled+available: init repo, .gitignore, initial commit
    def ensure_scratch(self, actor: str | None = None) -> None
        # `.scratch/` with its marker `.scratch/.gitignore` (rewritten when missing or changed) and, for `actor`,
        # the member's folder scratch_rel(actor); synchronous, best effort (an OSError is logged); a `.scratch`
        # that is a file or a link/junction resolving elsewhere is left alone, and a marker that is a link is
        # never written through
    def resolve(self, path: str) -> Path         # inside root (no escape, no .git); ToolError otherwise
    def rel(self, p: Path) -> str                # posix relative path
    def read(self, path: str, offset: int = 1, limit: int = 400) -> str   # lines offset..offset+limit-1, header "[path: lines a-b of N]"; binary -> ToolError
    async def write(self, actor: str, path: str, content: str) -> str
    async def edit(self, actor: str, path: str, old: str, new: str, replace_all: bool = False) -> str
        # both refuse a path under SKILLS_MIRROR (`.scratch/_skills`, a lexical check on the resolved relative path,
        # case-folded on Windows): ToolError "'.scratch/_skills/' holds the swarm's skills and is rebuilt by
        # DreamTeam. To change a skill, copy its folder (without node_modules) into your scratch folder and edit it
        # there, then call skill_propose." (§19); the last clause only for a caller whose tool set has skill_propose
        # (frozen.member_tool_names: an agent of revision 1 or 2 gets "... edit it there.")
        # a read-only file elsewhere (a copy of a mirror folder keeps its read-only files) is made writable first
        # (owner write bit), unless it has other hard links; the write is the member's explicit edit
        # old must occur exactly once unless replace_all; ToolError with hints otherwise
    def list(self, path: str = ".", depth: int = 2) -> str   # tree text, dirs end with "/", skips .git, sizes;
        # folders named `.scratch` are shown collapsed like the NO_EXPAND ones (".scratch/ (2 entries)"), so the
        # agents' temporary files do not inflate every listing; list(".scratch/<slug>") lists them; tree() (the
        # web file tree) shows them
    def tree(self, path: str = ".", depth: int = 4) -> list[dict]   # for the web UI: [{"path","type","size","mtime"}]
    def read_raw(self, path: str, max_bytes: int = 2_000_000) -> tuple[str, bool]  # (text, truncated) for UI
    def lock(self, actor: str, path: str, minutes: float | None = None) -> str
    def unlock(self, actor: str, path: str, force: bool = False) -> str   # force for USER
    def locks(self) -> list[dict]                # [{"path","owner","acquired_at","expires_at"}]
    def check_writable(self, actor: str, path: str) -> None
    def release_all(self, actor: str) -> int
    def expire_locks(self) -> int
    async def autocommit(self, actor: str, message: str = "", *, kind: str = "", detail: str = "",
                         skipped: list[str] | None = None, hold: bool = False) -> str | None
        # commits everything except `.scratch/` and nested repositories as `actor`, one commit per call; commit hash
        # or None (nothing outside .scratch changed). The message is `message` as given or, with `kind` ("shell",
        # "job j3", "jobs j3, j5"), commit_message(kind, the paths staged after `git add`, detail): the subject names
        # the changed files, the body holds `detail` (the command that changed them). `skipped` receives the paths
        # of the nested repositories left out. `hold` (the `shell` tool and the jobs pass it): new untracked files
        # wait one more command ("Held new files" below; `ended`: the serial of the command whose end the commit
        # follows, None when none has ended); without it everything is committed now, held files of anyone
        # included, and nothing stays held. Returns the last commit made (a commit of held files included)
    async def commit_held(self, actor: str | None, when: str, *, leaving: bool = False) -> str | None
        # a flush: the actor's task completion or turn end counts as its next command for the held files once none
        # of its commands running when they were first seen is left (`leaving`: it is no suspect any more); the
        # files no longer waiting for anyone are committed, each under its owner (None: every held file); `when`
        # ends the note of the commit ("at the end of Anna Smith's turn"); never raises; no git call when nothing
        # becomes committable
    def held(self) -> dict[str, HeldFile]
        # path -> HeldFile(owner, kind, detail, stamp, others, waiting, running, after)
    def nested_repos_note(self, actor: str, paths: list[str]) -> str
        # the line for the actor's tool result about them ("" when none is new to this actor in this session)
    def mtime(self, path: str) -> float | None
    def exists(self, path: str) -> bool
```
Writes/edits check locks, create parent dirs, write UTF-8, emit `file.changed` {path, op} (the actor is
the event's top-level `actor`), then commit that path; a path under `.scratch/` is written and emits
`file.changed` but is never committed, and the result says so (`Created .scratch/anna.smith/c.py (1 line, 9 B)
(in .scratch/: not committed).`). Lock events: `file.locked` {path, owner, expires_at, refreshed},
`file.unlocked` {path, owner, forced} or {path, owner, expired: true}. Unless the folder is adopted (below),
`init()` also writes repo-local settings (`_configure_repo`: core.autocrlf=false, commit.gpgsign=false and a
default identity), so that git commands run from the shell behave like the autocommits (line endings, no
signing); an agent's own commits carry its identity from the shell environment (`agent_git_env`, below).

The whole-tree autocommit (after every `shell` call): `ensure_scratch()` (an agent may have deleted the marker),
`git status --porcelain -z` (`porcelain_entries`), `git reset -q -- .scratch` when something under `.scratch/` is
staged (a manual `git add -f`; it only resets index entries to HEAD), nothing more when nothing outside `.scratch/`
changed, else `git add -A --ignore-errors -- . ":(exclude)[.]scratch" ":(exclude)[.]scratch/*"`
(`SCRATCH_EXCLUDES`, with pathspec magic, `literal=False`; plus the nested repositories; with `hold`, `git add -u`
instead, "Held new files" below) and a commit of the index without a pathspec (a merge an
agent left in progress concludes as before). With `kind`, its message is built from `git diff --cached --name-only
--no-renames -z` (`_index_message`; a rename shows as its old and its new path, deleted files are named; a failed
diff quotes the detail instead). The excludes are spelled with `[.]`: git add takes a literal
`:(exclude).scratch` as naming the folder and, when the root `.gitignore` ignores `.scratch/` (`DEFAULT_GITIGNORE`
does), reports it as an ignored path and exits 1 on every autocommit, burying real add errors in the log. Path
commits of `write`/`edit` keep literal pathspecs (`-- rel`).

**Nested repositories** (`_nested_repos`): an agent that runs `git init` in a generated project (e.g.
`examples/lab_project/a`, or a folder literally named `--help`) leaves an untracked folder with its own `.git`;
`git add -A` would commit it as a gitlink (mode 160000) without a `.gitmodules` entry, an empty reference nobody
can check out. Before `git add`, the untracked folders of the status (`git status --porcelain -z
--untracked-files=normal`, whatever the user's `status.showUntrackedFiles`, which `git add -A` ignores; `??`
entries that are directories, outside `.scratch/`; up to `NESTED_PATHSPECS_MAX` = 100 of them by name, else all) go to `git ls-files --others
--exclude-standard -z -- <folder>/...` (literal pathspecs after `--`): it stops at a nested repository and names it
with a trailing `/`, also one deep inside an untracked folder, which `git status` shows only as that folder. Those
with a `.git` (folder or file) are excluded with `:(exclude,literal)<path>` and passed to the caller (`skipped`).
Ignored folders are never listed, and tracked submodules (an adopted project's own, with `.gitmodules`) are
tracked, so they are left as they are. The `shell` tool appends one line to its result, each path named once per
session to that member (`Workspace._nested_reported`, in memory), at most `NESTED_SHOWN` = 5 paths + "and N more":
`Not committed: examples/lab_project/a, --help — folders with their own .git (nested repositories). Delete their
.git or move them into your scratch folder .scratch/<slug>/ if they are temporary.` Job commits leave them out the
same way without a note (the owner's next `shell` call names them); `write_file` / `edit_file` commit one path
and cannot create one. The initial "Initialize workspace" commit (`init_repo`, also for an adopted folder that was
not a repository yet) leaves them out as well (`nested_repo_paths` of `git ls-files --others --exclude-standard
-z`); the first `shell` autocommit afterwards names them to the agent.

#### Held new files
A test run often leaves files that the next run deletes (cookiecutter's test hooks: `…/test-hooks/hooks/
post_gen_project.bat (+2 more)` added by one `shell` commit, deleted by the next). So the `shell` and job autocommits
(`hold=True`) commit changes to tracked files (edits, deletions, renames) at once as before, but a **new untracked
file** only once it has outlived one more command; a held file that vanished meanwhile never enters the history, and
history is never rewritten.
- The untracked files are those `git add -A` would add: the `??` files of the status and, for its `??` folders,
  `git ls-files --others --exclude-standard -z` (the listing that finds nested repositories; `_untracked` maps each
  file to its `??` entry). Not `.scratch/`, not inside a nested repository, not ignored.
- Commands are told apart by `RunningCommand.serial` (one counter per process, `shell.last_serial()` the last one
  started): `jobs.running_commands()` = (serial -> member of every foreground `shell` call and running job, the last
  serial). The `shell` tool passes `ended` = its command's serial when the command has ended (None when it goes on as
  a job), a job's end commit its command's serial, a job commit while processes of it still run None.
- `Workspace._held` (in memory; path -> `HeldFile(owner, kind, detail, stamp, others, waiting, running, after)`): a
  file not held yet is held with the autocommit's actor as `owner` (the member whose commit first saw it), that
  commit's `kind` and `detail` (the command) and its (size, mtime) `stamp`. Git cannot tell who wrote a file, so the
  suspects are the owner and, as `others`, every member with a command running then or an autocommit of its own
  waiting for the git lock (`_queued`: the end of a command queues its commit at once, with no await in between),
  taken right after the file listing. `running`: the commands running then (the owner's own job too, e.g. its test
  run that went on as a job while it reads the log). `waiting`: the suspects whose next command the file has not
  outlived yet. A suspect's next command is one that ended (`ended`) with a serial above `after[member]`: the last
  serial at first sight, or, for a member with commands in `running`, the last serial when the last of them had
  ended and its commit was made (`_settle_held`, at every hold commit and flush) — so neither a command running when
  the file appeared, nor its own end commit, nor a quick command started while it still ran counts. Every
  whole-tree autocommit first forgets held paths no longer untracked (deleted, ignored now, committed by
  `write_file` / `edit_file` or by hand: those commit their file at once as before).
- At each `hold` autocommit of a member: a held file with another stamp (deleted and written again by the next run)
  is held anew, as a new file of this commit; one with its stamp counts the command for that member. Files no longer
  waiting for anyone have outlived the next command of every member that may have written them and are committed
  first, in a commit of their own per command that left them, under their owner: `git add -A --ignore-errors --
  <paths>` and `git commit -- <paths>` (only those paths), subject `commit_message(f"{kind} (new files)", paths)` ("shell (new files): src/new.py"), body the original
  command and `HELD_NOTE`: "New files first seen after this command of Anna Smith, committed after the next command
  of Anna Smith left them in place: a new file waits until it outlives one more command, so leftovers that the next
  command deletes (of a test run, say) never enter the history." With `others` it adds " (Boris King also had a
  command running then; git cannot tell who wrote a file)" after the owner and reads "after the next commands of
  Anna Smith and Boris King". Then the call's own commit: `git add -u --ignore-errors -- . <SCRATCH_EXCLUDES>`
  (tracked files only) and, for a tracked file moved with plain `mv` (`_moved`: a new file whose blob id, `git
  hash-object --stdin-paths`, equals that of a file the call deleted in HEAD, `git cat-file --batch-check` of
  `HEAD:<path>` lines through stdin, any number of them; exact content, at most `MOVED_CANDIDATES_MAX` = 500 new
  files), `git add -- <new paths>`, so the rename stays one commit; then the index commit as before. When every
  change outside `.scratch/` is a new file (all `??`), nothing was moved and no git operation is halfway
  (`_operation_in_progress`: GIT_OPERATIONS in the workspace's own `.git` folder; without one the commit is tried),
  there is nothing to stage: no `git add -u`, no index commit tried.
- Another member's commit never sweeps them up: with `hold` no untracked file but a moved one is staged, so every
  held file stays out, and a file a command still running creates between the status and the add waits for the next
  autocommit. The hold belongs to the tree, not to an actor: a tester's test run that leaves files while a
  developer's quick command ends is first seen by the developer's commit, and waits for the tester's next run too
  (which deletes them: nothing enters the history). The commit of held files is attributed to their owner, and its
  body names the other suspects (HELD_OTHERS). Paths of a commit of held files are collapsed: an untracked
  folder all of whose files go into it is named once (`_collapse`; never one holding a nested repository).
- Flush points (`commit_held(actor, when)`): the actor's boundary counts as its next command for every held file it
  is a suspect of, once none of its commands in `running` is left (its own job may still write or delete them); the
  files no longer waiting for anyone are committed, each under its owner. They are: its `task_complete`, right after
  the board accepted it ("when Anna Smith completed task #4": the reviewer and the user read HEAD; a refused call
  commits nothing); the end of an AI agent's activation (`AgentRuntime`, unless it is stopping: "at the end of Anna
  Smith's turn"); an external member's `wait_for_events` (the same note); `Swarm.retire` after the member's jobs
  stopped (`leaving=True`: it is no suspect any more, "when Anna Smith left the team"); `Swarm.stop` after
  `jobs.stop_all()` for every held file ("when the session stopped"). A file another member's command may have
  written waits for that member's next command or boundary, even at a task boundary: while its test run goes on, a
  flush of the developer must not commit what the run deletes at its end (the tree has the file meanwhile; HEAD gets
  it a step later). The price of the flushes: leftovers still there at one of them are committed (and their deletion
  by the next run is a commit too). No git call when nothing becomes committable.
- A held file that the next command rewrites (another size or mtime) is held anew rather than committed: a test run
  recreates its leftovers each time; a file every command rewrites goes in at a flush.
- A commit of held files that fails (e.g. git refuses a partial commit while a merge is in progress) unstages them
  again (`git reset -q -- <paths>`, never left for someone else's index commit) and keeps them held for the next try.
  Without a file listing (`git ls-files` failed) the autocommit commits everything as before and forgets the holds.
- Long pathspec lists (more than `PATHSPEC_ARGV_MAX` characters: 8000 on Windows, which limits a command line to
  32767 characters; 100000 elsewhere) go through stdin (`--pathspec-from-file=- --pathspec-file-nul`, git 2.25+;
  magic as on the command line); a git that does not know the option (exit code 129) gets them on the command line
  after all.
- The agent is told nothing: tool results do not change. A restart forgets the holds (the next autocommit holds the
  files again).

#### Scratch folder
Agents put temporary files (experiments, check scripts, test output, downloads) into **`.scratch/`** at the
workspace root, each in a folder of its own, `scratch_rel(name)` = `.scratch/<email_slug(name)>/` (e.g.
`.scratch/anna.smirnova/`; members work in parallel). Its path is in a new member's system prompt (`{scratch}`,
§3 `hire`) and in the `shell` environment (`DREAMTEAM_SCRATCH`). Nothing under `.scratch/` is ever committed, by
three independent layers:

| layer | what | where it applies |
|---|---|---|
| A. self-ignoring marker | `.scratch/.gitignore` = `SCRATCH_GITIGNORE` (a comment and `*`: git ignores the folder, the marker included), written by `ensure_scratch` | every workspace: a repository DreamTeam created, an adopted repository of the user, a folder without git |
| B. autocommit exclusion | the whole-tree autocommit stages `-- .` minus `SCRATCH_EXCLUDES` and resets anything staged under `.scratch/`; path commits of a path under `.scratch/` are skipped (and `ensure_scratch` runs, so a marker overwritten by `write_file` / `edit_file` is put back at once) | every repository; holds even without the marker or when `.scratch` is a file or a link |
| C. `DEFAULT_GITIGNORE` | the root `.gitignore` that `init_repo` writes has the line `.scratch/` | only repositories whose `.gitignore` DreamTeam created |

| workspace | `.scratch/` + marker | root `.gitignore` | repository config |
|---|---|---|---|
| new session folder (`init_repo` by DreamTeam) | created at `init()` | DreamTeam's, with `.scratch/` | `_configure_repo` |
| workspace created before `.scratch/` existed | created at the next `init()` | unchanged (the marker covers it) | `_configure_repo`, as before |
| adopted repository of the user | created at `init()` (untracked, self-ignored: `git status` stays clean, so a later adopt or start check does not see it) | never touched | never touched |
| adopted folder without a repository | created at `init()` | written by `init_repo` only when missing | never touched |
| `workspace.git = false` or no git | created (harmless) | none | none |

Edge cases: `.scratch/` is never emptied by DreamTeam (the user can inspect it); a move with `/cd` carries it
along, leaving an adopted folder leaves it there, deleting a session deletes it only with the session's own
folder. Something an agent commits by hand stays in history; a file under `.scratch/` that the user's repository
already tracks stays tracked, and the agents' changes to it are never autocommitted (known limitation). The
start check of an adopted repository (§17.2) leaves `.scratch/` out like the session's own `mcp/<name>` folders:
a marker an agent removed in its last command (`?? .scratch/`) does not refuse the start, which writes it again.
`.scratch2` and the like are ordinary paths (the exclude matches whole path components).

One exception to "agents own `.scratch/`": **`.scratch/_skills/`** (`SKILLS_MIRROR`) is DreamTeam's read-only copy
of the swarm's skills (§19), rebuilt from the session's copies at every start, on every attach and detach, and
within a minute of a change made there; `write_file` and `edit_file` refuse it, and other edits are undone. To change
a skill, an agent copies it into its own scratch folder. In an adopted folder it is removed when the swarm stops.
`_skills` is never a member's scratch folder (`email_slug` never starts with `_`). A Node skill's copy there holds
a `node_modules` link (a junction on Windows, a symlink elsewhere) to this session's own view of the skill's shared
environment (§19 "Script dependencies"); every DreamTeam walk skips links, `.scratch/` is never committed, and a
workspace move removes `.scratch/_skills` before it moves anything (§17.2).

The root is `config.workspace_dir`: `<project>/workspace`, or `config.workspace_path` when a session works in
another folder (§17.2 "Workspace folder"). Then:
- `init()` never creates a missing root (FileNotFoundError): a folder outside the session (the user's project,
  an unplugged drive, a host path inside the Docker sandbox) is not replaced by an empty stand-in.
- `adopted` (= `config.workspace_adopted`, the folder was the user's): `init()` never writes the repo-local
  settings, not even into a repository it initialized there. Every git call of the workspace passes its identity
  and settings per command (`-c user.name=<actor> -c user.email=… -c core.autocrlf=false -c commit.gpgsign=false
  -c core.quotepath=false -c safe.directory=<root>`), so autocommits never need the repository's config. In an
  adopted folder `-c core.autocrlf=false` is left out: the user's own line-ending settings apply to the agents'
  commits as to the user's (Git for Windows sets `core.autocrlf=true` system-wide; overriding it would commit
  CRLF rewrites of the user's LF files and make a clean checkout look modified). `Workspace.resolve` refuses any
  path with a `.git` component, so the agents never write into the repository database.
- `async settle(timeout=GIT_TIMEOUT)`: waits for git commands whose caller was cancelled (they run in threads);
  `Swarm.stop()` calls it, so no commit is still writing when a stopped session's files move or are deleted.

Module helpers (synchronous, for code without a swarm, e.g. sessions.py; run them in a thread):
```python
SCRATCH_DIR = ".scratch"                             # the agents' temporary files: never committed
SCRATCH_GITIGNORE: str                               # the marker's content: a comment and "*"
DEFAULT_GITIGNORE: str                               # the root .gitignore of init_repo; ends with ".scratch/"
GIT_LOCATION_VARS: frozenset[str]                    # GIT_DIR, GIT_WORK_TREE, GIT_INDEX_FILE, ... (agent_git_env)
def git_identity(actor) -> tuple[str, str]           # (name, e-mail) of commits for actor: (actor or "DreamTeam",
                                                     # "<email_slug>@dreamteam.local"); autocommits and the shell
def scratch_rel(actor) -> str                        # ".scratch/<email_slug(actor)>/"
def in_scratch(rel) -> bool                          # `.scratch` or inside it (case-insensitive on Windows)
SKILLS_MIRROR = ".scratch/_skills"                   # the agents' read-only copy of the session's skills (§19)
def scratch_is_local(root) -> bool                   # <root>/.scratch is a plain folder of the workspace (not a
                                                     # file, nor a link/junction resolving elsewhere): job logs
                                                     # and the skills mirror go there only then
def is_link(entry_or_path) -> bool                   # a symlink or a redirecting reparse point (junction); a
                                                     # DirEntry, Path or str; cloud placeholders are not links
def long_path(path) -> str                           # the \\?\ (\\?\UNC\) form on Windows, unchanged elsewhere
def rmtree(path) -> None                             # best effort, read-only files made writable first, long
                                                     # paths (sessions._rmtree is this function)
def commit_message(kind, paths, detail="") -> str    # subject "<kind>: p1, p2 (+N more)" in at most
    # COMMIT_SUBJECT_MAX (72) characters, each path at most COMMIT_PATH_MAX (40: "…/models/user.py", a file name
    # too long alone is cut with "…"); body = `detail` (CRLF normalized, control characters dropped, trailing
    # blanks removed, unpaired surrogates replaced), cut in the middle beyond COMMIT_DETAIL_MAX (2000) with
    # "[... N characters skipped ...]"; without paths the subject quotes the detail
def agent_git_env(env, actor) -> dict[str, str]      # a new env: GIT_LOCATION_VARS dropped, GIT_AUTHOR_NAME/EMAIL
                                                     # and GIT_COMMITTER_NAME/EMAIL = git_identity(actor)
def porcelain_entries(data: bytes) -> list[tuple[str, str]]   # (XY, path) of `git status --porcelain -z`; a
                                                     # rename or copy yields its new and its old path
def git_env() -> dict[str, str]                      # os.environ without GIT_*, GIT_TERMINAL_PROMPT=0
def git_argv(git, root, actor, args, safe_directory=None, *, adopted=False, literal=True) -> list[str]
    # identity from git_identity(actor); adopted: no core.autocrlf override; literal=False: pathspec magic such as
    # `:(exclude)` works (only the autocommit's fixed pathspecs use it; Workspace._git(..., literal=) passes it on)
def run_git(root, *args, actor=None, git=None, safe_directory=None, timeout=GIT_TIMEOUT, adopted=False,
            literal=True) -> CompletedProcess
def init_repo(root, git=None, *, adopted=False) -> str | None   # git init + .gitignore if missing + initial
    # commit of what is there (with the user's line endings when adopted); None = ok, else git's error text
def looks_like_git_dir(folder) -> bool                 # a repository database itself (HEAD + objects/ + refs/)
def repo_state(root, git=None, exclude=()) -> dict     # what an existing folder is to git (the user's config
    # applies, read-only, safe.directory=*): {"toplevel": work tree root containing root | None, "git_dir": a bare
    # repository or a .git folder, "branch": current branch | None (detached), "operation": "merge" | "rebase" |
    # "cherry-pick" | "revert" | "bisect" ... stopped halfway (GIT_OPERATIONS: MERGE_HEAD, CHERRY_PICK_HEAD,
    # REVERT_HEAD, rebase-merge/, rebase-apply/, sequencer/, BISECT_LOG in the git dir) | None, "changes":
    # `git status --porcelain --untracked-files=normal --ignore-submodules=none` lines (the user's
    # status.showUntrackedFiles or submodule settings cannot hide what `git add -A` would commit; `exclude`:
    # folders relative to root left out, e.g. the session's own `mcp/<name>`), "error": str | None}
def tracked(root, paths, git=None) -> list[str]        # which of `paths` hold files the repository tracks
def commit_paths(root, paths, message, git=None) -> (commit | None, error | None)
    # commit only the current state of `paths` (e.g. their removal) as DreamTeam, other changes left as they are;
    # refused on a detached HEAD or while an operation is in progress
```

```python
# shell.py
async def run_shell(command: str, *, cwd: Path, timeout: float, program: str = "auto",
                    env: dict[str, str] | None = None, max_output: int = 12000) -> tuple[int | None, str]
    # (exit code | None on timeout, combined stdout+stderr decoded, head+tail truncated): returns when the shell
    # exits, kills what it left running after LEFTOVER_GRACE (1 s), waits for the last output up to READ_SETTLE
    # (2 s); on timeout the tree is killed and the output so far returned; a cancelled caller's tree is killed and
    # awaited (KILL_WAIT, 5 s) before the cancellation goes on. For callers without jobs: the `shell` tool goes
    # through JobManager.run (§7.1)
async def start_command(command: str, *, cwd: Path, program: str = "auto", env: dict[str, str] | None = None,
                        max_output: int = 12000, token: str | None = None, offline: bool = False) -> RunningCommand
    # starts the command as a contained tree (ProcessTree.spawn in a daemon thread) and returns at once; `token`
    # goes into the environment as DREAMTEAM_PROCESS_TOKEN; `offline`: the shell's argv through netoff.wrap (§7.2;
    # the caller sets netoff.environment); ValueError for an unsupported shell
class RunningCommand:   # name (the shell's display name), tree, capture, started_at, serial; exited, eof, returncode
    async def wait_exit(self, timeout) -> bool          # the shell exited (its processes may run on)
    async def wait_eof(self, timeout) -> bool           # the output pipe reached EOF
    async def settle(self, grace) -> bool               # polls the tree (SETTLE_POLL 0.05 s): True once it is empty
    async def wait_dead(self, timeout) -> bool
    async def kill(self, term_grace: float = 0.0) -> bool   # terminate, term_grace, kill; at most term_grace + 2 s
    def output(self) -> str                             # the output so far (capture.text())
    def tail(self, lines=40, chars=4000) -> str
    def close(self) -> None                             # releases the tree and detaches the capture (idempotent)
async def kill_after_cancel(cmd: RunningCommand, timeout: float = KILL_WAIT) -> None
class StreamDecoder:    # feed(data: bytes, final=False) -> str: complete lines only; `pending` = the unfinished one
class Capture:          # Capture(max_output, program): feed(data), text(), tail(lines, chars), shrink(head, tail),
                        # attach_log(path, header, max_bytes), log_line(line), detach_log(footer), detach()
def resolve_shell(program: str = "auto") -> tuple[str, list[str]]   # (display name, argv prefix)
def shell_args(name: str, prefix: list[str], command: str) -> list[str] | str
    # Popen args: prefix + [command]; for cmd on Windows one string `<prefix> "<command>"` (list2cmdline would
    # escape the command's quotes as \", which cmd does not understand; /s strips exactly the outer pair)
def scrubbed_env(config: Config, extra: dict[str, str] | None = None) -> dict[str, str]
    # os.environ minus secrets (model api_key_env vars, *_API_KEY, *_TOKEN, *SECRET*, *PASSWORD*)
    # except config.shell.pass_env, and always minus _INTERNAL_ENV (DREAMTEAM_SKILLS_DIR, DREAMTEAM_HOST_SKILLS_DIR,
    # DREAMTEAM_SKILL_ENVS_DIR: agents' commands are never pointed at the skills library or the skills'
    # environments, even through pass_env); plus
    # PYTHONIOENCODING=utf-8, PYTHONUTF8=1 and `extra`
def kill_tree(proc) -> None   # tui/bash.py's `!` commands: POSIX os.killpg(proc.pid, SIGKILL); Windows taskkill /T /F

# procs.py (standard library only; kernel32 through ctypes, loaded lazily)
TOKEN_ENV = "DREAMTEAM_PROCESS_TOKEN"
class ProcessTree:      # proc, pid, pgid (POSIX: the shell's pid, stored), contained, token; released, watchdog
    @classmethod
    def spawn(cls, argv: list[str] | str, *, cwd, env, token=None,
              breakaway=True) -> "ProcessTree"         # blocking: via daemon_call
    def alive(self) -> bool; def pids(self) -> list[int]; def processes(self) -> list[str]
    def terminate(self) -> None; def kill(self) -> None; def release(self) -> None; def mark_eof(self) -> None
def live_groups(pgids) -> set[int]                     # Linux: one /proc pass, zombies ignored; other POSIX: killpg 0
def sweep_token(token: str) -> int                     # Linux: SIGKILL every own-user process carrying the token
def alive_all(trees) -> list[bool]                     # alive() of many trees (one /proc pass on Linux)
async def daemon_call(fn, *args, on_orphan=None)       # fn in a new daemon thread; on_orphan(result) when the
                                                       # caller was cancelled meanwhile (kills the new tree)
```
**Process layer.** Every command runs as a contained tree, because nobody knows in advance which command will
outlive its call:
- Windows: a Job Object with KILL_ON_JOB_CLOSE (no BREAKAWAY_OK); Popen with CREATE_NEW_PROCESS_GROUP |
  CREATE_NO_WINDOW; the shell is assigned through Popen's own handle right after Popen returns, in the spawning
  thread; then up to three Toolhelp32 passes assign the children (and their children) it already started, those
  created after it (creation time checked). Liveness = the job's count of active processes; names from its process
  id list (`conhost.exe` left out); terminate and kill = TerminateJobObject (Windows has no graceful stop); closing
  the handle (`release()`, or the hub's death) kills what is still in it. When the job cannot be created or
  assigned, the tree is degraded (`contained = False`, one warning): liveness = the shell or an open pipe, kill =
  `taskkill /T /F` in a daemon thread. No program may break away from the job (`shell.JOB_BREAKAWAY = False`,
  `spawn(..., breakaway=False)`): the Cygwin runtime of Git Bash and MSYS2 asks for CREATE_BREAKAWAY_FROM_JOB for
  every Windows program it starts whenever the job allows it, also when cmd or PowerShell runs `bash`, `sh` or a
  git hook, which would take what they run out of the job. A program that insists on breaking away fails to start
  instead.
- POSIX: `start_new_session=True`, the group id (= the shell's pid) is stored and never asked again (`getpgid` fails
  once the leader was reaped). A `sh` lifeline watchdog per command (`dreamteam-watchdog <pgid>`) reads a pipe the
  hub holds: a line (`release()`) ends it quietly, EOF (the hub died) sends TERM to the group, then KILL after 2 s.
  The watchdog's environment has no `DREAMTEAM_PROCESS_TOKEN`: a hub that itself runs as a command of another hub
  (an agent running `dreamteam run`, this project's tests run as a job) carries that outer token, and the outer
  job's token sweep must kill the inner hub but leave its watchdogs, which then kill the groups they guard.
  Liveness after the shell was reaped: Linux counts the group's live processes in /proc (zombies ignored, so
  init-less containers stay correct), other systems `killpg(pgid, 0)`. terminate = SIGTERM to the group; kill =
  SIGKILL to the group plus, on Linux, the token sweep (it also ends `setsid` escapes). Names from
  `/proc/<pid>/comm` (Linux), else none ("processes").
- After `release()` nothing is ever signalled again (idempotent); a tree dropped without it is released by a
  `weakref.finalize`.
- A command ends when its shell has exited and its tree is empty; the pipe's EOF is waited for at most
  `READ_SETTLE`, because a process the command left running may hold the pipe for hours.
- Output: a daemon reader thread owns the pipe's read end and alone ever closes it (a closed fd number is soon
  reused, and a reader still blocked on it would consume someone else's file or socket); a daemon waiter thread
  waits for the shell; every wait is `asyncio.wait` on a future only those threads resolve. `StreamDecoder` decodes
  strict UTF-8 and switches for good at the first invalid byte to the shell's fallback (`cp<OEM>` for cmd on Windows,
  else the locale), turns `\r\n` into `\n` (also across chunks) and keeps only the text after a line's last bare
  `\r` (a progress bar's final state); an unfinished line is held back (beyond PENDING_MAX, 65536, emitted as it is)
  but shown by `text()` and `tail()`. `Capture(max_output)` keeps a head (40 %) and a tail, so `text()` equals
  `truncate_middle(output, max_output)`; one lock guards its buffers and log writes.

**The `shell` tool** (tools/file_tools.py):
1. The console intercept: `await swarm.jobs.console(actor, command, max_chars=ctx.output_cap)`; a whole command line
   `jobs`, `jobs j3` or `kill j3` is answered by the hub (nothing spawned, nothing committed, §7.1).
2. timeout = `min(max(args.timeout or [shell] timeout, 1), [shell] max_timeout)`; `workspace.ensure_scratch(actor)`
   (so `> .scratch/<slug>/out.txt` works); env = `agent_git_env(scrubbed_env(config, {DREAMTEAM_AGENT: actor,
   DREAMTEAM_WORKSPACE: root, DREAMTEAM_SCRATCH: root/scratch_rel(actor), DREAMTEAM_SKILLS: swarm.skills.agent_dir()
   (the skills mirror, or the session copies when `.scratch` is not local, §19)}), actor)`: git in the shell always
   works
   on the workspace repository, and a commit an agent makes anyway (its system prompt forbids history-changing git
   commands) is authored and committed by that member, with the same identity as its autocommits; the identity is
   set after `scrubbed_env`, so it also beats `shell.pass_env` and inherited values. Other `GIT_*` variables (e.g.
   `GIT_SSH_COMMAND`) pass through. MCP servers and the user's TUI `!` commands keep their own environment.
3. `outcome = await swarm.jobs.run(actor, command, timeout=, env=, max_output=ctx.output_cap,
   background=args.background)` (§7.1): the command's result, or a job.
4. Unless the outcome is `killed` (the session stops or the member leaves: no git work may start then):
   `workspace.autocommit(actor, kind="shell", detail=command, hold=True)`, one commit per call; new files wait
   one more command ("Held new files" above), and the result says nothing about them.
5. The result is `outcome.render(has_jobs_tool=ctx.has_tool("jobs"), cfg=[shell], has_wait=ctx.has_tool("wait"))`.

An agent's command dies when the agent's task is cancelled (a stopped or retired agent) before the command became a
job: its command must not go on writing into the workspace after the swarm stopped.

### 7.1 Background jobs (jobs.py)

A `shell` command is no longer killed at its timeout. A command still running when its call stops waiting goes on
as a **job** `j<N>` (N = `jobs.id`; written `j3` everywhere, never `#3`, which is a task), and so do processes a
command leaves running and commands started with `background=true`. `Swarm.jobs` (`JobManager(swarm)`) owns every
job: its row (§2), its log, its lifetime, its end, its commits and its cleanup. Russian texts call a job «фоновый
процесс» («задача» is a board task).

```python
class JobManager:
    closing: bool                                        # stop_all ran or runs: no new command, no new job
    async def run(self, actor, command, *, timeout, env, max_output, background=False) -> ShellOutcome
    async def console(self, actor, command, *, max_chars=12000) -> str | None
    def listing(self, actor) -> str
    def describe(self, job_id: int, *, actor, lines=40, max_chars=12000) -> str      # ToolError: unknown id
    async def stop(self, job_id: int, by: str, *, force=False) -> str                # ToolError: unknown, not yours
    def forbid(self, owner) -> None                      # synchronous: no new command or job for owner (retire)
    async def stop_owner(self, owner, reason="retired") -> list[int]   # its jobs and foreground commands, awaited
    async def stop_all(self, reason="session") -> None   # idempotent; awaits every finish and call in flight
    def recover(self) -> None
    def busy(self, name) -> bool; def wakes(self, job) -> bool; def awaited(self, owner) -> list[Job]
    def running_of(self, owner=None) -> list[Job]; def get(self, job_id) -> dict | None
    def running_commands(self) -> tuple[dict[int, str], int]
        # (serial -> member of every foreground command and running job, shell.last_serial()): §7 "Held new files"
    def snapshot(self) -> list[dict]
    def check_start(self, actor) -> str | None           # "disabled" | "limit_member" | "limit_session" | None
    def command_env(self, env) -> dict; def cpu_threads(self) -> int
@dataclass
class Job:   # id, owner, command, origin, cmd: RunningCommand, log, log_path, started_at, background_at, stopping,
             # finishing, done: Future, command_done, exit_code, command_ended_at, shell, processes, status, reason,
             # stopped_by, ended_at, tail; label "j<id>", log_shown, notice_tail(), public(now=None) -> dict
@dataclass
class ShellOutcome:  # state, code, output, shell, wait, job=None, note="", names=[], note_args={}, max_output=12000
    def render(self, *, has_jobs_tool: bool, cfg: ShellSettings, has_wait: bool = True) -> str
def parse_job_id(text) -> int | None                     # "j3" "J3" "%3" "#3" "3" -> 3
def parse_console(command) -> tuple[str, list[int], bool] | None   # ("list"|"show"|"kill", ids, force)
def short_command(cmd, limit) -> str                     # one line, cut in the middle with "…"
def fmt_duration(seconds, lang="en") -> str; def format_names(names) -> str   # "node.exe ×2, python.exe"
def shown_log(log) -> str                                # a data_dir log with "(outside the workspace: ...)"
```

**Origins** (`jobs.origin`):

| origin | how a command becomes one | stopped at (from its start) | keeps its owner busy | its end wakes the owner |
|---|---|---|---|---|
| `timeout` | its shell still runs when the call's wait ended | `[shell] max_timeout` | always | unless the goal is done |
| `background` | `background=true` (revision-2 agents, external members; the user runs no `shell`) | `job_max_minutes` (0 = never) | while younger than `max_timeout` and the owner holds an in_progress task or a review | unless the goal is done |
| `detached` | the shell exited within the wait and processes it started run on (`cmd &`, `nohup`, `Start-Process -NoNewWindow`, `start /b`; with `background=true` such a job is a `background` one); also a timeout/background job whose shell exited later | `job_max_minutes` | never | only under an active goal while the owner holds an in_progress task (else quiet: shown at its next wake-up) |

`wakes(job)` is that last column; `awaited(owner)` = the owner's running jobs with `wakes(job)`.

**A call** — `JobManager.run(actor, command, *, timeout, env, max_output, background=False) -> ShellOutcome`:
- Refused (ToolError) while the session stops: `The session is stopping: no new commands are started.`; for a
  member being retired (`forbid`): `You are leaving the swarm: no new commands are started.`; with `background` when
  no job may start now (`check_start`): `You already have {n} background jobs ({ids}), the most allowed: stop one
  with job_stop, or run this command without background.` / `The session already runs {n} background jobs, the most
  allowed: run this command without background.` / `Background jobs are turned off in the settings ([shell]
  max_jobs_per_member or max_jobs is 0): run the command without background.`; with `background` in the user's
  wrap-up mode (below): `The user asked the team to wrap up, so no new background jobs are started. If the command is
  quick, run it without background (it is stopped at its timeout); otherwise finish your task with what you have
  (task_complete with the result so far and what is left).` (nothing starts).
- Wait: `BACKGROUND_GRACE` (2 s) with `background`, else `timeout`. With `background_on_timeout` an AI agent waits at
  most `[shell] foreground_max` (180 s; blocked in a long command it would see no message, comment or review), an
  external member at most `EXTERNAL_WAIT_MAX` (600 s, below the stdio proxy's read timeout of 700 s). Without
  `background_on_timeout` the timeout is the wait, as before. The cap only moves an AI agent's command to the
  background sooner: when the command cannot become a job when the cap ends (`check_start`: jobs turned off, a
  limit reached), the call waits on up to its own `timeout` (checking every `JOB_TICK`), so the command is killed
  only then, as before the cap, and goes on as a `timeout` job as soon as a slot frees. The external cap stays
  (it protects the MCP transport). Wrap-up mode is one more such case: while it lasts, an AI agent's call waits on
  past the cap up to its `timeout` (its command cannot become a job), whether the mode was on when the call started
  or came during it; switched off during the call (also by a new or reopened goal), the next tick moves the command
  to the background as usual instead of blocking the agent to its timeout.
- Environment (`command_env`): `PYTHONUNBUFFERED=1` (the output so far is live), `MSBUILDDISABLENODEREUSE=1` unless
  set (MSBuild nodes would become jobs), the thread caps of numeric libraries (`OMP_NUM_THREADS`, `MKL_NUM_THREADS`,
  `OPENBLAS_NUM_THREADS`, `NUMEXPR_NUM_THREADS`, `VECLIB_MAXIMUM_THREADS`, `RAYON_NUM_THREADS`) unless set:
  `cpu_threads()` = `[shell] cpu_threads` ("auto": the cores divided by the active AI agents, at least 1; N; 0 = no
  cap); plus a random `DREAMTEAM_PROCESS_TOKEN`.
- Outcomes (`ShellOutcome.state`; first match wins; the first line of the result always carries the outcome and the
  job, because the TUI shows first lines):

  | state | when | first line |
  |---|---|---|
  | `exited` | the shell exited within the wait and the tree emptied within LEFTOVER_GRACE (1 s) | `exit code: N [shell]` (as before) |
  | `quick` | the same, with `background` | `exit code: N [shell] (it ended within 2 s, so it did not stay in the background)` |
  | `detached` | the shell exited, processes run on, a job is allowed (without `background`) | `exit code: N [shell]; processes it started keep running as background job jN` |
  | `leftovers_killed` | the same, no job allowed (they are killed; in wrap-up mode note `wrap_up_left`) | `exit code: N [shell]; the processes it left running were stopped` |
  | `started` | `background`: the shell still runs after the wait, or it exited and processes it started run on | `STARTED in the background as job jN [shell]` |
  | `background` | the shell still runs when the wait ended | `STILL RUNNING after Ws: continues in the background as job jN [shell]` |
  | `timeout` | the same, but killed: `background_on_timeout` off (exactly the old result), wrap-up mode (note `wrap_up`), the wait is `max_timeout` or more (note `no_time`), or a limit at the end of the call's own timeout (note `limit_member`, `limit_session`, `disabled`) | `TIMEOUT after Ws (process tree killed) [shell]` |
  | `killed` | the session stopped or the member left while the command ran in the foreground | `KILLED: the session is stopping [shell]` / `KILLED: you are leaving the swarm [shell]` |

  Then the output (`(no output)`, or `(no output yet)` for a job), and for `leftovers_killed` / `timeout` a
  `[note]`, for a job a footer. The footer of `background`, `started` and `detached` says where the output goes,
  that the owner is notified at the end (so: no polling, other work or `wait`; `wait_for_events` for an external
  member, which has no `wait`: `has_wait`), how to see and stop the job and its lifetime. For a caller with the
  `jobs` tool (`ToolContext.has_tool`) it names `jobs` with job_id and `job_stop` (and for `background`: "start
  servers and long runs with background=true instead"); for a revision-1 agent it names the console commands ("run
  the shell command jobs jN / kill jN (each as a command of its own)") and `&` / `Start-Process -NoNewWindow` (a
  plain `Start-Process` opens a console window of its own on the user's desktop and its output never reaches the
  job), never a tool it lacks. `render()` never cuts the first line or the footer: only the output between them is
  cut to `max_output`.
- A caller cancelled before its command became a job: the tree is killed and awaited (KILL_WAIT), the cancellation
  goes on; any other failure kills it too. After the adoption the caller's cancellation never touches the job.

**Wrap-up mode** (the user's `Swarm.wrap_up`, §3; `JobManager.wrapping_up(actor)`: true for every member, AI agent
or external, never for the user): no command becomes a NEW job. `background=true` is refused before anything starts
(the error above); a foreground command still running at its timeout is killed then, like `background_on_timeout`
off, with the note `wrap_up` ("It was not moved to the background: the user asked the team to wrap up, so no new
background jobs are started. Finish your task with the results you have and say what is left."), an AI agent waiting
up to its own `timeout`, not `foreground_max`; processes a command leaves running after its shell exited are killed
at its end (`leftovers_killed`, note `wrap_up_left`: "They were not kept as a background job: …"), except for a
`background=true` call accepted before the mode began. Jobs started before keep running (their owners may
`job_stop` them; the monitor's `_command_done` of such a job still keeps its processes). Switched off, the usual rules
apply at once.

**Adoption** (`_adopt`, synchronous, so the limit check and the INSERT are atomic) is allowed when
`max_jobs_per_member > 0` and `max_jobs > 0`, the owner's running jobs < `max_jobs_per_member` (4), the session's
< `max_jobs` (16), the session is not stopping, and the owner is a member not being retired. The job keeps a head of
2000 and a tail of 16000 characters in memory (`JOB_HEAD_CHARS`, `JOB_TAIL_CHARS`); `job.started` is emitted.

**Log**: `.scratch/<slug>/jobs/j<N>.log` (UTF-8; readable with `read_file`, never committed, collapsed in
`list_files`): the header `# job jN of <owner> [<shell>]: <command>` and `# started HH:MM:SS, in the background since
HH:MM:SS (<origin phrase>)`, the output so far (its head and tail when it was cut), then the output as it comes;
`# command ended HH:MM:SS: exit code N; still running: <names>`; the footer `# ended HH:MM:SS: exit code N`, `# ended
HH:MM:SS: all its processes exited` (a job whose shell had ended before it: detached, or `background=true` whose shell
exited within the grace) or `# stopped HH:MM:SS (<reason>)`. At half of `job_log_mb` MiB (at
most 64) the file is renamed to `j<N>.prev.log` and a new one starts with `[earlier output: jN.prev.log]`; while
someone holds it open (Windows), the rename is retried at every chunk, and at the full size the log ends with `[log
limit reached: further output is not saved]` (the pipe is still drained). When `.scratch` is not a plain folder of
the workspace (a file, a link elsewhere) the log is `<data_dir>/jobs/j<N>.log`, shown as `<path> (outside the
workspace: read it with shell)`. At each adoption the owner's ended logs beyond `JOB_LOGS_KEPT` (20) are deleted (both
files), chosen by rows, never by listing the folder (two names may share a slug); a file in use is skipped.

**The monitor**: one task, started at an adoption and ended when no job is left; every `JOB_TICK` (1 s) it checks all
jobs in one `alive_all` pass: an empty tree → the job ended; a timeout/background job whose shell exited while its
tree lives (seen for LEFTOVER_GRACE) → `_command_done`; past its lifetime → stopped with reason `lifetime`.

**The shell exited, processes run on** (`_command_done`, once per job): a build that left a daemon is done, so its
owner must not wait for the daemon. The shell's code is stored, the log line written, the origin becomes `detached`
(never busy, lifetime `job_max_minutes`), the change is committed (kind `job jN`, detail `<command>\n\njN: the command
ended with exit code C; processes it started still run: <names>`), and the owner gets `n_job_command_done` ("The
command of your background job j3 has finished (exit code 0) after 4 min, but programs it started still run, so j3
goes on: java.exe. …": the job has not ended, so the notice never says it has; it wakes unless the goal is done or
the session stops).

**The end** (`_finish`): exactly one finisher per job. The monitor, `stop`, `stop_owner` and `stop_all` claim it
synchronously (`job.finishing`); any other ender awaits `job.done`; the manager never cancels a future a caller
awaits. The finisher waits for EOF (READ_SETTLE), writes the footer, closes the log, releases the tree, sets
`exit_code` (the code of the job's own end: its shell's, when the shell ended by itself as the last of the job; NULL
when it was stopped, or when its shell had ended before it — detached, `background=true` whose shell exited within
the grace, after `_command_done` — since that shell's code was reported when it ended, and a crash of what ran on
must not read as "exit code 0"; while such a job runs, its row holds the shell's code), updates the row (status,
reason, stopped_by, exit_code, ended_at, tail = the last 2000 characters, processes, origin), drops the job, emits
`job.ended` {job: public + `duration`}, commits unless the session stops (kind `job jN`, detail `<command>\n\njN: <how
it ended>`, e.g. "ended with exit code 0 after 4 min"; while the session stops the job is left to `stop_all`'s
commit), then notifies the owner when it is still a member:

| how it ended | row: status, reason, stopped_by | the owner's notice | wakes |
|---|---|---|---|
| by itself | `ended` | `n_job_done` {id, result: `job_exit` {code}, or `job_gone` when it has no code of its own, duration, command, tail, log} | `wakes(job)` |
| its shell exited, processes run on (`_command_done`) | still `running`, origin `detached` | `n_job_command_done` {id, result: `job_exit` {code}, duration, names, command, tail, log} | unless the goal is done |
| the user stopped it | `stopped`, `user`, `user` | `n_job_stopped` {id, duration, command, log} | `wakes(job)` |
| its owner stopped it | `stopped`, `owner`, the owner | none (the tool result says it) | — |
| its lifetime | `stopped`, `lifetime` | `n_job_limit` {id, duration, command, log} | `wakes(job)` |
| the session stopped | `stopped`, `session` | `n_job_gone` {id, command, log} | quiet |
| its owner retired | `stopped`, `retired` | none | — |
| lost after a crash (`recover`) | `lost`, `crash` | `n_job_gone` | quiet |

Only the owner or the user can stop a job. `{duration}` (`fmt_duration`: "45 s", "12 min", "2 h 05 min"; ru "45 с",
"12 мин", "2 ч 05 мин") and `{result}` are built in the owner's language; `{tail}` = the last 15 lines, at most 1200
characters, each indented by two spaces, or `job_no_output`; `{command}` = `short_command(command, 120)`; `{log}` =
the log as shown above. These texts reach agents of every tool revision, so they name no tool, only the log. A
paused swarm holds them until `resume` like any notice; an external member gets them through `wait_for_events`.

**Stop** — `async stop(job_id, by, *, force=False) -> str`, synchronous (a restart on the same port does not race):
only the owner or USER (else ToolError `Job j3 belongs to Anna Smith; only its owner or the user can stop it (ask them
with send_message).`); terminate, `STOP_GRACE` (3 s; none with `force`), kill, then the job is finished. Result:
`Stopped job j3 (<command>) of Anna Smith after 25 min, together with the processes it started. Last output:` and up
to 15 lines; a job that had ended (or ended meanwhile) is no error: `Job j3 had already ended (<how it ended>);
nothing to stop.`; an unknown id: ToolError `There is no job j9. Running jobs: j3, j5.` (or `No jobs are running.`).

**Views**: `listing(actor)` (the `jobs` tool without job_id): `Running background jobs (N):` with lines `- j3 <command
(60)>: Anna Smith (you), running 25 min, started in the background; log <log>` (the log only for the caller's own
jobs, every log for the user), or `No background jobs are running.`, then `Your jobs that ended in the last hour:`
(everyone's for the user: `Jobs that ended in the last hour:`) with lines `- j2 npm test: exit code 0 after 4 min,
ended 10 min ago`. Origin phrases: `went to the background at its timeout`, `started in the background`, `left running
by its command`. `describe(job_id, *, actor, lines=40, max_chars=12000)` (any member may read any job): `Job j3 of Anna
Smith: <command>`, `State: running for 25 min (started in the background) [bash]`, or `ended with exit code 0 after 4
min; processes it started still run: java.exe` after `_command_done`, or how it ended (`ended with exit code 1 after
4 min`, `ended after 4 min (all its processes exited)` for a job with no code of its own, `stopped by the user after
3 min`, `stopped by its owner
after …`, `stopped at its time limit after …`, `stopped with the session`, `stopped when Anna Smith left the swarm`,
each followed by `, 6 min ago`; `lost: the session's process ended unexpectedly`), `Processes: node.exe ×2` (a running
job), `Log: <log> (2.1 MB; below is its end)` and `--- last N lines ---` (1–400 lines, at most max_chars): a live
job from memory, an ended one from the end of its log file, or, when the file is gone (a `/cd` move, a deleted
scratch folder), the stored tail with `(log file not found)`.
`snapshot()`: the running jobs (from memory), then those ended in the last hour (one query, at most 50).
`get(job_id)`, `running_of(owner=None)`, `check_start(actor)`, `busy(name)`, `wakes(job)`, `awaited(owner)`.
Public job dict (snapshot, events): `{"id", "label": "j3", "owner", "command" (≤ 200 chars), "origin", "status",
"reason", "stopped_by", "exit_code", "started_at", "background_at", "ended_at", "log", "processes",
"last_output_at", "silent_since"}`.

**Silent jobs** (run 006: a job ran over an hour without output): `Capture.last_output_at` is the time bytes last
arrived (set by the reader thread in `feed`, no thread of its own). A running job whose last output (or, when it never
printed, its start) is more than `SILENT_MINUTES` (10, a constant) ago is silent: `Job.silent_for(now)` (seconds, else
None), `silent_phrase(lang)` `no output for 40 min` / «40 мин без вывода» (`job_silent`, `fmt_duration`). Not once
its output pipe reached EOF (`cmd.eof`: what its shell left running prints elsewhere or nowhere, e.g. a detached
server, and cannot print here, so the marker would only invite stopping it). It shows in
the `listing` line (`…, started in the background, no output for 40 min; log …`), the `describe` state (`running for
50 min (started in the background); no output for 40 min`), the `swarm_status` / `goal_complete` job list
(`j3 Anna Smith: npm run dev, 50 min, no output for 40 min`), its owner's `my_jobs` (§6.3), the public dict
(`silent_since`: when it last printed or started; None when not silent) and so the UI's Jobs tab and the TUI's
`/jobs`. The monitor publishes a live-only `job.silent` {job} each time a job turns silent or prints again, so the UI
reloads the state; no notice, no automatic stop: its owner (or the user) decides.

**Console intercept** — `async console(actor, command, *, max_chars=12000) -> str | None`: how revision-1 agents
list and stop jobs with the `shell` tool they have. The command is stripped and one trailing `;` dropped; the WHOLE
line must match (case-sensitive), else None and the command goes to the real shell (`kill j3; npm start`, `jobs |
wc -l`, `echo kill j3` all do):

| command line | answer |
|---|---|
| `jobs`, `jobs -l`, `jobs -p` | `listing(actor)` |
| `jobs [-l] <id>` (`j3`, `%3` or `3`) | `describe(id)`, 40 lines |
| `kill [-9 \| -KILL \| -SIGKILL \| -s KILL \| -s SIGKILL \| -15 \| -TERM \| -SIGTERM \| -s TERM] <id> [<id> …]` (`j3` or `%3`, never a bare number: that is a PID) | `stop(id, actor)` for each, joined by newlines; a KILL form skips the TERM grace |

The first line of every answer (and of its error) ends with ` [dreamteam]`; errors arrive as `Error: …` like any
tool error. Nothing is spawned or committed.

**Tools** (§12, audience LEU): `jobs(job_id?, tail?)` and `job_stop(job_id)`; `job_id` accepts `j3`, `J3`, `%3`,
`#3`, `3` (`parse_job_id`), else ToolError `Not a job id: '<x>'. Pass one such as j3 (jobs without job_id lists
them).`; a blank job_id lists. The user lists and stops every member's jobs through them (`POST /api/act/jobs`,
`/api/act/job_stop`, MCP).

**Lifecycle**:

| event | jobs |
|---|---|
| `Swarm.stop` (session stop, delete, `/cd` move, launcher shutdown, `dreamteam run` end, stdio `mcp` end, the end of print mode with an owned launcher, a failed start) | `stop_all()`: no new command or job (`closing`), the monitor cancelled; every job tree and every foreground tree terminated, after `STOP_ALL_TERM` (3 s) the survivors killed and awaited up to `STOP_ALL_KILL` (2 s); every job finished as `stopped`/`session` (quiet `n_job_gone`, no commit of its own), every finish, stop and foreground call in flight awaited; then one autocommit per owner of its stopped jobs and of its jobs that ended by themselves since the session began to stop (`Swarm.stop` sets `stopping` first and stops the runtimes before `stop_all`: such an end makes no commit of its own), kind `job j3` or `jobs j3, j4, j5`, detail `Stopped with the session:\nj3: <command>…` and `Ended while the session stopped:\nj4: <command> (<how it ended>)…`; every tree released, every log closed. About 5 s plus git; idempotent; nothing touches git or the database after it returned (`hub_call` also skips its trace while the swarm stops, §13). `Swarm.stop` runs it in `finally`, also after a failed step |
| `Swarm.retire` | §3 `retire`: `forbid`, then the runtime, then `stop_owner(name, "retired")` |
| `Swarm.start` | `recover()` (below) |
| pause | nothing: a pause stops LLM spending only |
| `complete_goal` | the jobs go on; USER gets `u_jobs_running`; their later ends are quiet |
| `reopen_goal` | nothing (the goal is active again, so ends wake again) |
| `reconfigure` | nothing: the limits are read at each use; turning `background_on_timeout` off kills no job |
| an external member disconnects | nothing: its lifetime, the user, its retirement or the session stop end its jobs |
| compaction | nothing |
| `docker stop` | SIGTERM reaches dreamteam through the container's init (§18.3 `sandbox`), `Swarm.stop` ends the jobs; what is left dies with the container |

**Crash recovery**: when the hub dies, Windows closes its job handles (KILL_ON_JOB_CLOSE kills every tree), POSIX
watchdogs see EOF and kill their groups, a container stops. At the next `Swarm.start`, `recover()` turns every row
still `running` into `lost`/`crash` (ended_at = now); on Linux, when the row's `hub_pid` is dead or is this process,
every own-user process carrying its token is killed (identity from a random token, never from a bare pid); each owner
still a member gets a quiet `n_job_gone`. View-only loads (`Swarm(...)` without `start`, `cli status`, the launcher
viewing a stopped session) change nothing. Sessions never started again are cleaned by the OS containment alone.

**Liveness**: `busy(name)` is true while the member has a running `timeout` job, or a `background` job younger than
`max_timeout` while it holds an in_progress task or a review (the board is queried only then); `detached` jobs never
make it busy. A busy member is not idle for the scheduler (§3): no `n_ready`, no stalled wake, no review reminder or
hand-over (its reminder time is renewed while busy), it holds `n_all_done` back and counts as working for
`u_reviews_waiting`; `AgentRuntime` skips its in-activation nudge (§6.2). A free member with a server started with
`background=true` is still offered ready tasks. A member that is not busy but whose running job's end will wake it
(`awaited`: a detached job while it holds an in_progress task, a server past its busy window) waits for that job:
the stalled wakes (`_wake_stalled`, `_wake_idle_holders`, §3) leave it alone meanwhile.

**Git**: the caller's `shell` autocommit at every return, also when the command went on as a job (not for
`killed`); the job's own commits under its owner when its shell exits with processes left, at its end and at the
session stop (above; a job that ends by itself while the session stops is committed by `stop_all`, never left to
another member's next commit). Every job commit passes `hold=True`, like `shell` (§7 "Held new files"): changes to
tracked files are committed at once, new files once they outlive one more command. Changes to tracked files a job
makes between those commits go into the next autocommit of any member, under that member's name (git cannot tell
writers apart); a new file is held with that member as its owner and waits for the job's owner too (it had a job
running): build output belongs in `.scratch/` or `.gitignore`. Job logs are never
committed. A job's own git commands carry its owner's identity (`agent_git_env`).

Settings: `[shell]` in §17.1; all of them are read at each use.

### 7.2 Sessions without network (netoff.py)
`[swarm] network = "on" | "off"` (§17.1; a session override like the others, so it is set at creation and in the
session's ⚙ and applies at once; `--network off` of `tui`, `serve --goal`, `run` and `sandbox`, §18.3;
`session_create` `network`, §18.2). With "off" the members' commands (`JobManager.run`: the command, and so the job
it may become) and the stdio MCP servers DreamTeam starts (`MCPForge._connect` / `_smoke_test`) lose the network;
DreamTeam itself keeps it (the LLM calls, the hub's port, the user's skill installs). The isolation is the best the
machine has, detected once per process (`netoff.isolation()`, cached; `Swarm.start` and the launcher's
`GET /api/settings` call it off the loop):
```
"docker"  DREAMTEAM_OFFLINE_USER = "UID:GID" set (only by sandbox_entry, below; never 0), euid 0, setpriv present:
          argv prefix  setpriv --reuid=UID --regid=GID --clear-groups --no-new-privs --
"netns"   Linux, the namespace made (below) and `<python> -I netns_helper.py --join ... --probe` printed "ok":
          argv prefix  <python> -I netns_helper.py --join /proc/<pid>/fd/U /proc/<pid>/fd/N UID GID --
"env"     anything else (Windows, macOS, Linux without user namespaces, a container without the firewall): no prefix
```
"netns" is one network namespace per DreamTeam process, shared by all its offline commands, jobs and MCP servers:
a dev server one command starts as a job is reachable from the next command, its tests and an MCP server, as the
machine's localhost would be (the sessions without network of one hub share it, as they share that localhost with
network); the machine's own localhost (the hub's port, a database the user runs) is not reachable from it. At the
detection a holder `unshare -rn <python> -I netns_helper.py --hold` brings the new namespace's loopback up and says
"ok"; DreamTeam opens its `/proc/<holder>/ns/{user,net}` (fds U, N, kept for the process's lifetime: an agent's
`pkill python` cannot take the namespace away) and closes the holder's stdin, at which it exits. `--join` opens both
fds through `/proc/<DreamTeam's pid>/fd/`, then `setns` the user namespace (its owner, this user, has every capability
there) and the network one, and then makes a nested user namespace in which the real uid/gid are themselves again
(files keep their owner, tools do not run as root, no capabilities). A `--join` that cannot enter the namespace exits
126 without running the command (it would have the network).
Every wrapper execs the shell (same pid: its process group, Job Object, token sweep and watchdog are unchanged).
`netoff.environment(env)` is set in every mode (pip or npm then fail at once instead of retrying): `HTTP(S)_PROXY`,
`ALL_PROXY`, `FTP_PROXY` and their lowercase forms = `http://127.0.0.1:9` (the closed discard port),
`NO_PROXY`/`no_proxy` = `localhost,127.0.0.1,::1` (loopback works in every mode: local servers and their tests),
`PIP_NO_INDEX=1`, `PIP_RETRIES=0`, `UV_OFFLINE=1`, `npm_config_offline=true`, `YARN_ENABLE_OFFLINE_MODE=1`,
`CARGO_NET_OFFLINE=true`, `GOPROXY=off`, `HF_HUB_OFFLINE=1`, `CONDA_OFFLINE=true`, `DREAMTEAM_NETWORK=off`, and git
through `GIT_CONFIG_COUNT` (appended after inherited entries; git 2.31+): `protocol.{http,https,git,ssh}.allow=never`
and `http.proxy` = the dead proxy (local paths and file:// still clone). "docker" also sets `HOME`, `USER`, `LOGNAME`
of the offline user and `UV_CACHE_DIR=$HOME/.cache/uv` (root's home and the skills' cache are not its to write).
"env" is not isolation, and the UI says so: Windows PowerShell 5.1's web cmdlets ignore proxy variables, a raw
socket ignores all of them, and the hub's HTTP API on localhost stays reachable (an agent could switch its session's
network back on through it; "docker" rejects the hub's ports, "netns" does not reach them). `netoff.shown(kind)` is
what the UI and the snapshot show: "env" in a container (`/.dockerenv`, `/run/.containerenv`) is "container", whose
hint says to restart `dreamteam sandbox` (it then adds the firewall, `docker.wants_firewall`).
- **Docker sandbox.** The image (§18.3) has `iptables` and the user `dreamteam-agent` (uid 10001, its own group);
  git's settings are `--system`, so they hold for both users. The offline commands run as `netoff.sandbox_ids()`:
  the owner of `/swarm` when it is not root (a bind mount on a Linux host: the agents' files are the user's own, and
  nothing of the user's is opened to another id), else `dreamteam-agent` (Docker Desktop shows a bind mount as root's;
  a named volume is root's); the image's home of `dreamteam-agent` is given to those ids. `docker.wants_firewall` is true for `--network off`,
  `[swarm] network = "off"` in the home's settings, or any `sessions/*/session.json` with that override; then
  `sandbox_command(..., firewall=True)` adds `--cap-add NET_ADMIN -e DREAMTEAM_OFFLINE_FIREWALL=1`, and an image whose
  `dreamteam.dockerfile` label is not `dockerfile_hash()` (sha256 of DOCKERFILE, 16 hex; `build_image` sets it) is
  rebuilt first (without the firewall only a note suggests `--build`). In the container `cli.main` calls
  `netoff.sandbox_entry()` before anything else: as root it adds the chain `DREAMTEAM_OFFLINE` (iptables: reject
  TCP to the hub's ports, `netoff.hub_ports(argv)` = CONTAINER_PORT and a `--port` of the command, on any address
  (the container's own IP goes through `lo` too: its HTTP API would let a command switch the session's network back
  on); reject Docker's DNS server 127.0.0.11; accept `-o lo`; reject the rest; ip6tables the same without the DNS
  rule, skipped when the container has no non-loopback IPv6 address), jumped to from `OUTPUT -m owner --uid-owner
  UID`. A chain whose jump is in place already (`iptables -C`) is kept: `docker exec <container> dreamteam ...` runs
  the entry again (DREAMTEAM_OFFLINE_FIREWALL stays in the container's environment) and must not warn. Then it sets
  `DREAMTEAM_OFFLINE_USER=UID:GID`, umask 002, and execs `setpriv --bounding-set=-net_admin --regid=GID --keep-groups
  -- python -m dreamteam ARGS`: the hub never keeps NET_ADMIN, and it runs as root with the offline group, so what it
  creates (files of `write_file`, `.git`) is the commands' to write without a set-group-id bit on any folder. A
  failure (an old image, no owner match) prints a warning, and the sessions without network get "env".
  `Swarm._prepare_offline` (at start, and when the setting turns "off") runs `netoff.prepare_workspace(root)` for
  "docker": an entry root owns (or the image's `dreamteam-agent`, of an earlier run) gets the group GID with the
  owner's bits copied to the group (a read-only entry, e.g. the skills mirror, stays read-only); what UID owns already
  and what anyone else owns is left as it is. It walks by file descriptors (`os.fwalk`, each entry opened with
  `O_NOFOLLOW`, then `fchown`/`fchmod`): the members' commands may run meanwhile, and a file swapped for a symlink
  must not get root's chmod on its target. A session switched off later in a container started without the firewall
  gets "env" (shown "container") until the sandbox restarts. `DREAMTEAM_OFFLINE_USER` and
  `DREAMTEAM_OFFLINE_FIREWALL` never reach the agents' environment (`shell._INTERNAL_ENV`).
- **MCP servers.** Their command and environment go through `netoff.command`. A command that names a URL of another
  machine (`netoff.remote_url`: http(s)/ws(s), not localhost, 127.x, ::1 or *.localhost; a stdio bridge such as
  `npx mcp-remote https://...`) is refused with a ToolError before it starts. A change of the setting closes the
  running connections (`MCPForge.close_connections`): each starts again with the new setting at its next use. Each
  connection records the setting it was started with (`MCPConnection.offline`): `_open` never hands out one of the
  other setting, and `_connect` starts a server again when the setting changed while it was starting (once; a second
  change is a ToolError to call again).
- **Skills.** `EnvManager.plan(..., network=False)` gives a member's proposal (`frm == "agent"`) no install: state
  `off`, reason `network` (`deps_reason_network`, UI `sk_reason_network`), unless its environment is ready on disk
  already; the plan key includes the setting, and a change replans (`SessionSkills.replan`). The user's own skills
  install as before: DreamTeam installs them, not an agent.
- **Git.** The workspace's own git commands (`Workspace._git_argv`) get `-c core.hooksPath=<os.devnull>` and `-c
  core.fsmonitor=false`: hooks and an fsmonitor command are commands the members may have written (`pre-commit
  install`, `git config core.fsmonitor ...`) that git would run with DreamTeam's network (`commit.gpgsign=false` is
  set always, `git_argv`). Other commands an agent can name in `.git/config` for git to run (filter, textconv and merge
  drivers with a matching `.gitattributes`) are not covered.
- **Telling the members.** `render_wake` (the wake-up and the incoming news) first calls `Swarm._tell_network(name)`:
  the member's last `n_network_off` / `n_network_on` notification is the state it knows; when that differs from the
  setting, a quiet one (wake=False) is added. Each member is told once, also across restarts, and again when the user
  switches it back; no system prompt changes. `swarm_status` adds `; NO NETWORK for the members' commands, jobs and
  MCP servers (<isolation>)` to the state.
- **A change while running** (`Swarm.reconfigure` → `_network_changed`): new commands get the new setting at once;
  running jobs keep the one they started with.
- The snapshot's `swarm.network` is `netoff.describe(config)` = {"mode": "on"|"off", "isolation": kind or null (not
  detected yet)}; the session summary has `network` ("on"/"off"); the launcher's `GET /api/settings` has
  `network_isolation`.

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
mcp.created mcp.status swarm.paused swarm.resumed swarm.wrap_up swarm.limits job.started job.ended
skill.attached skill.detached skill.proposed skill.library skill.deps`

`skill.attached` {name, from, files, replaced: bool, via, attached_via} and `skill.detached` {name, via}, actor = the
user (§19); `via` is the surface (`act`, `mcp`, `launcher-mcp`; `role` for the skills a role brings), `attached_via`
`user`, `role:<Role>` or `create`. `skill.proposed` {name, owner, files, replaced: bool}, actor = the member who
proposed it; `skill.library` {name, owner, status: saved or rejected, comment, via}, actor = the user (a decision
on a stopped session's files emits none). The feeds (web, TUI) name the owner of a decided proposal ("saved Ann
Lee's skill x to the library") and the surface of a skill change an MCP client made ("(via MCP)", "(via the launcher
MCP)"; the user's own surfaces `act`, `home`, `cli`, `role`, `create` are not named): a save no UI click made shows
as such. `skill.deps` {name, ecosystem: python | node, status: installing | ready |
failed, files, env, key, installer?, reused: bool, seconds?, error?}, actor None: a skill's script dependencies,
emitted by every session that has the skill when a build really starts and when it ends (§19 "Script
dependencies").

`swarm.wrap_up` {on: bool}, actor = who switched it (the user, or the actor of the set_goal / reopen_goal that ended
it).

`job.started` {job} and `job.ended` {job: … + `duration` (seconds)} carry the public job dict (§7.1), actor = the
job's owner; a job's output is never sent as events. `agent.retired` also carries `jobs` (the ids of the jobs it
stopped), `agent.compacted` `tools_revision` (§6.1).

Live-only events (sent with the EventBus method `publish(type, actor, **data)` instead of `emit`: delivered to
listeners and WebSocket subscribers with id None, not journaled): `trace.started`, `trace.span`,
`trace.finished` (§16), `job.silent` {job} (a job turned silent or printed again, §7.1).

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
    async def complete(self, system: str, messages: list[dict], tools: list[dict], *,
                       max_tokens: int | None = None) -> LLMResponse
        # tools: [{"name","description","parameters"}]; stop_reason normalised to
        # "end_turn" | "tool_use" | "max_tokens" | "refusal" | "other"; max_tokens: this request's output limit
        # (None = profile.max_tokens; the agent passes it only to raise the limit after a cut-off reply, §6.2, and
        # only to a client whose complete() takes it, `accepts_max_tokens`). A raised limit also gets a request
        # timeout of its own (the client's timeout is derived from profile.max_tokens)
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
    async def start(self) -> None     # register config.mcp.external servers (origin "config"); no processes yet;
                                      # refresh the stored cwd of forged/imported servers (server_dir: the
                                      # workspace may have moved, §17.2)
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
  the padding and right borders of rich's boxed tracebacks. In a session without network the command and the
  environment go through `netoff.command`, and a command naming a remote URL is refused (§7.2).
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
    via: str = "act"                         # the surface: "act" (web /api/act: web UI, TUI, console), "mcp"
                                             # (hub_call), "launcher-mcp"; recorded where the user's changes are
                                             # audited (skills, §19)
    @property kind -> str                    # swarm.kind_of(actor), "unknown" for a non-member
    @property output_cap -> int              # max chars of big outputs (shell, MCP results): with a runtime
                                             # (it cuts results for the model and traces them in full)
                                             # max(tool_output_chars, tracing.max_payload_chars), else
                                             # agents.tool_output_chars
    def has_tool(self, name: str) -> bool    # the caller has that tool: an LLM agent's own frozen set
                                             # (runtime.tool_names, §6.1), else the live registry for ctx.kind;
                                             # results that point at a tool are tailored with it
REGISTRY: dict[str, ToolSpec]               # sorted by `order` once all modules are loaded (import-order free)
def tool(name, en, ru, properties, required=(), audience=("llm","external","user"), allow_empty=()): decorator
def tools_for(kind: str) -> list[ToolSpec]
def schemas_for(kind: str, language: str) -> list[dict]   # [{"name","description","parameters"}], deterministic copies
async def call_tool(ctx: ToolContext, name: str, args: dict) -> tuple[str, bool]
    # rejects actors that are neither USER nor an active member; an LLM agent (a runtime with
    # tool_parameters) calls only the tools of its frozen set, with their frozen parameters: a tool it lacks is
    # "Unknown tool" with close matches and the list from its own set only (never a newer tool), and arguments
    # its schema lacks are dropped with the note "[Ignored unknown argument(s): ...; Usage: <its frozen
    # signature>]"; validation still uses the registry spec (hence R0′ below); unknown tool -> close matches + the
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
| `agents` | — (the roster, then the role catalog) | LEU |
| `hire_agent` | role, instructions ("" for a catalog role), model?, name? | LEU |
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
| `shell` | command, timeout?, background? (its environment: §7 after `scrubbed_env`; results and jobs: §7.1) | LE |
| `jobs` | job_id?, tail? (without job_id: the running jobs of the swarm and the caller's ended in the last hour, everyone's for the user; with it: that job, §7.1) | LEU |
| `job_stop` | job_id (only the job's owner or the user) | LEU |
| `mcp_create` | name, description, code, scope?, task_id?, requirements? | LE |
| `mcp_list` | query? | LEU |
| `mcp_tools` | server | LEU |
| `mcp_call` | server, tool, arguments? | LE |
| `join_swarm` | role, name?, instructions? | EU (the user via MCP only, before joining; creates kind "external") |
| `wait_for_events` | timeout? (s, default 50, max 600; an error at once when the swarm stops meanwhile), progress? (below) | EU |
| `set_goal` | title (a multi-line title is split: §3 `set_goal`), description? | U |
| `swarm_status` | brief? (the goal as one line) | EU |
| `swarm_control` | action (pause/resume), wrap_up? (true/false: the user's wrap-up mode, §3), max_cost_usd?, max_agents?, reopen_goal_comment? | U |
| `mcp_approve` | name, approve, comment? | U |
| `agent_traces` | agent?, trace_id?, span_id?, context?, status?, task?, limit?, offset? | EU (§16.2) |
| `skills` | available? (also the library's and Claude Code's skills not attached) | U (§19) |
| `skill_attach` | name? (a reference: `x`, `library:x`, `claude-user:x`, `claude-project:x`) or source? (a folder or .zip path on the hub's machine, or a Git URL: imported into the library first), pick?, replace? | U (§19) |
| `skill_detach` | name | U (§19) |
| `skill_approve` | name, approve, comment?, replace? (a member's proposal: saved to the skills library or not) | U (§19) |
| `skill_reinstall` | name, ecosystem? (`python` or `node`; default both: the skill's packages installed again) | U (§19) |
| `skill_propose` | path (the skill's folder, or its SKILL.md, in the workspace) | LE (revision 3, §19) |

Tool results are plain text (compact, line-oriented), language = English for data, ToolError texts in
English. The `wait` tool calls `ctx.runtime.end_turn()`. `send_message` calls
`ctx.runtime.note_sent_to_user()` when USER is a recipient.

**R0′: LLM tool schemas are released as revisions.** Every LLM agent sends its own frozen tools array (§6.1), and
Anthropic thinking blocks bind to the exact prefix, so a changed name, description, parameter, `required` list, enum
or order of a tool with the `llm` audience is released as a new revision, in one change:
1. change the code;
2. bump `frozen.TOOLSET_REVISION`;
3. run `python -m dreamteam.tools.frozen --write` (it writes `tools/snapshots/llm_r<N>.json`);
4. pin its exact-order digests (`sha256(db.dumps(tools))` per language) in tests/test_frozen_tools.py (`RELEASED`).

Existing agents keep their revision until their next compaction; new agents get the current one. Arguments are
still validated against the current registry, so the registry must keep serving every released revision
(tests/test_frozen_tools.py checks it): no tool leaves the `llm` audience, no property is removed or retyped
(`items.type` included), the current `required` is a subset of every released one, enums only grow, a free property
never gains an enum. Revisions: 1 = the tools before background jobs (sorted digest `e3c5cbc7…`, the baseline of
agents created before tool sets were frozen), 2 = background jobs (`shell` gains `background`, new `jobs` and
`job_stop`, new `shell` and `wait` descriptions), 3 = `skill_propose` (appended last; the array of revision 2
before it is unchanged). Notifications and wake-up texts reach agents of every revision, so
they name only tools of every released revision (tests/test_prompts.py); tool results that point at a newer tool
check `ToolContext.has_tool`. `allow_empty` is validation only, not part of the schema. Tools of the
`user`/`external` audiences only (e.g. `set_goal`) are not in the LLM tools array, and external members and the user
always see the live registry.

Results and checks of the review and role-catalog flows:
- `task_claim` by the task's designated reviewer (independent review, §4.1) adds "You were its reviewer; since
  you execute it now, another member will review it."
- `task_complete` says where the task went: `Task #N is done.`; `Task #N was sent to X for review.` (the task's
  own reviewer); `... was sent to the user for review: nobody else in the team can review it. You will be told the
  verdict; meanwhile take the next task or call wait.`; or `... was sent to X for review: a member other than you
  checks it, and you will be told the verdict.` (picked by the board). Once the board accepted the call (a refused
  one commits nothing) it commits the caller's held new files (`workspace.commit_held`, §7 "Held new files").
- Dependencies (§4 `update`): `task_get` of an open task (not in review) the caller holds while it is blocked adds `You hold it while
  it is blocked: if its result can be produced without some of #3, #4, remove those with task_update (depends_on =
  the ones still needed) and say why in its discussion.`; `task_update` in which an assignee that did not create the
  task removed dependencies adds `Its creator (X) was told that you removed #3, and the discussion records it.` (or
  `The discussion records that you removed #3.` when the creator is the user or has left) and, without a `comment`,
  ` Say there why the task can do without them (task_comment).`
- Likely duplicates (run 006: three agents created three alike tasks for one message to all): `task_create` of an
  agent or an external member (not the user) still creates the task, and when open tasks look like it its result
  adds, in the caller's language (`similar_tasks`), `Similar open tasks: #33 "Fix the parser" [in_progress, Anna
  Smith], #34 "..." [todo]. If yours duplicates one of them, cancel it (`task_update` status=cancelled) and take or
  comment on that one.` `Board.similar_open(task)`: the open tasks of other creators (a planner splits work into
  alike tasks on purpose; run 006's duplicates came from different members) except the task's parent and the tasks it
  depends on, ranked by `board.similarity` (pure). The titles decide: their words lower-cased, without punctuation,
  numbers, words under 3 letters and an en+ru stop list (generic verbs such as fix/implement/исправить, function
  words), each cut to its first 5 letters; two words match when equal or one (≥ 4 letters) is a prefix of the other;
  the score is the share of the shorter title's words found in the other (overlap coefficient), with ≥ 2 shared
  words. From 0.75 the titles alone decide; from 0.5 the goals must confirm (title + goal still ≥ 0.5 with a shared
  word more); goals never make a match alone (siblings repeat the swarm goal's words in theirs). Tasks told apart by
  a parameter (the tokens with a digit differ: N=100 / N=200, C5, 1e-3), a kind of work (fix / build / verify /
  optimize / measure / research / document / remove / deploy words, en+ru) or a side (lower / upper, staging /
  production, нижн / верхн, ...) score 0. At most 3, best first, then the older. "Fix crash in parser on empty input"
  ~ "Parser crashes on empty input" (1.0); "Benchmark solver on N=100" / "N=200", "Optimize the step function" /
  "Verify the step function", "Compute lower bound for C5" / "upper bound", "Write unit tests for the parser" /
  "for the lexer" (0.67, no goal confirms) and "Implement login page UI" / "Implement login API endpoint" (one shared
  word) are not.
- `goal_complete` (switch on) is refused while tasks are in review: ``Tasks are still waiting for review: #N
  (reviewer), .... The goal can be completed only after every task is reviewed: call `wait` ...``. The "already
  achieved" check comes first.
- `agents` appends, in the caller's language, `Role catalog (hire_agent role=<name>; a catalog role brings its
  description and model profile, and instructions may then be empty; any other name is a new role you describe
  in instructions):` and one line per visible role, `- <name>[ (model <profile>)]: <roles.summary(description)>`.
- `hire_agent` resolves `role` in the catalog (swarm language). `instructions` stays required in the schema
  (R0′), so a missing one is still "Missing required argument(s)"; `""` is accepted (`allow_empty`). An unknown
  role with empty instructions: ``'X' is not in the role catalog: describe the new role's responsibilities in
  instructions, or pick a catalog role (`agents` lists them).`` plus up to three similar catalog roles
  (`roles.close_roles`). Results start with `Hired <name> as "<canonical role>".`, then either `They got the
  catalog description of the role; model profile P.` or `"X" is a new role (not in the role catalog): your
  instructions are all they know about it.`, then `They are woken now: ...`. The user (the board's hire form, the
  TUI, the console: `ctx.is_user`) gets texts about the user's own tools instead: the error says to describe the
  new role in the instructions (`/hire X | instructions` in the terminal) or pick a catalog role (the hire form's
  list, `/roles`), and the result is `Hired <name> as "<role>" (catalog role, model profile P).` or `Hired <name>
  as "<role>": a new role, not in the role catalog; your instructions are all they know about it.`
- `join_swarm` shows the canonical role and, for a catalog role, appends `\nYour role (<role>): <description>`
  (an external member has no system prompt); its description and result say that members who join this way do not
  count toward max_agents.

Results of the team, wait and job flows:
- `agents` starts with `AI agents: 2 of max 6, plus 1 external member (not counted).` and the budget line (the
  same team line in `swarm_status`); a member line ends with `, background jobs: j3, j5` while it has running jobs.
- `swarm_status`: its first line `Swarm "<name>" — running` (or `PAUSED: <reason>`) ends with `; WRAP-UP MODE: the
  agents finish their tasks with what they have, no new computations or background jobs` while the user's wrap-up mode
  is on.
- `swarm_control`: the limits, then `reopen_goal_comment` (which ends wrap-up mode), then `action`, then `wrap_up`;
  `Done: ...` lists what was done: `wrap-up mode on: every agent was told to finish its tasks with what it has, and no
  new background jobs start` / `wrap-up mode was already on` / `wrap-up mode off` / `wrap-up mode was not on`. Nothing
  to do: `Nothing to do: pass action (pause/resume), wrap_up, a limit or reopen_goal_comment.`
- `swarm_status` adds `Background jobs: 2 running (j3 Anna Smith: npm run dev, 25 min; j5 ...).` while jobs run (a
  silent one, §7.1: `j5 Bob: pytest, 50 min, no output for 40 min`),
  and for a member caller `Your background jobs: j3, j5.` after its open tasks. `brief=true` shows the goal as one
  line, `Global goal [active]: <title>` (no description, no summary); the rest is the same.
- `wait` (description: the end of one of its background jobs wakes it too; "End every turn with it" stays): `OK,
  waiting. You will be woken up by new events.`, plus `, also when your background job j3 ends.` / `…jobs j3, j5 end.`
  for the caller's jobs whose end will wake it (`JobManager.awaited`: never one a done goal makes quiet).
- `goal_complete` appends, while jobs run, `Note: background jobs are still running: j3 (Anna Smith: npm run dev, 25
  min). Stop the ones nobody needs with job_stop; the rest stop with the session.` (a caller without `job_stop`: `with
  the shell command kill jN (as a command of its own)`).
- `shell`, `jobs`, `job_stop`: §7.1.
- `wait_for_events(progress=true)` also returns when others make board progress: `task.created`, `task.updated` with
  a status change, `goal.set`, `goal.completed`, `goal.reopened`, `agent.hired` (an external member: "joined"),
  `agent.retired`, `swarm.wrap_up` (`wrap-up mode on (user): finish your tasks with what you have, no new computations
  or background jobs` / `wrap-up mode off (user)`) (the caller's own changes and events about itself are skipped). They are read from the events
  journal after a per-actor cursor (kv `wait_progress:<actor>`; the first such call starts at the journal's end;
  calls without progress neither use nor move it) and appended as `Board progress:` / `Прогресс на доске:` with `• `
  lines (at most 20, then ``… and N more: see `board` ``), after the incoming header when nothing else is pending.
  Each progress line starts with the time of its journal event (`• 10:50 #2 Parser: todo → in_progress (Ann Lee)`).
- `wait_for_events` renders with `render_wake(actor, incoming=True, stamps=True, since=<oldest progress line>)`
  (§6.3): one result may hold hours of news (progress collected since the last progress call, notifications queued
  while the client was away), so when it spans more than a minute the header shows the range and every notification
  and message has its time; within a minute: one stamp, no times but the progress lines'. Example:
  ```
  === New events while you were working (2026-10-11 10:50–12:50) ===
  What happened:
  • 12:20 Task #3 "API" was assigned to you by Ann Lee. Claim it with `task_claim` when you start working on it.
  • 12:50 Message #7 from Ann Lee to you:
  status?
  Board progress:
  • 10:50 #2 Parser: created by user
  • 11:40 #2 Parser: todo → in_progress (Bob Stone)
  ```

## 13. Hub MCP (hub_mcp.py)

```python
def build_hub_server(swarm) -> mcp.server.lowlevel.Server
    # on_list_tools: tools for audiences E∪U, each schema extended with optional "as_agent"
    # (string: name returned by join_swarm; omit to act as the user). on_call_tool: actor =
    # as_agent (must be an active external member) or USER; returns
    # CallToolResult(content=[TextContent], is_error). `instructions=` explains the swarm.
async def hub_call(swarm, name: str, args: dict, client: str | None = None, *, via: str = "mcp") -> tuple[str, bool]
    # one MCP tool call with the actor rules above (also used by the launcher MCP, §18.2); calls of external
    # members are traced (tracer.external_call), except UNTRACED_TOOLS = {"wait_for_events"} and except while the
    # swarm stops (a call that returns then, e.g. a shell command killed by the session stop, must not write into
    # the database that Swarm.stop's caller closes next). `client` (default: serving_client's) names the MCP
    # client: a message it sends (as the call's actor) reaches the external members it acts as already read
    # (mailbox.sent_as_seen_by(members, actor)), so it never comes back to it as news; messages others send, e.g.
    # an agent this call hires, reach them as news. `via` becomes ToolContext.via (the launcher MCP: "launcher-mcp").
    # No self-approval: `skill_approve` with approve=true, as the user, of a proposal whose owner is an external
    # member this client acts as (HubClients.members_of(client)) returns "Error: skill 'x' was proposed by Ann Lee,
    # the member this MCP client acts as; the user decides it in the web UI, the TUI or the console." without
    # running the tool (also for the launcher MCP's forwarded calls, inside serving_client); rejecting is allowed.
    # A client nothing names (client None) cannot be told from any external member: approve=true of an external
    # member's proposal returns "Error: skill 'x' was proposed by Ann Lee, an MCP member, and this MCP client cannot
    # be told apart from it (it sends no X-DreamTeam-Client header and has no MCP session); the user decides it in
    # the web UI, the TUI or the console."
CLIENT_HEADER = "X-DreamTeam-Client"
def client_key(ctx, local: str) -> str | None
    # the client of a request: `local` without HTTP (stdio, in-process: one client per server), else
    # "client:<CLIENT_HEADER>", else "session:<mcp-session-id>", else None (the 2026-07-28 protocol without the
    # header: such a client cannot be told from another one and may get its own message back once)
def serving_client(client) -> ContextManager       # hub_call inside it (and in tasks it starts) comes from `client`
def hub_clients(swarm) -> HubClients               # external member -> the client that last acted as it (as_agent,
                                                   # or join_swarm: a new member or a welcome-back)
def proxy_http_client()                            # the stdio proxy's httpx client, with a random CLIENT_HEADER
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
signal ends them even while the client keeps stdin open. The launcher MCP (§18.2) names its clients the same way:
each launcher server has its own `local:<uuid>` key, and it forwards every call inside
`serving_client(client_key(ctx, local))`, so `hub_call` in the session gets the client (also in the task that
`_call_in_session` starts).

`INSTRUCTIONS` (also in the launcher's, through `HUB_INSTRUCTIONS`) tell the user's side that `wait_for_events` with
`progress=true` also returns when the board moves on and that `jobs` and `job_stop` list and stop every member's
background jobs; and the members' side that members who join do not count toward max_agents, that a `shell` command
still running at its timeout (at most 600 seconds over MCP) continues as a background job, that `background=true`
starts one at once, that processes a command leaves running become a job too, and that a job's end arrives through
`wait_for_events` (no polling); `swarm_control` also switches wrap-up mode (§3), in which no new background job
starts. The user's side also learns `skills`, `skill_attach`, `skill_detach` and `skill_approve` (not for a member
the client acts as); the members' side that skills (SKILL.md folders under `.scratch/_skills/`) are listed by
`join_swarm` and `swarm_status`, and that `skill_propose` proposes one.

## 14. Web (web.py) — REST for the UI and the console

`create_app(swarm, host, *, on_library_change=None) -> Starlette` is the app of ONE swarm: the whole server of `dreamteam run`, and the
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
| GET `/api/roles` | `{"language": swarm language, "roles": [{key, name, description, instructions, model, builtin, names, skills}], "hidden": [same]}`: the visible roles of the catalog in the swarm's language (§17.3; the board's hire dialog, the TUI's `/roles`), and the hidden ones, which a hire by name still resolves; `names`: every spelling `roles.find_role` resolves to the role (its name and id, a built-in role's name in either language); also for a stopped session; not in the snapshot (several KB) |
| GET `/api/hub` | {pid, project, name, version, sandbox}: identity of this hub process (checked against hub.json); `sandbox`: inside the Docker sandbox (the host library is a read-only source, folder paths are the container's) |
| GET `/api/tasks/{id}` | {task, discussion: [...], events: [...]} |
| GET `/api/messages?with=Name&limit=&since=` | conversation USER↔Name; without `with`: all recent messages |
| GET `/api/inbox?since_message=&since_notification=` | `{"messages","notifications"}` to USER: with since_* > 0 the ones with a larger id (oldest first, ≤ 500); without, the newest 500 (ascending). `since` = alias of since_message. Messages carry `read` |
| POST `/api/read` | body `{"with"?: name}`: marks USER's messages (from that member) read → {"ok", "unread"} |
| GET `/api/events?since=&limit=&types=` | events |
| GET `/api/agents/{name}/activity?limit=` | `agent.activity` events of that agent |
| GET `/api/files?path=` | dir → {"type":"dir","entries":[...]}; file → {"type":"file","content","truncated"} |
| POST `/api/act/{tool}` | run tool as USER: body = args → {"ok": bool, "result": str}; the user's `jobs` and `job_stop` (§7.1) go through it too: background jobs have no endpoint of their own |
| GET `/api/skills` | `{library_dir, library: [entry], claude: [item], host: [entry], sandbox, limits: {max_mb, max_files, max_attached, max_archive_mb}, install, attached: [snapshot item + source_digest]}` (§19): the Skills tab; `source_digest` is the attached skill's own source now (`SessionSkills.source_digest`: "Update" compares with it only) |
| POST `/api/skills` | an import body (§19 "Requests": exactly one of `path`, `zip`, `git`, `claude`, `host`; `pick`, `replace`) plus `attach?: bool` → the import report + `install`; with `attach` every imported skill is attached as `library:<name>` (`attached: [{name, status, files}]`, `failed: [{name, error, status}]`, as `attach_source`); `attach` on a stopped session is 409 `STOPPED_SKILLS_TEXT` (importing works) |
| GET `/api/skills/{name}?from=library\|claude-user\|claude-project\|host-library\|session` | the entry + `files_list`, `skill_md`, `deps` (`show_folder`) + `install`; `session`: the session copy with its metadata and shown path |
| DELETE `/api/skills/{name}?confirm={name}` | `{"removed": name}` (`Library.remove`; the sessions keep their copies); 400 without a matching confirm |
| GET `/api/skills/{name}/deps-log?ecosystem=python\|node` | `{name, ecosystem, status, path, log}`: the install log of the attached skill's environment (the failed one first; `EnvManager.log_tail` / `log_path`); 404 when DreamTeam installs nothing for it |
| `/api/traces...`, `/api/spans/...`, `/api/agents/{name}/traces.jsonl` | traces (§16.3) |
| WS `/ws` | pushes every bus event as JSON (also the live-only `trace.*` events) |
| `/mcp` | hub MCP over streamable HTTP |

The skills routes are `skill_routes(config_of, workspace_of, *, via, swarm=None, changed=None) -> [Route]`, shared
with the launcher (§18.1): `config_of()` the settings (here the swarm's), `workspace_of(request)` the folder whose
`.claude/skills` is `claude-project` (here the workspace), `via` recorded in the library (`act` here), `swarm` the
session (its attached list, `attach`, `from=session`, `deps-log`; the launcher passes none), `changed()` after a
library change (here `on_library_change`, which the launcher's `SessionRouter` sets so the home page reloads its
Skills tab). `on_library_change` is also called after a successful `/api/act` call and a successful call of the
app's `/mcp` (`build_hub_server(swarm, on_library_change)`) of a `hub_mcp.LIBRARY_TOOLS` tool (`skill_attach`,
`skill_approve`); the launcher MCP's forwarded calls of those tools send the `skills` change themselves. Every `{name}` goes through `check_name` before any path is built (400). Errors are
`skill_error_response(exc)`: `{"error", **extra}` (e.g. `exists`) with the SkillError's / ToolError's status. The
stopped check uses `Swarm.started`.

### 14.1 snapshot
```json
{"swarm": {"name","language","paused","pause_reason","wrap_up": bool,   // the user's wrap-up mode (§3)
           "limits":{"max_cost_usd","max_agents"},
           "agent_count": n,               // what limits.max_agents counts (Swarm.agent_count: external members do not)
           "spent_usd","goal": {...}|null, "counts": {status: n}, "mcp_url", "models": [profile names],
           "session": {"id","title","status": "running"|"stopped",
                       "pending_goal": {"title","description"}|null}|null,   // null outside a SessionManager;
                                       // pending_goal: the goal a never-started session gets at its first start
           "tracing": bool,
           "independent_review": bool,   // Board.independent_review, read live (§4.1)
           "workspace": {"path","default","adopted","exists"},    // Swarm.workspace_info(): the folder the
                                       // members work in (same shape as a session summary's "workspace", §17.2)
           "network": {"mode": "on"|"off", "isolation": "docker"|"netns"|"env"|"container"|null}},   // netoff.describe, §7.2
 "tasks": [Task.to_dict() + {"blocked_by","comments"}],
 "agents": [{"name","kind","role","status","model","language","cost_usd","tokens_in","tokens_out","hired_by",
             "created_at","last_active","instructions","context_tokens","open_tasks":[ids],"unread",
             "jobs": n}],                  // its running background jobs
 "triggers": [...],
 "jobs": [job...],                         // JobManager.snapshot(): the running jobs (from memory), then those that
                                           // ended in the last hour (one query, at most 50); the public job dict of
                                           // §7.1 ({"id","label","owner","command","origin","status","reason",
                                           // "stopped_by","exit_code","started_at","background_at","ended_at","log",
                                           // "processes"}); no "busy" (it needs the board)
 "mcp": [...],
 "skills": [{"name","description","from","owner","library","comment","attached_by","attached_at","updated_at",
             "attached_via","files","size","digest","manual",
             "path",                       // SessionSkills.shown_path: ".scratch/_skills/<name>/SKILL.md", or the
                                           // session copy's absolute path when .scratch is not local
             "roles": [names],             // the catalog roles that list it (§19); from memory, no query
             "deps": [EnvState]}],         // its script dependencies, one per ecosystem (§19), [] without manifests:
                                           // {"ecosystem","status","files","packages","env","key","python"?,"bin"?,
                                           // "commands"?,"node_modules"?,"linked"?,"view"?,"log"?,"error"?,
                                           // "installer"?,"reason"?,"queued"?,"other_process"?,"seconds"?,...}
 "locks": [...],
 "user": {"unread": n}}
```

## 15. CLI (cli.py)

Commands of a single-swarm **project** (a directory with `dreamteam.toml`, made by `init`); the launcher
commands of a **home** are in §18.3.
```
dreamteam init [DIR] [--lang ru|en] [--provider anthropic|openai|openrouter|ollama|vllm] [--name N] [--goal G] [--force]
dreamteam run [DIR] [--goal G] [--host H] [--port P] [--no-console] [--open] [--network on|off]
    # one swarm, its board at / (web.serve); refuses when a hub already runs for DIR; --goal sets the goal
    # unless the stored one is active with the same title (compared with split_goal(G)[0], so a multi-line
    # --goal does not reset the goal at every restart)
```
`console`, `mcp`, `status` and `sandbox` work with a project as well as with a home (§18.3).

## 16. Traces (tracing.py)

Each activation (wake-up) of an LLM agent is one **trace** (`kind = "llm"`). An external member (hub MCP
`as_agent`) gets **session traces** (`kind = "external"`) that group its tool calls. A trace holds **spans**:

| kind | name | input | output | notes |
|---|---|---|---|---|
| input | `wake` / `incoming` / `nudge` / `continue` | – | text added to the context | `record()` |
| llm | `step` / `compaction` (the purpose) while running; the model id when finished | `{"epoch","upto","messages","extra","tools_rev"}` (`tools_rev`: the revision of the frozen tool set the call sent, §6.1; absent in spans recorded before tool sets were frozen) | `{"stop_reason","thinking","text","tool_calls":[{id,name,arguments,parse_error?}]}` | usage, cost; meta `{purpose, attempt}` (+ `max_tokens` when the request's output limit was raised, §6.2) + `{stop_reason}`; a refusal ends the span with status `error`; every failed attempt is its own span with status `error`, `error` text and meta `{error_kind, retry_in?}`, named after the configured model; a call cut off by a stop is `interrupted` (error_kind `cancelled`, not counted as an error) |
| tool | tool name | arguments (dict) | full result text (before truncation for the model) | meta `{call_id, chars, shown_chars?}`; status `error` if is_error; task ids in `task_id` / `parent_id` / `depends_on` are added to the trace's `tasks` |
| compaction | `compaction` | `{"messages","prompt_tokens"}` | summary text | parent of its llm span; meta `{epoch_archived, fallback?, tools_revision}` (the tool set from then on, §6.1); status `error` when the summary call failed |
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
  gets it right too); its `tools` (unless given) are the tools that call sent: `frozen.for_span(db, agent, language,
  input.tools_rev)` = the agent's record when its revision matches, else that revision's shipped snapshot, and the
  baseline revision for a span without `tools_rev`. `export()` passes `tools=[]`.
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
  parent, tools_rev=None)`, `llm_end(span, response, cost)`, `llm_error(span, error, *, kind, retry_in=None,
  model=None, status="error")`, `tool_start(call_id, name, arguments)`, `tool_end(span, result, is_error,
  shown_chars=None)`, `finish(status, outcome)`.
- Text renderers (CLI, console, the tool; UTF-8-safe): `trace_line(t)`, `render_trace(trace, detail_chars=300)`,
  `render_span(span, limit=20000)`, `render_context(ctx, offset=0, limit=30, chars=4000)`.

### 16.1 Runtime integration (swarm.py, agent.py)
- `Swarm.__init__`: `self.tracer = Tracer(self.db, self.bus, enabled=cfg.tracing.enabled,
  max_payload_chars=cfg.tracing.max_payload_chars)`; `session_id` and `session_title` are None until a
  SessionManager sets them.
- `Swarm.start`: first `self.tracer.recover()` (then `self.jobs.recover()`, §7.1), then
  `self.tracer.prune(retention_days)` if it is > 0. The scheduler prunes again every 6 h.
- `Swarm.stop`: wakes the long polls, `self.tracer.close_external()`, then stops the runtimes (each closes its
  LLM client with `aclose()`), then the jobs (§3).
- `Swarm.reconfigure(config: Config)`: hot-apply new settings to a loaded swarm.
  - Keep `project_dir` (data_dir and workspace stay), `self.language` and the agents' frozen system prompts.
  - Replace `self.config`; update the tracer `enabled` / `max_payload_chars`.
  - Replace `llm_semaphore` with a new one if `max_parallel_llm_calls` changed.
  - For each runtime, `runtime.reset_llm()` (the next call builds a client from the new profile); an agent in
    ERROR whose activation was cut short is woken to retry it.
  - Lift a budget pause the new budget allows.
  - Called for the global settings (`update_settings`) and for one session (`SessionManager.update`: its
    `overrides`, or the partial `settings` of the board's settings dialog, §17.2, §18.4). The rest is read at each
    use: `[shell]` limits at the next command or job check, `agents.max_steps_per_turn` at the agent's next
    wake-up, `context_tokens` (when the model profile has none of its own) and `tool_output_chars` at the next
    call, `idle_nudge_seconds` at the next scheduler pass, `planning_hint_steps` / `planning_hint_minutes` at the
    planner's next step or scheduler pass.
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
  `SECTIONS = swarm, agents, server, shell, workspace, mcp, skills, tracing, roles, models` (same format as a project's
  `dreamteam.toml`; `roles` is an array of tables, `[[roles]]`, the role catalog of §17.3).
- `ensure_home(home, language="ru", provider="anthropic")` creates the directory, `sessions/` and the settings
  file from the template; `read_raw(home)`; `validate(home, raw) -> Config` (ValueError with a readable text);
  `dump_toml(data)` (a top-level non-empty list of tables is written as `[[name]]` blocks after every table,
  None values skipped); `deep_merge(base, override)`.
- `[swarm] independent_review` (bool, default true; a non-bool is a ValueError naming `[swarm]
  independent_review`): an agent's finished task is approved by another member, or by the user when nobody else
  can (§4.1); false: a task without a reviewer is done at once. The template has the line `independent_review =
  true` with a comment, after the `starting_roles` comment (which names the starting team rule, §17.3), and a
  commented `[[roles]]` example before the model profiles.
- `[swarm] network` ("on" | "off", default "on"; anything else is a ValueError naming `swarm.network`): "off"
  cuts the members' commands, their jobs and the stdio MCP servers off the network (§7.2). Read at each use;
  session overrides apply (a running session: from its next command). The template has the line after
  `independent_review`, with a Russian comment that says it is strict only in Docker and on Linux.
- `[agents] planning_hint_steps = 8` (whole number ≥ 0) and `planning_hint_minutes = 10` (number ≥ 0): the planning
  hint of §3 (scheduler item 4), after this many LLM steps or minutes of holding the planning task without creating a
  task, once more at twice the value; 0 turns that trigger off. Read at each use; session overrides apply. The
  template has both lines with Russian comments after `stagger_seconds`.
- `[shell]` (`ShellSettings`, read at each use, so `reconfigure` and session overrides apply at once; §7, §7.1):
  `program = "auto"` (auto | bash | sh | powershell | pwsh | cmd), `timeout = 120` (seconds a call waits; a command
  still running then continues in the background), `max_timeout = 1800` (the largest `timeout`, and the lifetime of a
  command that went on at its timeout; the wait itself is capped by `foreground_max`, over MCP by 600 s),
  `background_on_timeout = true` (false: such a command is killed, as before), `foreground_max = 180` (an AI agent's
  call waits at most this, whatever its `timeout`, while its command could become a job), `max_jobs_per_member = 4`
  and `max_jobs = 16` (running background jobs per member and per session; 0 = no background jobs),
  `job_max_minutes = 480` (lifetime of jobs started in the background or left running by their command; 0 = no
  limit), `job_log_mb = 2` (output kept per job in two files; at most 64 is used), `cpu_threads = "auto"` (the
  thread pools of numeric libraries per command: "auto" shares the cores among the AI agents, N, 0 = no cap; "auto"
  in any case or a whole number, also written as a string; anything else is a ValueError `[shell] cpu_threads must
  be "auto" or a whole number ...`), `pass_env = []`. `timeout`, `max_timeout`, `foreground_max` and `job_log_mb`
  are at least 1. The template lists every key with a Russian comment.
- `[skills]` (`SkillsSettings`, global only: not one of the session `OVERRIDE_SECTIONS`; §19): `library_dir =
  "~/.dreamteam/skills"` (the shared skills library; `Config.skills_library_dir`, where `DREAMTEAM_SKILLS_DIR` wins:
  the Docker sandbox and the tests set it), `claude = true` (offer Claude Code's skills), `max_mb = 10` and
  `max_files = 200` (one skill), `max_archive_mb = 50` (one zip or Git checkout before a skill is picked from it),
  `max_attached = 40` (skills of one session), `max_proposals = 10` (skills the members of one session may
  propose; 0 turns proposals off). Script dependencies (§19): `install = true` (install a skill's
  requirements.txt / pyproject.toml / package.json), `install_proposals = true` (also for members' proposals;
  never in a session without network, §7.2),
  `envs_dir = "~/.dreamteam/skill-envs"` (`Config.skill_envs_dir`, where `DREAMTEAM_SKILL_ENVS_DIR` wins: the
  sandbox's volume and the tests), `python = ""` (the environments' interpreter: "" = the base interpreter DreamTeam
  runs on; a path, a command, or with uv a version such as "3.12"), `install_scripts = false` (npm lifecycle
  scripts of the dependencies), `install_timeout = 900` (seconds per install command), `env_retention_days = 30`
  (unused environments are removed after this; 0 = never automatically), `install_env = []` (variables the
  installers of the user's own skills get, e.g. a private index's password; each must be a variable name, else
  ValueError `skills.install_env: 'x y' is not an environment variable name`). Changes apply at the next attach or
  session start (a plan is keyed by `skill_deps.settings_hash`). All but `max_proposals` and `env_retention_days`
  are at least 1. `Config.skills_host_library_dir` is `DREAMTEAM_HOST_SKILLS_DIR` (set only inside the sandbox),
  else None. The template has the block after `[mcp]` with Russian comments.
- `[[roles]]` → `Config.roles: list[RoleSettings]` (`RoleSettings(name, description="", instructions="",
  model=None, hidden=False, skills=[])`), checked by `config.parse_roles(raw, profiles)` after the model profiles (§17.3).
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
  - values of `mcp.external.<server>.env` are masked as `SECRET_MASK + hint` (`••••…abcd`);
  - `roles`: the stored `[[roles]]` entries `[{name, description, instructions, model, hidden, skills}]`, normalized
    (a built-in role by its id, e.g. «Тестировщик» → `tester`); `[]` when there are none.
- `apply_update(raw, update)` (a dict of sections; ValueError for bad input):
  - `roles` is the complete list of entries: not a list → "Section roles must be a list of roles", an item that
    is not an object → "Role N must be an object"; names get their whitespace collapsed; fields that are null,
    `""`, `[]` (no skills) or `hidden: false` are not stored (an untouched field of a built-in role keeps the built-in value);
    `[]` removes every customization; a role's `model` follows a profile rename; the rules themselves are
    checked by `validate` (`[roles.N] ...`);
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
  `default_session()`, `async create(title, goal="", description="", *, overrides=None, start=True,
  workspace=None, skills=None, via="home")`, `async load(sid)`, `async start(sid, *, skills=None, via="home")`,
  `async stop(sid, *, remember=False)`, `async unload_idle(sid)`, `update(sid, *, title=None, overrides=None,
  settings=None)`, `async delete(sid, *, skills=None, via="home")`, `async resume()`, `global_config()`,
  `session_skills(sid, show=None)`, `async decide_skill(sid, name, approve, *, comment="", replace=False,
  via="home")` ("Skills" below),
  `finished(sid)`, `async shutdown()`, `copy_overrides(sid)`, `default_workspace(sid)`, `workspace_of(sid)`,
  `async set_workspace(sid, path, *, expect=None)`; module functions `read_stats(db_path, conn=None)`,
  `slugify(title)` and `external_workspaces(home)`.
- `listen` callbacks receive `{"type": "sessions.changed", "session": sid | None, "change", "ts"}`. The change is
  one of `created`, `started`, `stopped`, `updated`, `deleted` or `settings` (session None).
- `SessionError(message, status=400, **extra)` (a ValueError) carries an HTTP status and `extra` (put into the
  JSON answer: `exists` for a skills clash, `pending_skills` for a refused delete): 400 bad input or invalid settings,
  404 unknown session, 409 conflict (deleting `main`, files in use, a dirty repository to adopt or to start in,
  a folder of another session, a folder that changed since the user was asked, a missing workspace folder at
  start), 500 damaged or cannot be opened.
- Session ids are `NNN-slug` from the title (Cyrillic and Ukrainian transliterated, accents dropped; e.g.
  `001-snake-game`) and match `^[a-z0-9][a-z0-9-]{0,63}\Z`. `main` is the legacy session: a home that was a
  single-swarm project (`<home>/.dreamteam/swarm.db`) lives in the home itself, uses the global settings
  directly (including a `swarm.goal`), cannot get overrides and cannot be deleted from the UI;
  `sessions/main/` is ignored. Titles are one line, at most 200 chars (default: the goal's first line,
  shortened to 60 characters at a word by `goal_text.shorten_title`, else "Session"; an empty `title` lets the
  server derive it).
- Effective config of a session = the global settings without `swarm.goal` / `goal_description`, deep-merged
  with its `overrides`. Overrides may only touch the sections swarm, agents, shell, workspace, mcp and tracing
  (`OVERRIDE_SECTIONS`; not models, server or roles: the role catalog is global, and a session chooses its
  starting team with `swarm.starting_roles` / `start_agents`); they are validated at create and update time
  (`update` applies them to a loaded swarm at once via `reconfigure`). `config_view(sid)` is the public view of
  the effective settings (the global ones when the overrides are invalid now).
- Partial update (`update(sid, settings=...)`, the settings dialog of the board, §18.4): `settings` is
  `{section: {key: value | None}}`; each key is set in the stored overrides, None (or "") removes it so the session
  inherits the global value again, and a section left empty disappears. The other overrides (those given at
  creation: language, team, model...) stay. Same sections, cleaning (`clean_overrides`, no goal keys) and
  validation as `overrides`; nothing is written when it fails (also not a `title` passed along). Passing both
  `overrides` and `settings`, a non-object, or `main` is a 400. A loaded swarm is reconfigured as for `overrides`.
- `create` splits the goal as `set_goal` does (`goal_text.split_goal(goal, description)`) and stores it already
  split as `pending_goal` {title, description, actor} (only with a non-empty title); the first `start` sets it
  once the members exist (a failure keeps it). Module function `pending_goal(meta)` → {title, description} | None
  splits again on read (a `session.json` written before the split may hold a multi-line title); the summary and
  the snapshot's `session.pending_goal` use it. `start` marks `autostart`; an explicit `stop` clears it
  (`remember=True`, used at shutdown, keeps it), and `resume()` starts the marked sessions except finished ones
  (`finished(sid)`: the goal is done and no task is open, read from the summary without loading it; False when it
  cannot be read), which stay stopped until started by hand. `stop` unloads the swarm (stop + close its database).
  Every unload (`stop`, `delete`, a workspace move, `shutdown`) goes through `Swarm.stop`, which ends the session's
  background jobs and closes their logs and handles before it returns (§7.1), so the folder can be moved or deleted
  on Windows too.
- `load(sid)` constructs the swarm without starting it (a stopped session is viewable) and closes the traces a
  crashed process left running (`tracer.recover()`). Load, start, stop, delete and set_workspace run under a
  per-session lock and re-check that the session still exists (404). `shutdown()` also waits for sessions in the
  middle of such an operation (e.g. a workspace move that starts the session again) and then stops them.
- `delete(sid)` renames the directory to `sessions/.deleted-<sid>-<ns>` first (a session whose files are open
  stays intact: 409), then removes it in a thread; leftovers are purged at the next delete. A workspace folder
  outside the session directory is never deleted (it is the user's folder): `delete` returns it (read under the
  session's lock, so a `set_workspace` that ran just before counts), else None.
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
   "spent_usd","paused","pause_reason","wrap_up","last_activity","jobs":{"running"},
   "workspace":{"path","default","adopted","exists"},"skills":[names],"skills_pending":[names]}
  ```
  `workspace` (= `workspace_of(sid)`): the folder the session works in; `default` = its own `workspace/` (created
  on demand, so it never counts as missing at start), `adopted` = a folder that held files before (the user's),
  `exists` = it is a folder now. `jobs.running`: the session's running background jobs (`read_stats` counts the
  rows, in a `try` of its own: a database older than background jobs gives 0). `skills`: the attached skills' names,
  `skills_pending`: those members proposed that wait for the user's library decision (from memory when loaded,
  else `read_session_skills` of the session's data dir).

#### Workspace folder
A session works in `<session dir>/workspace` (`default_workspace(sid)`) unless its `session.json` has
`"workspace": {"path": "<absolute path>", "adopted": bool, "in_home"?: "<relative path>"}` (absent = the default
folder; any other shape makes the session damaged, 500). `in_home` (posix, no `..`) is stored for a folder inside
the home, e.g. `projects/x`: the folder is then found relative to the home wherever the home is mounted (the
Docker sandbox's `/swarm`) or moved, and `path` is only informative. `config_for(sid)` sets
`Config.workspace_path` / `workspace_adopted` from it (`data_dir` stays in the session directory); a stored path
that is not absolute on this system (a Windows path read inside a Linux container) maps to a folder that does not
exist. `Swarm.reconfigure` keeps the workspace of a loaded swarm (it changes only through `set_workspace`, which
restarts the swarm) and, for a started swarm in an adopted folder, its `workspace.git` (a change applies at the
next start, which checks the folder).

`async set_workspace(sid, path, *, expect=None)` (`path` absolute, None = the default folder) returns
`{"mode": "moved"|"adopted"|"unchanged", "from", "to", "git": "moved"|"existing"|"initialized"|"disabled"|"none",
"mcp_moved": [names], "old_kept": path|null, "old_missing": bool, "old_changed": null|{"paths", "commit",
"error"}, "restarted": bool, "start_error": str|null}`:
- **Checks first** (nothing changes before they pass; symlinks and junctions are resolved). 400: not a string,
  empty, a relative path (clients resolve relative paths themselves), a drive or file system root, the user's
  home itself or a folder containing it, the DreamTeam home or a folder containing it, anything in
  `<home>/.dreamteam/`, anything in `<home>/sessions/` except this session's own default folder, the session's
  data folder, a repository database (a path with a `.git` component, a bare repository, a folder with `HEAD`,
  `objects/` and `refs/`; whether git is on or not), the shared MCP library (`mcp.library_dir`, the global one and
  the session's) or a folder inside or containing it, the shared skills library (`Config.skills_library_dir`, §19)
  or a folder inside or containing it, the skills' environments folder (`Config.skill_envs_dir`: "... is
  DreamTeam's skill environments folder ..., or inside it or contains it (every session runs its skills' packages
  from there)") or a folder inside or containing it, a folder inside the current workspace or containing it (or
  containing the session directory), an existing file, a folder that cannot be created or written (a temporary
  probe file). 409: a folder another session works in, or inside / containing one (the legacy `main` works in
  `<home>/workspace`). The current folder itself: `"unchanged"`, nothing happens. 404 unknown session; 409 for
  `main` (see below).
- `expect` (`"moved"` | `"adopted"`, anything else 400): the mode the user agreed to; the TUI and the console send
  the one their question announced. 409 when the folder turns out otherwise (e.g. files appeared in a folder that
  was empty when the user was asked); nothing changes.
- **Empty or missing target → move** (`"moved"`): every entry of the current folder (files, `.git` last,
  `mcp/`) is moved into it (a rename per entry; a copy and a delete across volumes); parents are created; the
  session's own default folder is removed afterwards (recreated on demand), another old folder is left empty.
  `git`: `"moved"` when the folder had a repository, else a repository is initialized there (`"initialized"`;
  `"disabled"` when `workspace.git` is off, `"none"` without git). After an *adopted* folder (the user's), an empty
  target starts a fresh workspace instead: the user's files stay where they are (`old_kept`), only the
  session's MCP servers move, and a repository is initialized.
- **Target with files → adopt** (`"adopted"`, stored `adopted: true`): the agents work with its files and their
  commits go to the current branch of its repository. With `workspace.git` on and git installed: refused (409)
  when the repository has uncommitted changes or untracked, not ignored files (`repo_state`, §7: `git status
  --porcelain --untracked-files=normal`, whatever the user's status settings; the agents commit with
  `git add -A`; the `mcp/<name>` folders of the session's own servers, absent there, are left out as at every
  start: a folder the session worked in before may track them, and their move brings them back), is in a
  detached HEAD, has a merge, rebase, cherry-pick, revert or bisect stopped halfway (the
  agents' next commit would conclude it), or git cannot read it; refused (400) when the folder is inside a
  repository but not its root; a folder that is not in a repository gets one (`init_repo`: git init, `.gitignore`
  if missing, an initial commit of the files there; `"initialized"`). The repository's configuration is never
  modified (no `_configure_repo`, §7), and the agents' commits there follow the user's line-ending settings. The
  old folder stays as it was (`old_kept`). The session's forged and imported MCP servers (`mcp_servers` rows with
  another origin than `config`, folders `<old>/mcp/<name>/`) move to `<target>/mcp/<name>/` (409 before anything
  changes when one of those exists there). Adopting the session's own default folder (which still holds its old
  files) is a plain switch back (stored without "workspace", not adopted, no git checks).
- **Leaving an adopted folder** (for an empty folder or the default one): the user's files stay, only the
  session's MCP servers move along. Those of them that the user's repository tracks (`tracked`: the agents'
  `git add -A` committed them there) would leave deletions behind, so with git on DreamTeam commits just their
  removal there (`commit_paths`, author DreamTeam, "Remove mcp/<name>: ..."), and the repository stays clean.
  `old_changed` = `{"paths": ["mcp/<name>", ...], "commit": hash|null, "error": why not|null}` (no commit on a
  detached HEAD, during a merge or rebase, or with git off for the session: the result says to commit the removal
  or restore the folders); null when nothing of the user's folder changed.
- **One change at a time per home**: under the session's lock the target is checked and a loaded session is
  unloaded; then, under a home-wide lock (also taken by `create(workspace=)`), the target is checked again with
  nothing running (an agent may have forged a server meanwhile, the folder may have changed, another session may
  have taken it: its `session.json` names it by then; another mode than the first check found is a 409, as with
  `expect`), the change is carried out and `session.json` is written. Two sessions never take one folder.
- **A loaded session** is unloaded first (stop + close, as `stop`, autostart kept; events `stopped`, then
  `updated`, then `started` after the restart of a session that was running). A restart that fails after a
  successful change leaves the session stopped and is reported in `start_error`.
- **Failures**: `.dreamteam/workspace-move.json` (`{"from","to","mode","started_at"}`) is written before the first
  change. Any failure undoes everything done so far in reverse, removes the journal, starts a session that was
  running again and raises SessionError: 409 when a file is in use or a name appeared in the target meanwhile,
  else 500, naming the folder the session still uses. The undo touches only what the change made: a move is
  recorded once its destination holds the entry; a destination that exists is never replaced or merged into
  (FileExistsError, also across volumes); an entry is put back only where nothing new appeared meanwhile (else it
  is reported); created folders are removed only when empty; `.git` / `.gitignore` only when the change created
  them (a `.git` that appears in the target meanwhile is a 409, never initialized over). When the undo cannot
  finish, the error (500) lists the files left in the target and the journal stays: `start` and `set_workspace`
  then refuse (409) naming both folders and the journal to delete once the files are together.
- The members hear of it at the next start: `n_workspace_moved` {path, old}, or `n_workspace_changed` {path, old}
  after an adopt or a fresh start (session.json `workspace_notice` until then); quietly for LLM members (their
  next wake-up), with a wake-up for external members (an MCP client's `wait_for_events` returns it).
- **Start** (and `resume()`, which logs the error and goes on): a session whose (non-default) folder does not
  exist or is not a folder does not start: SessionError 409 naming the path and how to fix it (`/cd <folder>` or
  `/cd --default` in `dreamteam` or `dreamteam console --session <id>`, `session_cd` in MCP); `Workspace.init`
  never creates it. Viewing it (`load`) still works. An adopted folder with git on is checked again before every
  start (the user may have worked there while the session was stopped, or git was off at the adopt): 409 for
  uncommitted changes or untracked files (the session's own `mcp/<name>` folders and `.scratch/` are left out), a
  detached HEAD, an operation in progress, an unreadable repository, or a folder inside another repository (a folder in no
  repository gets one at the start, as at an adopt).
- **Legacy `main`**: refused (409, `LEGACY_WORKSPACE_TEXT`): `dreamteam run` can still open the home as a
  single-swarm project, which reads only `dreamteam.toml` and would work in `<home>/workspace` again.
- `create(..., workspace=path)`: the same checks before anything is created (with the overrides' `workspace.git`),
  then the session is created in its own folder and `set_workspace` runs before the first start. A failure removes
  the new session; its error names only the folder (and what could not be removed there), never the removed
  session. The returned summary then has `"workspace_result"`: the result with mode `"created"` for an
  empty/missing folder or `"adopted"` (`old_kept` null: the unused default folder is removed).
- `external_workspaces(home)` → `[(sid, stored path)]` of the sessions whose folder is outside the home (no
  `in_home`; read from the `session.json` files, nothing opened): `dreamteam sandbox` warns about them (only the
  home is mounted).

#### Skills
The manager's side of §19, for the home page, the CLI and the launcher MCP (`via` home / cli / launcher-mcp):
- `global_config()`: the home's settings (`Config`) as the library routes use them (re-read when the file changed).
- `skills` of `create` and `start`: a list of references (`x`, `library:x`, `claude-user:x`, `claude-project:x`
  against the session's folder, `host-library:x`) or sources (an absolute folder or .zip path on this machine, a Git
  URL; `source_spec`). Everything is made ready first, in a thread (`_prepare_skills`): references resolved and
  checked, sources staged (`stage_source`, not yet in the library). SessionError 400 (not a list of non-blank
  strings, a bad source, a source holding several skills: "... holds several skills (a, b): add them in the Skills
  tab and name them."), 404 (an unknown name), 409 (a source whose skill the library has with other content,
  `exists`; two different skills of one name); nothing staged is left behind. Then the staged sources are committed
  to the library (`by` user, `via`) and every skill is copied into the session (`swarm_skills.attach_files`,
  `attached_via` "create").
- `create(..., skills=)`: prepared before the session exists; committed right after `session.json` is written and
  before `set_workspace`, so a failure (the new session is removed) leaves nothing in the user's folder. The first
  start mirrors them; the members find them in their first wake-up.
- `start(sid, skills=)`, also for a running session (the start itself is then a no-op): a name already attached
  with other content is 409 (`exists`: "replace it in the session's Skills tab"); past `[skills] max_attached` 409.
  A stopped session gets the copies before its Swarm starts (`SessionSkills.attached_again(names)` takes the names
  out of kv `skills_detached`, as a live attach does); a running one attaches them live with
  `swarm.skills.attach(USER, ref, via=via)` (the members are told).
- `async session_skills(sid, show=None)` → `{"attached": [...], "running"}`, stopped or running, always the snapshot
  items of §14.1: a loaded swarm's `SessionSkills.public()` (on the loop: `deps` read live state), else the same keys
  from the session's files (`swarm_skills.stopped_public`, in a thread: `path` the mirror's, `roles` from its
  catalog, `deps` []); `show` adds that session copy's `files_list`, `skill_md`, `deps` and `install` (the global
  `[skills]` switches its preview line is worded by: the home page's proposal dialog may open before anything else
  loaded them; 404 when not attached).
- Messages about a source show it as `Staged.shown` (a Git URL without its user info), never as typed.
- `decide_skill(...)`: a running session's `SessionSkills.decide`; a stopped one's `decide_files` (`via`
  "stopped-session", `project` = the session's swarm name, `session` = sid; a loaded swarm's cache is reloaded); an
  `updated` event, and after a save (also the saves of `delete(sid, skills="save")`) a `skills` one (session None:
  an open home Skills tab reloads the library). SessionError 404 / 409 as §19.
- `delete(sid, skills=None)`: proposals waiting for the user's decision are never lost silently: 409 "Session X has
  skills waiting for your library decision: a, b. Save them to the library first, or delete them with the
  session." (`pending_skills`) unless `skills` is `"save"` (every one saved first; a name the library has with other
  content refuses the delete before anything is saved, 409 `exists`; a stopped session's are saved on its files with
  `via` "stopped-session", as `decide_skill` does) or `"discard"`; anything else 400.
- **The mirror in the user's folder**: an adopted folder never keeps `.scratch/_skills` once the session leaves it:
  `Swarm.stop` removes it, and so do `delete` (a crashed or never-stopped session) and `set_workspace` when it
  leaves an adopted folder (`_drop_mirror`, never following a link; a failure is logged).

### 17.3 Role catalog (roles.py)
A leaf module (no package imports; config.py uses it to check `[[roles]]`). The catalog is global: the settings
of a home or a project, never a session override (§17.2).
- **Built-in roles** `ROLE_PRESETS: {language: {id: (name, description)}}`, in catalog order (`PRESET_IDS`):
  generalist «Универсал»/Generalist, developer «Разработчик»/Developer, tester «Тестировщик»/Tester, reviewer
  «Ревьюер»/Reviewer, analyst «Аналитик»/Analyst, architect «Архитектор»/Architect, writer «Технический
  писатель»/Technical writer, researcher «Исследователь»/Researcher. `DEFAULT_ROLE_ID = "generalist"`
  (`prompts.DEFAULT_ROLE` is its name). The descriptions are addressed to the agent ("You ...", the same in both
  languages): they go into a new member's system prompt right after "Your role: <name>.". The architect and the
  developer name the contracts file `CONTRACTS.md`, the one the planning task asks for.
- **`[[roles]]` entries** (`RoleSettings`): `name` (required: a built-in role's id or its name in either language
  customizes that role, only with the fields given; any other name is a role of the user's own), `description`
  (a non-empty one replaces a built-in description in both languages; required for an own role unless it is
  hidden), `instructions` (added for every member of the role), `model` (a profile of `[models]`; `""` = none),
  `hidden` (not offered: not in the team composer, not listed to agents; it still resolves), `skills` (skill names
  its agents get attached to their session at hire, marked as their own, §19; built-in presets list none). Deleting
  the entry
  of a built-in role resets it. `parse_roles` checks: a list of tables, at most 100 (`MAX_ROLES`); names 1–60
  characters (`NAME_LIMIT`) without `,` or `*` (they separate the items of a team), unique by `role_key`; texts at
  most 4000 characters (`TEXT_LIMIT`); errors `[[roles]] must be a list of role tables (write each role as a
  [[roles]] table)`, `At most 100 roles`, `[roles.N] name is empty`, `... the role 'X' is listed twice (also as
  role J)`, `... 'X': a role of your own needs a description`, `... 'X' uses the model profile 'M', which is not
  defined in [models]`, `[roles.N] skills: 'Bad Name' is not a skill name (lowercase letters, digits and
  hyphens)` (`skills.NAME_RE`, at most 64 characters), `[roles.N] skills: at most 20 skills per role`
  (`MAX_ROLE_SKILLS`) (and unknown keys or types as for the other sections); whether the library has a role's
  skills is not checked (a missing one is reported at hire). Stored normalized: a built-in role by its id, texts
  stripped, `model = ""` as None, skill names stripped and deduplicated in order.
- **Merge and resolution**: `catalog(entries, language, *, include_hidden=False) -> list[Role]` = the built-in
  roles in `PRESET_IDS` order with their entries applied, then the own roles in entry order; `Role(key, name,
  description, instructions="", model=None, builtin=False, customized=False, hidden=False, skills=())`, `key` = preset id or
  the casefolded name. `normalize(name)` collapses whitespace; `preset_id(name)` matches an id or a built-in name
  in either language, any case; `role_key(name)` = preset id or casefolded name; `find_role(entries, name,
  language)` (hidden roles too; None = a new role); `close_roles(entries, name, language, n=3)` (difflib, cutoff
  0.6, over the visible names, the preset ids and the other language's names; names in `language`);
  `summary(text, limit=200)` (the first sentence, else a cut at a word with "…").
- **Into an agent** (§3 `hire`): the canonical name in the member's language ("Tester" hired in a ru swarm becomes
  «Тестировщик»), the description into the frozen system prompt, the role's instructions before the hirer's, its
  model profile unless the hirer names one. A catalog edit reaches only members hired afterwards (also in running
  sessions, through `reconfigure`); existing members keep their prompts. Agents see the catalog in the `agents`
  output (§12), a new member's system prompt names it, and the kickoff lists the team (`Team: Name — Role; ...`).
- **Starting team** (no field of its own): `swarm.starting_roles` (ordered; a name repeated per agent; preset ids,
  built-in names in either language or own role names) and `swarm.start_agents`.
  `starting_team(starting_roles, start_agents)`: member i gets `starting_roles[i]`; members beyond the list and
  blank entries are generalists; roles beyond `start_agents` are ignored (`[]` + 3 = three generalists). A name
  not in the catalog is an own role without a description. UIs show the team as rows:
  - `team_rows(starting_roles, start_agents) -> [(role, count)]`: consecutive equal roles grouped, order kept (the
    same role may appear in two rows); a row's role is a preset id or an own role's name as written (its
    `role_key` is the catalog key);
  - `team_value(rows) -> (starting_roles, start_agents)`: expanded, built-in names stored as ids, trailing
    generalists dropped (`[("generalist", 3)]` → `([], 3)`), so an untouched default never changes the settings;
  - `parse_team(text) -> list[str]` (`--team`, `/team`): `"architect, developer*2, tester"` → `["architect",
    "developer", "developer", "tester"]` (names kept as typed, normalized); ValueError `'developer*0': the count
    must be a whole number from 1 to 50`, `'*2': the role name is empty`, `'<item>': a role name cannot be longer
    than 60 characters`, `The team is empty`, `A team has at most 200 agents`.
- **Views**: `presets_view()` → `[{id, name: {ru, en}, description: {ru, en}}]`; `catalog_view(entries)` →
  `[{key, builtin, customized, hidden, name: {ru, en}, description: {ru, en}, instructions, model, skills}]` (hidden roles
  included; an own role has the same text under both languages). Served by `GET /api/settings` (§18.1); the
  visible roles of one swarm by `GET /api/roles` (§14).
- **Compatibility**: no `[[roles]]` → the 8 built-in roles; existing `starting_roles` keep working (built-in names
  now bring their description); no database change.

## 18. Launcher: web, MCP, CLI, UI

### 18.1 Web (launcher.py)
`create_launcher_app(manager, host="127.0.0.1")` returns the app wrapped in the same `GuardMiddleware`
(host/origin, JSON POSTs). A `SessionRouter` in front sends `/s/{sid}/...` to that session's app
(`web.create_app(swarm, on_library_change=...)`, §14, served with `root_path = /s/{sid}`; a library change there
emits `sessions.changed` with change `skills`, also a `skill_attach` or `skill_approve` of `/api/act` that
succeeded: either may have changed the library) and everything else to the launcher routes.

| method path | response |
|---|---|
| GET `/` | `static/home.html` (launcher page, `no-store`) |
| GET `/static/{file}` | static files (`UIStaticFiles`, `no-cache`) |
| GET `/api/hub` | `{"pid","project": str(home),"home","name":"DreamTeam","version","mode":"launcher","sandbox"}` |
| GET `/api/sessions` | `{"sessions": [summary...]}` |
| POST `/api/sessions` | body `{title?, goal?, description?, overrides?, start?=true, workspace?, skills?: [ref or source], via?}` → `{"session": summary}` (201); `skills` attached before the first start (§17.2 "Skills"; `via` the surface recorded for them: `home` when absent, `cli` from the terminal's client, anything else 400); `goal` may be multi-line and `title` "" (the server splits the goal and derives the title, §17.2); with `workspace` (an absolute folder, §17.2 "Workspace folder") also `"result"` (mode `created` or `adopted`) |
| GET `/api/sessions/{sid}` | `{"session": summary, "config": manager.config_view(sid)}` |
| PATCH `/api/sessions/{sid}` | body `{title?, overrides?, settings?}` → `{"session": summary, "config": config_view(sid)}`; `overrides` replaces all of them, `settings` `{section: {key: value \| null}}` changes only those keys (null: the global value again; §17.2 "Partial update"); both at once, a bad value or `main` → 400 and nothing changes |
| DELETE `/api/sessions/{sid}?confirm={sid}&skills=save\|discard&via=` | `{"ok": true}` (+ `"workspace_kept": path` when the session worked in a folder outside it, which is never deleted); 400 without a matching confirm; 409 `{"error", "pending_skills"}` when members' proposals wait for the user's decision and `skills` is not given (§17.2) |
| POST `/api/sessions/{sid}/workspace` | body `{"path": "<absolute>"}` or `{"default": true}`, optionally `"expect": "moved"\|"adopted"` (the mode the user agreed to, §17.2) → `{"session": summary, "result": set_workspace result}`; 400 bad body or target, 404 unknown session, 409 dirty repository, MCP name conflict, a folder of another session, a folder that is not what `expect` says, a file in use, `main` |
| POST `/api/sessions/{sid}/start` / `/stop` | `{"session"}`; another action → 404; start takes an optional body `{"skills": [...], "via"?}` (attached first; live when it is running; `via` as for create; the delete's `?via=` too) |
| GET `/api/sessions/{sid}/skills?show=` | `manager.session_skills(sid, show)`: the attached skills (snapshot items, §14.1), also of a stopped session |
| POST `/api/sessions/{sid}/skills/{name}/decide` | body `{"approve": bool, "comment"?, "replace"?}` → `{"text", "session"}` (`decide_skill`, via home) |
| GET / POST `/api/skills`, GET / DELETE `/api/skills/{name}` | the library routes of §14 (`skill_routes` with `manager.global_config`, via `home`, no swarm: no `attached`, no `attach`, no `from=session`, no `deps-log`); `?workspace=<absolute>` names the folder whose `.claude/skills` is offered (a relative one 400); a change emits `sessions.changed` (session None, change `skills`) |
| POST `/api/skill-envs/cleanup` | `EnvManager.cleanup(env_retention_days, every_unused=True)` now (a daemon thread: every env no session uses, whatever its age) → `{"removed", "kept"}`; emits change `skill-envs` |
| GET `/api/settings` | `{"settings": public view, "providers": PROVIDER_CHOICES, "role_presets": roles.presets_view(), "role_catalog": roles.catalog_view(config.roles), "path": str(settings file), "home", "network_isolation": netoff.shown(netoff.isolation()) (§7.2)}` (a hand edit of the file is picked up first) |
| PUT `/api/settings` | body `{"settings": {...sections}}` (`settings.roles`: the complete list of `[[roles]]` entries, `[]` removes every customization) → `{"settings","applied_to","role_presets","role_catalog"}`; 400 with the `[roles.N] ...` text for a bad role (nothing saved) |
| POST `/api/settings/check` | body `{"profile": {...}, "name"?: stored profile name for the key fallback, "live"?: bool}` → `check_profile` result |
| WS `/ws` | pushes `sessions.changed` events (manager.listen) |
| `/mcp` | launcher MCP (§18.2) |
| GET `/s/{sid}` | 307 redirect to `/s/{sid}/` (query kept) |
| GET `/s/{sid}/` | the session's `index.html` (board UI) |
| `/s/{sid}/api/*`, `/s/{sid}/ws`, `/s/{sid}/mcp` | the session app; `manager.load(sid)` loads a stopped session for viewing |

- Errors are `{"error": text}` with SessionError.status; a body that is not JSON, not an object or has fields of
  the wrong type is 400; unexpected failures (e.g. the swarm failed to start, a locked file) are JSON 500s.
- `POST /s/{sid}/api/*` without `Content-Type: application/json` is 415 (the guard only knows `/api/...`).
- The app's lifespan runs `_clean_envs_daily(manager)`: unused skill environments are removed
  `ENV_CLEANUP_DELAY` (60 s) after the start and then every `ENV_CLEANUP_EVERY` (24 h), with the settings read
  each time (`env_retention_days = 0`: none); a failure is logged; the task is cancelled at exit.
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
  start_agents?, max_agents?, team?, max_cost_usd?, model?, network?, start?=true, workspace?, skills?)` (unset values come from the
  global settings; the board URL is shown only when a web server runs; `goal`: one line, or several, split as in
  §17.2; `team`: role names in order, a name repeated per agent (§17.3) → `swarm.starting_roles` (normalized) and
  `start_agents = len(team)` unless `start_agents` is given, `[]` = no team; not a list of non-blank strings →
  "team must be a list of role names, e.g. [...]", more than 200 → "A team has at most 200 agents.", a name over
  60 characters → "'<name>': a role name cannot be longer than 60 characters.", a name with `,` or `*` (the
  `--team` syntax) → "'<name>': a role name cannot contain ',' or '*'; give one item per agent and repeat a name
  for several agents, e.g. [...]."; `workspace`: an absolute folder, §17.2; `skills`: names or sources attached
  before the first start, §17.2 "Skills"),
  `session_start(session, skills?)` (the skills attached first, live when it runs), `session_stop(session)` and `session_cd(session, path)` (`set_workspace`; `path`
  absolute or `"default"`; the result in words by `workspace_text(result)`, worded as the TUI and the console
  word it: "Moved the session's files to X" only when they moved, "The session now works in X" after a folder of
  the user or a missing one, and what changed in the user's folder it left (`old_changed`)). `sessions_list`
  shows the folder of a session that does not work in its own one (`folder: <path> (adopted, MISSING)`), and for a
  running session with background jobs ` | jobs N running` (`session_line`, from `read_stats` "jobs"); a running one
  in the user's wrap-up mode is `[running, wrapping up]`; a session without network ends its first line with
  ` | no network` (summary `network`, §7.2); a session with skills gets `skills: a, b (waiting for your
  library decision: b)`. The last session tool is `skill_library(action: list | show | import | delete, name?,
  source?, pick?, replace?)`, the home page's Skills tab in words (via `launcher-mcp`): `list` the library, the
  host library and Claude Code's skills; `show` an entry (`name` may be `claude-user:x` / `host-library:x`) with
  its source, files, the dependency preview and SKILL.md; `import` either `source` (a folder or .zip path, a Git
  URL) or `name` (`claude-user:x`, `host-library:x`); several skills without `pick` list them; `delete` a library
  entry (the sessions keep their copies). Library changes emit `sessions.changed` (change `skills`). There is no
  `session_delete` tool.
- Every hub tool (§13) with an extra optional `session` property. Without it the call goes to
  `manager.default_session()`; when there is none, the error lists the sessions. An unknown or stopped session
  is an error result (`session_start` is suggested).
- Calls run `hub_mcp.hub_call` on the session's swarm (same actor rules: `as_agent`). A call still running when
  its session stops or is deleted ends with `Error: session X was stopped while <tool> was running.` Each call is
  dispatched inside `hub_mcp.serving_client(client_key(ctx, local))` (`local` = `local:<uuid>` per launcher server),
  so the session's `hub_call` knows the MCP client (§13: its own messages never come back to it as news).
- `dispatch_launcher_call(manager, name, args)` answers a tool name nobody knows (close matches + the list);
  `call_launcher_tool` turns SessionError / ToolError / unexpected failures into error results.
- `INSTRUCTIONS` explain sessions (with `skill_library` and the `skills` of `session_create` / `session_start`),
  then the hub instructions.
- `/s/{sid}/mcp` keeps the per-session hub MCP (§13).

### 18.3 CLI (cli.py)
```
dreamteam [tui|chat] ["PROMPT"] [-c | -r [SID]] [-p [--output-format text|json|stream-json] [--timeout S]
          [--no-auto-reply]] [--verbose] [--home H] [--lang ru|en] [--model PROFILE] [--agents N]
          [--team "ROLE[*N],..."] [--max-agents N] [--budget USD] [--title T] [--workspace DIR]
          [--skill REF|PATH|URL ...] [--host H] [--port P] [--network on|off]
dreamteam serve [DIR | --home H] [--host H] [--port P] [--goal G [--title T] [--workspace DIR]
          [--skill REF|PATH|URL ...] [--network on|off]] [--no-console] [--no-browser]
dreamteam sessions [DIR | --home H]
dreamteam sessions rm SID [SID ...] [--yes] [--skills save|discard] [--home H]
dreamteam traces [AGENT] [--home H] [--session SID] [--id N] [--span N [--context]] [--status S] [--limit N] [--json]
dreamteam console [DIR | --home H | --url URL] [--session SID] [--lang ru|en]
dreamteam mcp [DIR | --home H] [--no-web]
dreamteam status [DIR | --home H]
dreamteam sandbox [DIR | --home H] [--build] [--port P] [--goal G] [--network on|off]
```
- Default command (`_default_command`): `dreamteam` alone runs `tui` when stdin and stdout are terminals,
  else `serve`. Options only: `tui` when one of them is a TUI option (`-p -c -r --output-format --model
  --budget --agents --team --max-agents --verbose --timeout --no-auto-reply --lang`), `serve` when one is
  serve-only (`--no-console --no-browser --goal`) or there is no terminal, else `tui`. A first word that is not a command
  is the TUI prompt (`dreamteam "Сделай змейку"`), except a near-miss of a command name (difflib, 0.75), which
  argparse reports.
- `tui` (docs/TUI.md): `tui.main_tui` (interactive; refuses without a terminal, exit 2) or with `-p`
  `tui.main_print` (text piped into stdin — a pipe or a file, never a terminal or NUL — is appended to the
  prompt; no prompt → exit 2). `-r` without an id opens the session picker (`/sessions`). The flags `--model`,
  `--agents`, `--max-agents`, `--budget`, `--title` become the overrides of a session the TUI creates. The
  prompt is the goal: its first line becomes the title, the rest the description (split by the hub, §17.2).
  `--team "architect, developer*2, tester"` (also with `-p`) is checked with `roles.parse_team` before anything
  starts (`dreamteam: --team: <error>` on stderr, exit 2) and reaches the TUI as the raw text (`overrides["team"]`,
  only when given), which turns it into `swarm.starting_roles` of the sessions it creates (`start_agents` = the
  team's size unless `--agents` is given). With auto-replies on, `-p` also approves the tasks that wait for the
  user's review (print mode `AUTO_REVIEW`, docs/TUI.md); with `--no-auto-reply` they wait.
- `--network on|off` (`tui`, `-p`, `serve` with `--goal`, exit 2 without it; `run`; `sandbox`): `[swarm] network`
  (§7.2) of the session that run creates (`overrides["network"]` → `swarm.network`, tui.commands.FLAT_OVERRIDES; serve:
  `create(..., overrides={"swarm": {"network": ...}})`), of `run`'s swarm for this run only (the settings file keeps
  its value), and for `sandbox` the firewall of the container (`docker.wants_firewall`) plus `--network` of the
  command inside (a project's `run`, or `serve --goal`). Neither a TUI-only nor a serve-only flag (`_default_command`).
- `--workspace DIR` (`tui`, `-p`, and `serve` with `--goal`): the folder the session created by that run works in
  (`create(..., workspace=)`, §17.2: an empty or missing folder becomes its workspace; a folder with files is
  adopted, its repository must be clean). `~` and a relative DIR are resolved against the current directory
  (`os.path.abspath`); the hub checks the rest. Exit 2 for a file, with `-c` / `-r` (an existing session moves
  with `/cd`), and for `serve` without `--goal`. The TUI applies it to the first session it creates (and says so
  under the welcome box); `-p` names the folder on stderr with `--verbose`; `serve` prints the result
  (`launcher_mcp.workspace_text`) on stderr.
- `--skill X` (repeatable; `tui`, `-p`, and `serve` with `--goal`, exit 2 without it): a skill attached to the
  session (§17.2 "Skills"). A URL (`scheme://` or `user@host:path`) is passed as given, a path (it has a separator,
  starts with `.` or `~`, ends in `.zip`, or is a folder or .zip here) is made absolute against the current
  directory, anything else is a reference checked before anything starts against the home's settings
  (`resolve_ref`; `claude-project:x` against the `--workspace` folder; not checked when resuming, the hub resolves it
  against the session's folder): `dreamteam: --skill X: <error>` on stderr, exit 2. The TUI attaches them to the session it creates, or
  with `-c` / `-r` to the resumed one (`POST .../start {"skills", "via": "cli"}`, live when it runs; `-r` without an
  id: the session picked; one attempt, so a failed one never attaches them to another session later); `-p` the
  same; `serve` passes them to `create(..., skills=, via="cli")`. `--skill` is not a TUI-only flag (`serve` takes it too).
- The directory: `--home`, else DIR, else the current directory when it has `dreamteam.toml`, else the default
  home (`serve` never takes the current directory: `--home`, DIR or the default home). It is a launcher **home**
  when `--home` is given, when it has `sessions/` next to `dreamteam.toml`, or when it is the default home;
  otherwise it is a single-swarm project (§15). `serve`, `mcp` and `sandbox` create a home on demand; the
  listing commands (`sessions`, `status`, `traces`) never create one.
- `sessions rm SID...` (also `delete`, `del`, `remove`) deletes sessions as the sessions page does: through the
  running hub (`DELETE /api/sessions/{sid}?confirm={sid}`, which stops a running session first), else with a
  `SessionManager` of the home — unless another live DreamTeam process may run the home's sessions without a
  hub that answers: a `hub.json` of a live process (or of a Docker sandbox), or a launcher marker
  `<home>/.dreamteam/launcher-<pid>.pid` that `serve`, `mcp` and the TUI's in-process launcher keep while they
  run (markers of dead processes are removed). Then nothing is deleted (exit 1). All ids are checked first (an
  unknown one or the project's own `main` session: nothing is deleted, exit 1). It shows the sessions and asks
  `[y/N]` in a terminal (Ctrl+C: exit 130); without one it needs `--yes` (exit 1). Before deleting without a hub
  it looks for a hub again. A workspace folder outside the session is kept and named in the output. A session
  whose members' proposals wait for the user's library decision is refused (409, the error plus " Pass --skills
  save (to the skills library first) or --skills discard.", exit 1) unless `--skills save|discard` is given
  (`&skills=` on the hub's DELETE, `delete(sid, skills=, via="cli")` without one). The words
  of `sessions` are `[DIR]` or `rm SID...`, in any order with the options.
- `hub.json` lives in `<home>/.dreamteam/hub.json`, written once the port listens (before the resume) and
  removed at exit (only by the process that wrote it). `running_hub(dir)` trusts it only when `GET /api/hub`
  confirms the pid and the directory.
- `serve`: refuses when a hub already runs for the home (opening that one in the browser unless
  `--no-browser`) or when the port is busy;
  `--goal` creates and starts a session (its first line becomes the title unless `--title`) and opens its board;
  the terminal console attaches to the running session when stdin is a real terminal (on Windows the NUL device
  is not one). Settings errors and unusable homes are messages, not tracebacks.
- `console`: with a launcher hub it works against `{base}/s/{sid}` (`--session`, else the running one); it
  refuses a URL where no hub answers and a stopped session, except a stopped session whose folder outside it
  does not exist (it cannot start, and `/cd` is what fixes it: the console attaches and says so). Commands include `/trace [Name|#id]`,
  `/span N [ctx]` and `/session` (the current session; another one is `dreamteam console --session ID`),
  `/approve N [comment]` and `/reject N what to fix` (a task in review; the comment is free text taken as typed,
  without one pair of quotes around all of it, as in the TUI; a name instead of N approves or rejects
  an MCP server), `/hire Role [| instructions]` (instructions `""` when omitted: a catalog role needs none) and
  `/wrapup [off]` (`swarm_control` wrap_up true / false, §3; the TUI's words: `on`/`вкл` or nothing, `off`/`выкл`).
  `/skills` (the user's `skills` tool) with `lib` (the library, the host library and Claude Code's skills, marked when
  attached; `GET /api/skills`), `add X [--pick a,b] [--replace]` (a reference → `skill_attach name`; a URL or a path,
  resolved as for `/cd`, → `skill_attach source`), `update X` (`skill_attach <from>:X` with replace; a member's
  proposal has no source), `rm X` (`skill_detach`), `show X` (the entry and the first 40 lines of SKILL.md;
  `from=session` when attached), `approve X [replace]` / `reject X [reason]` (`skill_approve`), `reinstall X
  [python|node]` (`skill_reinstall`), `log X [python|node]` (`GET /api/skills/X/deps-log`: the path and the log) and
  `clean-envs` (the launcher's `POST /api/skill-envs/cleanup`; a single-swarm hub has none).
  `/cd` prints the session folder (`GET {hub}/api/sessions/{sid}` → `workspace`); `/cd PATH` or `/cd --default`
  prints the question worded as in the TUI (`console.cd_preview`, docs/TUI.md §4.5) ending in `[y/N]`: the next
  line `y`/`yes`/`д`/`да` moves the session (`POST {hub}/api/sessions/{sid}/workspace`) and prints the result
  (`console.cd_result_lines`), `n`/`нет`/an empty line cancels, any other line cancels and is handled as usual.
  `--yes` (`-y`) at either end skips the question. `~` and relative paths (and a Windows path rooted without a
  drive, `\projects\app`, which gets the current drive as with `--workspace`) are resolved against the console's
  current directory only when the hub runs on this computer (`Console(..., local=)`): the console inside `serve`
  and a hub found through the home's `hub.json` are local whatever their address, a `--url` one when its address
  is a loopback one; otherwise only absolute paths. A single-swarm hub (no `/s/{sid}` in the base URL) has no
  `/cd`.
- `mcp`: a running hub (for a home: the launcher) → a stdio proxy to its `/mcp` that hands on its
  instructions; otherwise a home gets the in-process launcher (SessionManager, the web UI when its port is free,
  resumed sessions) with the stdio launcher MCP, and a project the in-process swarm (§13).
- `traces`: with a hub, the session's REST (§16.3; `--session`, else the running one); without one, the
  session database is read directly (default: the most recently active session, printed on stderr; a session
  that never started prints "No traces." without creating its database).
- `status` of a home lists the sessions (from the hub, or read from the home); of a project, `swarm_status`.
- `sandbox` refuses when a hub runs for the directory; the container runs `serve /swarm` for a home and
  `run /swarm` for a project. `docker.sandbox_command` starts it with `docker run --rm --init ...`: a tiny init
  (tini) is PID 1, reaps the orphans of agents' commands (dreamteam never does, and a zombie would keep a job's
  process group alive) and forwards `docker stop`'s SIGTERM to dreamteam, whose `Swarm.stop` ends the jobs within
  Docker's 10 s grace (a launcher home unloads its sessions one after another, so with jobs in several sessions the
  last final commits may be cut off; the processes die with the container anyway). Ports that agents' dev servers
  open inside the container are not published. Only the directory is mounted: for a home,
  `docker.outside_home_warning(home)` prints a warning listing the sessions whose workspace folder is outside it
  (`external_workspaces`): they cannot start in the container (their start reports the missing folder). A folder
  inside the home is found in the container (session.json `in_home`, §17.2). The host's skills library is mounted
  read-only at `/dreamteam/host-skills` with `DREAMTEAM_HOST_SKILLS_DIR` pointing there and `DREAMTEAM_SKILLS_DIR=
  /swarm/.dreamteam/skills-library` (the sandbox's own library inside the mounted home; not `.dreamteam/skills`,
  the default library's name: a home that is the user's home folder would make the host's library writable);
  `env_passthrough` never passes the two skills variables by name; `run_sandbox` creates the host folder first and
  warns when the library is inside the mounted directory (`library_inside_warning`: writable through `/swarm`
  then), `sandbox_command` stays a pure argv builder; `~/.claude/skills` is not mounted (§19). The skills'
  environments are a named volume per home, `dreamteam-skill-envs-<sha256 of the home's path, 10 hex>` mounted at
  `/dreamteam/skill-envs` (`docker.envs_volume`; `DREAMTEAM_SKILL_ENVS_DIR` and `UV_CACHE_DIR=/dreamteam/skill-envs/
  .cache/uv` set; never the host's envs: another platform); `docker volume rm` resets it. Node is not in the image,
  so Node skills show as unavailable there. The image also has `iptables` and the user `dreamteam-agent` for the
  sessions without network, git's identity and `safe.directory '*'` as `--system` settings, and `build_image` labels
  it `dreamteam.dockerfile=<dockerfile_hash()>`; the firewall (`--cap-add NET_ADMIN`, dropped again at once,
  `netoff.sandbox_entry`) and the rebuild of an image with another label: §7.2.
- `run [DIR]` stays the single-swarm server of a project (§15); it does not serve the launcher.

### 18.4 UI
- **Look** (the tokens of style.css, shared by both pages): one quiet surface per theme (light and dark; the system
  preference unless the theme button chose), hairline borders, one accent — the TUI's orange `#d97757` (filled
  controls carry dark text on it). Monospace (`--mono`) is for what is read as data: ids, numbers, times, costs,
  badges, column and section labels; the rest is the sans font. Status colours: in progress blue, waiting amber,
  review violet, done green, failed red; «working» (an agent, a running session) is the accent. Icons are line
  icons of `icons.css` (`<i class="ic ic-NAME">`, a CSS mask in the colour of the text), not emoji. A member's
  colour is the one of the TUI (tui/theme.py `agent_color`: the palette, crc32 of the case-folded name; app.js
  `agentColor`), so an agent looks the same in the terminal and in the browser; its avatar is a rounded square
  tinted with it. The agent's activity in the chat drawer follows the TUI too: a coloured bullet per step, a tool
  result hangs under its call (⎿).
- **Skills on both pages** (§19): `skills.js` (loaded before home.js and app.js; `Skills`, also a Node module for
  the tests) holds what they share: the add form (sources «Папка» — a folder or .zip path on the server's computer —,
  «Zip» — a file read in the browser and sent as base64, refused over `max_archive_mb` before the upload — and
  «Git» — a URL, `#ref:subdir` or a GitHub / GitLab tree link) with its flow (`runImport`: a source holding several
  skills lists them as checkboxes and posts again with `pick`; a clash with the library, 409 `exists`, asks «Заменить?»
  and posts again with `replace`; Enter in a field imports and never submits an enclosing form, such as the
  new-session dialog's), the report line of an import (imported, attached, failed, skipped files) and the
  packages preview (`previewLine`: «Установится автоматически: pypdf, … (requirements.txt).» plus the trust line
  «Установка запускает собственный код пакетов с вашими правами (вне песочницы Docker).», an unsupported manifest's
  reason, or «Установка пакетов выключена в настройках.»; `install.proposals` decides for a member's proposal). A
  skill's SKILL.md is shown as Markdown on the session page (`md()`, its YAML front matter left out: `bodyOf`; the
  card shows the name and description) and escaped as preformatted text on the home
  page (it has no Markdown renderer); every other skill text is escaped. The icon is `ic-skill` (a book).
- **home.html + home.js + home.css** (launcher page, `/`): header (DreamTeam, tabs «Сессии» / «Скиллы» /
  «Настройки», RU/EN, theme, connection state; the tab is kept in the URL hash, `#skills`); live updates from `/ws`
  plus a 5 s poll (a `sessions.changed` event with change "skills" or "skill-envs" reloads the skills); texts in ru
  and en.
  - Sessions tab: a big «Начать новую сессию» button, search and the filters all / running / stopped, and a
    card per session.
    - Card: title (link to the board), goal (or «ждёт запуска» for a pending goal), status badge (running /
      paused / stopped / goal done) and, for a running session in the user's wrap-up mode (summary `wrap_up`, read
      from the session database like `paused`), a warning badge «Завершается» / «Wrapping up», task progress bar, agents (working), spent/budget, model, last activity,
      the session folder (summary `workspace`: the path, cut at the front when long and whole in the tooltip,
      and a badge «своя папка» (default) / «папка проекта» (adopted) / «перенесена» (moved with /cd); a folder
      outside the session that does not exist gets the badge «папки нет» and a warning that the session cannot
      start until it is moved with `/cd`), the error of a damaged session. The search also matches the path.
    - Card buttons: «Открыть», «Запустить» / «Остановить» (disabled while the request runs), delete (not for
      the legacy `main`) with a confirmation dialog naming the session; for a folder outside the session the
      dialog says the folder stays, and the toast names it (`workspace_kept` of the DELETE answer). The web UI
      never changes the folder (`/cd`, MCP `session_cd` or REST do). When members' skill proposals wait for the
      user (summary `skills_pending`, or a 409 with `pending_skills` for one proposed since), the dialog names them
      and offers «Сохранить в библиотеку и удалить» (`?skills=save`; a library clash refuses, shown in the dialog)
      and «Удалить вместе с ними» (`?skills=discard`) instead of a plain «Удалить».
    - Skills on the card: «скиллов: N» (summary `skills`) and, while proposals wait, a warning button «Скиллы ждут
      вашего решения: N» that opens a dialog with one row per proposal (owner, description, files): «Сохранить в
      библиотеку» / «Не сохранять» with an optional reason (`POST /api/sessions/{sid}/skills/{name}/decide`, also on
      a stopped session; a library clash, 409 `exists`, asks «В библиотеке уже есть другой скилл «x». Заменить его?»
      and decides again with `replace`).
  - New-session modal: title (default: the goal's first line, derived by the server); goal (required,
    multi-line: the first line is the title, the other lines its description, explained by a hint);
    description; model profile (from the saved settings; a role with its own profile keeps it); language, max
    agents, budget; the starting team composer (below; from the saved catalog); «Скиллы»: a checkbox per skill
    of the library and of Claude Code (a library skill is sent as its name, Claude Code's as `claude-user:x`;
    one changed outside DreamTeam carries the warn badge «изменён вне DreamTeam» and ticking it asks «подключить всё
    равно?», as the board's attach dialog does), the skills the chosen team's roles bring (from the catalog's
    `skills`; one not in the library or ~/.claude/skills gets a warning chip «нет в библиотеке») and «Добавить новый:
    папка, zip или Git» (the shared form; what it imports is ticked); the ticked ones go as `skills`. Deviation from
    the skills design (§7.4): the modal has no workspace folder field (a new session works in its own folder), so it
    offers no `claude-project:x` rows and loads `/api/skills` without `?workspace=`; a project's `.claude/skills`
    skill is attached at create through the CLI (`--workspace` with `--skill claude-project:x`) or the launcher MCP,
    or later on the board; under «Дополнительно» steps per wake-up, MCP
    approval and «Без интернета для агентов» (`swarm.network = "off"`, §7.2; sent only when it differs from the saved
    default; its hint is `networkHint(network_isolation of GET /api/settings)`: strict only for "docker" / "netns",
    the "env" and "container" texts say plainly that it is not isolation, "container" that a restart of the sandbox
    adds the firewall); «Запустить сразу» with «Создать и открыть» (or «Создать»). Only values that differ
    from the saved defaults are sent as overrides (the team as `swarm.starting_roles` / `start_agents`). A stray
    click or Escape asks before discarding a typed goal. Independent review is not a field of the modal (the
    settings page has it; sessions can still override it through the API).
  - Team composer (new-session modal and the settings' defaults): one row per group of the starting team (§17.3
    `team_rows`; JS twins `teamRows` / `teamValue`): a role select over the visible catalog plus «Своя роль…» (a
    name not in the catalog), −/+ count (1..50), the role's first sentence (full description as tooltip) and its
    model profile, ✕; «Добавить роль»; the total. An empty team, an own role name with `,` or `*` (they separate
    team items) or more agents than the max is refused in the form; an untouched default is never written back.
    The new-session modal sends `starting_roles` only when the saved list, at the new size, would give another
    team (the saved list may hold more roles than its `start_agents`: they would fill a larger team).
  - Settings tab: sections «Модели и эндпоинты», «Роли», «Новые сессии по умолчанию» (with the team composer and
    the switch «Независимое ревью задач», `swarm.independent_review`, and «Интернет для команд агентов»,
    `swarm.network`, a select with the same honest hint), «Агенты» (with the planning hint's
    `agents.planning_hint_steps` / `planning_hint_minutes` and their hints), «Консоль», «Workspace», «MCP»,
    «Скиллы», «Трейсы», «Сервер», with a section navigation.
    - Roles: a card per catalog role (from `role_presets` and `settings.roles`). A built-in role: its name in
      both languages (read-only), «встроенная» / «изменена» marks, «Сбросить» and «Скрыть» / «Показать»;
      description (a changed one applies to agents of both languages), model profile, extra instructions. An own
      role: an editable name, the same fields, delete. Both have «Скиллы»: chips of the role's skills (✕ removes one,
      a warning one is not in the library or ~/.claude/skills) and an input with the known names as suggestions
      plus «Добавить» (Enter too; a name must be a skill name: lowercase letters, digits and `-`). Saved as
      `settings.roles` (only what differs from the built-in texts; `skills` when not empty); client checks (names,
      duplicates, a description for an own role, a known profile, skill names) are shown in the save bar.
    - Model profiles: one card per profile. Fields: name, provider, base_url, key (mode «Переменная
      окружения» with its state on the server, or «Ключ»: a password input that is never filled from the
      server, «сохранён …abcd», a button to remove it), model (input + datalist filled by «Загрузить список»),
      max_tokens, context window, the max_tokens ceiling (after a cut-off reply, §6.2), effort, thinking (Anthropic) or the max-tokens parameter (OpenAI), and under
      «Дополнительно» thinking display, prefix-mismatch handling and fallbacks (Anthropic), prices and extra
      request parameters (JSON). Buttons «Проверить» (models list) and «Тестовый запрос» (live); key-like text
      in the results is masked. Default profile (★, cannot be deleted), add from a preset, delete.
    - The external MCP servers are a JSON field; their `env` values come masked (`••••…abcd`) and are kept
      when sent back unchanged.
    - «Консоль» (`[shell]`, §17.1): the shell; the default wait for a command (`timeout`); «Наибольший тайм-аут
      команды» (`max_timeout`, with a hint: a command that went to the background at its timeout lives this long
      from its start, and an AI agent's wait itself is capped by the next field, 600 s over MCP); «ИИ-агент ждёт
      команду не дольше» (`foreground_max`); the switch «Долгие команды — в фон» (`background_on_timeout`);
      background jobs per member and per session (`max_jobs_per_member`, `max_jobs`); their time limit in minutes
      (`job_max_minutes`, 0 = none); their output in MB (`job_log_mb`); compute threads per command (`cpu_threads`:
      empty or "auto" in any case saves "auto", digits save the number, anything else is sent as typed for the
      server to refuse); the environment variables passed to the console (`pass_env`).
    - «Скиллы» (`[skills]`, §19): the library folder, Claude Code's skills on or off, the limits (MB and files per
      skill, an archive or Git copy in MB, skills per session, members' proposals per session, 0 = none), package
      installation (on, also for proposals, npm scripts), the environments' folder and Python, the time limit of one
      install, removing unused environments after N days (0 = never) and the installers' environment variables (a
      list); a note says changes apply from the next attach or session start.
    - A save bar with «Сохранить» (PUT) and «Отменить изменения»; client-side checks (profile names,
      duplicates, a model is required, JSON fields) and server errors are shown there. Leaving the tab or the
      page with unsaved edits asks first.
    - Models and limits apply to running sessions at once; server settings after a restart of `dreamteam serve`.
  - Skills tab (`GET /api/skills`): «Библиотека» (the folder named; a card per skill: name, description, files and
    size, «добавил(а): … · через …», warning badges «изменён вне DreamTeam» (with what it means) and «только по
    запросу» for a manual one, an error for a damaged entry; «Показать» opens its files, the packages preview and its
    SKILL.md, `?from=library`; «Удалить из библиотеки» asks first, `DELETE /api/skills/{name}?confirm=name`, sessions
    keep their copy), «Добавить в библиотеку» (the shared form), «Claude Code» (~/.claude/skills and the project's,
    read only: «Показать» and «В библиотеку», `POST /api/skills {claude, origin}`, with «в библиотеке» / «в
    библиотеке другой» badges by digest) and, inside the Docker sandbox, «Библиотека хоста» (read only; «В
    библиотеку», `{host}`). The hero has «Удалить неиспользуемые окружения» (`POST /api/skill-envs/cleanup`; the toast
    says how many were removed and kept).
- **index.html + app.js + traces.js** (session board, `/s/{sid}/`):
  - All API/WS/MCP URLs are relative to the session base `/s/{sid}` (`BASE`, the same id pattern as the
    server), with `""` when the page is served at `/` (`dreamteam run`).
  - Texts written by agents, external members or the user are shown as Markdown (`markdown.js`, loaded before
    app.js; app.js `md()`): chat messages, the agent's reply, thinking and compaction summary in its activity (tool
    calls and results stay raw), task description (formatted, «Изменить» opens its textarea) and result, discussion
    posts (review comments included), the goal's description and summary, trigger notes, role and MCP
    descriptions, the reply and thinking of an LLM span in «Трейсы», and `.md` / `.markdown` files in «Файлы»
    (formatted by default, a button switches to the source). A safe subset: everything is escaped, raw HTML is shown
    as text, links only to http(s) and mailto (opened in a new tab, `rel="noopener noreferrer"`), images become
    links, no math (`$..$`, `$$..$$`, `\(..\)`, `\[..\]` are shown as written, no emphasis or escapes inside); a
    single newline is a line break, and a text without Markdown looks as written (lines keep their indentation,
    extra blank lines stay); task links `#12` and @mentions work in the plain text (not in code, math or links). Toasts, the feed, one-line previews and tooltips stay plain. Rendered HTML is cached by text.
  - Topbar: «← Все сессии» link to `/` and the session title; a stopped banner with «Запустить сессию»
    (POST `/api/sessions/{sid}/start`, then reload). Actions on a stopped session show the stopped message.
    A folder outside the session that does not exist (snapshot `swarm.workspace`) adds a banner naming the
    path and the fix (`/cd FOLDER` or `/cd --default`).
  - Wrap-up (§3): a toggle button «Завершать» / «Wrap up» (`ic-check`, label hidden on phones) right after «Пауза»
    calls `swarm_control` {wrap_up: !snapshot swarm.wrap_up} (`toggleWrapUp`); while the mode is on the button is
    pressed (class `on`, `aria-pressed`, the warning colour, tooltip: click to switch it off) and a banner under the
    header says what it means (`#wrapup-banner`). The feed shows `swarm.wrap_up` («… включил(а) режим завершения:
    агенты завершают задачи с тем, что есть» / «… выключил(а) режим завершения»).
  - Reviews (§4.1): a card waiting for the user's review has the badge «ждёт вашего ревью», another one in review
    names its reviewer; the task card's «Принять» / «Вернуть» (a return needs a comment: what to fix; for a
    task another member reviews, the card says the user may decide instead). Dragging a task from «review» to
    «done» approves it, to another column opens the card for the return reason, and dropping onto «review» is
    refused (a task goes there by `task_complete`). With independent review on, the empty reviewer option reads
    «— (выберет доска)».
  - Hire dialog: the role input offers the visible catalog (`GET api/roles`) and shows the matched role's
    description and model profile (matched as the hire resolves it: any of its `names`, hidden roles included),
    or that a typed name is a new role to describe in the instructions; the
    model select starts with «— профиль роли или сессии» (`model` is sent only when chosen); instructions are
    optional for a catalog role (sent as "").
  - Goal modal: the goal is a textarea with the split hint (first line = title), sent unchanged (the hub splits).
  - Settings dialog (⚙ «Настройки роя»): budget and max agents (`swarm_control`, stored in the session's
    database; sent only when changed). On a launcher session other than `main` it loads `GET /api/sessions/{sid}`
    and `GET /api/settings` (launcher routes, without `BASE`) and adds the groups «Фоновые процессы»
    (`shell.max_jobs_per_member`, `max_jobs`, `job_max_minutes`, `cpu_threads`) and «Агенты»
    (`agents.max_steps_per_turn`, `context_tokens`, `tool_output_chars`, `idle_nudge_seconds`,
    `max_parallel_llm_calls`, `planning_hint_steps`, `planning_hint_minutes`) and «Сеть» (`swarm.network`, kind
    `choice`: a select «как в общих настройках (…)» / «есть» / «нет», with the hint `networkHint` of the launcher's
    `network_isolation`, §7.2) (`SESSION_FIELDS`, where a field's `hint` key names its hint text). Each input holds the session's own override (empty when none) and
    shows the global value (the public view of `/api/settings`, defaults filled) as its placeholder; a hint says
    that an empty field takes the general value and that changes apply at once (steps from the agent's next
    wake-up, the rest from the next command or call), the context field that a model profile's own window wins
    (naming it when the session's profile has one). Save checks the values with config.py's minimums (whole
    numbers; `idle_nudge_seconds` and `planning_hint_minutes` numbers; `cpu_threads` "auto" or a whole number ≥ 0; the first error is a toast
    and gets the focus), then sends `PATCH /api/sessions/{sid}` `{"settings": ...}` with only the changed keys (an
    emptied field is null) before `swarm_control`, so a stopped session takes them too; when the PATCH succeeds and
    `swarm_control` fails, a toast says the session parameters were saved and the dialog stays open with them counted
    as saved. An emptied field counts as an unsaved edit when the dialog is closed (it removes an override). ⚙ is
    disabled while the two requests load, and a modal opened meanwhile is not replaced; if they fail, the dialog
    keeps budget and max agents with a line that the session parameters did not load. The pure parts are
    exported for the Node tests (`parseSessionField`, `sessionSettingsPatch`, `sessionFieldsHtml`, `contextHint`).
    `main` and a standalone `dreamteam run` board show only budget and max agents, with a line that the other
    parameters are in the general settings.
  - Header: the badge «Без интернета» (`#net-badge`) while the snapshot's `swarm.network.mode` is "off", its
    tooltip `networkHint(swarm.network.isolation)` (§7.2); the sessions page's card shows the same badge for a
    summary with `network: "off"`.
  - Background jobs (§7.1; «фоновый процесс» in Russian, shown as j3): the tab «Процессы» (badge: the running
    ones) has a card per job of the snapshot's `jobs` (running, then ended in the last hour): label, owner chip,
    state («работает 25 мин»; at the end by status and reason: «завершён: код 0», «завершён» for a job with no
    code of its own, «остановлен вами», «остановлен по пределу времени»…), for a silent one (`silent_since`, §7.1)
    a warn badge with the clock icon «40 мин без вывода» (tooltip: it may hang, look at its output and stop it if it
    is stuck), origin, process names while it runs,
    how long it ran and when it ended, and its log (a workspace log is a link that opens it in «Файлы»).
    «Вывод» shows its last 200 lines (`POST api/act/jobs {job_id, tail: 200}`), refreshed every 3 s while the
    tab is open and the job runs; «Остановить» asks first («Остановить процесс j3 и всё, что он запустил?»),
    then `POST api/act/job_stop {job_id}`. Keyboard focus stays on a card's button when the cards are rebuilt.
    In the team panel a member with running jobs has the marker ⚙N (tooltip «фоновых процессов: N»), and its
    drawer lists them, each with «Остановить». The feed shows `job.started` / `job.ended` (FEED_TYPES `job.*`).
    The header's agents counter is `swarm.agent_count` (members who joined through MCP do not count) against
    max_agents.
  - Tab «Файлы»: above the tree, the session folder (`swarm.workspace`): its absolute path (click to copy) and
    the badge «своя папка» / «папка проекта» / «перенесена» / «папки нет»; the tab button's tooltip is the
    path. A missing folder is not listed (no `/api/files` call). When the path (or its existence) changes
    after a `/cd`, the tab starts again from the root of the new folder. The viewer highlights the syntax of a file
    (and the source of a `.md`) by its name (`highlight.js`, in-house, offline, loaded before markdown.js; also
    Markdown fences that name a language): Python, JS/TS, JSON, TOML, YAML, INI/.env, shell, Dockerfile, Makefile,
    PowerShell, batch, HTML/XML/SVG, CSS, SCSS/LESS, SQL, C/C++, C#, Java, Kotlin, Go, Rust, Ruby, PHP, Lua, R, Julia, LaTeX,
    Markdown, diff; other kinds (CSV, text) and files over 300 000 characters stay plain. Safe and copy-exact:
    everything is escaped, the only markup is `<span class="hl-…">`, the text content is the file; one linear pass.
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
  - Skills tab (§19; the tab after «MCP», a warning badge with the number of proposals waiting): a bar with
    «Подключить скилл» and what a session's copy means, then a card per attached skill (the snapshot's `skills`):
    name, description, badges — where it came from, «роль: X» / «от роли X» (`attached_via`, `roles`), «автор: X» and
    «ждёт вашего решения» / «в библиотеке» / «не сохранён» (with the reason) for a member's proposal, «только по
    запросу», «источника нет» when its source is gone —, files and size, and «Пакеты»: a row per ecosystem
    (`deps`): «готовы» with the environment's path (click to copy), «ставятся…» (in the queue / another DreamTeam
    process builds it), «ошибка» with the error, «Журнал» (`GET api/skills/{name}/deps-log`) and «Переустановить»
    (`skill_reinstall`), or «вручную» with the reason (off, proposals off, missing on this machine, another Python,
    unsupported). Buttons: «Показать» (files and SKILL.md, `?from=session`), «Сохранить в библиотеку» / «Не
    сохранять» for a pending proposal (`skill_approve`; the clash text asks before `replace`), «Обновить» only when
    the skill's own source changed (its `source_digest` in `GET api/skills` differs from the copy's digest; never
    for a member's proposal: `<from>:name` with `replace`), «Отключить» (asks first). «Подключить скилл» opens a
    dialog listing the library's, the host's and Claude Code's skills not attached yet (a warning for one agents can
    write or one changed outside DreamTeam asks before attaching), each with «Показать» and «Подключить»
    (`skill_attach`), plus the shared form with «Добавить и подключить» (`attach: true`). On a standalone board
    (`dreamteam run`, no launcher home page) the tab also has the library itself under «Библиотека скиллов» (show,
    delete). The feed shows `skill.*` (attached, updated, detached, proposed, saved or not, packages ready or failed);
    a member's row and drawer and the hire dialog's role preview show the role's skills.

## 19. Skills (skills.py)

A **skill** is an Anthropic Agent Skill: a folder whose root holds `SKILL.md` (YAML front matter with `name` and
`description`, then instructions) plus any other files (`scripts/`, references, templates). Agents see names and
descriptions and read a SKILL.md when a task matches it (progressive disclosure). In Russian texts: «скилл».
Nothing from a skill is ever run, imported or rendered as HTML by DreamTeam: it is data until an agent reads it.

**Concepts.** The *library* (`[skills] library_dir`, default `~/.dreamteam/skills`, §17.1) is shared by every
session of every home on the machine. A library entry's *origin* says where it came from: `folder`, `zip`, `git`,
`agent` (a proposal the user saved), `claude-user`, `claude-project` (copied from Claude Code) or `host-library`
(copied in the Docker sandbox from the host's library). *Claude Code skills* (candidates) are found read-only in
`$CLAUDE_CONFIG_DIR/skills` (default `~/.claude/skills`; origin `claude-user`) and in `<workspace>/.claude/skills`
(origin `claude-project`, agent-writable). Claude Code plugin skills (`~/.claude/plugins`) are not offered (v1).

**Names** (`NAME_RE = [a-z0-9]+(-[a-z0-9]+)*`, `NAME_MAX = 64`; "anthropic" and "claude" are allowed):
`check_name(name)` (SkillError 400) is called by every function that takes a name before any path is built (reads,
writes, removals), and `child(root, name)` asserts the joined path resolves to a direct child of `root`: `..`, `.`,
`a/b`, `..\x`, a decoded `%5C` and a planted link never reach a file operation. `slug(text)` makes a name of any
text (Cyrillic transliterated, accents dropped: "My Skill" → `my-skill`). `for_agents(text)` replaces `<` / `>`
with ‹ › in every text built for agents.

**References** (`parse_ref(text) -> (source | None, name)`, `looks_like_ref(text)`): `x`, `library:x`,
`claude-user:x`, `claude-project:x`, and `host-library:x` in the sandbox. A Git scp URL (it has an `@`) and a
Windows path (a one-letter drive) are not references. `resolve_ref(config, ref, *, workspace=None) -> {"origin",
"name", "path"}`: a prefixed reference uses only that source; a bare name the library, then `claude-user` (when
`[skills] claude`), then `host-library` (sandbox only). `claude-project` is used **only** when named, because agents
can write `<workspace>/.claude/skills` (and a cloned repository may bring one). Nothing found: SkillError 404 "No
skill 'x' in the library or in ~/.claude/skills. Library: a, b." plus, when the workspace has one, "attach
claude-project:x to use it".

**Validation** (`check_skill_dir(folder, limits, *, strict=False, digest=True) -> SkillInfo(name, description,
path, files, size, digest, manual, manifest, renamed_from, skipped)`; `Limits(max_mb, max_files, max_archive_mb)`,
`Limits.from_config(config)`):
- `SKILL.md` at the root, spelled exactly (`skill.md` only on a case-insensitive file system, stored as `SKILL.md`),
  a regular file (not a link), at most `SKILL_MD_MAX` = 100 000 bytes, UTF-8 (a BOM is stripped). The skill folder
  itself must not be a link.
- `parse_frontmatter(text) -> dict[str, str]` (stdlib, no PyYAML): the first non-blank line is `---`, the front
  matter closes with `---` or `...` within 200 lines; top-level `key: value` with plain values (a ` #comment`
  stripped, indented continuation lines folded), `'single'` (`''` escape) and `"double"` (`\" \\ \n \t`) quotes, and
  block scalars `|` / `>` (with `-` / `+`); nested maps and lists are skipped, unknown keys ignored except
  `disable-model-invocation: true` → `manual` ("only when the user asks for it").
- `name`: the front matter's, else the folder's name (`fallback`: for a zip's or a clone's root, which is a
  staging folder, the zip's file name without `.zip`, or the Git subdir's last part, else the repository's name
  without `.git`). `strict=True` (agent proposals): it must match the rule as written; otherwise (imports, Claude Code) it is slugged and the report says `renamed_from`. The copy is stored under
  this name, not the folder's.
- `description`: required; whitespace collapsed; 1–1024 characters. `<` / `>` are kept (replaced only in agent
  texts); `strict=True` refuses tag-like text (`<tag`).
- The tree: `.git`, `node_modules`, caches (`workspace.NO_EXPAND`), `.DS_Store`, `__MACOSX`, `Thumbs.db` and our
  `.dreamteam.json` are skipped silently; links and junctions (`workspace.is_link`) and special files are skipped
  and reported in `skipped: [{path, error}]`, never followed; every path part must be portable (the Windows rules on
  every OS: no `<>:"|?*\`, control characters, trailing dot or space, reserved device names); two paths equal after
  NFC + casefold fail the skill naming both; at most `max_files` files, `max_mb` MB and 8 folder levels (413).
  Binary files are allowed.
- `digest`: sha256 over the sorted `"<posix rel>\0<sha256 of the bytes>"` lines (without `.dreamteam.json`): "same
  content". `manifest`: `[[rel, size, mtime_ns]]` of the walk, cheap to recompute (`stat_manifest`): "changed since".

**Safe copy** (`copy_skill(src, dest_parent, name, limits, *, strict=False, info=None, readonly=False, meta=None)
-> SkillInfo` of the copy): validated first (`info`: a check already made); built file by file in
`dest_parent/.t-<hex6>-<epoch>/` (same volume, a short name); each file is opened only after `lstat` says it is a regular
file, and the opened file must be that same file (`fstat` inode), so a link swapped in after the check is never
followed; the digest of the bytes written must equal the checked one ("the folder changed while it was being copied;
try again", 409). Source mode bits `& 0o755` are kept (scripts stay executable, the owner can write); `readonly`
makes the files 0o444 / 0o555 (the agents' mirror); `meta` writes `.dreamteam.json` into the copy before it appears.
The swap: an existing `<name>` (or a link there, never followed) is renamed to `.o-<hex6>-<epoch>`, the new folder
renamed in, the old one removed; a failure restores the old one. Renames are retried 5 times over about a second on `PermissionError` (a scanner or an indexer
holding a file). Every file operation uses `workspace.long_path`. `clean_stale(parent)` removes `.t-*` / `.o-*`
folders older than an hour (`Library.list` and staging call it), judged by the later of the time in the name and the
mtime (a rename keeps an old mtime: an `.o-` copy mid-swap must not look stale).

**The library** (`Library(root, limits=None, *, read_only=False, config=None)`, `Library.from_config(config)`,
`Library.host(config)`): one folder per skill; `.dreamteam.json` = `{name, description, origin, source, added_at,
updated_at, digest, manifest, by, via, files, size, manual}`, where `source` is `{path}` (folder, Claude Code, host
library), `{filename, subdir?}` (zip) or `{url, ref, subdir, commit}` (Git; the redacted URL), `by` is `user` or a
member, and `via` the surface (`act`, `mcp`, `launcher-mcp`, `stopped-session`, `home`, `cli`).
- `list()` (sorted; folders whose name is not a skill name and dot folders are left out), `get(name)`,
  `path(name)` (a valid entry's folder), `show(name)` (entry + `show_folder`: `files_list` [{path, size}] ≤ 500,
  `skill_md` and `deps`; a link entry `deps: {}`),
  `digest_of(name)` (hashed again when the entry changed outside), `add(folder, origin, source, *, replace=False,
  by="user", via="act", strict=False, info=None)`, `remove(name)` (renamed away, then removed; 404 when missing; a
  link removes only the link).
- A public entry: `{name, description, origin, source, added_at, updated_at, files, size, digest, manual, by, via,
  changed_outside}`; a folder that fails validation is `{name, error, origin}` (shown, not attachable), and so is a
  link or junction there (never followed: one link cannot break the listing). Missing metadata (a folder dropped in,
  or its `.dreamteam.json` deleted) is rebuilt from SKILL.md once: origin `folder`, `by` "", `via` `outside` and
  `outside: true`, which keeps `changed_outside` true.
- **Tamper signal**: `changed_outside` is true when the stat walk differs from the stored manifest (a file edited,
  added or removed behind DreamTeam's back) or the entry was not added through DreamTeam; it stays until the skill
  is imported again (which records the new `by` / `via`) or deleted. A signal, not a protection.
- `add`: the same name with the same digest is `"unchanged": true` (only `updated_at` and `source` change, and a
  stale manifest is rewritten); other content is SkillError 409 `exists: [name]` unless `replace` (then `added_at`
  is kept). No lock between processes: every change is a rename.
- `read_only` (the host library in the sandbox): `add` / `remove` raise 403 "The host's skills library is read-only
  inside the sandbox."
- `sessions._plan` refuses a workspace that is the skills library, inside it or containing it (§17.2).

**Sources.** `stage_source(lib, spec, *, pick=None, workspace=None) -> Staged` fetches and validates into
`library_dir/.t-<hex6>/` (nothing visible in the library); `Staged.commit(replace=False, by="user", via="act")`
checks every clash first (409 `exists` listing all of them, nothing added) and adds the picked skills with
`Library.add`; `Staged.discard()` (also on leaving `with`) removes the staging folder; `Staged.conflicts()` names
the picked skills the library has with other content; `import_source(lib, spec, *, pick, replace, workspace, by,
via)` is stage + commit. `spec`: `{"path": abs}` (a folder on the hub's machine, or a `.zip` file read from disk;
relative: 400), `{"zip": base64, "filename"}`, `{"git": url}`, `{"claude": name, "origin": "claude-user" |
"claude-project"}`, `{"host": name}` (sandbox). The report: `{"imported": [entry + "unchanged"], "candidates":
[{name, description, path, files, size, digest, manual, renamed_from?, error?, deps}], "skipped": [{path, error}],
"need_pick": bool}`.
- `find_skills(root, limits, max_depth=4)`: the root alone when it has a SKILL.md, else every folder with one down to
  4 levels (no links, the skip-list left out, never below a found skill); each candidate is checked; a second one
  with the same name gets "duplicate name". No valid candidate: the single candidate's own error, or 400 "No SKILL.md
  found in …"; exactly one valid or `pick` given: those (an unknown name is 400, a picked candidate's error is
  raised); several without `pick`: `need_pick`, nothing imported.
- Folder and Claude Code sources are copied into the staging folder first, so the commit takes what was checked.
- Zip (`extract_zip(data, dest, limits) -> skipped`): the base64 text is capped before decoding
  (`Limits.zip_b64_max`, 413); members: `\` read as `/`; absolute names, drive letters, `..`, `.` and empty parts,
  duplicates, NFC + casefold collisions (also a file beside a folder of that name), link entries and encrypted
  entries are refused; directories are implied; `__MACOSX/`, `.DS_Store` and `.dreamteam.json` skipped; the
  portable-name rule applies; at most 5000 entries; the declared sizes and the bytes actually streamed are both
  capped by `max_archive_mb` (413); a member over 1 MB compressed more than 200 times "looks like a zip bomb" (413);
  a damaged archive is a 400; exec bits come from `external_attr`. `max_mb` / `max_files` apply per picked skill,
  so a multi-skill "Download ZIP" of a repository fits. Origin `zip`, `source = {filename, subdir?}`.
- Git (`parse_git_source(text) -> GitSource(url, ref, subdir, shown, scheme)`, pure): `https://`, `ssh://`,
  scp-like `user@host:path` and `file://`; plain `http://`, `transport::` remote helpers, a leading `-`, spaces, a
  user or host starting with `-` and invalid hosts are refused. `URL#ref:subdir`, `URL#ref`, `URL#:subdir`; GitHub
  `/tree/<ref>/<sub>` and `/blob/<ref>/<sub>/SKILL.md` and GitLab `/-/tree/` / `/-/blob/` become the repository URL,
  the ref and the folder (a ref with `/` in a tree URL cannot be told from the path: `URL#ref:path`). Refs match
  `[A-Za-z0-9][A-Za-z0-9._/-]*`, ≤ 200 characters, without `..`, `@{`, `//` or a trailing `/`, `.` or `.lock`;
  folders are plain relative POSIX paths (no `\`, `:`, `..`, `.` or empty parts) and are checked again after the
  checkout (inside the clone, no link on the way). **Credentials**: `shown` has no user info (except the bare user of
  ssh / scp URLs), no query and no fragment; it is the only form stored (`source.url`), shown, logged or put into an
  error; `redact(text, src)` removes the full URL, the user info (git prints it without the scheme too) and any
  `//user:secret@` from git's output. A credential helper is recommended over a token in the URL.
- `clone(src, dest, limits, git=None, timeout=120)` (a dedicated helper, not `run_git`): no git → 400 "git is not
  installed: …"; `-c` options before the subcommand: `credential.interactive=never`, `core.symlinks=false`,
  `core.longpaths=true`, `protocol.allow=never` with `https` and `ssh` allowed (`file` only for a `file://` URL: a
  user `url.<base>.insteadOf` can never reach a remote helper), `core.hooksPath=<staging>/nohooks` (an empty folder:
  no global or system hook runs), `submodule.recurse=false`, `fetch.recurseSubmodules=false`. With git ≥ 2.35:
  `clone --depth 1 --single-branch --no-tags --filter=blob:none --no-checkout [--branch=<ref>] -- URL DEST`, then
  `sparse-checkout set --no-cone -- /<subdir>/` (or `/*`) and `checkout` (only the folder's blobs are fetched; a
  server without filter support falls back to a full shallow clone); older git: a plain shallow clone. The
  environment (`clone_env`): `git_env()`, `GCM_INTERACTIVE=never`, `GIT_LFS_SKIP_SMUDGE=1`, the user's
  `GIT_SSH_COMMAND` / `GIT_SSH` / `GIT_SSH_VARIANT` put back, and `GIT_SSH_COMMAND="ssh -o BatchMode=yes"` when the
  user has neither those nor a `core.sshCommand` (a host-key question or a passphrase fails at once). stdin is
  closed, and on POSIX git runs in a new session (no controlling tty for ssh to read). A watchdog sums the staging
  folder every half second and kills git (`shell.kill_tree`) past `max_archive_mb` (413 "point the URL at the
  skill's folder") or the timeout. The user's git configuration is only read, nothing is ever written. Errors give
  the first `fatal:` line, redacted: "Could not clone <shown>: …". `rev-parse HEAD` gives `commit`. Origin `git`,
  `source = {url: shown, ref, subdir, commit}`.
- Claude Code (`claude_skills(config, workspace=None) -> [item]`, `[]` when `[skills] claude` is false): one level
  of folders in each Claude Code skills folder (links skipped); an item is `{name, description, origin, path, files,
  size, digest, manual, renamed_from?, library: "absent"|"same"|"different", agent_writable? (claude-project),
  error?}`; names and descriptions are normalised with `strict=False`, so existing Claude Code skills are attachable.
  "Add to library" is `{"claude": name, "origin": …}` (origin `claude-user` / `claude-project`, `source = {path}`).
  SKILL.md files written for Claude Code may name its tools or `${CLAUDE_SKILL_DIR}`: they are copied unchanged.
- Requests (`import_request(config, body, workspace=None, *, by, via)`): exactly one of `path`, `zip`, `git`,
  `claude` (or `host`), else 400 "Give exactly one of path, zip, git, claude."; `pick` a list of strings, `replace`
  a bool; a `workspace` for `claude-project` must be absolute. `read_import_body(request, config)` reads a JSON
  body capped by `body_limit(config)` (the base64 of `max_archive_mb` plus 64 KB; 413 from Content-Length or while
  streaming, 415 when not JSON). `SkillError(message, status, **extra)` maps to the HTTP status; `to_json()` is
  `{"error", **extra}`.
- Previews (what the user sees before attaching): `show_folder(folder, limits) -> {files_list, skill_md, deps}`
  (`deps: {}` and `error` when it cannot be read); `deps_preview(folder) -> {"python"?: {files, packages,
  unsupported?}, "node"?: {files, packages, lock, unsupported?, ignored?}}` (`skill_deps.detect`: files read, no tool
  probed, nothing started; `{}` without manifests or when they cannot be read); `install_info(config) -> {enabled,
  proposals, scripts}` (`[skills] install`, `install_proposals`, `install_scripts`), which the skills routes add as
  `install` so a preview can say whether the packages would be installed.

**The session's skills** (swarm_skills.py; `Swarm.skills: SessionSkills`). A skill attached to a session is
visible to all its members. No database table: the authoritative copies are `<data_dir>/attached-skills/<name>/`
(`skills_dir`; not `skills/`, since for a project in the home folder the data dir is `~/.dreamteam`, whose `skills/`
is the default library; outside every workspace, so never committed; they survive `/cd` moves and are deleted with
the session), each with the session's `.dreamteam.json` = `{name, description, digest, files, size, from: library | claude-user | claude-project
| host-library | agent, source (the library entry's source plus its "origin", or {path}), attached_by,
attached_at, updated_at, owner (from = agent), attached_via: user | role:<Role> | create | proposal, manual,
library: none | pending | saved | rejected, comment, decided_while_stopped}`. `read_session_skills(data_dir, library_dir=None)`
lists them without a Swarm (a stopped session); a folder without valid metadata is left out. `library_clash(data_dir,
library_dir)`: the session's folder is the library, or inside it or contains it (a `library_dir` set to it); then
nothing is read from it, `load()` cleans nothing there, and attach and detach are 409.
- `SessionSkills`: `load()` (in `Swarm.__init__` and `start()`; `.t-*` / `.o-*` leftovers cleaned), `list()`,
  `get(name)`, `names()`, `pending()`, `public()` (the snapshot's `skills`, §14.1, from memory), `shown_path(name) ->
  (text, inside)`, `agent_dir()` (`DREAMTEAM_SKILLS` of the agents' shells), `role_skills(member)` (the names its
  catalog role lists, resolved live), `source_digest(skill)` (its own source's digest now: the library entry for
  `library`, the folder at `source.path` for Claude Code, the host library; None when gone or for `agent`),
  `detached()`, `proposed()`, `hire_report(name)`.
- `async attach(actor, ref, *, replace=False, announce=True, attached_via="user", via="act", exclude=()) -> (SessionSkill,
  "attached" | "updated" | "unchanged")`: only USER (ToolError "Only the user can attach skills to the swarm.");
  the reference is resolved with `resolve_ref` against the session's workspace (`claude-project` only when named);
  under the lock: the same name with the same digest is "unchanged", other content is ToolError 409 unless
  `replace`, a new name past `[skills] max_attached` is 409; `copy_skill` into `<data_dir>/attached-skills/` with the
  metadata, then its script dependencies planned in a daemon thread and held in use ("Script dependencies" below),
  the mirror sync, `skill.attached`, the name leaves kv `skills_detached`, (`announce`) `n_skill_added` /
  `n_skill_updated` to the members but `exclude`, and last `ensure_deps([name])`, which only starts the builds.
- `async attach_source(actor, source, *, pick=None, replace=False, via="act") -> ImportReport + {"attached": [{name,
  status, files}], "failed": [{name, error, status}]}`: a folder or .zip path on the hub's machine, or a Git URL
  (`source_spec`: `scheme://` or `user@host:path` is Git), imported into the library (`import_source`, `by` user),
  then each imported skill attached as `library:<name>`; one attach refused (the cap, a clash) leaves the others
  attached and is listed in `failed` (it stays in the library); `skill_attach` reports both and is an error only
  when nothing was attached. With `need_pick` nothing changes.
- `async detach(actor, name, *, via="act") -> SessionSkill`: only USER; 404 when not attached; the session copy is
  renamed away and removed, its environments leave the session's in-use set (they stay for other sessions and
  cleanup), the mirror synced, `skill.detached`, the name added to kv `skills_detached` (a role never attaches it
  again until the user does), `n_skill_removed` to the members.
- Errors are ToolErrors carrying `status` (and `extra`, e.g. `exists`) for REST callers (`skill_error`).
- `attach_files(data_dir, folder, name, frm, source, limits, *, info=None, attached_via="create", library_dir=None)
  -> (SessionSkill, "attached" | "unchanged")` (module function, no Swarm): a checked folder copied into a session
  being created or a stopped one about to start (`SessionManager`, §17.2); the same digest is "unchanged", other
  content 409 (`exists`), a `library_clash` 409; the caller checks `max_attached`. `attached_again(names)` takes such
  names out of kv `skills_detached` once the Swarm runs.

**The mirror** `<workspace>/.scratch/_skills/<name>/` (`workspace.SKILLS_MIRROR`) is what agents read: `read_file`
cannot leave the workspace, and a link would be refused, so it is a real copy (`copy_skill(..., readonly=True)`:
files 0o444 / 0o555, the read-only attribute on Windows; no `.dreamteam.json`). Its expected state lives only in
memory (`_mirror`: name -> (session copy digest, the mirror copy's listing as it was written, from copy_skill's
manifest)); nothing in the mirror is trusted. A sync (in a thread, under the skills lock and a files lock that the
removal at stop shares) runs `ensure_scratch()`; when `scratch_is_local(root)` it rebuilds a copy when asked for a
full sync, when it is missing or a link, when the session copy's digest changed, or when `_listing` of it differs
(every entry with its kind, size and mtime: unlike the skill walk, the skip-list's names such as `node_modules/` or
`.git` and links count too, and the walk stops past the expected number of entries) (each rebuild is copy_skill's
build-and-swap, so a reader never sees half a skill), and removes everything else there (detached skills, folders a
command put there). A removal Windows refuses (a process holding a file there or using
it as its working directory) does not fail the caller: the name goes into `_pending_removal`, is left out of the
index and retried by every later sync. Syncs: full at every `Swarm.start`, on every attach and detach, and from the
scheduler at most every `SKILLS_CHECK_SECONDS` (60 s), so a tampered mirror is repaired within a minute and at every
start. `write_file` / `edit_file` refuse the mirror (§7). The listing leaves out the places of DreamTeam's own
`node_modules` links, which are checked by themselves ("Script dependencies"). Without a local `.scratch` there is
no mirror and agents are shown the session copy's absolute path with `skills_path_outside` ("read it with `shell`");
the Node link then goes into the session copy. In an adopted folder
`Swarm.stop` removes `.scratch/_skills` after the agents stopped; a session's own folder keeps it (it is rebuilt at
start anyway).

**Delivery to agents.** The system prompt is frozen at hire and the tool array until a compaction, so running agents
learn through texts: new members' system prompts have a generic **Skills** bullet (no concrete names) and item 1 of
"How you work" says the wake-up shows the skills. The **Skills index** (§6.3): `skills_index` (with
`skills_index_own` when the member's role lists an attached skill) followed by one `skills_item` line per skill
(`- name{own}: description → path{deps}`): the member's role skills first, then the rest, by name; `own` is `skills_own`
(" (your role's)") for its role's skills plus `skills_manual` for a `disable-model-invocation` skill; the description
is cut to 300 characters and `<` / `>` become ‹ ›; the path is `shown_path`; `deps` is the dependency fragment
(`deps_fragment`, "Script dependencies"), "" for a skill without manifests. The header tells how to use a skill
written for Claude Code (its tool names mapped to `shell`, `read_file`, `write_file`, `edit_file`;
`${CLAUDE_SKILL_DIR}` = the folder shown) and what a line's packages part means (a named Python runs that skill's
scripts, install nothing into it; linked Node packages are found, run no npm install or npm ci in the skill's
folder; a line without one lists none: install what SKILL.md asks for with `shell`, best in the scratch folder). It
is shown in full once per agent memory (`Swarm._skills_shown`), then
as `skills_names` (`names_line`); `AgentRuntime._replace_with_summary` puts the full index into the compaction text
(after the notes) and sets `skills_shown(name, True)`, or None when there is none. Notifications
(`SessionSkills._announce(key, exclude, *, name, description=None, owner=None)`: every non-retired member but
`exclude`, AI agents with `wake=False`, external members with a wake so `wait_for_events` returns; `path` and
`deps` per recipient): `n_skill_added` {name, description, path, deps} on an attach, `n_skill_updated` on a replace,
`n_skill_removed` {name} on a detach, `n_skill_deps_ready` / `n_skill_deps_failed` {name, deps} when a build this
session waited for ends. External members have no wake-up index: `join_swarm` lists the attached skills
(also on a rejoin, "Welcome back"; it adds that `skill_propose` proposes one) and `swarm_status` gives "Skills: N attached (a, b)" (with the item lines for a
member, not with `brief`; "; packages being installed for 1 (pdf)" / "; packages failed for 1 (pdf)" when that
applies); `agents` ends with
"Skills attached to the swarm: a, b (the wake-up lists them; read their SKILL.md with read_file)." The user's tools:
`skills` (the attached list with source, who, description, path and "source changed" / "source gone" against
`source_digest`, and a `packages: python ready; node manual (...)` line; `available=true` adds the library's, the
host library's and Claude Code's skills not attached), `skill_attach` (name or source; its result ends with " Its
packages (requirements.txt) are being installed; members are told when they are ready." or " Its packages are
installed already."), `skill_detach`, `skill_reinstall` (§12).

**Proposals** (`skill_propose(path)`, audience llm and external: an LLM tool of frozen revision 3, so new members
and external members have it at once and older agents after their next compaction, whose `tools_added` names it;
the system prompt's Skills bullet ends with the proposing sentence, which agents hired before revision 3 never got).
`SessionSkills.propose(actor, path)`: not the user ("Members propose skills; the user attaches them with
skill_attach."); `[skills] max_proposals = 0` → "Proposing skills is turned off in this swarm's settings."; the path
goes through `Workspace.resolve` (inside the workspace, never `.git`), a `SKILL.md` file means its folder, and a
folder under `.scratch/_skills/` is refused ("That is the swarm's copy of an attached skill. Copy the folder
(without node_modules) into your scratch folder, edit it there, and propose that copy."); `check_skill_dir(strict=True)`
(the name as written,
no tag-like description), its error followed by " Fix SKILL.md and call skill_propose again.". Names (ToolError 409,
with a suggested free name: `<name>-<the proposer's first name, slugged>`, then `<name>-2`, ...): a name attached by
the user, a role or another member → "A skill named 'x' is already attached; choose another name, e.g. 'x-ann'.";
a name the library or the host library has, or a catalog role lists, unless it is the proposer's own attached
proposal → "The skills library already has a skill named 'x' (or a role lists it); choose another name, e.g.
'x-ann'." (a later role attach would take the draft for the role's skill, and the approval would clash). Count: kv
`skills_proposed` holds every name members proposed in the session (detached ones too); a new name past
`max_proposals` is 409; proposing one's own name again replaces it and counts once; a new name past `max_attached`
is 409. The same content as the attached copy changes nothing ("Your skill "x" is unchanged ...") while the
session copy still holds it (else it is copied again, as a replacement). Otherwise, under
the lock, `copy_skill(strict=True)` into the session folder with `from = agent`, `owner = attached_by = actor`,
`attached_via = proposal`, `library = pending`, `source = {path: workspace-relative}`; then its dependencies planned
(trust class `agent`), the mirror sync, and after
it, also when that await is cancelled (as `mcp_create`, no silent proposal): `skill.proposed`, `n_skill_shared`
{owner, name, description, path} to every other member (`n_skill_updated` for a replacement), `u_skill_proposed`
{owner, name, description without its final period, files, installs: `u_skill_installs` when its packages are
installed automatically, else ""} to the user; then `ensure_deps([name])`. The result: `Skill "x" is attached to the swarm (4 files, 12 KB) and every member sees it at
.scratch/_skills/x/SKILL.md. The user was asked whether to save it to the skills library; you are told the answer.
Edit it by proposing the same name again.` (a replacement: `Your skill "x" was replaced by the new version ...; ...
The user was asked again ...`), plus the links left out, plus " Its packages (requirements.txt) are being
installed; you are told when they are ready." (an AI member, whom the quiet news does not wake: "...; a later
wake-up of yours says when they are ready (the install does not wake you).") or " Its requirements.txt is not
installed automatically: <reason>.".

**The user's decision** (`skill_approve {name, approve, comment?, replace?}`, user only; `SessionSkills.decide(actor,
name, approve, *, comment="", replace=False, via="act") -> str`): only USER; `check_name`; the skill must be
attached (404) and `library == "pending"` (409 "Skill 'x' is not waiting for a library decision (status:
saved)."). The file part is `decide_files(data_dir, library, name, approve, *, comment="", replace=False,
via="stopped-session", project="", session=None, stopped=True) -> (SessionSkill, text)`, a plain function over a data
dir, so a stopped session's proposal can be decided without a Swarm.
approve: the session copy is checked again and must still have the digest recorded at the proposal (an agent's
shell can write into the session folder): else 409 "Skill 'x' changed since it was proposed, so it is not saved.
Detach it, or ask its owner to propose it again." and nothing changes; then `Library.add(<session copy>, "agent",
source={owner, project, session, swarm}, replace, by="user", via, info=<that check>)` (copy_skill refuses bytes that
differ from it);
another skill of that name in the library is 409 "The library already has a different skill 'x'. Approve with
replace to overwrite it, or reject." (`exists`); the result `Skill "x" saved to the skills library.`. A "no" keeps
it attached to this session only: `Skill "x" will not be saved to the library; it stays attached to this swarm
(detach it to remove it).`. The session metadata gets `library` saved / rejected, `comment` (whitespace collapsed,
at most 2000 characters) and `decided_while_stopped` (`stopped`). A running swarm then emits `skill.library` and
tells the owner (`n_skill_saved` {name} / `n_skill_not_saved` {name, comment: " <comment>" or ""}, a wake) unless it is the user or
has left; a decision on a stopped session's files is told quietly by the next `SessionSkills.start`, once (the
flag is cleared). There is no switch to skip the decision: saving to the library always takes the user's yes. A
proposal the user detaches is discarded.

**Role skills.** A `[[roles]]` entry may list `skills` (§17.3). `SessionSkills.ensure_role_skills(names=None, *,
announce=True) -> {"attached": [names], "missing": {role: [names]}, "detached": {role: [names]}, "over_cap": {role:
[names]}}` takes the given members (default: all), each role once: names already attached are skipped, names in kv
`skills_detached` go to `detached` (the user's detach stands), a name past `max_attached` to `over_cap`, the rest
are attached as bare names (the library, `claude-user`, the host library; never the workspace's
`.claude/skills`) with actor USER, `attached_via = "role:<canonical role name>"`, `via = "role"`, the given members
not told; a name nothing has goes to `missing`. The user is told once per (role, names) in memory:
`u_role_skills_missing` {role, names}, `u_role_skills_over_cap` {role, names, max}. It never raises (a hire or a
start goes on; failures are logged). Callers: `Swarm.hire` (the new member), `Swarm.start` (everybody,
`announce=False`) and `Swarm.reconfigure` (the background task, §3). `hire_agent` appends ` They also got the skills
of their role: a, b.` (the role's skills attached now), ` Not found in the skills library (the user was told): c.`,
` Detached by the user, so not attached: d.` and ` Not attached, the swarm has the maximum of 40 skills: e.`;
`agents` shows a role's skills in its catalog line (`- Tester (model x; skills: a, b): …`). Marks are resolved live
(`role_skills`): skills a role brought stay attached when the role stops listing them, without the "your role's"
mark, until the user detaches them; the snapshot's `roles` lists the catalog roles that list a skill.

**Docker sandbox** (§18.3): the hub runs in the agents' container and agents have `shell`, so the host library is
mounted read-only at `/dreamteam/host-skills` (`DREAMTEAM_HOST_SKILLS_DIR`; `host_skills(config)` lists it as the
read-only source `host-library`, attachable, never written), and the sandbox's own writable library is
`/swarm/.dreamteam/skills-library` (`DREAMTEAM_SKILLS_DIR`), i.e. `<home>/.dreamteam/skills-library` on the host,
which the host never reads by itself (import that folder on the host to keep a sandbox skill). It is not
`.dreamteam/skills`, the default library's name: for a home that is the user's home folder that would be the host's
own library, writable from the container. A host library inside the mounted directory would be writable through `/swarm`, so
`run_sandbox` warns about it. `~/.claude/skills` is not mounted (it
would expose all of the user's personal skills); add a Claude Code skill to the host library first. In the sandbox,
folder paths are container paths: the zip upload is the portable source. The pages say both when the listing's
`sandbox` is true: the add form's folder field gets a hint (`sk_folder_sandbox`; skills.js `setSandbox` shows it in
a form mounted before the listing came) and the home Skills tab's Claude Code section says to add such a skill to
the library of a DreamTeam run on the host (`sk_claude_sandbox`). (The MCP library is not mounted into the
sandbox at all; it stays container-local.) The sandbox's skill environments are a named volume per home (§18.3).

**Script dependencies** (skill_deps.py). A skill's `requirements.txt` / `pyproject.toml` (Python) or `package.json`
(Node) is installed automatically into an environment of its own, one per skill and dependency set, shared by every
session that attaches the same skill with the same manifests; without one, agents install what SKILL.md asks for
themselves (the index header says so). SKILL.md prose is never parsed.
- **Detection** (`detect(folder) -> Detected(python: PyManifest | None, node: NodeManifest | None)`, pure). Fixed
  places: `requirements.txt`, `pyproject.toml`, `scripts/requirements.txt`, `scripts/pyproject.toml` (every one found
  goes into one venv, in that order: `-r` each, a pyproject as a generated `pyproject-deps-<n>.txt` of its
  `[project].dependencies`), `package.json` then `scripts/package.json` (the first one; the other and a lone
  `yarn.lock` / `pnpm-lock.yaml` are `ignored`) with `npm-shrinkwrap.json` or `package-lock.json` beside it. A
  manifest is a regular file (never a link), at most 256 KiB (a lock 8 MiB), UTF-8; an empty dependency list means
  none. A skill's own `node_modules`, `.venv`, `venv` never reach the session copy (skip list). Requirement lines
  (`parse_requirements`: continuations joined, comments dropped; `-r` / `-c` includes inside the skill, at most 5
  deep, their bytes in the key): requirements with extras, specifiers and markers, `name @ https://…` and
  `git+https://`, `--hash`, `--pre`, `--prefer-binary`, `--only-binary`, `--require-hashes` are kept; refused, so
  the agent installs by hand (`unsupported`, a text naming the file, the line and the rule, without parentheses):
  `-e`, local paths, `file:`, every index option (`-i`, `--extra-index-url`, `-f`, `--trusted-host`, `--no-index`: a
  skill never chooses the index), any other option, `${…}`, `http://`, credentials in a URL, more than 1000 lines.
  Hashes are all or nothing: when one line or file uses them, every requirement must be pinned with `==` and hashed,
  and no pyproject may take part. `parse_pyproject` reads only `[project].dependencies` and `requires-python`
  (`dynamic` dependencies and Poetry / PDM tables are unsupported; optional dependencies ignored); the key covers the
  canonical JSON of those two, not the file. `python_satisfies(spec, version)` checks `requires-python` against the
  environments' Python (`>=`, `>`, `<=`, `<`, `==`, `!=`, `~=`, `==X.*`; an unknown specifier is ignored).
  `parse_package_json`: `dependencies`, `optionalDependencies`, `overrides` (with a lock also `devDependencies` and
  `peerDependencies`, which `npm ci` compares); a spec is kept only when it is a semver range or tag, an `npm:` alias
  (its own spec checked too), an `https://` tarball, a `git+https://` or `github:` spec, or a hosted `user/repo`
  shorthand; refused: `workspaces`, `file:`, `link:`, `workspace:`, `portal:`, local paths, `http://`, credentials and
  every other scheme (`git:`, `ssh:`, `git+ssh:`, ...). The env always gets a generated `package.json` (no `scripts`,
  `bin` or the like: the skill's own lifecycle scripts never run); `check_lock` wants lockfileVersion 2 or 3, no
  `link: true`, every `resolved` https.
- **Environments** under `Config.skill_envs_dir` (`~/.dreamteam/skill-envs`): `<skill[:24]>-py-<key12>` /
  `…-js-<key12>` (`env_dirname`, `DIRNAME_RE`). `env_key` hashes the skill name, the ecosystem, the trust class, the
  manifests' material and the tool: Python's implementation, minor version, platform, cache tag, ABI flags, base
  interpreter path, C library and Linux distribution (`os_marker`), or Node's major version, ABI, platform and arch.
  Not the installer, the index configuration or the skill's other files (editing a script reinstalls nothing).
  Trust class (`trust_class(from)`): `user` for `library`, `claude-user`, `host-library`; `agent` for `agent` and
  `claude-project` (agent-writable origins): the two never share an environment, both are shared across sessions.
  Layout: `env.json`, `src/` (the manifests, the generated files), `venv/` or `package.json` + lock +
  `node_modules/`, `views/<owner12>/node_modules` (Node), `install.log` (redacted, at most 2 MiB), `failed.json`,
  `ok` (written last: "ready"), `used` (mtime = last use); `.locks/`, `.cache/`, `.trash-*` are reserved.
- **Tools** (`INSTALLERS`, a `RealInstallers`; tests replace it): Python is `[skills] python` (default the base
  interpreter DreamTeam runs on), probed once per process (`probe_python`, `-I -c`; a probe that failed is run again
  after 5 minutes, or at once by `skill_reinstall`, which also plans an `unavailable` skill again); the installer is
  uv when there is one (`uv venv --python <interp> venv`, then `uv pip install --python <venv python>
  [--require-hashes] -r …`),
  else the interpreter's pip ≥ 22.3 (`-m venv --without-pip`, then `-m pip --python <venv python> install`), else a
  venv's ensurepip; none: unavailable. Node: the real `node` binary behind any shim (`process.execPath`; on Windows
  only `node.exe`), npm run as `node npm-cli.js` (never `npm.cmd`), `npm ci` with a lock else `npm install`, both
  with `--omit=dev --no-audit --no-fund` and `--ignore-scripts` unless `install_scripts`. Commands run with cwd =
  the env folder, as contained process trees (`ProcessTree`), the venv step 120 s and the install `install_timeout`
  (then killed: "timed out after 900 s"), in `installer_env(config, trust)`: `scrubbed_env`, plus `install_env` for
  the `user` class only, without `VIRTUAL_ENV`, `PYTHONHOME`, `PYTHONPATH`, `PIP_USER`, `UV_PYTHON` and the like,
  with `PIP_NO_INPUT`, `PYTHONNOUSERSITE`, `UV_NO_PROGRESS`; the user's own pip / uv / npm configuration applies.
  On Windows with long paths off, a venv holding a path of 260 characters or more fails with a text saying so.
- **The manager** (`EnvManager.for_config(config)`, one per process and root): `plan(folder, skill, trust,
  settings, *, config, proposal) -> [EnvSpec | state]` (blocking, a daemon thread; a state for off, unsupported,
  unavailable), `peek(spec, owner) -> state` (never blocks: memory, then stats; a ready env is verified at most
  once a minute: the venv's python and every installed dist-info, or npm's hidden lockfile and every dependency's
  folder; one that fails is broken, shown as installing and rebuilt), `ensure(spec, *, owner, force) ->
  Future[state]` (one build per env in the process, joined by later callers), `use(owner, dirnames)`,
  `exclusive(dirname, requester)`, `check_reinstall`, `view(spec, owner)` / `view_ok`, `touch`, `cleanup(days)`,
  `log_tail`, `subscribe(fn) -> unsubscribe` (published on the subscriber's loop with a phase: queued, waiting,
  started, done). A build takes the OS build lock `.locks/<dirname>.lock` first (another process's build is waited
  for, as `installing, other_process`, holding no slot, at most `install_timeout + 120` s, then a failure kept in
  memory), then decides: ready (reused), failed less than an hour ago (not retried; a reinstall is), failed or half
  built (removed and rebuilt), broken (rebuilt for everyone), ready with `force` (the reinstall: refused 409 while
  another session uses it); then one of `INSTALL_PARALLEL = 2` slots (`installing, queued` meanwhile), the manifests
  copied (the bytes must be the key's), the commands, the checks, and `ok` last. A failure writes `failed.json`
  (`error` = the first `ERROR` / `error:` / `npm ERR!` line or the last one, redacted, prefixed by the installer);
  an exception outside the commands (env.json or the log cannot be written: a full disk, a lock, a long path) is a
  failure too (`OSError: …`), and one whose `failed.json` cannot be written is kept in memory (the half-built folder
  alone would read as installing). A build that raises before that (a reinstall's exclusive lock refused) publishes
  the env's real state as its end (still ready, an older failure, else a failure in memory) before the caller gets
  the error, so no subscriber keeps waiting. While this process takes or holds an env's use lock exclusively, its own
  shared takes (a peek, `use`) wait: taking one back would make that exclusive lock fail.
  **In use**: every process holds a shared OS lock (`LockFileEx` / `flock`) on `.locks/<dirname>.use` while one of
  its sessions has the env attached (taken before the first peek); a reinstall of a ready env, a removal and cleanup
  take it exclusively: "The environment of skill "x" is used by N other running session(s); stop them, or
  reinstall from there." / "... is used by another DreamTeam process; ...". A rename is no in-use test (Windows
  renames a venv whose python runs).
- **Per session** (`SessionSkills`): the plan of each attached skill is cached by (digest, `settings_hash`) and made
  in a daemon thread at `start()` (every skill; then `use`, and the views of ready Node envs), on every attach and
  proposal; `ensure_deps(names=None, *, force, ecosystem)` starts the builds in the background (at every start and
  after each attach); `deps(name)` (the snapshot's `deps`) and `deps_fragment(name, lang, shell=None)`: `skills_deps_python`
  {python: the venv's python written for the swarm's shell (`shell_ready`: bare or `& '…'` in PowerShell, always
  quoted for cmd, `shlex.quote` elsewhere; never in backticks), files, extra: `skills_deps_bin` {names, bin} for the
  commands its packages installed, `skills_deps_own` when a pyproject is among the files}, `skills_deps_node`
  (linked) or `skills_deps_node_path` (the link could not be made), `skills_deps_installing` (an external member,
  whom the end wakes: "you are told when they are ready") or, with `quiet` (the default; an AI member, `item_lines`
  and `_announce` pick it by kind), `skills_deps_installing_quiet` (a later wake-up says so: the install does not
  wake it), `skills_deps_failed`
  {files, error, log} and `skills_deps_manual` {files, reason: `deps_reason_off` / `_proposals` / `_unavailable`
  {detail} / `_python` {need, have} / `_unsupported` {detail}}. The subscription emits `skill.deps` and, when a build
  ends that this session saw as installing (or an env it saw as failed turns out ready: another process built it, a
  retry succeeded), `n_skill_deps_ready` / `n_skill_deps_failed` to the members (quiet for AI
  agents, a wake for external ones) and `u_skill_deps_failed` {name, files, error, log} to the user. Node: when its
  env is ready the session's view is made (a hardlink farm of the env's `node_modules`, a copy where the file system
  has no hardlinks; reused while it matches the env's `ok`) and the mirror copy gets a `node_modules` link to it at
  its package.json's folder (made after the copy's swap; checked by every sync: a missing, dangling or re-pointed
  link is made again, a view an agent's command emptied rebuilt, a real folder in its place — an agent's own npm
  install — left alone until the next full rebuild); an agent's `npm ci` or `rm -rf node_modules/` through the link
  reaches only that view. A view that cannot be made shows as failed ("this session's view of its node_modules could
  not be made: …"; the members are told) and is tried again by the minute check: an agent is never pointed at the
  shared env itself. `reinstall(actor, name, ecosystem=None)` (`skill_reinstall`, user only): "Reinstalling the
  packages of skill "x" (requirements.txt); members are told when they are ready.", "... are being installed
  already; ...", "No skill "x" is attached to this swarm." (404), "Skill "x" has no requirements.txt,
  pyproject.toml or package.json.", "Skill "x" has no Python packages (requirements.txt or pyproject.toml)." / "...
  no Node packages (package.json).", "Skill "x": its packages are not installed automatically (<reason>).", the 409
  texts above. `touch_envs()` (hourly from the scheduler) refreshes the `used` stamps, rebuilds broken envs,
  retries failures older than an hour and builds an env that reads as installing while nothing in this process
  builds it (`ensure`: another process's build is waited for); `stop_deps()` at `Swarm.stop`.
- **Cleanup** (`EnvManager.cleanup(retention_days) -> {"removed": [dirname], "kept": [{dirname, why: in use | being
  built}]}`): envs whose `used` is older than `env_retention_days` (0: none by age), failed or half-built ones older
  than a day, views unused for a day, trash older than an hour; never an env whose use lock any process holds or
  whose build lock is held; `ok` goes first, then the folder is renamed away (a refused rename puts `ok` back).
  A standalone `dreamteam run` runs it once, 60 s after the start, in a daemon thread; the launcher 60 s after its
  start and then daily (`ENV_CLEANUP_DELAY`, `ENV_CLEANUP_EVERY`, §18.1), and at once on `POST
  /api/skill-envs/cleanup` (the home page's button, the console's and the TUI's `/skills clean-envs`). The user's run
  is `cleanup(…, every_unused=True)`: every env no session uses now goes, whatever its age and also with
  `env_retention_days = 0` (which only turns the automatic runs off); the in-use and being-built ones are kept and
  reported.
- **Trust model**: Installing a skill's dependencies runs code from the packages it lists (Python build backends of
  source packages; npm install scripts of its dependencies only when `install_scripts` is on) with DreamTeam's
  rights, which outside the Docker sandbox are the user's. It happens only for skills attached to a session — by the
  user, by a role in the user's catalog, or by a member's proposal. Packages come from the indexes the user
  configured, or from https and git URLs the skill names directly (including the resolved URLs of its npm
  lockfile); a skill cannot add or replace an index, or name a local path or an environment variable. Skills a
  member proposed, or taken from the workspace's agent-writable `.claude/skills`, install into environments kept
  apart from the user's own skills and never get `install_env`. A non-sandboxed agent can still write
  `~/.dreamteam/skill-envs` directly, as it can write the skills library: environments are a convenience, not a
  security boundary. Logs, errors and the recorded commands are redacted (URL credentials, the values of
  `install_env` and of the index variables).
- **Not in v1**: Poetry, PDM, `[dependency-groups]` and optional dependencies; PEP 723 inline metadata; conda,
  Pipfile, pnpm and yarn; installing the skill itself (`pip install .`); local paths, `-e` and indexes a skill names;
  one environment for several skills; a skill's venv or `.bin` on agents' `PATH` (the index names the folders);
  settings for the installer, `node` and `npm`; a per-environment list, removing one environment, launcher MCP
  actions for them, a size cap; Node in the Docker image.

**Security.** "Only the user attaches, imports, deletes and saves to the library" is a user-interface convention,
not a security boundary, as `mcp_approve` is: the hub has no authentication (GuardMiddleware stops browsers on
other sites, not local processes), and without the sandbox agents run as the user and can write
`~/.dreamteam/skills` directly. What DreamTeam does: the sandbox mounts the host library read-only; library entries
changed outside DreamTeam are flagged (`changed_outside`); a hub MCP client cannot save to the library a proposal of
a member it acts as, nor one of any external member when nothing names the client (§13); the library gets only
the content that was proposed (the digest is checked again); every library change records `by` and `via`, and every
attach the surface it came through (`skill.attached` `via`); agents' shells never get `DREAMTEAM_SKILLS_DIR`,
`DREAMTEAM_HOST_SKILLS_DIR` or `DREAMTEAM_SKILL_ENVS_DIR` (§7);
`<workspace>/.claude/skills` is never used implicitly; Git URLs are hardened and redacted; zips and folders are
checked as above; sizes are capped per skill (`max_mb`, `max_files`), per archive or checkout (`max_archive_mb`) and
for SKILL.md (100 KB). A per-launch hub token, which would make "only the user" a real boundary, is out of scope.

**Not in v1**: Claude Code plugin skills; automatic updates of library skills from their Git source (the recorded
`commit` makes a later check possible); per-role visibility; mounting the MCP library or `~/.claude/skills` into the
sandbox; rendered Markdown on the home page; refs given as commit SHAs (`--branch` takes branches and tags only).
