"""Agent console: run shell commands in the workspace with a timeout and a scrubbed environment.

A command runs as a contained process tree (procs.py): a Windows Job Object or a POSIX process group, so that the
processes it starts can be found, kept or killed after the shell itself exited. A daemon thread reads the output
pipe and another one waits for the shell; nothing runs on the loop's default executor (git uses it, and
asyncio.run waits for its threads at exit). A command ends when its shell exited and its tree is empty: the pipe's
EOF is waited for only briefly, because a process the command left running may hold the pipe for hours."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import functools
import itertools
import locale
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import netoff
from .config import Config
from .procs import TOKEN_ENV, ProcessTree, daemon_call

log = logging.getLogger(__name__)

_SECRET_RE = re.compile(r"(API_?KEY|_TOKEN$|^TOKEN$|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH_TOKEN)", re.IGNORECASE)
_KNOWN_SECRETS = {
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "OPENROUTER_API_KEY",
    "GITHUB_TOKEN", "GH_TOKEN", "HF_TOKEN", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
}
# DreamTeam's own settings that must not point agents' commands at the skills library or the skills' shared
# environments (docs/SPEC.md §19): dropped even when shell.pass_env names them. (DREAMTEAM_SKILLS, the folder of the
# swarm's skills, is set for the shell.) The sandbox's markers of the offline firewall (netoff.py) are no business of
# the commands either.
_INTERNAL_ENV = frozenset({"DREAMTEAM_SKILLS_DIR", "DREAMTEAM_HOST_SKILLS_DIR", "DREAMTEAM_SKILL_ENVS_DIR",
                           "DREAMTEAM_OFFLINE_USER", "DREAMTEAM_OFFLINE_FIREWALL"})

POWERSHELL_UTF8 = ("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
                   "$OutputEncoding = [System.Text.Encoding]::UTF8; ")
READ_CHUNK = 65536
PENDING_MAX = 65536      # characters of an unfinished line kept back (a \r progress line may still collapse)
SETTLE_POLL = 0.05       # seconds between two looks at a tree that should become empty
LEFTOVER_GRACE = 1.0     # after the shell exited: how long its tree may take to become empty
READ_SETTLE = 2.0        # after the tree is empty or killed: how long the last output may take to arrive
KILL_WAIT = 5.0          # a killed tree must be gone by then
KILL_SETTLE = 2.0        # RunningCommand.kill: how long the tree may take to vanish after the kill
LOG_LIMIT_LINE = "[log limit reached: further output is not saved]\n"
# Windows: whether a program that asks for CREATE_BREAKAWAY_FROM_JOB may leave its command's job. Off: the Cygwin
# runtime of Git Bash and MSYS2 asks for it for every Windows program it starts whenever the job allows it, also
# when cmd or PowerShell runs bash, sh or a git hook, which would take what they run out of the job (a program
# that insists on breaking away fails to start instead)
JOB_BREAKAWAY = False


def scrubbed_env(config: Config, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for agent processes: everything except secrets, unless whitelisted in shell.pass_env, and never
    DreamTeam's skills variables (_INTERNAL_ENV)."""
    allowed = set(config.shell.pass_env)
    secret_names = set(_KNOWN_SECRETS)
    for profile in config.models.values():
        if profile.api_key_env:
            secret_names.add(profile.api_key_env)
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if key.upper() in _INTERNAL_ENV:
            continue
        if key in allowed:
            env[key] = value
            continue
        if key.upper() in secret_names or _SECRET_RE.search(key):
            continue
        env[key] = value
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if extra:
        env.update(extra)
    return env


def resolve_shell(program: str = "auto") -> tuple[str, list[str]]:
    """Return (display name, argv prefix); the command string is appended as the last argument."""
    program = (program or "auto").lower()
    if program == "auto":
        if sys.platform == "win32":
            program = "pwsh" if shutil.which("pwsh") else "powershell"
        else:
            program = "bash" if shutil.which("bash") else "sh"
    if program in ("powershell", "pwsh"):
        exe = shutil.which(program) or program
        return program, [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command"]
    if program == "cmd":
        return "cmd", [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c"]
    if program in ("bash", "sh", "zsh"):
        exe = shutil.which(program) or program
        return program, [exe, "-c"]
    raise ValueError(f"Unsupported shell program: {program}")


def shell_args(name: str, prefix: list[str], command: str) -> list[str] | str:
    """Popen args running `command` through the shell `name` (name and prefix from resolve_shell).

    On Windows a list goes through subprocess.list2cmdline, which escapes every `"` as `\\"` (the C runtime's
    rules): cmd.exe does not parse its command line that way and would see `echo \\"a b\\"`. So cmd gets a
    string: with /s it strips exactly the outer pair of quotes and runs what is inside verbatim."""
    if name == "cmd" and sys.platform == "win32":
        return f'{subprocess.list2cmdline(prefix)} "{command}"'
    return prefix + [command]


def _fallback_encoding(program: str = "") -> str:
    """The encoding of output that is not UTF-8: for cmd.exe the console's OEM code page (cp866 on a Russian
    Windows: `echo привет` is not cp1251), else the locale's encoding."""
    encoding = locale.getpreferredencoding(False) or "utf-8"
    if program == "cmd" and sys.platform == "win32":
        with contextlib.suppress(Exception):
            import ctypes

            encoding = f"cp{ctypes.windll.kernel32.GetOEMCP()}"
    return encoding


def decode(data: bytes, program: str = "") -> str:
    """Command output as text: UTF-8, else the shell's fallback encoding (_fallback_encoding)."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode(_fallback_encoding(program), errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def truncate_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = int(limit * 0.4)
    tail = limit - head
    skipped = len(text) - head - tail
    return f"{text[:head]}\n... [{skipped} characters skipped] ...\n{text[-tail:]}"


def kill_tree(proc: subprocess.Popen) -> None:
    """Kill a command started in its own process group (Windows: its process tree) and the shell itself."""
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        else:
            # start_new_session: the group id is the shell's pid, also once the shell was reaped (getpgid fails then)
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


# --- output -------------------------------------------------------------------------------------------------

def _collapse(line: str) -> str:
    """A line as a terminal shows it: a bare \\r moves back to its start, so only the text after the last one stays
    (a progress bar keeps its final state)."""
    return line.rsplit("\r", 1)[-1]


class StreamDecoder:
    """A command's output bytes as text, line by line: only complete lines are returned; the unfinished one stays
    `pending` (it may still be collapsed by a \\r). "\\r\\n" becomes "\\n", also when split between two chunks.

    It decodes strict UTF-8 incrementally; at the first invalid byte it switches for good to the shell's fallback
    encoding (with errors replaced), re-decoding the bytes it held back together with the failing chunk."""

    def __init__(self, program: str = "") -> None:
        self.program = program
        self._decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self.fallback = False
        self.pending = ""

    def _decode(self, data: bytes, final: bool) -> str:
        if not self.fallback:
            try:
                return self._decoder.decode(data, final)
            except UnicodeDecodeError:
                held = self._decoder.getstate()[0]  # unchanged by the failed call
                data = held + data
                self.fallback = True
                try:
                    self._decoder = codecs.getincrementaldecoder(_fallback_encoding(self.program))(errors="replace")
                except LookupError:
                    self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        return self._decoder.decode(data, final)

    def feed(self, data: bytes, final: bool = False) -> str:
        """The complete lines `data` finishes (each ending with "\\n"), or "" while the line goes on. An unfinished
        line longer than PENDING_MAX is returned as it is, without a line end."""
        text = self._decode(data, final)
        if not text:
            return ""
        text = self.pending + text
        lines = text.split("\n")
        self.pending = lines.pop()
        out = "".join(_collapse(line.rstrip("\r")) + "\n" for line in lines)
        if len(self.pending) > PENDING_MAX:
            out += self.pending_view()
            self.pending = ""
        return out

    def pending_view(self) -> str:
        """The unfinished line as it would show now (a trailing \\r may be the first half of "\\r\\n")."""
        return _collapse(self.pending.rstrip("\r"))


def _utf8_prefix(data: bytes, size: int) -> bytes:
    """The longest prefix of `data` with at most `size` bytes that does not split a UTF-8 character."""
    if len(data) <= size:
        return data
    cut = max(size, 0)
    while cut > 0 and (data[cut] & 0xC0) == 0x80:
        cut -= 1
    return data[:cut]


class Capture:
    """The output of one command without keeping all of it: the head (the first 40 % of `max_output`), the tail
    and the total length, so that text() is exactly truncate_middle(whole output, max_output). Optionally the
    output also streams into a log file (attach_log), rotated once to `<name>.prev.log`.

    feed() runs in the reader thread, the rest in the loop thread: one lock guards the buffers and every log
    write, rotation and close."""

    def __init__(self, max_output: int = 12000, program: str = "") -> None:
        self._decoder = StreamDecoder(program)
        self._lock = threading.Lock()
        self.limit = max(1, int(max_output))
        self._head_cap = int(self.limit * 0.4)
        self._tail_keep = self.limit
        self._head = ""        # the first _head_cap characters of the complete lines
        self._tail = ""        # the last _tail_keep characters of the complete lines
        self.total = 0         # characters of complete lines so far
        self._shrunk = False
        self.detached = False
        self._log = None
        self._log_path: Path | None = None
        self._log_size = 0
        self._log_max = 0
        self._log_full = False
        self.last_output_at: float | None = None  # when bytes last arrived (a job silent for long is shown so)

    # buffers
    def feed(self, data: bytes, final: bool = False) -> None:
        with self._lock:
            if self.detached:
                return  # the reader goes on draining the pipe into nothing
            if data:
                self.last_output_at = time.time()
            text = self._decoder.feed(data, final)
            if text:
                self._keep(text)
                self._log_write(text)

    def _keep(self, text: str) -> None:
        if len(self._head) < self._head_cap:
            self._head += text[:self._head_cap - len(self._head)]
        self._tail = (self._tail + text)[-self._tail_keep:]
        self.total += len(text)

    def _emitted(self) -> str | None:
        """All complete lines, when the head and the tail still hold them all."""
        if self.total <= len(self._tail):
            return self._tail
        if self.total - len(self._tail) <= len(self._head):
            return self._head[:self.total - len(self._tail)] + self._tail
        return None

    def _view(self, with_pending: bool) -> str:
        pending = self._decoder.pending_view() if with_pending else ""
        total = self.total + len(pending)
        if self._shrunk:
            head_len, tail_len = self._head_cap, self._tail_keep
        else:
            head_len = int(self.limit * 0.4)
            tail_len = self.limit - head_len
        if total <= head_len + tail_len:
            full = self._emitted()
            if full is not None:
                return full + pending
        known_end = self._tail + pending
        head = self._head[:head_len] if self.total >= head_len else known_end[:head_len]
        tail = known_end[-tail_len:] if tail_len else ""
        return f"{head}\n... [{total - len(head) - len(tail)} characters skipped] ...\n{tail}"

    def text(self) -> str:
        """The output so far, the unfinished last line included, cut in the middle like truncate_middle."""
        with self._lock:
            return self._view(with_pending=True)

    def tail(self, lines: int = 40, chars: int = 4000) -> str:
        """The last `lines` lines of the output (the unfinished one included), at most `chars` characters."""
        with self._lock:
            text = self._tail + self._decoder.pending_view()
        if text.endswith("\n"):
            text = text[:-1]
        out = "\n".join(text.split("\n")[-max(1, lines):])
        return out[-chars:] if len(out) > chars else out

    def shrink(self, head_chars: int = 2000, tail_chars: int = 16000) -> None:
        """Keep less from now on (a command that became a background job): `head_chars` of the head (or what the
        head already had) and the last `tail_chars` characters."""
        with self._lock:
            if self.total <= self._head_cap:  # the head is still all the output: it may grow on
                self._head_cap = head_chars
            else:
                self._head_cap = min(self._head_cap, head_chars)
            self._head = self._head[:self._head_cap]
            self._tail_keep = tail_chars
            self._tail = self._tail[-tail_chars:]
            self._shrunk = True

    # log
    def attach_log(self, path: Path, header: str, max_bytes: int) -> None:
        """Stream the output into `path` from now on: first `header`, then the complete lines so far (only their
        head and tail when they were cut). The file rotates to `<stem>.prev.log` at half of `max_bytes`."""
        with self._lock:
            if self.detached or self._log is not None:
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            self._log = open(path, "wb")  # noqa: SIM115 - closed by detach_log / detach
            self._log_path = path
            self._log_max = max(int(max_bytes), 2 * len(LOG_LIMIT_LINE) + 64)
            self._log_size = 0
            self._log_full = False
            self._log_write(header)
            self._log_write(self._view(with_pending=False))

    def log_line(self, line: str) -> None:
        """A line of DreamTeam's own in the log ("# command ended ..."), between two complete output lines."""
        with self._lock:
            self._log_write(line if line.endswith("\n") else line + "\n")

    def detach_log(self, footer: str) -> None:
        """End the log: the unfinished last line, `footer`, close."""
        with self._lock:
            if self._log is None:
                return
            pending = self._decoder.pending_view()
            if pending:
                self._log_write(pending + "\n")
            self._log_write(footer if footer.endswith("\n") else footer + "\n")
            self._close_log()

    def detach(self) -> None:
        """Keep and log nothing more (the reader drains the pipe into nothing until EOF); closes the log."""
        with self._lock:
            self.detached = True
            self._close_log()

    def _close_log(self) -> None:
        log_file, self._log = self._log, None
        if log_file is not None:
            with contextlib.suppress(OSError):
                log_file.close()

    def _raw_write(self, data: bytes) -> None:
        try:
            self._log.write(data)
            self._log.flush()
            self._log_size += len(data)
        except (OSError, ValueError) as exc:
            log.warning("cannot write the job log %s: %s", self._log_path, exc)
            self._close_log()

    def _log_write(self, text: str) -> None:
        """Append `text`, rotating at half of the maximum. On Windows the rotation fails while anyone else has the
        file open (read_file, the UI, an agent's Get-Content): the file then grows on and the rotation is retried at
        the next chunk; at the full maximum the log stops with LOG_LIMIT_LINE."""
        if self._log is None or self._log_full or not text:
            return
        data = text.encode("utf-8", "replace")
        rotate_at = self._log_max // 2
        cap = self._log_max - len(LOG_LIMIT_LINE)
        failed = False
        while data and self._log is not None:
            if self._log_size >= rotate_at and not failed and not self._rotate():
                failed = True  # retried with the next chunk
            limit = rotate_at if self._log_size < rotate_at else cap
            piece = _utf8_prefix(data, limit - self._log_size)
            if not piece:
                if limit == rotate_at and not failed:
                    self._log_size = rotate_at  # not even one character fits: rotate first
                    continue
                self._raw_write(LOG_LIMIT_LINE.encode("utf-8"))
                self._log_full = True
                return
            self._raw_write(piece)
            data = data[len(piece):]

    def _rotate(self) -> bool:
        path = self._log_path
        prev = path.with_name(f"{path.stem}.prev{path.suffix}")
        with contextlib.suppress(OSError):
            self._log.close()
        try:
            os.replace(path, prev)
        except OSError:
            try:
                self._log = open(path, "ab")  # noqa: SIM115
                self._log_size = self._log.tell()
            except OSError as exc:
                log.warning("cannot reopen the job log %s: %s", path, exc)
                self._log = None
            return False
        try:
            self._log = open(path, "wb")  # noqa: SIM115
        except OSError as exc:
            log.warning("cannot reopen the job log %s: %s", path, exc)
            self._log = None
            return False
        self._log_size = 0
        self._raw_write(f"[earlier output: {prev.name}]\n".encode("utf-8"))
        return True


# --- a running command ---------------------------------------------------------------------------------------

_serials = itertools.count(1)
_last_serial = 0


def _next_serial() -> int:
    global _last_serial
    _last_serial = next(_serials)
    return _last_serial


def last_serial() -> int:
    """The serial of the last command started in this process (0: none yet): one with a higher serial started
    later (the workspace's held new files, docs/SPEC.md §7)."""
    return _last_serial


def _set_done(future: asyncio.Future) -> None:
    if not future.done():
        future.set_result(None)


class RunningCommand:
    """One started command: its tree, its captured output and two daemon threads, a reader that owns the pipe's
    read end (it alone ever closes it: a closed fd number is soon reused, and a reader still blocked on it would
    then consume someone else's file or socket) and a waiter for the shell. Every wait is asyncio.wait on a future
    only those threads resolve: nothing ever cancels them."""

    def __init__(self, name: str, tree: ProcessTree, capture: Capture) -> None:
        self._loop = asyncio.get_running_loop()
        self.name = name
        self.tree = tree
        self.capture = capture
        self.started_at = time.time()
        self.serial = _next_serial()
        self._exited: asyncio.Future = self._loop.create_future()
        self._eof: asyncio.Future = self._loop.create_future()
        self._stdout = tree.proc.stdout  # referenced here, so garbage collection cannot close it under the reader
        threading.Thread(target=self._read, name="dreamteam-shell-read", daemon=True).start()
        threading.Thread(target=self._wait, name="dreamteam-shell-wait", daemon=True).start()

    def _resolve(self, future: asyncio.Future) -> None:
        with contextlib.suppress(RuntimeError):  # the loop is closed: nobody waits any more
            self._loop.call_soon_threadsafe(_set_done, future)

    def _read(self) -> None:
        stream = self._stdout
        try:
            fd = stream.fileno()
            while True:
                try:
                    data = os.read(fd, READ_CHUNK)
                except OSError:
                    break
                if not data:
                    break
                try:
                    self.capture.feed(data)
                except Exception:  # noqa: BLE001 - the pipe must be drained whatever happens
                    log.exception("output capture failed")
            with contextlib.suppress(Exception):
                self.capture.feed(b"", final=True)
        finally:
            self.tree.mark_eof()
            with contextlib.suppress(Exception):
                stream.close()
            self._resolve(self._eof)

    def _wait(self) -> None:
        with contextlib.suppress(Exception):
            self.tree.proc.wait()
        self._resolve(self._exited)

    @property
    def exited(self) -> bool:
        return self._exited.done()

    @property
    def eof(self) -> bool:
        return self._eof.done()

    @property
    def returncode(self) -> int | None:
        code = self.tree.proc.returncode
        if code is None:
            with contextlib.suppress(Exception):
                code = self.tree.proc.poll()
        return code

    async def _wait_for(self, future: asyncio.Future, timeout: float | None) -> bool:
        if not future.done():
            await asyncio.wait({future}, timeout=None if timeout is None else max(0.0, timeout))
        return future.done()

    async def wait_exit(self, timeout: float | None) -> bool:
        """True once the shell exited (its processes may run on)."""
        return await self._wait_for(self._exited, timeout)

    async def wait_eof(self, timeout: float | None) -> bool:
        """True once the output pipe reached EOF (every holder of it is gone)."""
        return await self._wait_for(self._eof, timeout)

    async def settle(self, grace: float) -> bool:
        """Poll the tree for up to `grace` seconds: True as soon as it is empty (the shell included)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + grace
        while True:
            if not self.tree.alive():
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(SETTLE_POLL)

    async def wait_dead(self, timeout: float) -> bool:
        """After a kill: True once nothing of the tree runs any more."""
        return await self.settle(timeout)

    async def kill(self, term_grace: float = 0.0) -> bool:
        """End the tree: with `term_grace`, ask first (SIGTERM; Windows has no graceful stop) and give it that long,
        then kill. True once it is gone (at most term_grace + KILL_SETTLE seconds)."""
        if term_grace > 0:
            self.tree.terminate()
            if await self.settle(term_grace):
                return True
        self.tree.kill()
        return await self.settle(KILL_SETTLE)

    def output(self) -> str:
        return self.capture.text()

    def tail(self, lines: int = 40, chars: int = 4000) -> str:
        return self.capture.tail(lines, chars)

    def close(self) -> None:
        """Release the tree (nothing is signalled afterwards; a Windows job still holding processes kills them) and
        stop keeping the output. The reader keeps the pipe until EOF. Idempotent."""
        self.tree.release()
        self.capture.detach()


def _discard_tree(tree: ProcessTree) -> None:
    """A tree whose starter was cancelled while Popen ran: nobody will read or wait for it."""
    tree.kill()
    tree.release()
    with contextlib.suppress(Exception):
        tree.proc.stdout.close()  # no reader thread exists for it
    threading.Thread(target=tree.proc.wait, name="dreamteam-shell-reap", daemon=True).start()


async def start_command(command: str, *, cwd: Path, program: str = "auto", env: dict[str, str] | None = None,
                        max_output: int = 12000, token: str | None = None, offline: bool = False) -> RunningCommand:
    """Start `command` through the configured shell as a contained tree and return at once. `token` goes into the
    environment as DREAMTEAM_PROCESS_TOKEN (Linux finds escaped processes by it). `offline`: the shell runs in the
    session's isolation from the network (netoff.wrap; its environment is the caller's). ValueError: unsupported
    shell."""
    name, prefix = resolve_shell(program)
    if name in ("powershell", "pwsh"):
        command = POWERSHELL_UTF8 + command
    if token:
        env = {**(os.environ if env is None else env), TOKEN_ENV: token}
    args = shell_args(name, prefix, command)
    if offline:  # (the wrappers exec the shell: it keeps the pid, so its process group and tree stay the same)
        args = netoff.wrap(args)
    spawn = functools.partial(ProcessTree.spawn, args, cwd=cwd, env=env, token=token, breakaway=JOB_BREAKAWAY)
    tree = await daemon_call(spawn, on_orphan=_discard_tree)
    return RunningCommand(name, tree, Capture(max_output, program=name))


async def kill_after_cancel(cmd: RunningCommand, timeout: float = KILL_WAIT) -> None:
    """The caller was cancelled: kill at once and wait (at most `timeout` seconds) until the tree is gone, whatever
    further cancellations arrive, so that nothing writes into the workspace after its agent stopped. The caller
    re-raises its cancellation afterwards."""
    cmd.tree.kill()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            if await cmd.wait_dead(deadline - loop.time()):
                return
        except asyncio.CancelledError:
            cmd.tree.kill()


async def run_shell(command: str, *, cwd: Path, timeout: float, program: str = "auto",
                    env: dict[str, str] | None = None, max_output: int = 12000) -> tuple[int | None, str]:
    """Run `command` through the configured shell. Returns (exit code or None on timeout, output). Processes the
    command leaves running are killed once its shell exited (after LEFTOVER_GRACE). A cancelled caller's command
    is killed before the cancellation goes on."""
    cmd = await start_command(command, cwd=cwd, program=program, env=env, max_output=max_output,
                              token=secrets.token_hex(8))
    try:
        if await cmd.wait_exit(timeout):
            if not await cmd.settle(LEFTOVER_GRACE):
                await cmd.kill()
            await cmd.wait_eof(READ_SETTLE)
            return cmd.returncode, cmd.output()
        await cmd.kill()
        await cmd.wait_eof(READ_SETTLE)
        return None, cmd.output()
    except asyncio.CancelledError:
        await kill_after_cancel(cmd)
        raise
    finally:
        cmd.close()
