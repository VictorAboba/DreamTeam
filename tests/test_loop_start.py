"""cli.run_loop: every event loop starts through it, and on Windows it first checks the local connection that
asyncio's self-pipe needs and says what holds it (docs/SPEC.md §18.3, issue #62). The hang is simulated by
replacing socket.socketpair; every wait of a replacement is bounded. Sync tests only: pytest-asyncio's own loop
would call the replacement too."""

from __future__ import annotations

import _thread
import ast
import asyncio
import inspect
import os
import signal
import socket
import threading
import time
from pathlib import Path

import pytest

from dreamteam import cli

CHECK = "dreamteam-loopback"  # the thread of the check
WATCH = "dreamteam-loop-start"  # the thread that watches the loop's own pair
real_socketpair = socket.socketpair


@pytest.fixture
def windows(monkeypatch):
    """run_loop as on Windows, with a long wait: no notice unless a replacement holds the pair."""
    monkeypatch.setattr(cli, "LOOPBACK_SOCKETPAIR", True)
    monkeypatch.setattr(cli, "LOOPBACK_WAIT", 30.0)


def _noticed(monkeypatch) -> threading.Event:
    """An Event set after each notice run_loop writes."""
    event = threading.Event()
    original = cli._start_notice

    def notice(text: str) -> None:
        original(text)
        event.set()

    monkeypatch.setattr(cli, "_start_notice", notice)
    return event


def _socketpair(monkeypatch, hold=None) -> list:
    """Replace socket.socketpair: each call is recorded as (thread name, pair); `hold(thread name)`, when given,
    runs first in the calling thread (it may wait or raise)."""
    calls = []

    def replacement(*args, **kwargs):
        name = threading.current_thread().name
        if hold is not None:
            hold(name)
        pair = real_socketpair(*args, **kwargs)
        calls.append((name, pair))
        return pair

    monkeypatch.setattr(socket, "socketpair", replacement)
    return calls


async def _answer() -> int:
    return 42


def _join(name: str) -> None:
    for thread in threading.enumerate():
        if thread.name == name:
            thread.join(10)
            assert not thread.is_alive(), name


def test_a_quick_pair_starts_the_loop_after_a_check_that_says_nothing(windows, monkeypatch, capsys):
    calls = _socketpair(monkeypatch)
    assert cli.run_loop(_answer()) == 42
    names = [name for name, _pair in calls]
    me = threading.current_thread().name
    assert names[0] == CHECK and names[1:] and set(names[1:]) == {me}  # the check, then the loop's own pair
    assert all(sock.fileno() == -1 for sock in calls[0][1])  # the check's pair is closed
    _join(WATCH)  # the watch ends with the loop's first step
    assert capsys.readouterr().err == ""


def test_a_held_check_says_what_holds_it_and_goes_on_once_it_opens(windows, monkeypatch, capsys):
    monkeypatch.setattr(cli, "LOOPBACK_WAIT", 0.05)
    noticed = _noticed(monkeypatch)

    def hold(name):
        if name == CHECK:
            noticed.wait(10)
            cli.LOOPBACK_WAIT = 30.0  # (the loop's own pair is not held: no second notice; monkeypatch restores it)

    calls = _socketpair(monkeypatch, hold)
    assert cli.run_loop(_answer()) == 42
    err = capsys.readouterr().err
    assert err.count("is not starting") == 1 and "127.0.0.1" in err and cli._python_image() in err
    assert "Ctrl+C stops it" in err and cli.LOOPBACK_HELP in err
    assert "opened after" in err.split("Ctrl+C stops it")[1] and err.rstrip().endswith("DreamTeam goes on.")
    names = [name for name, _pair in calls]
    assert names[0] == CHECK and names[1:] and set(names[1:]) == {threading.current_thread().name}


def test_ctrl_c_while_the_check_waits_stops_the_start(windows, monkeypatch, capsys):
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("Ctrl+C reaches the main thread only")
    monkeypatch.setattr(cli, "LOOPBACK_WAIT", 0.05)
    noticed = _noticed(monkeypatch)
    release = threading.Event()

    def hold(name):
        if name == CHECK:
            noticed.wait(10)
            _thread.interrupt_main()
            release.wait(10)

    calls = _socketpair(monkeypatch, hold)
    ran = []

    async def body():
        ran.append(True)

    coro = body()
    # (a process that inherited SIGINT ignored, e.g. a background job of a POSIX shell, has no KeyboardInterrupt)
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with pytest.raises(KeyboardInterrupt):
            cli.run_loop(coro)
    finally:
        signal.signal(signal.SIGINT, previous)
        release.set()
        # the check's last call is over before the replacement goes (no join: on 3.11 a join that a KeyboardInterrupt
        # broke off marks the thread as stopped while it runs)
        deadline = time.monotonic() + 10
        while not calls and time.monotonic() < deadline:
            time.sleep(0.01)
    assert not ran and inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED
    assert "Ctrl+C stops it" in capsys.readouterr().err
    assert [name for name, _pair in calls] == [CHECK]  # no loop was made


def test_a_check_that_fails_says_so_and_the_start_goes_on(windows, monkeypatch, capsys):
    def hold(name):
        if name == CHECK:
            raise ConnectionError("Unexpected peer connection")

    calls = _socketpair(monkeypatch, hold)
    assert cli.run_loop(_answer()) == 42
    err = capsys.readouterr().err
    assert "could not open a connection" in err and "(ConnectionError: Unexpected peer connection)" in err
    assert "is not starting" not in err and "keeps waiting" not in err and cli.LOOPBACK_HELP in err
    assert [name for name, _pair in calls] == [threading.current_thread().name]  # the loop's own pair
    assert "(TimeoutError)." in cli.loopback_text(TimeoutError())  # (an error without a text)


def test_a_loop_pair_held_after_the_check_passed_is_told_too(windows, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_wait_for_loopback", lambda: None)  # (the check passed)
    monkeypatch.setattr(cli, "LOOPBACK_WAIT", 0.05)
    noticed = _noticed(monkeypatch)
    me = threading.current_thread().name

    def hold(name):
        if name == me:
            noticed.wait(10)

    _socketpair(monkeypatch, hold)
    assert cli.run_loop(_answer()) == 42
    err = capsys.readouterr().err
    assert err.count("is not starting") == 1 and "Ctrl+C cannot stop this wait" in err
    assert "keeps waiting" not in err and "Ctrl+C stops it" not in err


def test_the_watch_of_the_loop_s_pair_ends_when_the_loop_runs(windows, monkeypatch, capsys):
    """A watch that lasted as long as the loop would say "is not starting" in every long command (serve, the TUI)."""
    threads = []
    original = cli._daemon_thread

    def daemon_thread(target, name):
        thread = original(target, name)
        threads.append(thread)
        return thread

    monkeypatch.setattr(cli, "_daemon_thread", daemon_thread)

    async def long_run() -> bool:
        watch = next(thread for thread in threads if thread.name == WATCH)
        deadline = time.monotonic() + 10
        while watch.is_alive() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        return watch.is_alive()

    assert cli.run_loop(long_run()) is False  # it ended while the loop still ran
    assert capsys.readouterr().err == ""


def test_a_ctrl_c_that_breaks_off_the_cleanup_comes_out_as_keyboard_interrupt(windows):
    """A Ctrl+C while asyncio.run cancels the tasks at its end leaves the main one pending. run_loop leaves it so,
    as asyncio.run does: closing it would make a `finally` that awaits (`await conn.close()` of the TUI) raise
    RuntimeError "coroutine ignored GeneratorExit" in place of the KeyboardInterrupt."""
    awaiting = [True]

    def interrupt():
        raise KeyboardInterrupt

    async def body():
        loop = asyncio.get_running_loop()
        loop.call_soon(interrupt)  # (as the second Ctrl+C of _StopSignals raises it)
        try:
            try:
                await asyncio.sleep(10)
            finally:  # the cancel of asyncio.run's cleanup
                loop.call_soon(interrupt)
                await asyncio.sleep(10)
        finally:
            if awaiting[0]:
                await asyncio.sleep(0)

    coro = body()
    try:
        with pytest.raises(KeyboardInterrupt):
            cli.run_loop(coro)
    finally:
        awaiting[0] = False
        coro.close()  # (no await in its way now)


def test_a_loop_that_cannot_be_made_closes_main_unstarted(windows, monkeypatch):
    def no_loop(main):
        raise ConnectionError("Unexpected peer connection")  # (as the loop's own socketpair may fail)

    monkeypatch.setattr(asyncio, "run", no_loop)
    coro = _answer()
    with pytest.raises(ConnectionError):
        cli.run_loop(coro)
    assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED  # (no "never awaited" warning)


def test_without_a_thread_to_spare_the_start_goes_on(windows, monkeypatch, capsys):
    class NoThread(threading.Thread):
        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading, "Thread", NoThread)
    assert cli.run_loop(_answer()) == 42
    assert capsys.readouterr().err == ""


def test_elsewhere_run_loop_is_asyncio_run(monkeypatch, capsys):
    monkeypatch.setattr(cli, "LOOPBACK_SOCKETPAIR", False)
    calls = _socketpair(monkeypatch)
    assert cli.run_loop(_answer()) == 42
    assert {name for name, _pair in calls} == {threading.current_thread().name}  # no check
    assert capsys.readouterr().err == ""


def test_the_notice_names_a_program_file():
    assert os.path.isfile(cli._python_image())


LOOP_STARTS = {("asyncio", "run"), ("asyncio", "Runner"), ("asyncio", "new_event_loop"),
               ("asyncio", "get_event_loop"), ("anyio", "run")}


class _LoopStarts(ast.NodeVisitor):
    """Calls of LOOP_STARTS (`asyncio.run(...)`) and imports of them (`from asyncio import run`), with the function
    they are in."""

    def __init__(self, where: str):
        self.where, self.functions, self.found = where, [], []

    def visit_FunctionDef(self, node):
        self.functions.append(node.name)
        self.generic_visit(node)
        self.functions.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def _add(self) -> None:
        self.found.append((self.where, self.functions[-1] if self.functions else None))

    def visit_Call(self, node):
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and (
                (func.value.id, func.attr) in LOOP_STARTS):
            self._add()
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if any((node.module, alias.name) in LOOP_STARTS for alias in node.names):
            self._add()


def test_every_event_loop_starts_through_run_loop():
    src = Path(cli.__file__).resolve().parent
    found = []
    for path in sorted(src.rglob("*.py")):
        visitor = _LoopStarts(path.relative_to(src).as_posix())
        visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        found += visitor.found
    assert sorted(set(found)) == [("cli.py", "run_loop")]
