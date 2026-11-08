# Configuration (`dreamteam.toml`)

**English** · [Русский](../ru/configuration.md) · [All docs](README.md)

Global settings live in `<home>/dreamteam.toml`. It is easier to change them on the "Settings" tab: the page checks
the values before saving and keeps the previous version in `dreamteam.toml.bak`. Editing the file by hand also works,
but comments are not kept when you save from the UI; an edit of the file while the server runs is picked up. When you
create a session, you can override the language, the starting team (roles and number of agents), the budget, the
model profile, the number of steps, MCP approval and [clarify mode](features.md). These values are stored in
`sessions/<id>/session.json`; the rest comes from the global settings.
For a session that already exists, the ⚙ button on its page changes the budget and max agents, as well as the
session's own background job limits (per member, per session, time limit, compute threads), agent parameters
(steps per wake-up, context window, tool output limit, nudging idle agents, parallel LLM calls, how long a
`file_changed` trigger waits for the file to stop changing, how often a repeating trigger may wake its owner), the
network and clarify mode. An empty field means
the value from the general settings; changes apply at once, with no restart. For the `main` session (and the
`dreamteam run` board), ⚙ changes only the budget and max agents: the rest is set in the general settings.
The goal is set when you create a session (the `goal` key in `[swarm]` is used only in `dreamteam run` mode and in the
`main` session of a former project).

```toml
[swarm]                  # defaults for new sessions
name = "My swarm"
language = "ru"          # language of the agents' prompts: ru | en
start_agents = 3         # how many agents start
max_agents = 8           # max AI agents (they hire new ones themselves; members via MCP do not count)
max_cost_usd = 5.0       # budget; when it runs out, the swarm pauses
starting_roles = []      # the starting team in order: ["architect", "developer", "developer", "tester"]
independent_review = true # another member accepts an agent's task (or you, if there is no one else)
network = "on"           # "off": the agents' commands and MCP servers have no internet (see "Session without internet")
clarify = false          # true: the planner asks you about the goal first; a critical question pauses the swarm

[[roles]]                # your own role in the catalog (to change a built-in one, give its id: name = "tester")
name = "Designer"
description = "You design interfaces: mockups, screen texts, user scenarios."
model = ""               # model profile for this role ("" means the default)

[agents]
default_model = "default"
max_steps_per_turn = 40
context_tokens = 120000  # threshold for compacting an agent's history into a summary
file_settle_seconds = 60 # file_changed fires once the file has not changed this long or the agents who changed it ended their turns (0 means at every change)
file_max_wait_seconds = 300 # ...but no later than this after the first change (0 means no limit)
trigger_wake_seconds = 180  # a member's repeating trigger wakes it at most this often; the fires in between come in one notice (0 means at every fire)

[shell]                  # the agents' shell (the full list is on the "Settings" tab)
timeout = 120            # a call waits this many seconds for a command; after that it runs as a background job
foreground_max = 180     # an AI agent waits for a command no longer than this, whatever timeout it sets
max_jobs = 16            # background jobs per session (0 means no background jobs)
job_max_minutes = 480    # lifetime of a job started in the background on purpose or left running by a command (0 means no limit)

[workspace]
git = true               # the shared workspace under git with automatic commits
lock_ttl_minutes = 30    # how long a file stays locked when the agent gives no time (at least 1)
lock_wanted_minutes = 5  # tell the holder of a lock others wait for once it has held it this many minutes (0 means at the first refusal)
copy_max_mb = 1024       # the largest copy of files into a new session (--copy-from, an uploaded .zip), MB
copy_max_files = 100000  # files in such a copy (of a folder: .git, node_modules and the like do not count)

[models.default]         # Claude
provider = "anthropic"
model = "claude-opus-5-5"
api_key_env = "ANTHROPIC_API_KEY"
effort = "medium"
fallbacks = "default"           # server-side fallback when the model refuses (beta)
thinking_mismatch = "drop_block" # do not fail when the dialog prefix changes (beta)
max_tokens = 16000
max_tokens_ceiling = 32000      # after a reply cut off by the limit, it doubles up to this (default 2 × max_tokens)

[models.local]           # Ollama / vLLM / OpenRouter: any OpenAI-compatible API
provider = "openai"
base_url = "http://localhost:11434/v1"
model = "qwen3:32b"
api_key = "ollama"
input_price = 0
output_price = 0

[mcp]
require_approval = false
library_dir = "~/.dreamteam/library"  # the shared MCP library (DREAMTEAM_MCP_LIBRARY_DIR wins)
[mcp.external.fetch]     # external MCP servers for the swarm
command = ["uvx", "mcp-server-fetch"]
description = "Fetching web pages"

[tracing]
enabled = true           # agent traces
max_payload_chars = 200000
retention_days = 0       # 0 means keep forever

[server]
host = "127.0.0.1"
port = 8765
resume_sessions = true   # resume the sessions that were running when the server stopped
```

Prices of known Claude models are built in. For other models, set `input_price` and `output_price` in dollars per
1M tokens; `0` means a free model (a local one, for example), while an empty field means "not set". If the provider
reports the cost of a call itself, that cost is used. A profile that has neither prices nor a provider-reported
cost has its tokens counted as $0: the spending shown is lower than the real one, and the session budget does not
see those calls. DreamTeam says so:

- in the settings, the profile card shows a warning with the "Enter prices" and "Free model (0 / 0)" buttons, and
  the "Test request" result says how its cost was obtained;
- after the first such call of a session start, you get a notice naming the profile, the model and the missing
  prices (also on stderr in `dreamteam -p`);
- `dreamteam serve` and `dreamteam run` print a note after their startup message, and `/model` in the terminal shows a
  "Prices" column.

An agent keeps the model profile it was hired with. When that profile is renamed on the "Settings" tab, the agent
stays on it under the new name, in running and in stopped sessions. An agent whose profile was deleted (or renamed
by hand in `dreamteam.toml`) uses the default profile.

`copy_max_mb` and `copy_max_files` limit a [copy of files into a new session](projects.md#working-on-a-copy-of-files):
the size and number of the files copied from a folder or unpacked from a .zip; `copy_max_mb` also caps the size of a
.zip uploaded from the browser. In a folder, `.git`, `node_modules` and the like do not count; in a .zip, the
archive's own size counts against `copy_max_mb`, and every entry (left-out ones and folders included) against a
limit of twice `copy_max_files`. Both are at least 1. They are global settings: the copy is made before the session
exists, so a value in a session's own settings has no effect.

## Roles and the starting team

DreamTeam has a role catalog: generalist, developer, tester, reviewer, analyst, architect, technical writer and
researcher. Each role has a description that goes into the agent's system prompt when it is hired, as well as its own
instructions and model profile, if they are set. The catalog is configured on the "Settings" → "Roles" tab
(or in `[[roles]]`). There you can edit a built-in role, hide it or add your own. An edit reaches only the agents hired
after it: the prompts of agents already working do not change. Agents hire from the catalog
(`hire_agent` with a role from it can do without instructions), and you hire with the `/hire Role` command or the
hire button on the board. **The starting team** is the roles of the agents a session starts with. You set it:

- in the new session dialog or in the settings ("Role ×N", rows in order);
- with the flag `--team "architect, developer*2, tester"`;
- with the `/team` command in the terminal.

Agents beyond the list become generalists.

## Shared folders and this computer's settings

Some folders are shared by all sessions and live outside the home, in `~/.dreamteam`: the skills library
(`[skills] library_dir`, by default `~/.dreamteam/skills`), the skills' dependency environments (`[skills]
envs_dir`, `~/.dreamteam/skill-envs`) and the MCP library (`[mcp] library_dir`, `~/.dreamteam/library`). The
environment variables `DREAMTEAM_SKILLS_DIR`, `DREAMTEAM_SKILL_ENVS_DIR` and `DREAMTEAM_MCP_LIBRARY_DIR` win over
these settings: the Docker sandbox sets its skills folders this way, and the tests set all three. The agents'
console never gets these variables.

An [import](transfer.md) (`dreamteam import`) never changes the settings that belong to the computer itself, in
the global settings or in a session's settings: `server.host`, `server.port`, `shell.program`, `skills.library_dir`,
`skills.envs_dir`, `skills.python` and `mcp.library_dir` keep the value they have here, and API keys and the `env`
values of external MCP servers never go into an export at all.
