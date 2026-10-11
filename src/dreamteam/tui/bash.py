"""`!command` of the terminal UI (docs/TUI.md §4.2): one shell command in the session workspace that the user
can interrupt.

A thread of the loop's default executor would block until the command ends: neither Esc nor quitting could stop
it, and asyncio.run waits for that thread at exit. Here a daemon thread reads the output, and the whole process
tree is killed on interrupt(), on the timeout and when the task running it is cancelled."""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from ..shell import decode, kill_tree, resolve_shell, shell_args, truncate_middle

MAX_OUTPUT = 12000       # characters kept (the middle of a longer output is cut), as for the agents' console
KILL_WAIT = 5.0          # after a kill, how long the rest of the output may take to arrive
POWERSHELL_UTF8 = ("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
                   "$OutputEncoding = [System.Text.Encoding]::UTF8; ")


def _settle(future: asyncio.Future, result: Any) -> None:
    if future.done():
        return
    if isinstance(result, BaseException):
        future.set_exception(result)
    else:
        future.set_result(result)


class ShellCommand:
    """One `!command`: run() starts it and returns (exit code, output); interrupt() ends it early. The exit
    code is None when the command was killed (timeout, interrupt)."""

    def __init__(self, command: str, *, cwd: Path, program: str = "auto", env: dict[str, str] | None = None):
        self.command = command
        self.cwd = Path(cwd)
        self.program = program or "auto"
        self.env = env
        self.interrupted = False
        self._proc: subprocess.Popen | None = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    async def run(self, timeout: float) -> tuple[int | None, str]:
        name, prefix = resolve_shell(self.program)  # ValueError: an unsupported shell
        command = POWERSHELL_UTF8 + self.command if name in ("powershell", "pwsh") else self.command
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kwargs["start_new_session"] = True  # its own process group: killpg ends the whole tree
        loop = asyncio.get_running_loop()
        done: asyncio.Future = loop.create_future()
        proc = subprocess.Popen(shell_args(name, prefix, command), cwd=str(self.cwd), env=self.env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs)
        self._proc = proc

        def read() -> None:
            try:
                out = proc.stdout.read() if proc.stdout is not None else b""  # until every writer is gone
                result: Any = (proc.wait(), out or b"")
            except BaseException as exc:  # noqa: BLE001 - handed to the waiting task
                result = exc
            with contextlib.suppress(RuntimeError):  # the loop is closed: nobody waits any more
                loop.call_soon_threadsafe(_settle, done, result)

        # a daemon thread: an interpreter that exits never waits for it
        threading.Thread(target=read, name="dreamteam-tui-bash", daemon=True).start()
        if self.interrupted:
            self._kill(block=False)
        try:
            try:
                code, raw = await asyncio.wait_for(asyncio.shield(done), timeout)
            except TimeoutError:
                self._kill(block=False)
                code, raw = None, b""
                with contextlib.suppress(TimeoutError):
                    _, raw = await asyncio.wait_for(asyncio.shield(done), KILL_WAIT)
        except asyncio.CancelledError:
            self._kill(block=True)  # the UI is closing: the process must not outlive it
            raise
        if self.interrupted:
            code = None
        text = decode(raw, name).replace("\r\n", "\n")
        return code, truncate_middle(text, MAX_OUTPUT)

    def interrupt(self) -> None:
        """Kill the command and every process it started (Esc / Ctrl+C in the UI)."""
        self.interrupted = True
        self._kill(block=False)

    def _kill(self, *, block: bool) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        if block or sys.platform != "win32":
            kill_tree(proc)
        else:  # taskkill takes a moment: not on the event loop
            threading.Thread(target=kill_tree, args=(proc,), name="dreamteam-tui-kill", daemon=True).start()


__all__ = ["ShellCommand", "decode"]
