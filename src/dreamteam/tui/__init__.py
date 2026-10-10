"""Terminal client of a DreamTeam hub in the style of Claude Code (docs/TUI.md).

Entry points for cli.py:
- `main_tui(home, ...)` / `main_print(home, prompt, ...)`: synchronous; connect to the hub of the home
  (starting the launcher in this process when none runs), run, close the connection; return the exit code.
- `run_tui(conn, ...)` / `run_print_mode(conn, prompt, ...)`: the same on an open Connection.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any, Callable

from .app import TUI, run_tui
from .client import Connection, HubError, connect
from .commands import session_overrides

CONNECT_OPTIONS = ("host", "port", "llm_factory", "web")


async def run_print_mode(conn: Connection, prompt: str, *, overrides: dict | None = None,
                         title: str | None = None, **options: Any) -> int:
    """`dreamteam -p`: printmode.run_print with the CLI overrides (model, agents, max_agents, budget, title,
    or override sections) turned into the session overrides of a new session."""
    from .printmode import run_print

    sections, flat_title = session_overrides(overrides)
    return await run_print(conn, prompt, title=title or flat_title, overrides=sections or None, **options)


def _default_logging(home: Path) -> None:
    """Log to <home>/.dreamteam/logs (nothing on stderr: it would tear the UI) unless the caller configured
    logging already."""
    if logging.getLogger().handlers:
        return
    try:
        from ..cli import setup_logging

        setup_logging(home, quiet_stderr=True)
    except Exception:  # logging must never stop the client
        logging.getLogger().addHandler(logging.NullHandler())


def main_tui(home: str | Path, *, lang: str | None = None, session: str | None = None,
             continue_last: bool = False, initial_prompt: str | None = None, overrides: dict | None = None,
             host: str | None = None, port: int | None = None, verbosity: str = "normal",
             llm_factory: Callable | None = None) -> int:
    """Run the interactive TUI for `home`. `overrides` (model, agents, max_agents, budget, title) apply to
    sessions created from it. Returns the exit code (130 on Ctrl+C outside the UI)."""
    home = Path(home).expanduser()
    _default_logging(home)

    async def run() -> int:
        conn = await connect(home, host=host, port=port, llm_factory=llm_factory)
        try:
            return await run_tui(conn, lang=lang, session=session, continue_last=continue_last,
                                 initial_prompt=initial_prompt, verbosity=verbosity, overrides=overrides)
        finally:
            await conn.close()

    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 130
    except HubError as exc:
        print(f"dreamteam: {exc.message}", file=sys.stderr)
        return 1


def main_print(home: str | Path, prompt: str, **options: Any) -> int:
    """Run `dreamteam -p prompt` for `home`. Options: host, port (the in-process launcher), plus the
    keyword arguments of run_print_mode (session, continue_last, title, overrides, output_format, verbose,
    timeout, auto_reply, lang, out, err). Sessions of the home are not resumed by it, and when it runs the
    launcher itself, the session it used is stopped at the end: a one-shot run is not resumed by the next
    `dreamteam` (`dreamteam -c` still continues it)."""
    home = Path(home).expanduser()
    connect_options = {key: options.pop(key) for key in CONNECT_OPTIONS if key in options}
    _default_logging(home)

    async def run() -> int:
        try:
            conn = await connect(home, resume=False, **connect_options)
        except HubError as exc:
            print(f"dreamteam: {exc.message}", file=options.get("err") or sys.stderr)
            return 1
        used: list[str] = []
        try:
            return await run_print_mode(conn, prompt, on_session=used.append, **options)
        finally:
            try:
                if conn.owned and conn.manager is not None:
                    for sid in used:
                        if sid in conn.manager.running:
                            await conn.manager.stop(sid)  # an explicit stop clears the resume mark
            except Exception:
                logging.getLogger(__name__).exception("could not stop the print mode session")
            finally:
                await conn.close()

    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 130


__all__ = ["TUI", "run_tui", "run_print_mode", "main_tui", "main_print", "Connection", "HubError", "connect"]
