# Contributing to DreamTeam

Thanks for your interest! Bug reports, ideas, docs fixes and code are all welcome.
Issues and pull requests can be written in English or Russian.

## Questions and ideas

[Discussions](https://github.com/VictorAboba/DreamTeam/discussions) are for everything that isn't a bug or a
concrete task: questions in **Q&A**, ideas that are still taking shape in **Ideas**, what you built with the swarm
in **Show and tell**, everything else in **General**. An idea that the discussion turns into an agreed feature
becomes an issue.

## Reporting a bug

Open an issue with the **Bug report** template. The most useful reports include:

- what you ran (`dreamteam …` command, TUI, web board or MCP client) and what you expected;
- your OS, Python version and model provider (Anthropic, OpenAI, Ollama, …);
- the relevant part of the agent trace: `dreamteam traces "<Agent Name>" --status failed`
  or the **Export** button on the Traces tab. Check it for secrets before posting.

Security problems: please don't open a public issue — see [SECURITY.md](SECURITY.md).

## Development setup

You need Python 3.11+, [uv](https://docs.astral.sh/uv/) and git. New work goes into the `dev` branch, while
`master` holds only releases, so start from `dev`:

```bash
git clone https://github.com/VictorAboba/DreamTeam.git
cd DreamTeam
git switch dev
uv sync
uv run pytest -q
```

The tests drive agents with a scripted LLM (`dreamteam.llm.scripted.ScriptedLLM`), so they need no API keys
and no network. A few tests are platform-specific (Windows Job Objects, Linux network namespaces) and skip
themselves elsewhere; the Docker tests are opt-in with `DREAMTEAM_DOCKER_TESTS=1`.

To try your changes for real, install the checkout as a tool once — edits take effect immediately:

```bash
uv tool install --editable .
dreamteam
```

## Where to start

- **Pick an issue.** [`good first issue`](https://github.com/VictorAboba/DreamTeam/labels/good%20first%20issue)
  issues are small and self-contained, each with the files involved and what "done" means;
  [`help wanted`](https://github.com/VictorAboba/DreamTeam/labels/help%20wanted) ones are bigger. Ask for it first
  (see [Taking an issue](#taking-an-issue)), so two people don't do the same work. Questions in the issue are welcome.
- **Run only the tests you need.** The whole suite takes 8–16 minutes; while you work, run the files for the
  module you change, e.g. `uv run pytest -q tests/test_board.py`, or one test with `-k name`. Run everything
  once before you open the pull request (or let CI do it).
- **Web board tests** (`tests/test_ui.py`) execute the JavaScript in Node.js and skip themselves without `node`
  on PATH. If you change `src/dreamteam/static/`, install Node.js so they run.
- **Try it by hand.** `dreamteam serve --no-browser` starts the board on `http://127.0.0.1:8765`; a real session
  needs a model, and a local one through Ollama costs nothing.

### Map of the code

| Area | Files | Tests |
|---|---|---|
| Command line | `cli.py`, `console.py` | `test_cli*.py` |
| Terminal UI | `tui/` (`app.py`, `render.py`, `commands.py`, `i18n.py` with all its texts) | `test_tui_*.py` |
| Web board and REST | `web.py`, `launcher.py`, `static/` (`app.js` board, `home.js` sessions page, `traces.js`) | `test_ui.py`, `test_surfaces.py`, `test_launcher.py` |
| Task board, messages, triggers | `board.py`, `mailbox.py`, `triggers.py` | `test_board.py`, `test_mailbox.py`, `test_triggers.py` |
| Files, git, shell, jobs | `workspace.py`, `shell.py`, `jobs.py`, `procs.py`, `netoff.py` | `test_workspace.py`, `test_shell.py`, `test_jobs.py`, `test_procs.py`, `test_netoff.py` |
| Skills | `skills.py`, `swarm_skills.py`, `skill_deps.py`, `tools/skill_tools.py` | `test_skills.py`, `test_swarm_skills.py`, `test_skill_deps.py`, `test_skill_envs.py` |
| MCP | `mcp_forge.py` (servers agents write), `hub_mcp.py`, `launcher_mcp.py` (the swarm as an MCP server) | `test_mcp_forge.py`, `test_hub_clients.py` |
| Agents | `agent.py` (the agent loop), `prompts.py` (texts in ru/en), `roles.py`, `tools/` (agents' tools) | `test_runtime.py`, `test_prompts.py`, `test_roles.py`, `test_tools.py` |
| Models | `llm/` (`anthropic_llm.py`, `openai_llm.py`, `scripted.py` for tests) | `test_llm.py` |
| Settings | `config.py` (defaults), `settings.py` (the Settings page) | `test_launcher.py` |

`docs/SPEC.md` describes every module's contract: read the section of the module you change first.

## Taking an issue

1. Comment on the issue that you'd like to work on it, and wait until a maintainer assigns it to you. A pull
   request for an issue assigned to someone else may be closed.
2. **`good first issue` issues are for people making their first contribution to DreamTeam.** Take one at a
   time. Once your first pull request is merged, move on to `help wanted` or any other issue and leave the rest
   to the next newcomer.
3. If you can't go on, say so in the issue. An assigned issue with no activity for two weeks goes back to the pool.

## AI tools and automated accounts

- **AI coding assistants are fine as tools.** Say in the pull request which one you used. You must have run the
  tests yourself, understand every line you submit and answer review comments yourself. A pull request whose
  author can't explain it is closed.
- **`good first issue` issues are not open to automated accounts**: bots and agents that claim issues and open
  pull requests across many repositories. Their claims are ignored and their pull requests are closed without
  review. These issues exist so that people can learn the project.

## Pull requests

1. Open an issue first for anything bigger than a small fix, so we can agree on the approach.
2. Branch from `dev` and open the pull request against `dev`. `master` changes only when a new version is
   released, so a pull request against `master` gets its base switched to `dev`.
3. One pull request, one issue: write `Fixes #N` in the description, and leave unrelated changes for another pull
   request. Add tests for new behaviour and fixed bugs.
4. Write code like the code around it, and reuse existing helpers instead of copying them. If you have to
   duplicate something (because of a circular import, say), explain why in the pull request.
5. Run `uv run pytest -q` and `uvx ruff check src tests` before pushing; CI runs both on Linux, Windows and
   macOS. On your first pull request, CI waits until a maintainer approves the run (GitHub's protection for new
   contributors): that's expected, there's nothing to fix and no need to reopen the pull request.
6. Update the docs when behaviour visible to users changes: the guide pages in [docs/en](docs/en/README.md) and
   [docs/ru](docs/ru/README.md) (the same files in both languages — change both, or say in the PR which one needs
   a translation), the README if the short overview changes ([README.md](README.md) and [README.ru.md](README.ru.md)
   mirror each other) and, for module contracts, [docs/SPEC.md](docs/SPEC.md). For a change to the web board or
   the terminal UI, add a screenshot.

## Code conventions

- Code, comments and docstrings are in English. Texts that agents see live in `src/dreamteam/prompts.py`
  and exist in both Russian and English — change both.
- Module contracts are described in [docs/SPEC.md](docs/SPEC.md) and the terminal UI in
  [docs/TUI.md](docs/TUI.md); keep them in sync with the code.
- An agent's history is append-only and its tool set is fixed at hire time (prompt caching and Claude's
  thinking blocks depend on an unchanged prefix). New capabilities for agents usually go through MCP
  (`mcp_call`) rather than new built-in tools.
- No module prints to stdout: the stdio MCP mode uses it. Use `logging`.

## License

By contributing you agree that your contributions are licensed under the [MIT License](LICENSE).
