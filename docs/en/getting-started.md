# Install and quick start

**English** · [Русский](../ru/getting-started.md) · [All docs](README.md)

## Install

You need Python 3.11+, [uv](https://docs.astral.sh/uv/) and git. To make the `dreamteam` command work from any folder,
install DreamTeam as a uv tool:

```bash
uv tool install git+https://github.com/VictorAboba/DreamTeam.git
```

- uv puts the `dreamteam` command into its folder for commands (`uv tool dir --bin`, usually `~/.local/bin`). If
  `dreamteam` is not found after the install, add this folder to PATH with `uv tool update-shell` and open the
  terminal again.
- To update to the latest version: `uv tool upgrade dreamteam`. To remove: `uv tool uninstall dreamteam`.

To change the code, clone the repository and install it from the repository folder with `--editable`. Code changes
then take effect at once.

```bash
uv tool install --editable .
```

After you change the dependencies in `pyproject.toml` or move the repository folder, reinstall:
`uv tool install --editable . --force`. Without an install, run DreamTeam from the repository folder through uv:
`uv sync`, then `uv run dreamteam …`. The tests run the same way, see [CONTRIBUTING.md](../../CONTRIBUTING.md).

## Quick start

```bash
dreamteam                                   # interface in the terminal (in the style of Claude Code)
dreamteam "Build a snake game in Python"    # a new session with this goal right away
dreamteam serve                             # web interface only (the default without a terminal)
```

In a terminal, `dreamteam` opens the [terminal interface](terminal.md). If the server is not running yet, it starts
in the same process, so the web board at `http://127.0.0.1:8765` is available too.
`dreamteam serve` starts only the web interface. The home page `http://127.0.0.1:8765` opens:

1. On the **"Settings" → "Models and endpoints"** tab, choose a provider and a model, give the key (the name of an
   environment variable, for example `ANTHROPIC_API_KEY`, or the key itself) and click "Check".
2. On the **"Sessions"** tab, click **"Start a new session"** and describe the goal: the first line becomes the title,
   the rest becomes the description. You can also put together the starting team there. The swarm makes a plan and
   starts working, and you land on the session board `/s/<id>/`.

Sessions and settings are stored in the DreamTeam home folder: `~/DreamTeam` (set it with `--home` or the
`DREAMTEAM_HOME` variable). It holds `dreamteam.toml` with the global settings and `sessions/<id>/` with the data,
workspace files and traces of each session. You can create a session right from the command line:
`dreamteam serve --goal "Build a CLI tool that converts CSV to JSON, with tests" --title "CSV → JSON"`.

Commands:

| command | what it does |
|---|---|
| `dreamteam ["goal"]` / `dreamteam tui` | terminal interface; `-c` continues the last session, `-r [ID]` picks or opens a session; for a new session: `--model`, `--agents`, `--team "architect, developer*2"`, `--max-agents`, `--budget`, `--title`, `--workspace DIR` |
| `dreamteam -p "goal"` | no interface: run the goal and print the result (`--output-format text/json/stream-json`, `--verbose`, `--timeout`, `-c`/`-r ID` for the next instruction to an existing session, `--workspace DIR` for the folder of a new session); text from stdin is added to the request |
| `dreamteam serve [HOME]` | home page, settings, the boards of all sessions and MCP (`--host`, `--port`, `--goal` and `--title` (with `--workspace DIR`), `--no-browser`, `--no-console`); without a terminal, `dreamteam` runs this one |
| `dreamteam sessions` / `dreamteam status` | list of sessions and whether the server is running |
| `dreamteam sessions rm ID [ID …]` | delete sessions with their board, messages, workspace files and traces (asks for confirmation; `--yes` skips the question); a folder outside the session (your project or a folder after `/cd`) stays where it is |
| `dreamteam console [--session ID]` | console chat with a running session (`/help` lists the commands) |
| `dreamteam traces [Agent] [--session ID] [--id N] [--span N --context] [--status failed] [--json]` | agent traces in the terminal (with or without the server) |
| `dreamteam mcp` | DreamTeam as an MCP server over stdio (a proxy to the running server or its own process) |
| `dreamteam sandbox [HOME]` | the same in Docker |
| `dreamteam init DIR` + `dreamteam run DIR` | the old mode: one swarm in a project folder, without the home page |

To give these commands (except `init` and `run`) another home folder, pass `--home DIR`.

You don't have to store API keys in the settings: if `api_key_env` is set, the key is read from the environment
variable. For Claude, the `ant auth login` profile works too.
