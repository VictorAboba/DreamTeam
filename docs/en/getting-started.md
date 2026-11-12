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
- To update to the latest version: `uv tool upgrade dreamteam` (from 0.1.x, read
  [Updating to 0.2.0](#updating-to-020) first). To remove: `uv tool uninstall dreamteam`.

To change the code, clone the repository and install it from the repository folder with `--editable`. Code changes
then take effect at once.

```bash
uv tool install --editable .
```

After you change the dependencies in `pyproject.toml` or move the repository folder, reinstall:
`uv tool install --editable . --force`. Without an install, run DreamTeam from the repository folder through uv:
`uv sync`, then `uv run dreamteam …`. The tests run the same way, see [CONTRIBUTING.md](../../CONTRIBUTING.md).

### Updating to 0.2.0

0.2.0 brings [crew tasks](features.md) and a new tool set for the agents. It does not open the sessions in which
agents of an earlier version already worked (almost every session of 0.1.x), and there is no conversion.

- **Before updating**, finish the sessions you still need, or export them and keep the folder:
  `dreamteam export FOLDER --include sessions` (one session: add `--session ID`; see
  [Moving to another computer](transfer.md)).
- **After updating**, such sessions stay in the list, marked as made by an earlier version: in `dreamteam sessions`,
  on the home page ("Earlier version") and in the terminal interface's `/sessions` ("earlier version"). The session's
  card on the home page says why it does not open, and so does Enter in the terminal interface's list. Such a
  session can be deleted and exported, but not opened, started or resumed, and the import of 0.2.0 refuses it.
  When the most recent session is one of them, `dreamteam -c` says so and opens nothing: start a new session.
- **A session in which no agent has run yet** (for example, one that was never started) opens as usual.
- **To open such a session again**, import its export into a separate home with DreamTeam 0.1.1 (0.1.0 has no
  import; 0.1.1 opens the sessions of 0.1.0 too). With uv this runs beside 0.2.0, for example:

  ```bash
  uvx --from "git+https://github.com/VictorAboba/DreamTeam.git@v0.1.1" dreamteam import FOLDER --home OLD_HOME
  uvx --from "git+https://github.com/VictorAboba/DreamTeam.git@v0.1.1" dreamteam --home OLD_HOME
  ```

  Export only the sessions (`--include sessions`) and set up the models in the new home's settings, as after a
  fresh install: once 0.2.0 has saved its settings, 0.1.x may not read them. For the same reason, do not go back to
  0.1.x in the home 0.2.0 used.
- **The `main` session of an old project** cannot be exported or deleted from the interface: finish its work before
  updating. Afterwards it can only be removed by hand: delete `<home>/.dreamteam/swarm.db` together with its `-wal`
  and `-shm` files (its message names the path).

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
   the rest becomes the description. You can also put together the starting team there or pick a saved team. Tick
   **"Clarify the goal first"** if the goal has open points: the planner then asks you about them before it plans,
   and the other agents wait ([clarify mode](features.md)). The swarm makes a plan and starts working, and you land on
   the session board `/s/<id>/`.

Sessions and settings are stored in the DreamTeam home folder: `~/DreamTeam` (set it with `--home` or the
`DREAMTEAM_HOME` variable). It holds `dreamteam.toml` with the global settings and `sessions/<id>/` with the data,
workspace files and traces of each session. You can create a session right from the command line:
`dreamteam serve --goal "Build a CLI tool that converts CSV to JSON, with tests" --title "CSV → JSON"`.

Commands:

| command | what it does |
|---|---|
| `dreamteam ["goal"]` / `dreamteam tui` | terminal interface; `-c` continues the last session, `-r [ID]` picks or opens a session; for a new session: `--model`, `--agents`, `--team "architect, developer*2"` or `--team NAME` (a saved team), `--max-agents`, `--budget`, `--title`, `--workspace DIR`, `--copy-from DIR\|ZIP` (the session works on a [copy of these files](projects.md#working-on-a-copy-of-files)), `--clarify` / `--no-clarify` (clarify mode for the sessions this window creates); a session made by an earlier version is not opened: `-c` whose most recent session is one says so and creates nothing, `-r ID` of one shows why ([Updating to 0.2.0](#updating-to-020)) |
| `dreamteam -p "goal"` | no interface: run the goal and print the result (`--output-format text/json/stream-json`, `--verbose`, `--timeout`, `-c`/`-r ID` for the next instruction to an existing session (one made by an earlier version: exit code 1 with its message), `--workspace DIR` for the folder of a new session, `--copy-from DIR\|ZIP` for a copy of files in it, `--clarify` only with `--no-auto-reply`); text from stdin is added to the request |
| `dreamteam serve [HOME]` | home page, settings, the boards of all sessions and MCP (`--host`, `--port`, `--goal` and `--title` (with `--workspace DIR`, `--copy-from DIR\|ZIP`, `--clarify`), `--no-browser`, `--no-console`); without a terminal, `dreamteam` runs this one |
| `dreamteam sessions` / `dreamteam status` | list of sessions and whether the server is running; sessions made by an earlier version are marked |
| `dreamteam sessions rm ID [ID …]` | delete sessions with their board, messages, workspace files and traces (asks for confirmation; `--yes` skips the question); a folder outside the session (your project or a folder after `/cd`) stays where it is; sessions made by an earlier version are deleted too, but not the `main` session of an old project: its message names the file to delete by hand |
| `dreamteam export FOLDER` | write the settings, skills, MCP servers and sessions into a folder for another computer or a backup (`--include settings,skills,mcp,sessions`, `--session ID`, `--dry-run`); API keys and `env` values stay here, see [Moving to another computer](transfer.md) |
| `dreamteam import FOLDER` | add what is new from such a folder and rewrite what changed; nothing the export does not carry is deleted (shows the plan and asks for confirmation; `--yes` or `-y` skips the question, `--dry-run` shows the plan only, `--include`, `--session ID`); refused while DreamTeam runs this home |
| `dreamteam console [--session ID]` | console chat with a running session (`/help` lists the commands) |
| `dreamteam traces [Agent] [--session ID] [--id N] [--span N --context] [--status failed] [--json]` | agent traces in the terminal (with or without the server) |
| `dreamteam mcp` | DreamTeam as an MCP server over stdio (a proxy to the running server or its own process) |
| `dreamteam sandbox [HOME]` | the same in Docker |
| `dreamteam init DIR` + `dreamteam run DIR` | the old mode: one swarm in a project folder, without the home page |

To give these commands (except `init` and `run`) another home folder, pass `--home DIR`.

You don't have to store API keys in the settings: if `api_key_env` is set, the key is read from the environment
variable. For Claude, the `ant auth login` profile works too.
