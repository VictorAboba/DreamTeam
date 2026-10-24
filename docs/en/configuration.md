# Configuration (`dreamteam.toml`)

**English** · [Русский](../ru/configuration.md) · [All docs](README.md)

Global settings live in `<home>/dreamteam.toml`. It is easier to change them on the "Settings" tab: the page checks
the values before saving and keeps the previous version in `dreamteam.toml.bak`. Editing the file by hand also works,
but comments are not kept when you save from the UI; an edit of the file while the server runs is picked up. When you
create a session, you can override the language, the starting team (roles and number of agents), the budget, the
model profile, the number of steps and MCP approval. These values are stored in `sessions/<id>/session.json`; the rest
comes from the global settings.
For a session that already exists, the ⚙ button on its page changes the budget and max agents, as well as the
session's own background job limits (per member, per session, time limit, compute threads) and agent parameters
(steps per wake-up, context window, tool output limit, nudging idle agents, parallel LLM calls). An empty field means
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

[[roles]]                # your own role in the catalog (to change a built-in one, give its id: name = "tester")
name = "Designer"
description = "You design interfaces: mockups, screen texts, user scenarios."
model = ""               # model profile for this role ("" means the default)

[agents]
default_model = "default"
max_steps_per_turn = 40
context_tokens = 120000  # threshold for compacting an agent's history into a summary

[shell]                  # the agents' shell (the full list is on the "Settings" tab)
timeout = 120            # a call waits this many seconds for a command; after that it runs as a background job
foreground_max = 180     # an AI agent waits for a command no longer than this, whatever timeout it sets
max_jobs = 16            # background jobs per session (0 means no background jobs)
job_max_minutes = 480    # lifetime of a job started in the background on purpose or left running by a command (0 means no limit)

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
1M tokens. If the provider reports the cost itself (like OpenRouter), that cost is used.

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
