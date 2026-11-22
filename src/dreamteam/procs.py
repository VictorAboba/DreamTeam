"""Process trees of the agents' shell commands (docs/SPEC.md §7): a command and every process it starts are watched
and stopped as one tree, also after the shell itself exited, and the tree dies with the hub.

Windows: a Job Object with KILL_ON_JOB_CLOSE. The shell is assigned right after Popen returns, in the spawning
thread, and one Toolhelp32 pass assigns the children it may already have started. Liveness is the job's count of
active processes; closing the job's handle (release(), or the hub's death) kills whatever is still in it.

POSIX: a new session, whose process group id (the shell's pid) is stored: getpgid fails once the leader was reaped.
A `sh` lifeline watchdog per command reads a pipe the hub holds; when the hub dies the pipe reaches EOF and the
watchdog kills the group. On Linux, processes are also found by a random environment token
(DREAMTEAM_PROCESS_TOKEN), which catches the processes that left the group (`setsid`, GNU `timeout`): they keep the
tree alive and listed, and they are signalled with it.

Only the standard library is used: a test helper runs this module in a bare interpreter."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import weakref
from collections.abc import Callable, Iterable
from typing import Any

log = logging.getLogger(__name__)

TOKEN_ENV = "DREAMTEAM_PROCESS_TOKEN"
# Linux: how long a tree keeps the list of its token scan; within it, only the processes found are looked at again
TOKEN_SCAN_SECONDS = 2.0
# Linux: a process in execve reads an empty /proc/<pid>/environ until its new image's environment is set up; such a
# read is tried once more after this pause. PF_KTHREAD (the flags of /proc/<pid>/stat): a kernel thread, never in it
_EXEC_RETRY_SECONDS = 0.005
_PF_KTHREAD = 0x00200000
_WINDOWS = sys.platform == "win32"
_LINUX = sys.platform.startswith("linux")
# the watchdog: a line from the hub (release) ends it quietly; EOF (the hub died) kills the group, TERM first.
# `kill -s`, never `kill -KILL --`: dash rejects the latter.
WATCHDOG_SCRIPT = 'read _ || { kill -s TERM -- -"$1"; sleep 2; kill -s KILL -- -"$1"; } 2>/dev/null'
WATCHDOG_NAME = "dreamteam-watchdog"

_warned: set[str] = set()


def _warn_once(key: str, message: str, *args: Any) -> None:
    if key not in _warned:
        _warned.add(key)
        log.warning(message, *args)


# --- Windows: kernel32 through ctypes, built on first use -----------------------------------------------------

_K32: Any = None

JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x800      # a program that asks for CREATE_BREAKAWAY_FROM_JOB still starts
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_EXTENDED_LIMIT_INFORMATION = 9
_BASIC_ACCOUNTING_INFORMATION = 1
_BASIC_PROCESS_ID_LIST = 3
_PROCESS_TERMINATE = 0x1
_PROCESS_SET_QUOTA = 0x100
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TH32CS_SNAPPROCESS = 0x2
_INVALID_HANDLE = -1
_ID_LIST_MAX = 1024


def _kernel32() -> Any:
    """kernel32 with the prototypes used here, and the structures (built once, on Windows only)."""
    global _K32
    if _K32 is not None:
        return _K32
    import ctypes
    from ctypes import wintypes as wt

    class HeldGil(ctypes.WinDLL):
        """Calls that keep the GIL: every function used here is short and never blocks, and releasing the GIL for
        each of the hundreds of calls of a Toolhelp32 walk costs a switch interval apiece while another thread
        computes (seconds instead of a millisecond)."""
        _func_flags_ = ctypes.WinDLL._func_flags_ | getattr(ctypes, "_FUNCFLAG_PYTHONAPI", 0)

    k32 = HeldGil("kernel32", use_last_error=True)

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD)]

    class IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount",
                                                   "OtherOperationCount", "ReadTransferCount",
                                                   "WriteTransferCount", "OtherTransferCount")]

    class Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    class Accounting(ctypes.Structure):
        _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                    ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                    ("TotalPageFaultCount", wt.DWORD), ("TotalProcesses", wt.DWORD),
                    ("ActiveProcesses", wt.DWORD), ("TotalTerminatedProcesses", wt.DWORD)]

    class IdList(ctypes.Structure):
        _fields_ = [("NumberOfAssignedProcesses", wt.DWORD), ("NumberOfProcessIdsInList", wt.DWORD),
                    ("ProcessIdList", ctypes.c_size_t * _ID_LIST_MAX)]

    class ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                    ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    def proto(name: str, restype: Any, *argtypes: Any) -> None:
        fn = getattr(k32, name)
        fn.restype, fn.argtypes = restype, argtypes

    u64p = ctypes.POINTER(ctypes.c_uint64)
    proto("CreateJobObjectW", wt.HANDLE, ctypes.c_void_p, wt.LPCWSTR)
    proto("SetInformationJobObject", wt.BOOL, wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD)
    proto("QueryInformationJobObject", wt.BOOL, wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD,
          ctypes.POINTER(wt.DWORD))
    proto("AssignProcessToJobObject", wt.BOOL, wt.HANDLE, wt.HANDLE)
    proto("TerminateJobObject", wt.BOOL, wt.HANDLE, wt.UINT)
    proto("IsProcessInJob", wt.BOOL, wt.HANDLE, wt.HANDLE, ctypes.POINTER(wt.BOOL))
    proto("CloseHandle", wt.BOOL, wt.HANDLE)
    proto("OpenProcess", wt.HANDLE, wt.DWORD, wt.BOOL, wt.DWORD)
    proto("GetProcessTimes", wt.BOOL, wt.HANDLE, u64p, u64p, u64p, u64p)
    proto("QueryFullProcessImageNameW", wt.BOOL, wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD))
    proto("CreateToolhelp32Snapshot", wt.HANDLE, wt.DWORD, wt.DWORD)
    proto("Process32FirstW", wt.BOOL, wt.HANDLE, ctypes.POINTER(ProcessEntry))
    proto("Process32NextW", wt.BOOL, wt.HANDLE, ctypes.POINTER(ProcessEntry))
    k32.ctypes, k32.wt = ctypes, wt
    k32.Extended, k32.Accounting, k32.IdList, k32.ProcessEntry = Extended, Accounting, IdList, ProcessEntry
    _K32 = k32
    return k32


def _create_job(breakaway: bool = False) -> int | None:
    """A new Job Object that kills its processes when its last handle closes, or None (degraded mode).
    `breakaway`: a program that asks for CREATE_BREAKAWAY_FROM_JOB leaves the job (without it, it fails to start)."""
    k32 = _kernel32()
    job = k32.CreateJobObjectW(None, None)  # the handle is not inheritable
    if not job:
        return None
    info = k32.Extended()
    info.BasicLimitInformation.LimitFlags = (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                                             | (JOB_OBJECT_LIMIT_BREAKAWAY_OK if breakaway else 0))
    if not k32.SetInformationJobObject(job, _EXTENDED_LIMIT_INFORMATION, k32.ctypes.byref(info),
                                       k32.ctypes.sizeof(info)):
        k32.CloseHandle(job)
        return None
    return job


def _assign_process(job: int, handle: int) -> bool:
    return bool(_kernel32().AssignProcessToJobObject(job, handle))


def _creation_time(handle: int) -> int | None:
    k32 = _kernel32()
    times = [k32.ctypes.c_uint64() for _ in range(4)]
    if not k32.GetProcessTimes(handle, *(k32.ctypes.byref(t) for t in times)):
        return None
    return times[0].value


def _assign_shell(job: int, proc: subprocess.Popen) -> bool:
    """Put the shell into the job. Popen's own handle has PROCESS_ALL_ACCESS; a CPython without `_handle` gets an
    OpenProcess handle with just the rights the assignment needs."""
    handle = getattr(proc, "_handle", None)
    if handle is not None:
        return _assign_process(job, int(handle))
    k32 = _kernel32()
    opened = k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, proc.pid)
    if not opened:
        return False
    try:
        return _assign_process(job, opened)
    finally:
        k32.CloseHandle(opened)


def _exited_before_assignment(proc: subprocess.Popen) -> bool:
    """The assignment failed because the shell had already exited or was exiting (ERROR_ACCESS_DENIED): `cmd /c
    "start /b x & exit"` takes a few milliseconds, and the spawning thread may wait longer than that for the GIL.
    A terminating process is refused before it counts as exited, hence the short wait. The job is still good for
    what the shell started: Popen's handle keeps its pid and creation time, so the child pass finds its
    children."""
    if _last_error() != 5:
        return False
    try:
        proc.wait(timeout=0.5)
    except Exception:  # noqa: BLE001 - TimeoutExpired: it runs on, the refusal has another cause
        return False
    return True


def _assign_early_children(job: int, proc: subprocess.Popen) -> int:
    """One Toolhelp32 pass right after the shell's assignment: a child the shell started before it was assigned is
    outside the job, so every process whose parent is the shell (or such a child, in the job or not), created no
    earlier than that parent (a reused parent pid does not count), is assigned too. Failures are ignored: the
    process may be gone. Returns the number of processes assigned by this pass."""
    k32 = _kernel32()
    ctypes = k32.ctypes
    shell_handle = getattr(proc, "_handle", None)
    shell_created = _creation_time(int(shell_handle)) if shell_handle is not None else None
    if shell_created is None:
        return 0
    snapshot = k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == ctypes.c_void_p(_INVALID_HANDLE).value:
        return 0
    children: dict[int, list[int]] = {}
    try:
        entry = k32.ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        ok = k32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            children.setdefault(int(entry.th32ParentProcessID), []).append(int(entry.th32ProcessID))
            ok = k32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snapshot)
    assigned = 0
    pending = [(proc.pid, shell_created)]
    seen = {proc.pid}
    while pending:
        parent, parent_created = pending.pop()
        for pid in children.get(parent, ()):
            if pid in seen:
                continue
            seen.add(pid)
            handle = k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE | _PROCESS_QUERY_LIMITED_INFORMATION,
                                     False, pid)
            if not handle:
                continue
            try:
                created = _creation_time(handle)
                if created is None or created < parent_created:
                    continue  # the parent's pid was reused: not a child of this tree
                inside = k32.wt.BOOL()
                if not (k32.IsProcessInJob(handle, job, ctypes.byref(inside)) and inside.value):
                    if _assign_process(job, handle):
                        assigned += 1
                pending.append((pid, created))
            finally:
                k32.CloseHandle(handle)
    return assigned


def _job_active(job: int) -> int:
    k32 = _kernel32()
    info = k32.Accounting()
    if not k32.QueryInformationJobObject(job, _BASIC_ACCOUNTING_INFORMATION, k32.ctypes.byref(info),
                                         k32.ctypes.sizeof(info), None):
        return 0
    return int(info.ActiveProcesses)


def _job_pids(job: int) -> list[int]:
    k32 = _kernel32()
    ids = k32.IdList()
    # a list longer than the buffer still fills it (ERROR_MORE_DATA): the names of 1024 processes are plenty
    k32.QueryInformationJobObject(job, _BASIC_PROCESS_ID_LIST, k32.ctypes.byref(ids), k32.ctypes.sizeof(ids), None)
    count = min(int(ids.NumberOfProcessIdsInList), _ID_LIST_MAX)
    return [int(pid) for pid in ids.ProcessIdList[:count]]


def _image_name(pid: int) -> str | None:
    k32 = _kernel32()
    handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        buf = k32.ctypes.create_unicode_buffer(1024)
        size = k32.wt.DWORD(1024)
        if not k32.QueryFullProcessImageNameW(handle, 0, buf, k32.ctypes.byref(size)):
            return None
        return os.path.basename(buf.value)
    finally:
        k32.CloseHandle(handle)


def _taskkill(proc: subprocess.Popen) -> None:
    """Degraded mode: the tree by its parent links (the shell first must still run: /T finds its children through
    it), then the shell itself."""
    with contextlib.suppress(Exception):
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    with contextlib.suppress(Exception):
        proc.kill()


# --- POSIX: process groups through /proc (Linux) or killpg ----------------------------------------------------

def _stat_fields(pid: str) -> list[str] | None:
    """The fields of /proc/<pid>/stat after comm (state first). They are read after the LAST ")": comm may hold
    spaces and ")"."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    return data[data.rfind(")") + 2:].split()


def _proc_stat(pid: str) -> tuple[str, int] | None:
    """(state, pgrp) of /proc/<pid>/stat."""
    rest = _stat_fields(pid)
    try:
        return (rest[0], int(rest[2])) if rest else None
    except (IndexError, ValueError):
        return None


def _proc_pids() -> list[str]:
    try:
        return [entry for entry in os.listdir("/proc") if entry.isdigit()]
    except OSError:
        return []


def _group_members(pgids: set[int]) -> dict[int, list[int]]:
    """Linux: the live (not zombie) processes of each group in `pgids`, from one pass over /proc."""
    found: dict[int, list[int]] = {}
    for pid in _proc_pids():
        stat = _proc_stat(pid)
        if stat and stat[1] in pgids and stat[0] not in ("Z", "X"):
            found.setdefault(stat[1], []).append(int(pid))
    return found


def live_groups(pgids: Iterable[int]) -> set[int]:
    """The process groups among `pgids` that still have a live process. Linux: one pass over /proc, zombies ignored
    (an init-less container never reaps them). Other POSIX systems: killpg(pgid, 0), EPERM counting as alive.
    Windows has no process groups: always empty."""
    wanted = {int(p) for p in pgids if p}
    if not wanted or _WINDOWS:
        return set()
    if _LINUX and os.path.isdir("/proc"):
        return set(_group_members(wanted))
    alive: set[int] = set()
    for pgid in wanted:
        try:
            os.killpg(pgid, 0)
            alive.add(pgid)
        except PermissionError:
            alive.add(pgid)
        except OSError:
            pass
    return alive


def _uid_ok(owner: int, hub: int) -> bool:
    """The uid rule of the token scan (docs/SPEC.md §7 "Process layer"): the hub's own processes; a hub running as
    root looks at every uid, since the random token alone identifies a command's processes. It reaches another uid
    only where the kernel lets root read that process's environment: not in the Docker sandbox with Docker's default
    capabilities, whose offline commands (`setpriv --reuid`, netoff.py) are unreadable and so skipped."""
    return hub == 0 or owner == hub


def _environ(pid: str) -> bytes | None:
    """/proc/<pid>/environ, None when it cannot be read."""
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            return f.read()
    except OSError:
        return None


def _may_exec(pid: str) -> bool:
    """Whether an empty environment read of `pid` may be a process in execve: it lives and is no kernel thread."""
    rest = _stat_fields(pid)
    try:
        return bool(rest) and rest[0] not in ("Z", "X") and not int(rest[6]) & _PF_KTHREAD
    except (IndexError, ValueError):
        return False


def _token_scan(tokens: Iterable[str]) -> dict[str, dict[int, int]]:
    """Linux: {token: {pid: pgrp}} of the live (not zombie) processes whose environment holds
    DREAMTEAM_PROCESS_TOKEN=<token>, for several tokens in one pass over /proc/<pid>/environ. The hub's own pid is
    skipped; a process the uid rule leaves out, or whose environment cannot be read, is not seen. An empty read of a
    live process (one in execve) is read again once, after _EXEC_RETRY_SECONDS, for all such processes together."""
    wanted = {t for t in tokens if t}
    if not wanted or not _LINUX or not os.path.isdir("/proc"):
        return {}
    prefix = f"{TOKEN_ENV}=".encode()
    me, uid = os.getpid(), os.getuid()
    found: dict[str, dict[int, int]] = {}

    def match(pid: str, data: bytes) -> None:
        if prefix not in data:
            return
        hits = {entry[len(prefix):].decode("utf-8", "replace") for entry in data.split(b"\0")
                if entry.startswith(prefix)} & wanted
        stat = _proc_stat(pid) if hits else None
        if stat is None or stat[0] in ("Z", "X"):
            return
        for token in hits:
            found.setdefault(token, {})[int(pid)] = stat[1]

    empty: list[str] = []
    for pid in _proc_pids():
        if int(pid) == me:
            continue
        try:
            if not _uid_ok(os.stat(f"/proc/{pid}").st_uid, uid):
                continue
        except OSError:
            continue
        data = _environ(pid)
        if data:
            match(pid, data)
        elif data is not None and _may_exec(pid):
            empty.append(pid)
    if empty:
        time.sleep(_EXEC_RETRY_SECONDS)
        for pid in empty:
            match(pid, _environ(pid) or b"")
    return found


def token_pids(tokens: Iterable[str]) -> dict[str, list[int]]:
    """Linux: the live processes carrying each of `tokens` (DREAMTEAM_PROCESS_TOKEN), from one /proc pass; a token
    no process carries is left out. Empty elsewhere."""
    return {token: sorted(pids) for token, pids in _token_scan(tokens).items()}


def _carries(pid: int, entry: bytes) -> bool:
    """Linux: whether `pid` is a live process whose environment still holds `entry` (a pid freed and given to a
    process of another command does not). An empty read of a process in execve is read again once."""
    if not _may_exec(str(pid)):
        return False
    data = _environ(str(pid))
    if data == b"":
        time.sleep(_EXEC_RETRY_SECONDS)
        data = _environ(str(pid))
    return entry in (data or b"").split(b"\0")


def _signal_token(token: str, sig: int, *, skip_group: int | None = None) -> int:
    """Send `sig` to every process carrying `token` (found afresh, never from a kept list: a bare pid may name
    another process by now), except the members of `skip_group`. Returns how many were signalled."""
    sent = 0
    for pid, pgrp in _token_scan([token]).get(token, {}).items():
        if skip_group is not None and pgrp == skip_group:
            continue
        with contextlib.suppress(OSError):
            os.kill(pid, sig)
            sent += 1
    return sent


def sweep_token(token: str) -> int:
    """Linux: SIGKILL every process whose environment holds DREAMTEAM_PROCESS_TOKEN=`token` (the uid rule of
    `_uid_ok`); this also ends processes that left the group (`setsid`, GNU `timeout`). Returns how many were
    signalled (0 elsewhere)."""
    if not token:
        return 0
    return _signal_token(token, signal.SIGKILL)


# --- the tree ---------------------------------------------------------------------------------------------------

class _Handles:
    """What release() gives back: the job handle (Windows) or the watchdog's lifeline (POSIX). Kept apart from the
    tree so that weakref.finalize can release a tree that was dropped without release()."""

    def __init__(self) -> None:
        self.job: int | None = None
        self.lifeline: int | None = None
        self.watchdog: subprocess.Popen | None = None
        self.released = False
        self.lock = threading.Lock()

    def release(self) -> None:
        with self.lock:
            if self.released:
                return
            self.released = True
            job, lifeline, watchdog = self.job, self.lifeline, self.watchdog
        if job is not None:
            with contextlib.suppress(Exception):
                _kernel32().CloseHandle(job)
        if lifeline is not None:
            with contextlib.suppress(OSError):
                os.write(lifeline, b"q\n")  # the watchdog ends without a signal
            with contextlib.suppress(OSError):
                os.close(lifeline)
        if watchdog is not None:
            threading.Thread(target=_reap, args=(watchdog,), name="dreamteam-watchdog-reap", daemon=True).start()


def _reap(proc: subprocess.Popen) -> None:
    with contextlib.suppress(Exception):
        proc.wait()


class ProcessTree:
    """One command's process tree: the shell `proc` and everything it starts. Built by spawn() (blocking: call it
    through daemon_call). `contained` is False in the degraded Windows mode (no job): liveness is then the shell
    or an open pipe, and kill() uses taskkill."""

    def __init__(self, proc: subprocess.Popen, *, token: str | None = None) -> None:
        self.proc = proc
        self.pid = proc.pid
        self.pgid: int | None = None
        self.contained = False
        self.token = token
        self._handles = _Handles()
        self._eof = False
        # Linux: (time.monotonic() of alive()'s token scan, made with the group gone, the pids it found); dropped by
        # terminate() and kill()
        self._token_seen: tuple[float, list[int]] | None = None
        self._finalizer = weakref.finalize(self, self._handles.release)
        # at the hub's exit a tree still referenced is not released: the job's close or the lifeline's EOF must
        # then end what still runs
        self._finalizer.atexit = False

    @classmethod
    def spawn(cls, argv: list[str] | str, *, cwd: Any, env: dict[str, str] | None,
              token: str | None = None, breakaway: bool = False) -> "ProcessTree":
        """Start `argv` (stdin closed, stdout and stderr on one pipe) as a contained tree. `breakaway` (Windows):
        whether a program that asks for CREATE_BREAKAWAY_FROM_JOB may leave the job (else it fails to start)."""
        common: dict[str, Any] = dict(cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT)
        if _WINDOWS:
            try:
                job = _create_job(breakaway)
            except Exception as exc:  # noqa: BLE001 - e.g. ctypes unavailable: degraded mode
                log.debug("CreateJobObject: %s", exc)
                job = None
            try:
                proc = subprocess.Popen(argv, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                                        | getattr(subprocess, "CREATE_NO_WINDOW", 0), **common)
            except BaseException:
                if job is not None:
                    _kernel32().CloseHandle(job)
                raise
            tree = cls(proc, token=token)
            # no other Python work between Popen and the assignment: the shell needs milliseconds to start a child
            if job is not None and (_assign_shell(job, proc) or _exited_before_assignment(proc)):
                tree._handles.job = job
                tree.contained = True
                with contextlib.suppress(Exception):
                    # a child assigned by a pass may have started its own child before that (the venv launcher
                    # does): repeat until a pass finds nothing new
                    for _attempt in range(3):
                        if not _assign_early_children(job, proc):
                            break
            else:
                if job is not None:
                    _kernel32().CloseHandle(job)
                _warn_once("job", "cannot put agent commands into a Windows job object (error %s): processes they "
                           "leave running are tracked by their output pipe only", _last_error())
            return tree
        proc = subprocess.Popen(argv, start_new_session=True, **common)
        tree = cls(proc, token=token)
        tree.pgid = proc.pid  # == the shell's pid under start_new_session; never asked again (getpgid fails later)
        tree.contained = True
        tree._arm_watchdog()
        return tree

    def _arm_watchdog(self) -> None:
        read_end, write_end = os.pipe()  # both non-inheritable (PEP 446): only the watchdog gets the read end
        # never with a process token: a hub that itself runs as a command of another hub (an agent running
        # `dreamteam run`, or this project's tests) carries that outer token, and the outer token sweep would kill
        # the watchdog together with this hub, before it could kill the group it guards
        env = {key: value for key, value in os.environ.items() if key != TOKEN_ENV}
        try:
            watchdog = subprocess.Popen(["sh", "-c", WATCHDOG_SCRIPT, WATCHDOG_NAME, str(self.pgid)],
                                        stdin=read_end, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        start_new_session=True, env=env)
        except OSError as exc:
            os.close(write_end)
            _warn_once("watchdog", "cannot start the lifeline watchdog of agent commands (%s): their processes may "
                       "outlive a crashed hub", exc)
            return
        finally:
            os.close(read_end)
        self._handles.lifeline = write_end
        self._handles.watchdog = watchdog

    @property
    def released(self) -> bool:
        return self._handles.released

    @property
    def watchdog(self) -> subprocess.Popen | None:
        return self._handles.watchdog

    def mark_eof(self) -> None:
        """The output pipe reached EOF (called by the reader): every holder of the pipe is gone."""
        self._eof = True

    def _shell_running(self) -> bool:
        with contextlib.suppress(Exception):
            return self.proc.poll() is None
        return False

    def _token_scanned(self) -> bool:
        return bool(_LINUX and self.token)

    def _token_kept(self, now: float) -> list[int] | None:
        """The live processes of the last token scan while it is younger than TOKEN_SCAN_SECONDS, or None when a new
        scan must decide: the scan is older, or every process it found has ended since (one of them may have
        started another before it ended). An empty scan is kept: it was made with the group gone (only alive() and
        alive_all keep a scan), so no process of the tree was left to start one."""
        seen = self._token_seen
        if seen is None or now - seen[0] >= TOKEN_SCAN_SECONDS:
            return None
        entry = f"{TOKEN_ENV}={self.token}".encode()
        live = [pid for pid in seen[1] if _carries(pid, entry)]
        return live if live or not seen[1] else None

    def _token_found(self) -> list[int]:
        """Linux: the live processes carrying the tree's token (the kept scan, or a new one); [] without a token
        or elsewhere."""
        if not self._token_scanned():
            return []
        now = time.monotonic()
        kept = self._token_kept(now)
        if kept is not None:
            return kept
        token = self.token or ""
        found = token_pids([token]).get(token, [])
        self._token_seen = (now, found)
        return found

    def alive(self) -> bool:
        """True while any process of the tree runs (the shell included). The pipe's EOF never decides: a process
        may close or redirect its output and go on running. Linux: once the shell and the group are gone, while a
        process carries the tree's token (it left the group: `setsid`, GNU `timeout`)."""
        if self._handles.released:
            return self._shell_running()
        if _WINDOWS:
            job = self._handles.job
            if job is None:  # degraded: the shell, or whoever still holds its output pipe
                return self._shell_running() or not self._eof
            return _job_active(job) > 0
        if self._shell_running():
            return True
        if self.pgid and live_groups([self.pgid]):
            return True
        return bool(self._token_found())

    def pids(self) -> list[int]:
        """The tree's process ids, as far as the system tells (Windows: the job; Linux: the group and the token's
        processes outside it, from a new token scan: a kept one would miss a child started outside the group since;
        it is not kept, an empty one made while the group runs proves nothing for alive())."""
        if self._handles.released:
            return [self.pid] if self._shell_running() else []
        if _WINDOWS:
            job = self._handles.job
            if job is None:
                return [self.pid] if self._shell_running() else []
            return _job_pids(job)
        if _LINUX and self.pgid:
            members = set(_group_members({self.pgid}).get(self.pgid, []))
            if self._token_scanned():
                token = self.token or ""
                members |= set(token_pids([token]).get(token, []))
            return sorted(members)
        return [self.pid] if self._shell_running() else []

    def processes(self) -> list[str]:
        """Names of the tree's processes for messages ("node.exe"), conhost.exe left out; empty when unknown."""
        names: list[str] = []
        if self._handles.released:
            return names
        if _WINDOWS and self._handles.job is not None:
            for pid in _job_pids(self._handles.job):
                name = _image_name(pid)
                if name and name.lower() != "conhost.exe":
                    names.append(name)
        elif _LINUX and self.pgid:
            for pid in self.pids():
                with contextlib.suppress(OSError):
                    with open(f"/proc/{pid}/comm", encoding="utf-8", errors="replace") as f:
                        names.append(f.read().strip())
        return names

    def terminate(self) -> None:
        """Ask the tree to end: SIGTERM to the group plus (Linux) to the token's processes outside it. Windows has
        no graceful stop: the job is terminated."""
        if self._handles.released:
            return
        self._token_seen = None
        try:
            if _WINDOWS:
                self._kill_windows()
            elif self.pgid:
                with contextlib.suppress(OSError):  # the group may be gone (or not ours), the token's processes not
                    os.killpg(self.pgid, signal.SIGTERM)
                if self.token:
                    _signal_token(self.token, signal.SIGTERM, skip_group=self.pgid)
        except Exception as exc:  # noqa: BLE001 - the group may be gone already
            log.debug("terminate of %s: %s", self.pid, exc)

    def kill(self) -> None:
        """End the tree at once: SIGKILL to the group plus (Linux) the token sweep; Windows: the job is terminated
        (degraded mode: taskkill /T in a daemon thread)."""
        if self._handles.released:
            return
        self._token_seen = None
        try:
            if _WINDOWS:
                self._kill_windows()
            else:
                if self.pgid:
                    with contextlib.suppress(OSError):  # the sweep runs all the same
                        os.killpg(self.pgid, signal.SIGKILL)
                if self.token:
                    sweep_token(self.token)
        except Exception as exc:  # noqa: BLE001
            log.debug("kill of %s: %s", self.pid, exc)

    def _kill_windows(self) -> None:
        job = self._handles.job
        if job is not None:
            _kernel32().TerminateJobObject(job, 1)
            return
        threading.Thread(target=_taskkill, args=(self.proc,), name="dreamteam-taskkill", daemon=True).start()

    def release(self) -> None:
        """Give the job handle or the lifeline back, once the tree is empty or killed: nothing is signalled after
        this. Closing a job still holding processes kills them (KILL_ON_JOB_CLOSE). Idempotent."""
        self._handles.release()


def _last_error() -> int:
    with contextlib.suppress(Exception):
        return _kernel32().ctypes.get_last_error()
    return 0


def alive_all(trees: Iterable[ProcessTree]) -> list[bool]:
    """alive() of many trees with one /proc pass on Linux for their groups and one token scan for the trees whose
    group is gone and whose kept scan does not decide (the job monitor checks every job each second)."""
    trees = list(trees)
    if not _LINUX:
        return [t.alive() for t in trees]
    result: list[bool | None] = []
    groups: set[int] = set()
    for tree in trees:
        if tree.released or tree._shell_running() or not tree.pgid:
            result.append(tree.alive())
        else:
            result.append(None)
            groups.add(tree.pgid)
    live = live_groups(groups) if groups else set()
    now = time.monotonic()
    scan: list[int] = []  # indexes of the trees a new token scan decides
    for i, tree in enumerate(trees):
        if result[i] is not None:
            continue
        if tree.pgid in live:
            result[i] = True
        elif not tree._token_scanned():
            result[i] = False
        elif (kept := tree._token_kept(now)) is not None:
            result[i] = bool(kept)
        else:
            scan.append(i)
    if scan:
        found = token_pids(trees[i].token or "" for i in scan)
        for i in scan:
            tree = trees[i]
            pids = found.get(tree.token or "", [])
            tree._token_seen = (now, pids)
            result[i] = bool(pids)
    return [bool(r) for r in result]


async def daemon_call(fn: Callable[..., Any], *args: Any,
                      on_orphan: Callable[[Any], None] | None = None) -> Any:
    """Run `fn(*args)` in a new daemon thread and await its result. Never the loop's default executor: git uses it,
    and asyncio.run waits for its threads at exit. When the caller was cancelled meanwhile, `on_orphan` gets the
    result that nobody awaits any more (e.g. a process tree to kill)."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()

    def settle(ok: bool, value: Any) -> None:
        if future.cancelled():
            if ok and on_orphan is not None:
                on_orphan(value)
            return
        if ok:
            future.set_result(value)
        else:
            future.set_exception(value)

    def run() -> None:
        try:
            value, ok = fn(*args), True
        except BaseException as exc:  # noqa: BLE001 - handed to the awaiting task
            value, ok = exc, False
        try:
            loop.call_soon_threadsafe(settle, ok, value)
        except RuntimeError:  # the loop is closed: nobody waits any more
            if ok and on_orphan is not None:
                with contextlib.suppress(Exception):
                    on_orphan(value)

    threading.Thread(target=run, name="dreamteam-spawn", daemon=True).start()
    return await future


__all__ = ["ProcessTree", "TOKEN_ENV", "TOKEN_SCAN_SECONDS", "alive_all", "daemon_call", "live_groups", "sweep_token",
           "token_pids"]
