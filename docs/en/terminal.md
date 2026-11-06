# Terminal UI and console

**English** · [Русский](../ru/terminal.md) · [All docs](README.md)

## Terminal UI

`dreamteam` in a terminal looks and works like Claude Code:

- **The first message with no session** becomes the goal of a new session: the first line is the title, the rest
  (`Alt+Enter` for a new line) is the description. The swarm builds a team and starts working.
  After that, messages go to the current addressee. It is shown in the frame's title: the agent who wrote to you
  last, otherwise the one responsible for planning, otherwise everyone. `@Name text` writes to a specific agent and
  makes it the addressee, `@all` writes to everyone.
- **Feed.** Agents' tool calls look like `⏺ Anna Smirnova · task_claim(task_id=3)` with the result under them after
  `⎿`. Messages to you are shown in full as markdown, tasks as a to-do list `☐ ◐ ◑ ☒`, the goal in a frame.
  `Shift+Tab` switches the verbosity: quiet, normal (the default) or verbose, with the agents' thoughts and tool
  results. `Ctrl+O` toggles verbose mode.
- **The spinner** `✻ Huddling… (12s · ↑ 4.2k ↓ 310 · $0.12 · esc to pause)` is shown while the agents work.
  `Esc` pauses the swarm, `/pause` resumes it.
- **Clarify mode and questions.** `/clarify on` (or `--clarify` at the start) makes the planner ask you about the goal
  before it plans, and lets an agent pause the whole swarm with a critical question ([more](features.md)). In a
  session `/clarify on|off` changes that session; without one, the sessions this window creates next; `/clarify`
  alone shows the state (the `main` session has no settings of its own: it shows the general setting). While the
  swarm waits for your answer the feed shows `⏸ The swarm waits for your answer to Anna`, the asker becomes the
  addressee and the frame's title says `answer to #12: everyone gets it`: plain text is the answer, every member gets
  it with the question, and the swarm goes on. `/pause` goes on without an answer.
- **Commands** (`/` opens suggestions): `/new [goal]`, `/sessions` (`Del` in the list deletes a session),
  `/resume ID`, `/delete [ID]`, `/cd [path | --default]`, `/copy-from [path | --none]`, `/goal`, `/board`,
  `/task N`, `/agents`, `/roles [role]`, `/jobs [jN | stop jN]`, `/team [role*N, …]`, `/to Name`,
  `/hire Role [| instructions]`,
  `/trace [Name | N]`, `/span N [ctx]`, `/files [path]`, `/cat path`, `/cost`, `/status`, `/model [profile]`,
  `/pause`, `/wrapup [off]`, `/clarify [on|off]`, `/budget N`, `/max N`, `/approve name|N`, `/reject name|N`, `/stop`,
  `/web`, `/settings`, `/verbose`, `/lang ru|en`, `/clear`, `/exit`.
- **Review.** A task the swarm handed to you for review shows as `◑ #3 … waits for your review`. `/approve 3`
  accepts it, `/reject 3 what to fix` returns it to the assignee. On the web board the task card has buttons for this,
  and dragging a task from "Review" to "Done" also accepts it.
- **Agents' background jobs.** While they run, the status line shows `jobs N`, and the agent in `/agents` shows
  `⚙N`. `/jobs` lists them (and the ones that finished in the last hour), `/jobs j3` shows a job's output,
  `/jobs stop j3` stops it together with everything it started. `Esc` and `/pause` do not stop them.
- **Wrap-up mode.** `/wrapup` (on the web board, the "Wrap up" button next to "Pause") asks the team to wrap up:
  each agent gets a notice about it once, and every wake-up reminds it not to start new computations and to finish
  its tasks with what it has. In this mode no new background jobs start: a command that does not fit in its timeout
  is cut off, and jobs already running go on. `/wrapup off` switches the mode off; a new goal or returning the goal
  to work switches it off by itself.
- **`!command`** runs a command in the session folder (including after `/cd`). This works when the server is local.
- **`/cd`** shows the session folder or moves the session to another folder, including your project
  (more in [The session folder and an existing project](projects.md)).
- **`/copy-from`** chooses a folder or a .zip whose copy the next new session gets (the originals are never changed;
  see [Working on a copy of files](projects.md#working-on-a-copy-of-files)).
- **Keys:**
  - `Enter` sends;
  - `Alt+Enter`, `Ctrl+J` or `\` at the end of a line adds a new line;
  - `↑/↓` browses the history;
  - `Tab` completes;
  - `Ctrl+L` clears the screen;
  - `Ctrl+C` twice or `Ctrl+D` exits;
  - `?` shows the key help.
- **MCP approval.** When an agent creates an MCP server and approval is on in the settings, the input box is
  replaced by an "allow / reject" dialog with the server's description, tools and the SHA-256 of its code. Esc puts
  the decision off: `/approve name` then approves only the code that dialog showed.

If a server for the home folder is already running (`dreamteam serve`), the UI connects to it. Otherwise the server
starts in the same process, and the sessions stop on exit; `dreamteam -c` continues the last one.
In Git Bash (mintty), run `winpty dreamteam` or use Windows Terminal / PowerShell.

Non-interactive mode for scripts:

```bash
dreamteam -p "Write a script that counts the lines of code in the repository" --output-format json
cat error.log | dreamteam -p "Find the cause of the error" -c
```

In this mode, agents' questions to the user get the answer "decide yourself", and tasks waiting for your review are
accepted automatically with a note that nobody checked them (`--no-auto-reply` turns off both). With automatic
replies clarify mode does not apply: the session plans as usual and critical questions do not pause the swarm, even
with `clarify = true` in the settings (in the rare gap right after a server restart or a session reload, the run
answers such a pause at once, and `stream-json` may then show a short `swarm.paused` / `swarm.resumed` pair).
`--clarify` therefore needs `--no-auto-reply`; then the planner first asks you and waits for your answer on the board
or in `dreamteam tui`, a critical question pauses the swarm until you answer, and the run waits too (until
`--timeout`, if you gave one). In the `text` and `json` formats each agent message to you prints a line on stderr,
and a question that pauses the swarm one more (`stream-json` shows the events instead).
`--clarify` / `--no-clarify` do not work with `-c` / `-r`: change an existing session with `/clarify`.
Background jobs still running at the end (for example, a dev server) are listed in the `jobs` field of the
`--output-format json` result. A server started in the same process stops them together with the session. If the
server was started separately, the jobs run until the session stops, and the text mode reports them on stderr.
Exit codes:

- `0` — the goal is reached;
- `2` — the budget is spent;
- `3` — `--timeout` expired;
- `1` — error;
- `130` — Ctrl+C.

## Console commands

`@First Last text` writes to an agent, `/chat Name` is dialog mode, `/all text` writes to everyone,
`/goal Title | description`, `/task Title | goal`, `/board`, `/agents`, `/status`,
`/show N` (a task and its discussion), `/comment N text`, `/hire Role [| instructions]`, `/pause`, `/resume` (while
the swarm waits for your answer to an agent's question, `@Name text` to that agent answers it, and `/resume` goes on
without an answer), `/wrapup [off]` (wrap-up mode),
`/budget 10`, `/max 6`, `/approve name|N`, `/reject name|N` (an MCP server or a task in review; `/approve name sha256`
approves the server only while its code still has that SHA-256 (the one the dialog, `mcp_list` and the MCP card
show), given in full or by its first 8 or more characters),
`/jobs [jN | stop jN]` (agents' background jobs: list, one job's output, stop),
`/trace [Name | N]` (traces), `/span N [ctx]` (a trace step
and the request context), `/session` (the current session), `/cd [path | --default]` (the session folder; a move asks
for confirmation `y/N`, `--yes` skips the question),
`/skills` (the attached skills) and `/skills lib | add X [--pick a,b] [--replace] | update X | rm X | show X |
approve X [replace] | reject X [reason] | reinstall X | log X | clean-envs` ([skills](skills.md): the library,
attaching by name, path or URL, the decision on a member's proposal, packages and their log).
The console connects to a running session: `dreamteam console` (or `dreamteam console --session 003-snake`).
It is a simple line-based client for terminals where the [terminal UI](#terminal-ui) does not work.
