"""Bounded waits for the end-to-end tests that start a server or a subprocess: a hung process fails its test after a
time that fits it, with its whole process tree killed first, instead of hanging the suite for half an hour.

On Windows a venv's python.exe is a launcher whose child is the real interpreter (and a hub starts more children):
Popen.kill() alone ends only the launcher and leaves the rest running, holding the pipes, so a reader of them never
sees EOF. spawn() starts a process as a tree that kill_tree() ends whole, also after the process itself exited and
left a child holding its pipes: on Windows a Job Object (as procs.py puts agents' commands into one), on POSIX a new
session whose group is killed. A process started by plain Popen gets taskkill /T (its tree by the parent links, so
only while it runs) or, on POSIX, its group when it leads one."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
import threading
import weakref
from typing import Any, Awaitable, Callable

import pytest

from dreamteam import procs

WINDOWS = sys.platform == "win32"


def spawn(argv: list[str], **popen: Any) -> subprocess.Popen:
    """subprocess.Popen(argv, **popen) as a tree for kill_tree(). POSIX: in a new session unless the caller chose
    a group. Windows: in a Job Object that kills what is left when it is closed (by kill_tree, or when the Popen
    is dropped); without one (degraded) kill_tree falls back to taskkill /T."""
    if not WINDOWS:
        if "process_group" not in popen:
            popen.setdefault("start_new_session", True)
        proc = subprocess.Popen(argv, **popen)
        if popen.get("start_new_session"):
            proc._guard_pgid = proc.pid  # type: ignore[attr-defined] - getpgid fails once the leader was reaped
        return proc
    handles = procs._Handles()
    with contextlib.suppress(Exception):
        handles.job = procs._create_job()
    try:
        proc = subprocess.Popen(argv, **popen)
    except BaseException:
        handles.release()
        raise
    # no other work between Popen and the assignment: the venv launcher starts its child within milliseconds
    if handles.job is not None and (procs._assign_shell(handles.job, proc) or procs._exited_before_assignment(proc)):
        with contextlib.suppress(Exception):
            for _attempt in range(3):  # children started before the assignment, and theirs
                if not procs._assign_early_children(handles.job, proc):
                    break
        proc._guard_handles = handles  # type: ignore[attr-defined]
        weakref.finalize(proc, handles.release)
    else:
        handles.release()
    return proc


def kill_tree(proc: subprocess.Popen, wait: float = 10) -> None:
    """End `proc` and every process it started, then reap it (at most `wait` seconds). Idempotent. A spawn()ed
    tree is killed whole even when `proc` already exited; see the module's docstring for a plain Popen."""
    handles = getattr(proc, "_guard_handles", None)
    pgid = getattr(proc, "_guard_pgid", None)
    if handles is not None:
        if not handles.released:
            with contextlib.suppress(Exception):
                procs._kernel32().TerminateJobObject(handles.job, 1)
            handles.release()
    elif pgid is not None:
        with contextlib.suppress(OSError):
            os.killpg(pgid, signal.SIGKILL)  # the group outlives its reaped leader while a member runs
    if proc.poll() is None:
        if WINDOWS:
            procs._taskkill(proc)  # taskkill /T /F (the tree by its parent links), then the process itself
        else:
            with contextlib.suppress(OSError):
                if pgid is None and os.getpgid(proc.pid) == proc.pid:  # it leads its own group
                    os.killpg(proc.pid, signal.SIGKILL)
            with contextlib.suppress(OSError):
                proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(wait)


def _readers(proc: subprocess.Popen) -> list[threading.Thread]:
    if not hasattr(proc, "_guard_readers"):
        proc._guard_readers = []  # type: ignore[attr-defined]
    return proc._guard_readers  # type: ignore[attr-defined]


def close_pipes(proc: subprocess.Popen) -> None:
    """Close proc's pipes, except one that a reader thread of read_line() still blocks on: closing it would wait for
    that read (BufferedReader's lock) for as long as some process holds the pipe. It is left to the GC."""
    for reader in _readers(proc):
        reader.join(5)
    blocked = any(reader.is_alive() for reader in _readers(proc))
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is not None and not (blocked and stream is proc.stdout):
            with contextlib.suppress(OSError):
                stream.close()


@contextlib.contextmanager
def guarded(proc: subprocess.Popen):
    """`with guarded(spawn(...)) as proc:` the tree is killed on the way out (whether the test passed, an assertion
    failed or a wait timed out); its pipes are closed."""
    try:
        yield proc
    finally:
        kill_tree(proc)
        close_pipes(proc)


def wait_exit(proc: subprocess.Popen, timeout: float, what: str = "the process") -> int:
    """proc.wait(timeout); on timeout the tree is killed and the test fails."""
    try:
        return proc.wait(timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        pytest.fail(f"{what} did not exit within {timeout:g} s: its process tree was killed")


def _bounded_read(proc: subprocess.Popen, read: Callable[[], bytes], timeout: float, failure: str) -> bytes:
    """read() of proc.stdout in a daemon thread (a blocked read cannot be interrupted); when it has not returned in
    time the tree is killed, which closes the pipe and ends the reader, and the test fails. A reader that still
    blocks after that is left behind (close_pipes() does not wait for it)."""
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["data"] = read()
        except Exception as exc:  # noqa: BLE001 - reported below
            box["error"] = exc

    reader = threading.Thread(target=target, name="test-read", daemon=True)
    _readers(proc).append(reader)
    reader.start()
    reader.join(timeout)
    if reader.is_alive():
        kill_tree(proc)
        reader.join(10)
        pytest.fail(f"{failure} within {timeout:g} s (exit={proc.poll()}): its process tree was killed")
    _readers(proc).remove(reader)
    if "error" in box:
        raise box["error"]
    return box.get("data", b"")


def read_line(proc: subprocess.Popen, timeout: float, what: str = "the server") -> bytes:
    """One line of proc.stdout, bounded (_bounded_read)."""
    return _bounded_read(proc, proc.stdout.readline, timeout, f"no output line from {what}")


def read_rest(proc: subprocess.Popen, timeout: float, what: str = "the server") -> bytes:
    """The rest of proc.stdout up to EOF, bounded (_bounded_read): a child left holding the pipe fails the test."""
    return _bounded_read(proc, proc.stdout.read, timeout, f"the output of {what} did not end")


def run(argv: list[str], timeout: float, *, capture_output: bool = False, **popen: Any) -> subprocess.CompletedProcess:
    """subprocess.run with a bound that holds: on timeout the whole tree is killed before the pipes are drained
    (subprocess.run kills only the child and then waits for the pipes, which a grandchild may hold for ever), and
    the test fails. Popen's `with` is not used: its exit closes a pipe that a reader thread of communicate() may
    still block on, which waits for that read."""
    if capture_output:
        popen.setdefault("stdout", subprocess.PIPE)
        popen.setdefault("stderr", subprocess.PIPE)
    proc = spawn(argv, **popen)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.communicate(timeout=10)
        pytest.fail(f"{argv[:3]} did not finish within {timeout:g} s: its process tree was killed")
    kill_tree(proc)  # what it left running (it exited: nothing else to wait for)
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def call(fn: Callable[..., Any], *args: Any, timeout: float, what: str | None = None) -> Any:
    """fn(*args) in a daemon thread, failing the test when it has not returned in `timeout` seconds (code under
    test that hangs in-process cannot be killed: the thread is left behind, the suite goes on)."""
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = fn(*args)
        except BaseException as exc:  # noqa: BLE001 - re-raised in the test's thread
            box["error"] = exc

    worker = threading.Thread(target=target, name="test-bounded-call", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        pytest.fail(f"{what or getattr(fn, '__name__', 'the call')} did not return within {timeout:g} s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def run_async(main: Callable[[], Awaitable[Any]], timeout: float, what: str = "the scenario", *,
              grace: float = 60) -> Any:
    """asyncio.run(main()) bounded also when a step hangs in a worker thread (asyncio.to_thread): wait_for cancels
    only the awaiting coroutine, and asyncio.run's shutdown then joins the default executor without a timeout (3.11).
    main() is cancelled after `timeout` (its finally blocks run: a hub it started stops) and fails the test; the
    loop's thread is given up `grace` seconds later."""
    async def bounded() -> Any:
        try:
            return await asyncio.wait_for(main(), timeout)
        except TimeoutError as exc:
            if isinstance(exc.__cause__, asyncio.CancelledError):  # the bound, not a step's own TimeoutError
                pytest.fail(f"{what} did not finish within {timeout:g} s")
            raise

    return call(lambda: asyncio.run(bounded()), timeout=timeout + grace,
                what=f"{what} (a step hung in a worker thread past its {timeout:g} s bound)")
