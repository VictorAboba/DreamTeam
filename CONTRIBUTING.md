# Contributing to DreamTeam

Thanks for your interest! Bug reports, ideas, docs fixes and code are all welcome.
Issues and pull requests can be written in English or Russian.

## Reporting a bug

Open an issue with the **Bug report** template. The most useful reports include:

- what you ran (`dreamteam …` command, TUI, web board or MCP client) and what you expected;
- your OS, Python version and model provider (Anthropic, OpenAI, Ollama, …);
- the relevant part of the agent trace: `dreamteam traces "<Agent Name>" --status failed`
  or the **Export** button on the Traces tab. Check it for secrets before posting.

Security problems: please don't open a public issue — see [SECURITY.md](SECURITY.md).

## Development setup

You need Python 3.11+, [uv](https://docs.astral.sh/uv/) and git.

```bash
git clone https://github.com/VictorAboba/DreamTeam.git
cd DreamTeam
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

## Pull requests

1. Open an issue first for anything bigger than a small fix, so we can agree on the approach.
2. Keep a pull request to one change, with tests for new behaviour and fixed bugs.
3. Run `uv run pytest -q` and `uvx ruff check --select E9,F63,F7,F82 src tests` before pushing; CI runs both
   on Linux, Windows and macOS.
4. Update the docs when behaviour visible to users changes: the guide pages in [docs/en](docs/en/README.md) and
   [docs/ru](docs/ru/README.md) (the same files in both languages — change both, or say in the PR which one needs
   a translation), the README if the short overview changes ([README.md](README.md) and [README.ru.md](README.ru.md)
   mirror each other) and, for module contracts, [docs/SPEC.md](docs/SPEC.md).

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
