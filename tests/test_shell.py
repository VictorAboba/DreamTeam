import asyncio
import os
import random
import signal
import subprocess
import sys
import time
import types

import pytest

from dreamteam import shell as shell_module
from dreamteam.shell import (PENDING_MAX, Capture, StreamDecoder, kill_tree, resolve_shell, run_shell, scrubbed_env,
                             truncate_middle)

WINDOWS = sys.platform == "win32"
QUICK = "cmd" if WINDOWS else "sh"  # the fastest shell to start
CHILD = ("import os, pathlib, sys, time\n"
         "pathlib.Path(sys.argv[2]).write_text(str(os.getpid()))\n"
         "print('child up', flush=True)\n"
         "time.sleep(float(sys.argv[1]))\n")


async def test_echo_and_exit_code(fake_swarm):
    cwd = fake_swarm.config.workspace_dir
    code, out = await run_shell("echo привет", cwd=cwd, timeout=60)
    assert code == 0 and "привет" in out
    code, out = await run_shell("exit 3", cwd=cwd, timeout=60)
    assert code == 3


async def test_timeout_kills(fake_swarm):
    cwd = fake_swarm.config.workspace_dir
    cmd = "Start-Sleep -Seconds 30" if sys.platform == "win32" else "sleep 30"
    started = time.monotonic()
    code, _ = await run_shell(cmd, cwd=cwd, timeout=2)
    assert code is None
    assert time.monotonic() - started < 25


def test_scrubbed_env(fake_swarm, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("MY_SERVICE_TOKEN", "y")
    monkeypatch.setenv("HARMLESS", "z")
    env = scrubbed_env(fake_swarm.config, {"DREAMTEAM_AGENT": "A"})
    assert "ANTHROPIC_API_KEY" not in env and "MY_SERVICE_TOKEN" not in env
    assert env["HARMLESS"] == "z" and env["DREAMTEAM_AGENT"] == "A"
    fake_swarm.config.shell.pass_env = ["MY_SERVICE_TOKEN"]
    assert scrubbed_env(fake_swarm.config)["MY_SERVICE_TOKEN"] == "y"


def test_truncate_and_resolve():
    assert truncate_middle("a" * 100, 50).count("skipped") == 1
    name, argv = resolve_shell("auto")
    assert name and argv


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe")
async def test_cmd_output_is_decoded_with_the_oem_code_page(tmp_path):
    """cmd.exe writes in the console's OEM code page (cp866 on a Russian Windows), not the ANSI one (cp1251)."""
    code, out = await run_shell("echo привет", cwd=tmp_path, timeout=30, program="cmd")
    assert code == 0 and out.strip() == "привет"


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe")
async def test_cmd_keeps_double_quotes_verbatim(tmp_path):
    """subprocess.list2cmdline escapes `"` as `\\"`, which cmd.exe takes literally: the agents' console and the
    TUI `!command` must hand cmd the command line as is."""
    from dreamteam.tui.bash import ShellCommand

    spaced = tmp_path / "a folder"
    spaced.mkdir()
    (spaced / "Needle.txt").write_text("x", encoding="utf-8")
    (spaced / "other.txt").write_text("x", encoding="utf-8")

    async def agents(command):
        return await run_shell(command, cwd=tmp_path, timeout=30, program="cmd")

    async def tui(command):
        return await ShellCommand(command, cwd=tmp_path, program="cmd").run(timeout=30)

    for run in (agents, tui):
        code, out = await run('echo "a b"')
        assert code == 0 and out.strip() == '"a b"'
        code, out = await run(f'if exist "{tmp_path}" (echo yes) else (echo no)')
        assert code == 0 and out.strip() == "yes"
        code, out = await run('echo one & echo "two" && exit 4')
        assert code == 4 and out.split() == ["one", '"two"']
        code, out = await run('echo ""')
        assert code == 0 and out.strip() == '""'
        code, out = await run(f'dir "{spaced}" /b | findstr /i "needle"')
        assert code == 0 and out.strip() == "Needle.txt"


# --- the rebuilt run_shell (docs/SPEC.md §7) -------------------------------------------------------------------

def _alive(pid: int) -> bool:
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
            stat = f.read().decode("utf-8", "replace")
        return stat[stat.rfind(")") + 2:].split()[0] not in ("Z", "X")
    except OSError:
        return True


async def _gone(pid: int, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not _alive(pid):
            return True
        await asyncio.sleep(0.05)
    return not _alive(pid)


def _env(**extra: str) -> dict[str, str]:
    return {**os.environ, "PYEXE": sys.executable, **extra}


async def test_run_shell_returns_at_shell_exit_and_kills_leftovers(tmp_path):
    """A process the command leaves running holds the output pipe: run_shell still returns when the shell exits
    (it used to wait for the pipe until the timeout) and kills the leftover."""
    (tmp_path / "child.py").write_text(CHILD, encoding="utf-8")
    pidfile = tmp_path / "pid.txt"
    if WINDOWS:
        command = f'start /b "" "%PYEXE%" child.py 30 "{pidfile}" & ping -n 3 127.0.0.1 > nul & echo shell done'
    else:
        command = f"\"$PYEXE\" child.py 30 '{pidfile}' & sleep 2; echo shell done"
    started = time.monotonic()
    try:
        code, out = await run_shell(command, cwd=tmp_path, timeout=60, program=QUICK, env=_env())
        assert time.monotonic() - started < 10
        assert code == 0 and "shell done" in out
        assert await _gone(int(pidfile.read_text())), "the leftover was not killed"
    finally:
        if pidfile.exists() and _alive(int(pidfile.read_text())):  # cleanup of our own child only
            os.kill(int(pidfile.read_text()), signal.SIGTERM)


async def test_output_so_far_on_timeout(tmp_path):
    (tmp_path / "slow.py").write_text("import time\nprint('up')\ntime.sleep(20)\n", encoding="utf-8")
    command = '"%PYEXE%" slow.py' if WINDOWS else '"$PYEXE" slow.py'
    started = time.monotonic()
    code, out = await run_shell(command, cwd=tmp_path, timeout=1.5, program=QUICK, env=_env(PYTHONUNBUFFERED="1"))
    assert code is None and "up" in out
    assert time.monotonic() - started < 6


async def test_run_shell_uses_no_executor_thread(tmp_path, monkeypatch):
    """git runs on the loop's default executor, and asyncio.run waits for its threads at exit: no process work
    may go there."""
    def refuse(*args, **kwargs):
        raise AssertionError("the default executor was used")

    monkeypatch.setattr(asyncio, "to_thread", refuse)
    monkeypatch.setattr(asyncio.get_running_loop(), "run_in_executor", refuse)
    code, out = await run_shell("echo hi", cwd=tmp_path, timeout=30, program=QUICK)
    assert (code, out.strip()) == (0, "hi")


def test_stream_decoder(monkeypatch):
    def feed_all(decoder: StreamDecoder, chunks: list[bytes]) -> str:
        return "".join(decoder.feed(chunk) for chunk in chunks)

    data = "привет\n".encode("utf-8")
    decoder = StreamDecoder()
    assert feed_all(decoder, [bytes([b]) for b in data]) == "привет\n" and not decoder.fallback
    decoder = StreamDecoder()
    assert feed_all(decoder, [b"a\r", b"\nb\r", b"\n"]) == "a\nb\n"  # \r\n split between two chunks
    for chunks in ([b"a\r50%\r100%\nb"], [b"a\r50%", b"\r100%\nb"]):
        capture = Capture(1000)
        for chunk in chunks:
            capture.feed(chunk)
        assert capture.text() == "100%\nb"  # the final state of a \r line; the unfinished line shows too
    decoder = StreamDecoder()
    long_line = b"x" * (PENDING_MAX + 100)
    out = decoder.feed(long_line[:50000]) + decoder.feed(long_line[50000:])
    assert out == "x" * (PENDING_MAX + 100) and decoder.pending == ""  # emitted as it is past PENDING_MAX
    # not UTF-8: the shell's fallback encoding from the first invalid byte on; earlier text stays as it was
    monkeypatch.setattr(shell_module, "_fallback_encoding", lambda program="": "cp866")
    decoder = StreamDecoder("cmd")
    first = decoder.feed("ok ✓\n".encode("utf-8"))
    rest = decoder.feed("при".encode("cp866")) + decoder.feed("вет\n".encode("cp866"))
    assert first == "ok ✓\n" and rest == "привет\n" and decoder.fallback


def test_capture_log_gets_only_complete_lines(tmp_path):
    capture = Capture(1000)
    log = tmp_path / "j1.log"
    capture.feed(b"one\ntw")
    capture.attach_log(log, "# head\n", 2 ** 20)
    capture.feed(b"o\nthr")
    assert log.read_text(encoding="utf-8") == "# head\none\ntwo\n"
    assert capture.text() == "one\ntwo\nthr"
    capture.detach_log("# end")
    assert log.read_text(encoding="utf-8") == "# head\none\ntwo\nthr\n# end\n"


def test_capture_equals_truncate_middle():
    rng = random.Random(7)
    for cap in (50, 200, 1000):
        for size in (cap // 2, cap - 1, cap, cap + 1, cap * 5):
            words: list[str] = []
            while sum(len(w) for w in words) < size:
                words.append(rng.choice(["abc ", "привет ", "x" * rng.randint(1, 40), "\n", "line two\n"]))
            full = "".join(words)[:size]
            full = full[:-1] + "\n" if full else "\n"
            data = full.encode("utf-8")
            capture = Capture(cap)
            pos = 0
            while pos < len(data):
                step = rng.randint(1, 97)
                capture.feed(data[pos:pos + step])
                pos += step
            assert capture.text() == truncate_middle(full, cap), (cap, size)
            capture.feed("tail without end".encode("utf-8"))  # an unfinished last line belongs to it too
            assert capture.text() == truncate_middle(full + "tail without end", cap), (cap, size)


def test_capture_after_shrink_keeps_its_head_and_tail():
    capture = Capture(100)
    capture.feed(b"".join(f"{i:03d}\n".encode() for i in range(10)))
    capture.shrink(head_chars=8, tail_chars=12)
    capture.feed(b"".join(f"{i:03d}\n".encode() for i in range(10, 30)))
    text = capture.text()
    assert text.startswith("000\n001\n\n... [") and text.endswith("027\n028\n029\n")
    assert capture.tail(2, 100) == "028\n029"


def test_log_rotation_caps_the_size(tmp_path):
    log = tmp_path / "j1.log"
    capture = Capture(1000)
    capture.attach_log(log, "# job j1\n", 4096)
    for i in range(400):
        capture.feed(f"line {i:04d} {'y' * 30}\n".encode("utf-8"))
    capture.detach_log("# ended")
    prev = tmp_path / "j1.prev.log"
    assert prev.exists()
    assert log.stat().st_size + prev.stat().st_size <= 4096
    text = log.read_text(encoding="utf-8")
    assert text.startswith("[earlier output: j1.prev.log]") and "line 0399" in text and text.endswith("# ended\n")


@pytest.mark.skipif(not WINDOWS, reason="Windows sharing violations")
def test_log_rotation_retries_while_the_file_is_open(tmp_path):
    """Windows cannot rename a file someone else has open (read_file, the UI, Get-Content): the log grows on and
    the rotation is retried at every chunk; it stops at the hard cap only when the holder never lets go."""
    log = tmp_path / "j1.log"
    capture = Capture(1000)
    capture.attach_log(log, "# job j1\n", 4096)
    line = f"{'z' * 39}\n".encode("utf-8")
    with open(log, "rb"):
        for _ in range(60):  # 2400 bytes: past the rotation point (2048), below the hard cap
            capture.feed(line)
        assert not (tmp_path / "j1.prev.log").exists() and log.stat().st_size > 2048
    capture.feed(line)  # the holder is gone: rotated at the next chunk
    assert (tmp_path / "j1.prev.log").exists() and log.stat().st_size < 2048
    capture.detach_log("# ended")
    log2 = tmp_path / "j2.log"
    capture2 = Capture(1000)
    capture2.attach_log(log2, "# job j2\n", 4096)
    with open(log2, "rb"):
        for _ in range(200):
            capture2.feed(line)
        capture2.detach_log("# ended")
    text = log2.read_text(encoding="utf-8")
    assert log2.stat().st_size <= 4096 and text.endswith("[log limit reached: further output is not saved]\n")


def test_kill_tree_uses_the_process_group(monkeypatch):
    """POSIX: the group id of a command started in its own session is its pid, also once it was reaped (getpgid
    would fail then and the kill was lost)."""
    calls = []
    proc = types.SimpleNamespace(pid=4242, kill=lambda: calls.append("kill"))
    if WINDOWS:
        monkeypatch.setattr(subprocess, "run", lambda argv, **kw: calls.append(argv[:4]))
        kill_tree(proc)
        assert calls == [["taskkill", "/T", "/F", "/PID"], "kill"]
    else:
        monkeypatch.setattr(os, "killpg", lambda pgid, sig: calls.append((pgid, sig)))
        kill_tree(proc)
        assert calls == [(4242, signal.SIGKILL), "kill"]
