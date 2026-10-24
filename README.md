# DreamTeam

**English** · [Русский](README.ru.md)

[![CI](https://github.com/VictorAboba/DreamTeam/actions/workflows/ci.yml/badge.svg)](https://github.com/VictorAboba/DreamTeam/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)

**A self-organizing swarm of AI agents with no leader.** You give the swarm a goal; agents with human names
plan it on a shared task board, claim tasks, review each other's work, talk in task threads and direct
messages, share a git workspace, set themselves triggers and write their own tools as MCP servers.
The swarm itself is an MCP server, so Claude Code can watch it, steer it, or join it as a member.

<!-- demo: a short GIF of the kanban board while agents move tasks goes here -->

```bash
uv tool install git+https://github.com/VictorAboba/DreamTeam.git
dreamteam "Build a snake game in Python with tests"
```

## What makes it different

- **No orchestrator.** Setting a goal puts a planning task on the board; whoever is free takes it, the others
  pick up tasks as they appear. Claiming is atomic, dependencies block, and every task is accepted by a
  *different* member (independent review) — or by you, if nobody else is left.
- **Agents build their own tools.** The built-in tool set is small: the board, messages, files, a console and
  triggers. Anything else an agent writes as an MCP server (`mcp_create`); it is checked, registered for
  itself, its task or the whole swarm, and kept in a library for later runs.
- **The swarm is an MCP server.** Connect Claude Code (or any MCP client) to act as the user — set goals, read the
  board, approve reviews — or call `join_swarm` and work inside the swarm as a teammate.
- **Every step is traceable.** Each time an agent wakes up becomes a trace: every LLM call with its thinking,
  tool calls, tokens (including cache), cost, latency and the exact request context, even after the history was
  compacted. Browse it on the board, in the terminal or over MCP, export it as JSON.
- **Any model.** Claude, OpenAI, OpenRouter, Ollama, vLLM, LM Studio or any OpenAI-compatible endpoint, with a
  different model per agent or role, token and dollar accounting per agent, and a budget that pauses the swarm.
- **Built to run unattended.** Everything is in one SQLite file per session; after a restart the swarm continues
  where it stopped. Long commands become background jobs instead of being killed, sessions can run without
  network for benchmarks, and the whole swarm can run in a Docker sandbox.

## Interfaces

| | |
|---|---|
| **Terminal UI** | `dreamteam` opens a Claude Code–style terminal UI: a feed of agents' actions (`⏺ Anna Smirnova · task_claim(task_id=3)`), slash commands, `@mentions`, `esc` to pause. `dreamteam -p "goal"` runs headless for scripts and CI with `text`, `json` or `stream-json` output. |
| **Web board** | `http://127.0.0.1:8765` — sessions page, settings, and per session a live kanban with drag-and-drop, task threads, chat with any agent, its live activity, files, MCP servers, triggers, background jobs and traces. Light/dark, English/Russian. |
| **MCP** | `/mcp` over HTTP or `dreamteam mcp` over stdio: session management plus every board, message and trace tool. |
| **REST + WebSocket** | Everything the board does, for your own tooling. |

## Install

You need Python 3.11+, [uv](https://docs.astral.sh/uv/) and git.

```bash
uv tool install git+https://github.com/VictorAboba/DreamTeam.git
```

This puts the `dreamteam` command on your PATH (run `uv tool update-shell` if it isn't found). To hack on it,
clone the repository and run `uv tool install --editable .` instead — see [CONTRIBUTING.md](CONTRIBUTING.md).

## Quick start

```bash
export ANTHROPIC_API_KEY=sk-ant-...        # or configure any provider in Settings
dreamteam                                   # terminal UI; the web board is served too
dreamteam "Build a CLI that converts CSV to JSON, with tests"   # start a session with this goal
dreamteam serve                             # web board only
```

1. Open **Settings → Models and endpoints** on `http://127.0.0.1:8765`, pick a provider and model, give the key
   (an environment variable name or the key itself) and press **Check**.
2. On **Sessions**, press **Start a new session** and describe the goal: the first line becomes the title, the rest the
   description. You can also pick the starting team, e.g. `--team "architect, developer*2, tester"`.
3. Watch the swarm plan and work; write to any agent with `@Name`, review tasks it hands you with
   `/approve 3` or `/reject 3 what to fix`.

Agent prompts are in English or Russian: set `language = "en"` under `[swarm]` in `~/DreamTeam/dreamteam.toml`
or in the new-session dialog.

Point a session at an existing project instead of its own folder — agents then commit to your current branch:

```bash
git switch -c dreamteam/billing
dreamteam --workspace ~/code/my-app "Cover the billing module with tests"
```

### Connect Claude Code

```bash
claude mcp add --transport http dreamteam http://127.0.0.1:8765/mcp   # to a running server
claude mcp add dreamteam -- dreamteam mcp                               # or let Claude Code start it
```

### Run in Docker

```bash
dreamteam sandbox ~/DreamTeam --goal "..."
```

Agents' commands and their MCP servers then run inside the container. Add `--network off` to cut them off
from the internet (strict in Docker and on Linux).

## Configuration

Settings live in `~/DreamTeam/dreamteam.toml` and are easiest to change on the **Settings** page.
A minimal example:

```toml
[swarm]
language = "en"
start_agents = 3
max_agents = 8
max_cost_usd = 5.0

[models.default]
provider = "anthropic"
model = "claude-opus-5-5"
api_key_env = "ANTHROPIC_API_KEY"

[models.local]            # Ollama, vLLM, OpenRouter — any OpenAI-compatible API
provider = "openai"
base_url = "http://localhost:11434/v1"
model = "qwen3:32b"
api_key = "ollama"
```

## Security

Agents run commands and MCP server code **with your user's rights**. For untrusted goals use
`dreamteam sandbox`. API keys and other secrets from the environment are not passed to agents' commands.
The server listens on `127.0.0.1` and has no authentication: don't expose it without a reverse proxy that adds
one. See [SECURITY.md](SECURITY.md) for how to report a vulnerability.

## Documentation

- [README.ru.md](README.ru.md) — the full reference: every command, setting, the terminal UI, skills, sessions in
  your own project, sessions without network, traces (in Russian for now; an English translation is welcome).
- [docs/SPEC.md](docs/SPEC.md) — how it works: module contracts, events, the agent loop.
- [docs/TUI.md](docs/TUI.md) — the terminal UI contract.

Ideas are borrowed from Claude Code Agent Teams, Gas Town / Beads, CAMEL Workforce, Magentic-One, MetaGPT,
Strands Swarm, Letta, Alita, A2A and blackboard architectures — [details](README.ru.md#как-это-устроено).

## Contributing

Issues and pull requests are welcome, in English or Russian. Start with [CONTRIBUTING.md](CONTRIBUTING.md);
tests run with a scripted LLM and need no API keys.

## License

[MIT](LICENSE)
