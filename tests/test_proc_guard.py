"""The bounded waits of the end-to-end tests (proc_guard.py): a process whose grandchild holds its output pipe makes
a plain readline() or subprocess.run(timeout=...) hang for as long as that grandchild lives; the guard fails fast
and leaves nothing running."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
import time

import pytest
from proc_guard import call, guarded, kill_tree, read_line, read_rest, run, run_async, spawn, wait_exit

# starts a grandchild that inherits stdout and sleeps, then sleeps itself: nothing is ever printed
HOLDER = ("import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
          "time.sleep(60)")
# the same grandchild, but the child exits at once: only a tree known from the start can still be killed
LEAVER = "import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])"


def _eof_soon(proc: subprocess.Popen, within: float = 15) -> bool:
    """Whether proc.stdout reaches EOF in time: every holder of the pipe (the grandchild too) is gone."""
    done = threading.Event()
    threading.Thread(target=lambda: (proc.stdout.read(), done.set()), daemon=True).start()
    return done.wait(within)


def test_read_line_kills_the_tree_and_fails():
    with guarded(spawn([sys.executable, "-c", HOLDER], stdout=subprocess.PIPE)) as proc:
        started = time.monotonic()
        with pytest.raises(pytest.fail.Exception, match="no output line from the holder within 1 s"):
            read_line(proc, 1, "the holder")
        assert time.monotonic() - started < 30
        assert proc.poll() is not None
        assert _eof_soon(proc), "the grandchild still holds the pipe"


def test_read_line_returns_a_line():
    with guarded(spawn([sys.executable, "-c", "print('hi', flush=True)"], stdout=subprocess.PIPE)) as proc:
        assert read_line(proc, 30).strip() == b"hi"
        assert wait_exit(proc, 30) == 0


def test_run_kills_the_tree_on_timeout():
    started = time.monotonic()
    with pytest.raises(pytest.fail.Exception, match="did not finish within 1 s"):
        run([sys.executable, "-c", HOLDER], 1, capture_output=True)
    assert time.monotonic() - started < 40  # subprocess.run would wait for the grandchild's 60 s
    out = run([sys.executable, "-c", "print('ok')"], 30, capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.strip() == "ok"


def test_wait_exit_and_kill_tree():
    proc = spawn([sys.executable, "-c", HOLDER], stdout=subprocess.PIPE)
    try:
        with pytest.raises(pytest.fail.Exception, match="the holder did not exit within 0.5 s"):
            wait_exit(proc, 0.5, "the holder")
        assert proc.poll() is not None and _eof_soon(proc)
        kill_tree(proc)  # idempotent
    finally:
        kill_tree(proc)
        proc.stdout.close()


def test_a_grandchild_left_behind_is_killed_and_the_reads_stay_bounded():
    """The direct child exits and leaves a grandchild holding the pipe: reading to EOF fails in time, and both that
    failure and leaving guarded() end the grandchild instead of waiting for its 60 s."""
    started = time.monotonic()
    with guarded(spawn([sys.executable, "-c", LEAVER], stdout=subprocess.PIPE)) as proc:
        assert wait_exit(proc, 30) == 0
        with pytest.raises(pytest.fail.Exception, match="the output of the leaver did not end within 1 s"):
            read_rest(proc, 1, "the leaver")
        assert _eof_soon(proc), "the grandchild still holds the pipe"
    assert time.monotonic() - started < 40
    # leaving guarded() kills it too, also with nothing read (the pipe is not waited for)
    started = time.monotonic()
    with guarded(spawn([sys.executable, "-c", LEAVER], stdout=subprocess.PIPE)) as proc:
        assert wait_exit(proc, 30) == 0
    assert time.monotonic() - started < 40
    with guarded(spawn([sys.executable, "-c", "print('a'); print('b')"], stdout=subprocess.PIPE)) as proc:
        assert read_rest(proc, 30).split() == [b"a", b"b"]
    # run() ends what its command left running
    started = time.monotonic()
    with pytest.raises(pytest.fail.Exception, match="did not finish within 2 s"):
        run([sys.executable, "-c", LEAVER], 2, capture_output=True)
    assert time.monotonic() - started < 40


def test_call_is_bounded():
    assert call(lambda a, b: a + b, 2, 3, timeout=10) == 5
    with pytest.raises(ValueError):
        call(int, "x", timeout=10)
    never = threading.Event()
    with pytest.raises(pytest.fail.Exception, match="the hang did not return within 0.2 s"):
        call(never.wait, timeout=0.2, what="the hang")
    never.set()


def test_run_async_is_bounded_also_when_a_worker_thread_hangs():
    """asyncio.run(asyncio.wait_for(...)) waited for a hung asyncio.to_thread step at its shutdown: no bound."""
    async def answer() -> int:
        return await asyncio.to_thread(lambda: 42)

    assert run_async(answer, 30) == 42
    never = threading.Event()
    stopped = threading.Event()

    async def hangs() -> None:
        try:
            await asyncio.to_thread(never.wait)
        finally:
            stopped.set()  # the coroutine's own cleanup runs at the bound

    started = time.monotonic()
    with pytest.raises(pytest.fail.Exception, match="a step hung in a worker thread past its 0.2 s bound"):
        run_async(hangs, 0.2, grace=1)
    assert time.monotonic() - started < 10 and stopped.is_set()
    never.set()

    async def own_timeout() -> None:
        raise TimeoutError("a step's own")

    with pytest.raises(TimeoutError, match="a step's own"):
        run_async(own_timeout, 30)
