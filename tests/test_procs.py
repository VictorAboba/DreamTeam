"""The process layer of the agents' console (docs/SPEC.md §7): contained process trees (a Windows Job Object, a
POSIX process group with a lifeline watchdog), leftover detection after the shell exited, kills by the stored
group, crash containment and the reader thread that alone owns the output pipe.

Self-contained (no conftest fixtures): child processes are Python scripts in tmp_path, every one of them writes
its pid, and each test checks that none is left running."""

from __future__ import annotations

import asyncio
import gc
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from dreamteam import procs
from dreamteam.shell import resolve_shell, shell_args, start_command

WINDOWS = sys.platform == "win32"
LINUX = sys.platform.startswith("linux")
posix_only = pytest.mark.skipif(WINDOWS, reason="POSIX process groups")
windows_only = pytest.mark.skipif(not WINDOWS, reason="Windows Job Objects")
linux_only = pytest.mark.skipif(not LINUX, reason="Linux /proc")
SRC = Path(__file__).resolve().parents[1] / "src"
CHILD = ("import os, pathlib, sys, time\n"
         "if len(sys.argv) > 2:\n"
         "    pathlib.Path(sys.argv[2]).write_text(str(os.getpid()))\n"
         "print('child up', flush=True)\n"
         "time.sleep(float(sys.argv[1]))\n"
         "print('child end', flush=True)\n")
QUICK = "cmd" if WINDOWS else "sh"  # the fastest shell to start


def pid_alive(pid: int) -> bool:
    if WINDOWS:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            state = f.read().decode("utf-8", "replace")
        return state[state.rfind(")") + 2:].split()[0] not in ("Z", "X")
    except OSError:
        return True


def kill_pid(pid: int) -> None:
    """Cleanup of a process this test started (never anything else)."""
    if WINDOWS:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def wait_until(predicate, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


class Children:
    """Pid files of the test's child processes; at the end every child must be gone (a leftover is killed and
    fails the test)."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.files: list[Path] = []
        (folder / "child.py").write_text(CHILD, encoding="utf-8")

    def pidfile(self) -> str:
        path = self.folder / f"pid{len(self.files)}.txt"
        self.files.append(path)
        return str(path)

    def pid(self, index: int = -1, timeout: float = 20.0) -> int:
        path = self.files[index]
        assert wait_until(lambda: path.exists() and path.read_text().strip(), timeout), f"{path.name} not written"
        return int(path.read_text())

    def pids(self) -> list[int]:
        return [int(p.read_text()) for p in self.files if p.exists() and p.read_text().strip()]

    def check(self) -> list[int]:
        left = [pid for pid in self.pids() if not wait_until(lambda pid=pid: not pid_alive(pid), 5)]
        for pid in left:
            kill_pid(pid)
        return left


@pytest.fixture
def children(tmp_path):
    kids = Children(tmp_path)
    yield kids
    assert kids.check() == [], "child processes were left running"


def env_for(tmp_path: Path, python: str | None = None) -> dict[str, str]:
    return {**os.environ, "PYEXE": python or sys.executable, "PYTHONUNBUFFERED": "1"}


def py_cmd(program: str, script: str, *args: str) -> str:
    """`script args` run with $PYEXE in the syntax of `program`."""
    if program in ("powershell", "pwsh"):
        return "& $env:PYEXE " + " ".join(f"'{a}'" for a in (script, *args))
    if program == "cmd":
        return '"%PYEXE%" ' + " ".join(f'"{a}"' for a in (script, *args))
    return '"$PYEXE" ' + " ".join(f"'{a}'" for a in (script, *args))


def spawn(command: str, program: str, cwd: Path, env: dict[str, str], token: str | None = None) -> procs.ProcessTree:
    name, prefix = resolve_shell(program)
    return procs.ProcessTree.spawn(shell_args(name, prefix, command), cwd=cwd, env=env, token=token)


def drain(tree: procs.ProcessTree) -> threading.Thread:
    """Read a bare tree's pipe in a daemon thread (the way RunningCommand does), so no writer ever blocks."""
    out = bytearray()

    def read() -> None:
        fd = tree.proc.stdout.fileno()
        while True:
            try:
                data = os.read(fd, 65536)
            except OSError:
                break
            if not data:
                break
            out.extend(data)
        tree.mark_eof()
        tree.proc.stdout.close()  # like the real reader: only it closes the pipe, at EOF

    thread = threading.Thread(target=read, daemon=True)
    thread.out = out  # type: ignore[attr-defined]
    thread.start()
    return thread


# --- quick commands ----------------------------------------------------------------------------------------------

async def test_quick_command_leaves_no_tree(tmp_path):
    cmd = await start_command("echo hi", cwd=tmp_path, program=QUICK)
    try:
        assert await cmd.wait_exit(30)
        assert await cmd.settle(1.0)  # empty within a second of the shell's exit
        assert await cmd.wait_eof(2.0)
        assert cmd.returncode == 0 and cmd.output().strip() == "hi"
        assert cmd.tree.contained
    finally:
        cmd.close()
    cmd.tree.release()  # idempotent
    cmd.close()
    assert cmd.tree.released and not cmd.tree.alive()


# --- leftovers ---------------------------------------------------------------------------------------------------

def _orphan_command(children: Children) -> tuple[str, str]:
    pidfile = children.pidfile()
    if WINDOWS:
        return ("powershell", "Start-Process -NoNewWindow -FilePath $env:PYEXE -ArgumentList "
                              f"'child.py','20','{pidfile}'; 'shell done'")
    return "sh", f'"$PYEXE" child.py 20 \'{pidfile}\' & echo shell done'


async def test_orphan_holding_the_pipe_does_not_block(tmp_path, children):
    """A process the command started keeps the output pipe open: the command still ends when its shell exits
    (EOF would come only when the orphan exits), the leftover is seen and the kill ends it."""
    program, command = _orphan_command(children)
    cmd = await start_command(command, cwd=tmp_path, program=program, env=env_for(tmp_path))
    started = time.monotonic()
    try:
        assert await cmd.wait_exit(30)
        pid = children.pid()
        assert not await cmd.settle(0.5)  # the orphan is in the tree
        assert time.monotonic() - started < 15
        assert not cmd.eof  # it holds the pipe
        assert pid_alive(pid)
        names = cmd.tree.processes()
        if WINDOWS or LINUX:
            assert any(n.lower().startswith("python") for n in names), names
        assert pid in cmd.tree.pids() or not (WINDOWS or LINUX)
        assert await cmd.kill()
        assert not cmd.tree.alive()
        assert await cmd.wait_eof(3)
        assert wait_until(lambda: not pid_alive(pid), 3)
        assert "shell done" in cmd.output()
    finally:
        cmd.close()


@windows_only
async def test_cmd_start_b_is_contained(tmp_path, children):
    pidfile = children.pidfile()
    cmd = await start_command(f'start /b "" "%PYEXE%" child.py 20 "{pidfile}" & echo done', cwd=tmp_path,
                              program="cmd", env=env_for(tmp_path))
    try:
        assert await cmd.wait_exit(30)
        pid = children.pid()
        assert cmd.tree.alive() and pid in cmd.tree.pids()
        assert await cmd.kill()
        assert wait_until(lambda: not pid_alive(pid), 3)
    finally:
        cmd.close()


def _cygwin_bash() -> str | None:
    """Git Bash, MSYS2 or Cygwin bash; not WSL's launcher in System32 (that one runs Linux)."""
    path = shutil.which("bash") if WINDOWS else None
    return path if path and "system32" not in path.lower() else None


@windows_only
@pytest.mark.skipif(_cygwin_bash() is None, reason="no Git Bash / MSYS2 bash")
async def test_git_bash_commands_are_contained(tmp_path, children):
    """The Cygwin runtime of Git Bash (MSYS2) starts every Windows program with CREATE_BREAKAWAY_FROM_JOB whenever
    the job allows it: a bash command's tree must still hold what it runs, in the foreground and left running with
    `&`, and its kill must end it."""
    for suffix in ("", " & echo shell done"):
        pidfile = children.pidfile()
        cmd = await start_command(py_cmd("bash", "child.py", "20", pidfile) + suffix, cwd=tmp_path, program="bash",
                                  env=env_for(tmp_path))
        try:
            pid = children.pid()
            assert pid in cmd.tree.pids(), (suffix, cmd.tree.processes())
            assert await cmd.kill()
            assert wait_until(lambda: not pid_alive(pid), 3)
        finally:
            cmd.close()


@windows_only
@pytest.mark.skipif(_cygwin_bash() is None, reason="no Git Bash / MSYS2 bash")
@pytest.mark.parametrize("program", ["cmd", "powershell"])
async def test_bash_run_by_cmd_or_powershell_is_contained(tmp_path, children, program):
    """cmd or PowerShell running `bash script` (or a git hook): what that bash starts asks to leave the job, and
    the job must not let it, so the command's kill still ends it."""
    (tmp_path / "run.sh").write_text('"$PYEXE" child.py 20 "$1"\n', encoding="utf-8", newline="\n")
    pidfile = children.pidfile().replace("\\", "/")
    bash = _cygwin_bash()
    command = (f'"{bash}" run.sh "{pidfile}"' if program == "cmd" else f"& '{bash}' run.sh '{pidfile}'")
    cmd = await start_command(command, cwd=tmp_path, program=program, env=env_for(tmp_path))
    try:
        pid = children.pid()
        assert pid in cmd.tree.pids(), cmd.tree.processes()
        assert await cmd.kill()
        assert wait_until(lambda: not pid_alive(pid), 3)
    finally:
        cmd.close()


@posix_only
async def test_kill_uses_the_stored_pgid(tmp_path, children):
    """getpgid fails once the leader was reaped: the stored group id still reaches `cmd &`."""
    pidfile = children.pidfile()
    cmd = await start_command(f"\"$PYEXE\" child.py 30 '{pidfile}' & echo started", cwd=tmp_path, program="sh",
                              env=env_for(tmp_path))
    try:
        assert await cmd.wait_exit(30)
        pid = children.pid()
        assert cmd.tree.proc.returncode == 0  # the leader is reaped
        with pytest.raises(ProcessLookupError):
            os.getpgid(cmd.tree.pid)
        assert cmd.tree.alive()
        assert await cmd.kill()
        assert wait_until(lambda: not pid_alive(pid), 3)
    finally:
        cmd.close()


# --- containment when the hub dies ---------------------------------------------------------------------------

HUB = """
import os, sys, time
sys.path.insert(0, {src!r})
from dreamteam import procs
from dreamteam.shell import resolve_shell, shell_args
name, prefix = resolve_shell({program!r})
tree = procs.ProcessTree.spawn(shell_args(name, prefix, {command!r}), cwd={cwd!r}, env=dict(os.environ))
tree.proc.wait()
print("spawned", flush=True)
time.sleep(120)
"""


async def test_tree_dies_with_its_owner(tmp_path, children):
    """The hub is killed hard (no cleanup at all): the Job Object's last handle closes (Windows) or the lifeline
    watchdog sees EOF (POSIX), and the processes the command left running die with it."""
    pidfile = children.pidfile()
    if WINDOWS:
        program, command = "cmd", f'start /b "" "%PYEXE%" child.py 60 "{pidfile}" & echo started'
        python = getattr(sys, "_base_executable", sys.executable)  # no venv launcher (and its job) around the hub
    else:
        program, command = "sh", f"\"$PYEXE\" child.py 60 '{pidfile}' & echo started"
        python = sys.executable
    script = tmp_path / "hub.py"
    script.write_text(HUB.format(src=str(SRC), program=program, command=command, cwd=str(tmp_path)),
                      encoding="utf-8")
    hub = subprocess.Popen([python, str(script)], cwd=tmp_path, env=env_for(tmp_path), stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT)
    try:
        pid = children.pid(timeout=30)
        assert pid_alive(pid)
        hub.kill()  # TerminateProcess / SIGKILL
        hub.wait(10)
        assert wait_until(lambda: not pid_alive(pid), 4), "the leftover outlived its hub"
    finally:
        if hub.poll() is None:
            hub.kill()
            hub.wait(10)
        hub.stdout.close()


@posix_only
async def test_watchdog_release_does_not_kill(tmp_path, children):
    """release() ends the watchdog quietly (exit 0, no signal): nothing is signalled after it."""
    pidfile = children.pidfile()
    tree = await procs.daemon_call(lambda: spawn(f"\"$PYEXE\" child.py 20 '{pidfile}' & echo x", "sh", tmp_path,
                                                 env_for(tmp_path)))
    reader = drain(tree)
    pid = children.pid()
    tree.proc.wait(10)
    watchdog = tree.watchdog
    assert watchdog is not None and watchdog.poll() is None
    tree.release()
    assert watchdog.wait(5) == 0
    time.sleep(0.3)
    assert pid_alive(pid)  # released: the group is not signalled any more
    tree.kill()            # a no-op after release()
    time.sleep(0.3)
    assert pid_alive(pid)
    kill_pid(pid)
    reader.join(5)


@posix_only
async def test_finalizer_releases_a_leaked_tree(tmp_path):
    """A tree dropped without release() (a bug) is released by its finalizer: its watchdog ends quietly instead of
    firing at the hub's exit at a group number that may have been reused."""
    tree = await procs.daemon_call(lambda: spawn("echo leaked", "sh", tmp_path, env_for(tmp_path)))
    tree.proc.wait(10)
    tree.proc.stdout.close()
    watchdog = tree.watchdog
    assert watchdog is not None
    del tree
    await asyncio.sleep(0)  # the loop handle that resumed this step still holds the spawn's future (and the tree)
    gc.collect()
    assert watchdog.wait(5) == 0


@windows_only
async def test_assignment_failure_degrades(tmp_path, children, monkeypatch):
    """Without a job (the assignment failed) the command still runs; the kill falls back to taskkill /T."""
    monkeypatch.setattr(procs, "_assign_process", lambda job, handle: False)
    monkeypatch.setattr(procs, "_exited_before_assignment", lambda proc: False)
    cmd = await start_command("echo hi", cwd=tmp_path, program="cmd")
    try:
        assert await cmd.wait_exit(30) and await cmd.settle(2) and await cmd.wait_eof(2)
        assert cmd.output().strip() == "hi" and not cmd.tree.contained
    finally:
        cmd.close()
    pidfile = children.pidfile()
    cmd = await start_command(py_cmd("cmd", "child.py", "20", pidfile), cwd=tmp_path, program="cmd",
                              env=env_for(tmp_path))
    try:
        pid = children.pid()
        assert cmd.tree.alive() and not cmd.tree.contained
        assert await cmd.kill()
        assert await cmd.wait_exit(5)
        assert wait_until(lambda: not pid_alive(pid), 5)
    finally:
        cmd.close()


@linux_only
async def test_linux_token_sweep(tmp_path, children):
    """`setsid` leaves the process group; the kill still finds the process by its environment token."""
    if not shutil.which("setsid"):
        pytest.skip("setsid is not installed")
    pidfile = children.pidfile()
    cmd = await start_command(f"setsid \"$PYEXE\" child.py 20 '{pidfile}' > /dev/null 2>&1 & echo x", cwd=tmp_path,
                              program="sh", env=env_for(tmp_path), token="sweep" + os.urandom(4).hex())
    try:
        assert await cmd.wait_exit(30)
        pid = children.pid()
        assert pid_alive(pid)
        await cmd.kill()
        assert wait_until(lambda: not pid_alive(pid), 3)
    finally:
        cmd.close()


NESTED_HUB = """
import os, sys, time
sys.path.insert(0, {src!r})
from dreamteam import procs
from dreamteam.shell import resolve_shell, shell_args
name, prefix = resolve_shell("sh")
token = "inner" + os.urandom(4).hex()
tree = procs.ProcessTree.spawn(shell_args(name, prefix, {command!r}), cwd={cwd!r},
                               env={{**os.environ, procs.TOKEN_ENV: token}}, token=token)
tree.proc.wait()
print("spawned, watchdog", tree.watchdog.pid, flush=True)
time.sleep(120)
"""


@linux_only
async def test_a_nested_hubs_commands_die_with_it(tmp_path, children):
    """A hub that itself runs as a command of another hub (an agent running `dreamteam run`, or this project's tests
    run as a job) carries the outer token. Stopping that outer command kills the inner hub through the outer token
    sweep; the inner hub's watchdogs carry no token, so they survive it and kill the groups they guard."""
    pidfile = children.pidfile()
    script = tmp_path / "inner_hub.py"
    script.write_text(NESTED_HUB.format(src=str(SRC), command=f"\"$PYEXE\" child.py 60 '{pidfile}' & echo started",
                                        cwd=str(tmp_path)), encoding="utf-8")
    outer = await start_command(f"\"$PYEXE\" '{script}'", cwd=tmp_path, program="sh", env=env_for(tmp_path),
                                token="outer" + os.urandom(4).hex())
    try:
        pid = children.pid(timeout=30)
        assert pid_alive(pid)
        assert wait_until(lambda: "spawned, watchdog" in outer.output(), 10)
        inner_watchdog = int(outer.output().split("spawned, watchdog", 1)[1].split()[0])
        assert not _environ_has(str(inner_watchdog), procs.TOKEN_ENV.encode() + b"=" + outer.tree.token.encode())
        assert await outer.kill()
        assert wait_until(lambda: not pid_alive(pid), 6), "the inner hub's command outlived it"
        assert wait_until(lambda: not pid_alive(inner_watchdog), 6)  # it did its work (TERM, 2 s, KILL) and ended
    finally:
        if outer.tree.alive():  # a failed check above: the inner hub must not run on
            await outer.kill()
        outer.close()


def _environ_has(pid: str, entry: bytes) -> bool:
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            return entry in f.read().split(b"\0")
    except OSError:
        return False


@linux_only
def test_live_groups_ignores_zombies():
    zombie = subprocess.Popen(["sh", "-c", "exit 0"], start_new_session=True)
    try:
        assert wait_until(lambda: procs._proc_stat(str(zombie.pid)) and procs._proc_stat(str(zombie.pid))[0] == "Z",
                          5)
        assert procs.live_groups([zombie.pid]) == set()
    finally:
        zombie.wait(5)


@linux_only
def test_proc_stat_reads_after_the_last_parenthesis(tmp_path):
    """comm may hold spaces and ")": the fields are read after the last one."""
    script = tmp_path / "a b) c.py"
    script.write_text("import time; time.sleep(10)", encoding="utf-8")
    exe = tmp_path / "x) y"
    exe.symlink_to(sys.executable)
    proc = subprocess.Popen([str(exe), str(script)], start_new_session=True)
    try:
        assert wait_until(lambda: procs._proc_stat(str(proc.pid)) is not None, 5)
        state, pgrp = procs._proc_stat(str(proc.pid))
        assert pgrp == proc.pid and state in "RSD"
    finally:
        proc.kill()
        proc.wait(5)


@windows_only
async def test_early_children_are_contained(tmp_path, children):
    """A child the shell starts before it is put into the job (the spawning thread waits for the GIL: another
    thread keeps it busy) is still caught by the Toolhelp32 pass: in every run the leftover is in the job."""
    stop = threading.Event()

    def burn() -> None:
        while not stop.is_set():
            sum(range(20000))

    burners = [threading.Thread(target=burn, daemon=True) for _ in range(2)]
    for thread in burners:
        thread.start()
    try:
        for _ in range(30):
            pidfile = children.pidfile()
            tree = await procs.daemon_call(lambda pidfile=pidfile: spawn(
                f'start /b "" "%PYEXE%" child.py 20 "{pidfile}" & exit', "cmd", tmp_path, env_for(tmp_path)))
            reader = drain(tree)
            try:
                tree.proc.wait(10)
                children.pid()
                assert tree.alive(), "the shell's child escaped the job"
                tree.kill()
                assert wait_until(lambda: not tree.alive(), 3)
            finally:
                tree.release()
                reader.join(3)
    finally:
        stop.set()
        for thread in burners:
            thread.join(5)


# --- the reader owns the pipe ------------------------------------------------------------------------------------

SPAWNER = """
import subprocess, sys
# CREATE_BREAKAWAY_FROM_JOB | CREATE_NO_WINDOW: outside the command's job, holding its output pipe
subprocess.Popen([sys.executable, "child.py", "20", sys.argv[1]], stdout=sys.stdout.fileno(),
                 stderr=subprocess.STDOUT, creationflags=0x01000000 | 0x08000000)
print("spawned", flush=True)
"""


async def test_reader_never_closes_under_a_blocked_read(tmp_path, children, monkeypatch):
    """close() only detaches: while a process outside the tree still holds the pipe, the reader stays blocked on
    its fd and the fd stays open (a closed number would soon be reused by another file or socket). The reader
    closes it itself at EOF."""
    pidfile = children.pidfile()
    if WINDOWS:
        import dreamteam.shell

        monkeypatch.setattr(dreamteam.shell, "JOB_BREAKAWAY", True)  # the holder must get out of the job
        (tmp_path / "spawner.py").write_text(SPAWNER, encoding="utf-8")
        program, command = "cmd", f'"%PYEXE%" spawner.py "{pidfile}"'
        env = env_for(tmp_path, getattr(sys, "_base_executable", sys.executable))
    else:
        program, command = "sh", f"\"$PYEXE\" child.py 20 '{pidfile}' & echo x"
        env = env_for(tmp_path)
    cmd = await start_command(command, cwd=tmp_path, program=program, env=env)
    pid = children.pid()
    assert await cmd.wait_exit(30)
    started = time.monotonic()
    cmd.close()  # releases the tree (the holder is outside it or survives the release) and detaches the capture
    assert time.monotonic() - started < 1
    await asyncio.sleep(0.5)
    assert pid_alive(pid) and not cmd.eof
    assert not cmd._stdout.closed  # still the reader's
    kill_pid(pid)
    assert await cmd.wait_eof(10)
    assert cmd._stdout.closed


async def test_cancelled_spawn_kills_the_new_tree(tmp_path, children, monkeypatch):
    """A caller cancelled while Popen runs in its daemon thread: the tree it then gets is killed and released."""
    pidfile = children.pidfile()
    started = threading.Event()
    spawned: list[procs.ProcessTree] = []
    real = procs.ProcessTree.spawn.__func__

    def slow_spawn(cls, *args, **kwargs):
        started.set()
        time.sleep(0.5)
        tree = real(cls, *args, **kwargs)
        spawned.append(tree)
        return tree

    monkeypatch.setattr(procs.ProcessTree, "spawn", classmethod(slow_spawn))
    task = asyncio.ensure_future(start_command(py_cmd(QUICK, "child.py", "20", pidfile), cwd=tmp_path,
                                               program=QUICK, env=env_for(tmp_path)))
    while not started.is_set():
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    end = time.monotonic() + 10
    while not spawned and time.monotonic() < end:
        await asyncio.sleep(0.05)
    assert spawned and wait_until(lambda: spawned[0].released, 5)
    assert wait_until(lambda: not spawned[0].alive(), 5)
    # the child may not have written its pid before the kill: then it never ran
    for pid in children.pids():
        assert wait_until(lambda pid=pid: not pid_alive(pid), 5)
