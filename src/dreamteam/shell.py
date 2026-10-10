"""Agent console: run shell commands in the workspace with a timeout and a scrubbed environment."""

from __future__ import annotations

import asyncio
import contextlib
import locale
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from .config import Config

_SECRET_RE = re.compile(r"(API_?KEY|_TOKEN$|^TOKEN$|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH_TOKEN)", re.IGNORECASE)
_KNOWN_SECRETS = {
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "OPENROUTER_API_KEY",
    "GITHUB_TOKEN", "GH_TOKEN", "HF_TOKEN", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
}


def scrubbed_env(config: Config, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for agent processes: everything except secrets, unless whitelisted in shell.pass_env."""
    allowed = set(config.shell.pass_env)
    secret_names = set(_KNOWN_SECRETS)
    for profile in config.models.values():
        if profile.api_key_env:
            secret_names.add(profile.api_key_env)
    env: dict[str, str] = {}
    for key, value in os.environ.items():
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


def decode(data: bytes, program: str = "") -> str:
    """Command output as text: UTF-8, else for cmd.exe the console's OEM code page (cp866 on a Russian Windows:
    `echo привет` is not cp1251), else the locale's encoding."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    encoding = locale.getpreferredencoding(False) or "utf-8"
    if program == "cmd" and sys.platform == "win32":
        with contextlib.suppress(Exception):
            import ctypes

            encoding = f"cp{ctypes.windll.kernel32.GetOEMCP()}"
    try:
        return data.decode(encoding, errors="replace")
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
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


def _run_blocking(argv: list[str], cwd: Path, timeout: float, env: dict[str, str] | None,
                  started: list[subprocess.Popen] | None = None) -> tuple[int | None, bytes]:
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs,
    )
    if started is not None:
        started.append(proc)
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out or b""
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        try:
            out, _ = proc.communicate(timeout=10)
        except Exception:
            out = b""
        return None, out or b""


async def run_shell(command: str, *, cwd: Path, timeout: float, program: str = "auto",
                    env: dict[str, str] | None = None, max_output: int = 12000) -> tuple[int | None, str]:
    """Run `command` through the configured shell. Returns (exit code or None on timeout, output)."""
    name, prefix = resolve_shell(program)
    if name in ("powershell", "pwsh"):
        command = ("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
                   "$OutputEncoding = [System.Text.Encoding]::UTF8; " + command)
    started: list[subprocess.Popen] = []
    work = asyncio.ensure_future(asyncio.to_thread(_run_blocking, prefix + [command], cwd, timeout, env, started))
    try:
        code, raw = await asyncio.shield(work)
    except asyncio.CancelledError:
        # the caller (an agent) was stopped: its command must not go on writing into the workspace meanwhile
        # (the session may be deleted or its workspace moved right after the stop)
        killed = False
        for _attempt in range(20):  # the process may start only now: kill it once it exists
            if started and not killed:
                await asyncio.to_thread(kill_tree, started[0])
                killed = True
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait({work}, timeout=1)
            if work.done():
                if not work.cancelled():
                    work.exception()  # retrieved: the caller is gone anyway
                break
        raise
    text = decode(raw, name).replace("\r\n", "\n")
    return code, truncate_middle(text, max_output)
