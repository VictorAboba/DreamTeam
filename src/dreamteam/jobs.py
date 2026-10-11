"""Background jobs of the agents' shell commands (docs/SPEC.md §7.1).

A `shell` call whose command still runs when its wait ends no longer kills it: the command becomes job `j<N>` and
runs on in the background (origin "timeout"). A caller may also ask for the background at once (`background`,
origin "background"), and processes a command leaves running after its shell exited are kept as a job too
(origin "detached"). The JobManager owns every running job: its row in the `jobs` table, its log
(`.scratch/<member>/jobs/j<N>.log`), its lifetime, its end (one finisher per job), the autocommit under its owner
and the cleanup at retire and session stop. Old agents (whose frozen tool set has no `jobs` tool) list and stop
jobs through a console intercept: the whole command line `jobs`, `jobs j3` or `kill j3`.

The owner's notices (n_job_*, in the owner's language) and the journaled job.started / job.ended events go
through the `_announce_*` hooks, which never raise."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import secrets
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import prompts
from .db import dumps, loads
from .models import USER, AgentKind, Status, ToolError
from .procs import alive_all, sweep_token
from .shell import (KILL_SETTLE, PENDING_MAX, SETTLE_POLL, RunningCommand, kill_after_cancel, start_command,
                    truncate_middle)
from .workspace import SCRATCH_DIR, format_size, scratch_rel

log = logging.getLogger(__name__)

LEFTOVER_GRACE = 1.0       # after the shell exited: how long its tree may take to become empty
READ_SETTLE = 2.0          # after the tree is empty or killed: how long the last output may take to arrive
BACKGROUND_GRACE = 2.0     # background=true: the call returns after this, with the first output
JOB_TICK = 1.0             # the monitor's period: ends, shell exits and lifetimes of all jobs
STOP_GRACE = 3.0           # a stop asks first (SIGTERM) and kills after this
KILL_WAIT = 5.0            # a killed foreground command must be gone by then
STOP_ALL_TERM = 3.0        # session stop: terminate everything, then kill what still runs after this
STOP_ALL_KILL = 2.0        # ... and wait this long for the killed rest
EXTERNAL_WAIT_MAX = 600    # an external member's call waits at most this (below hub_mcp.PROXY_READ_TIMEOUT)
NOTICE_TAIL_LINES = 15
NOTICE_TAIL_CHARS = 1200
JOB_HEAD_CHARS = 2000      # what a job keeps in memory: this head ...
JOB_TAIL_CHARS = 16000     # ... and this tail (the whole output is in its log)
END_TAIL_CHARS = 2000      # the output's end stored in the row
JOB_LOGS_KEPT = 20         # ended jobs' logs kept per owner
RECENT_SECONDS = 3600      # `jobs` lists the jobs that ended this recently
DETAIL_LINES = 40
DETAIL_LINES_MAX = 400
STOP_RESULT_LINES = 15
COMMAND_CHARS = 200        # the command in the public dict
JOB_LOG_MB_MAX = 64
DAEMON_ENV = {"MSBUILDDISABLENODEREUSE": "1"}  # MSBuild node reuse leaves daemons that would become jobs
# the thread pools of numeric libraries (OpenMP, MKL, OpenBLAS, numexpr, Accelerate, Rayon): each would take every
# core, so a few agents running numpy or a solver at once overload the machine ([shell] cpu_threads)
THREAD_ENV = ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS",
              "VECLIB_MAXIMUM_THREADS", "RAYON_NUM_THREADS")

__all__ = ["BACKGROUND_GRACE", "JobManager", "Job", "ShellOutcome", "PENDING_MAX", "SETTLE_POLL", "THREAD_ENV",
           "fmt_duration", "format_names", "parse_console", "parse_job_id", "short_command", "shown_log"]

# [shell] settings this module reads live (config.ShellSettings declares them; a test double may not)
_SETTING_DEFAULTS: dict[str, Any] = {"background_on_timeout": True, "foreground_max": 180, "max_jobs_per_member": 4,
                                     "max_jobs": 16, "job_max_minutes": 480, "job_log_mb": 2, "cpu_threads": "auto"}

ORIGIN_PHRASES = {"timeout": "went to the background at its timeout",
                  "background": "started in the background",
                  "detached": "left running by its command"}
RUNNING, ENDED, STOPPED, LOST = "running", "ended", "stopped", "lost"

STOPPING_ERROR = "The session is stopping: no new commands are started."
LEAVING_ERROR = "You are leaving the swarm: no new commands are started."
BACKGROUND_ERRORS = {
    "limit_member": "You already have {n} background jobs ({ids}), the most allowed: stop one with job_stop, or run "
                    "this command without background.",
    "limit_session": "The session already runs {n} background jobs, the most allowed: run this command without "
                     "background.",
    "disabled": "Background jobs are turned off in the settings ([shell] max_jobs_per_member or max_jobs is 0): run "
                "the command without background.",
}
# the user's wrap-up mode (Swarm.wrapping_up): no command of a member becomes a new job
WRAP_UP_ERROR = ("The user asked the team to wrap up, so no new background jobs are started. If the command is quick, "
                 "run it without background (it is stopped at its timeout); otherwise finish your task with what you "
                 "have (task_complete with the result so far and what is left).")
NOTES = {
    "limit_member": "It was not moved to the background: you already have {n} background jobs ({ids}), the most "
                    "allowed. Stop one you no longer need ({how}) or wait for one to end.",
    "limit_session": "It was not moved to the background: the session already runs {n} background jobs, the most "
                     "allowed.",
    "disabled": "Background jobs are turned off in the settings ([shell] max_jobs_per_member or max_jobs is 0).",
    "no_time": "{max} is the longest a command may run unless it was started in the background on purpose ({how}).",
    "wrap_up": "It was not moved to the background: the user asked the team to wrap up, so no new background jobs "
               "are started. Finish your task with the results you have and say what is left.",
    "wrap_up_left": "They were not kept as a background job: the user asked the team to wrap up, so no new "
                    "background jobs are started.",
}
# (new callers, old agents). PowerShell: Start-Process only with -NoNewWindow keeps the output in the job (without it
# the program writes into a console window of its own on the user's desktop, and the job's log stays empty)
NOTE_HOW = {
    "limit_member": ("job_stop", "run the shell command kill jN on its own"),
    "no_time": ("background=true", "end it with &, in PowerShell use Start-Process -NoNewWindow"),
}
# {wait}: the tool that sleeps until the end is announced: `wait` (AI agents), `wait_for_events` (external members)
FOOTERS_NEW = {
    "background": "[Job j{id} keeps running in the background; its output goes on into {log}. You will be notified "
                  "when it ends, so don't poll it: do other work, or call {wait} if nothing else is left. jobs with "
                  "job_id \"j{id}\" shows its latest output; job_stop stops it. It is stopped if it still runs {max} "
                  "after it started; start servers and long runs with background=true instead, then they may run "
                  "up to {job_max}.]",
    "started": "[Job j{id} runs in the background; its output goes into {log}. You will be notified when it ends, so "
               "don't poll it: do other work, or call {wait} if nothing else is left. jobs with job_id \"j{id}\" "
               "shows its latest output; job_stop stops it. It may run up to {job_max} and stops when the session "
               "stops.]",
    "detached": "[Still running: {names}. They are kept as job j{id}: their output goes on into {log}, they stop "
                "when the session stops or after {job_max}, and you are told when they have all ended (only at your "
                "next wake-up unless you hold a task in progress). jobs with job_id \"j{id}\" shows their latest "
                "output; job_stop stops them.]",
}
FOOTERS_OLD = {
    "background": "[Job j{id} keeps running in the background; its output goes on into {log}. You will be notified "
                  "when it ends, so don't poll it: do other work, or call {wait} if nothing else is left. To see its "
                  "latest output, run the shell command jobs j{id}; to stop it, run the shell command kill j{id} "
                  "(each as a command of its own); the command jobs lists all jobs. It is stopped if it still runs "
                  "{max} after it started; start servers yourself in the background instead (end the command with "
                  "&, in PowerShell use Start-Process -NoNewWindow), then they may run up to {job_max}.]",
    "detached": "[Still running: {names}. They are kept as job j{id}: their output goes on into {log}, they stop "
                "when the session stops or after {job_max}, and you are told when they have all ended (only at your "
                "next wake-up unless you hold a task in progress). To see their latest output, run the shell "
                "command jobs j{id}; to stop them, run the shell command kill j{id} (each as a command of its "
                "own).]",
}
CONSOLE_MARK = " [dreamteam]"


# --- small helpers ---------------------------------------------------------------------------------------------

def parse_job_id(text: Any) -> int | None:
    """A job id as people write it: "j3", "J3", "%3", "#3" or "3" -> 3; None for anything else."""
    if isinstance(text, bool):
        return None
    if isinstance(text, int):
        return text if text > 0 else None
    m = re.fullmatch(r"\s*[jJ%#]?(\d{1,18})\s*", str(text or ""))
    if not m:
        return None
    value = int(m.group(1))
    return value if value > 0 else None


_CONSOLE_LIST = re.compile(r"jobs(?:\s+-[lp])?")
_CONSOLE_SHOW = re.compile(r"jobs(?:\s+-l)?\s+[j%]?(\d{1,18})")
_KILL_FORCE = {"-9", "-KILL", "-SIGKILL", "-s KILL", "-s SIGKILL"}
_CONSOLE_KILL = re.compile(r"kill(?:\s+(-9|-KILL|-SIGKILL|-s\s+KILL|-s\s+SIGKILL|-15|-TERM|-SIGTERM|-s\s+TERM))?"
                           r"((?:\s+[j%]\d{1,18})+)")


def parse_console(command: str) -> tuple[str, list[int], bool] | None:
    """The console intercept (old agents' way to list and stop jobs from `shell`): ("list" | "show" | "kill", ids,
    force) when the WHOLE line (stripped, one trailing ";" dropped) is `jobs`, `jobs j3` or `kill [-9|-TERM|…] j3
    [%4 …]`, else None and the command goes to the real shell. Case-sensitive; `kill` never takes a bare number
    (that is a PID)."""
    line = (command or "").strip()
    if line.endswith(";"):
        line = line[:-1].strip()
    if _CONSOLE_LIST.fullmatch(line):
        return "list", [], False
    m = _CONSOLE_SHOW.fullmatch(line)
    if m:
        return "show", [int(m.group(1))], False
    m = _CONSOLE_KILL.fullmatch(line)
    if m:
        signal_name = " ".join((m.group(1) or "").split())
        ids = [int(token[1:]) for token in m.group(2).split()]
        if all(i > 0 for i in ids):
            return "kill", ids, signal_name in _KILL_FORCE
    return None


def short_command(cmd: str, limit: int) -> str:
    """`cmd` on one line (whitespace collapsed), cut in the middle with "…" beyond `limit` characters."""
    text = " ".join((cmd or "").split())
    if len(text) <= limit:
        return text
    head = max(1, (limit - 1) // 2)
    tail = max(0, limit - 1 - head)
    return text[:head] + "…" + (text[-tail:] if tail else "")


_UNITS = {"en": ("s", "min", "h"), "ru": ("с", "мин", "ч")}


def fmt_duration(seconds: float, lang: str = "en") -> str:
    """en "45 s", "12 min", "2 h 05 min"; ru "45 с", "12 мин", "2 ч 05 мин"."""
    s_unit, m_unit, h_unit = _UNITS.get(lang, _UNITS["en"])
    total = max(0, int(seconds))
    if total < 60:
        return f"{total} {s_unit}"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes} {m_unit}"
    return f"{minutes // 60} {h_unit} {minutes % 60:02d} {m_unit}"


def format_names(names: list[str]) -> str:
    """"node.exe ×2, python.exe" (first-seen order), or "processes" when nothing is known."""
    counts = Counter(names)
    parts = [f"{name} ×{counts[name]}" if counts[name] > 1 else name for name in dict.fromkeys(names)]
    return ", ".join(parts) or "processes"


def shown_log(log_value: str | None) -> str:
    """A job's log path for messages: workspace-relative as stored, or for the data_dir fallback (an absolute
    path) that path with a hint, since read_file cannot reach it."""
    if log_value and Path(log_value).is_absolute():
        return f"{log_value} (outside the workspace: read it with shell)"
    return log_value or ""


def _clock(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _secs(value: float) -> str:
    return f"{value:g}"


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except PermissionError:
        return True
    except (OSError, ValueError, OverflowError):
        return False
    return True


# --- records -----------------------------------------------------------------------------------------------------

@dataclass
class Job:
    """A running job in memory (its row in `jobs` holds the same facts for the UI and for after the end)."""
    id: int
    owner: str
    command: str
    origin: str
    cmd: RunningCommand
    log: str               # workspace-relative posix path, or absolute (data_dir fallback)
    log_path: Path
    started_at: float
    background_at: float
    stopping: str | None = None          # the reason of a stop under way: the monitor leaves the job alone
    finishing: bool = False              # D23: claimed synchronously by the one finisher
    done: asyncio.Future | None = None   # resolved by the finisher; every other ender awaits it
    command_done: bool = False           # the shell ended by itself while processes it started run on
    exit_code: int | None = None         # the shell's code once it ended by itself; at the end: the job's own code
                                         # (None when stopped, or when its shell had ended before it)
    command_ended_at: float | None = None
    shell: str = ""
    processes: list[str] = field(default_factory=list)
    status: str = RUNNING
    reason: str | None = None
    stopped_by: str | None = None
    ended_at: float | None = None
    tail: str = ""
    exited_seen_at: float | None = None  # the monitor first saw the shell's exit

    @property
    def label(self) -> str:
        return f"j{self.id}"

    @property
    def log_shown(self) -> str:
        """The log for messages ({log}): workspace-relative, or the data_dir fallback with a hint."""
        return shown_log(self.log)

    def notice_tail(self) -> str:
        """The end of the output for a notice ({tail}): the last NOTICE_TAIL_LINES lines, at most NOTICE_TAIL_CHARS
        characters, each indented by two spaces; "" when there was none (the notice then says job_no_output)."""
        text = self.cmd.tail(NOTICE_TAIL_LINES, NOTICE_TAIL_CHARS).strip("\n")
        return "\n".join("  " + line for line in text.split("\n")) if text.strip() else ""

    def public(self, now: float | None = None) -> dict:
        """The job for the snapshot, events and the UI (docs/SPEC.md §14.1). `processes` is read live while the job
        runs; `now` is accepted for symmetry with rows (a running job has no end yet)."""
        processes = self.processes
        if self.status == RUNNING and not self.finishing:
            with contextlib.suppress(Exception):
                processes = self.cmd.tree.processes() or processes
        return {"id": self.id, "label": self.label, "owner": self.owner, "command": self.command[:COMMAND_CHARS],
                "origin": self.origin, "status": self.status, "reason": self.reason, "stopped_by": self.stopped_by,
                "exit_code": self.exit_code, "started_at": self.started_at, "background_at": self.background_at,
                "ended_at": self.ended_at, "log": self.log, "processes": list(processes)}


def _row_public(row: Any) -> dict:
    return {"id": row["id"], "label": f"j{row['id']}", "owner": row["owner"],
            "command": (row["command"] or "")[:COMMAND_CHARS], "origin": row["origin"], "status": row["status"],
            "reason": row["reason"], "stopped_by": row["stopped_by"], "exit_code": row["exit_code"],
            "started_at": row["started_at"], "background_at": row["background_at"], "ended_at": row["ended_at"],
            "log": row["log"], "processes": loads(row["processes"], []) or []}


@dataclass
class _Foreground:
    """A `shell` call whose command runs in the foreground: stop_all and stop_owner kill it and await `done`."""
    actor: str
    cmd: RunningCommand
    done: asyncio.Future
    killed: str | None = None  # the reason when stop_all / stop_owner killed it


@dataclass
class ShellOutcome:
    """What a `shell` call came to; render() builds the tool result (English, like every tool result)."""
    state: str          # exited | quick | background | started | detached | leftovers_killed | timeout | killed
    code: int | None
    output: str
    shell: str
    wait: float
    job: Job | None = None
    note: str = ""      # limit_member | limit_session | disabled | no_time | wrap_up | wrap_up_left; for killed:
                        # session | retired
    names: list[str] = field(default_factory=list)
    note_args: dict = field(default_factory=dict)
    max_output: int = 12000

    def _note_text(self, has_jobs_tool: bool, cfg: Any) -> str:
        if self.note not in NOTES:
            return ""
        how = NOTE_HOW.get(self.note, ("", ""))[0 if has_jobs_tool else 1]
        return NOTES[self.note].format(how=how, max=fmt_duration(cfg.max_timeout, "en"), **self.note_args)

    def render(self, *, has_jobs_tool: bool, cfg: Any, has_wait: bool = True) -> str:
        """The result text (docs/SPEC.md §7.1). The head line and the footer are never cut: when the whole text
        would pass max_output, only the output in between is cut in the middle. The footer of a job names the tools
        the caller has: `jobs`/`job_stop` or the console commands (`has_jobs_tool`), `wait` or, for an external
        member, `wait_for_events` (`has_wait`)."""
        shell, output = self.shell, self.output
        job_id = self.job.id if self.job else 0
        no_output, footer = "(no output)", ""
        if self.state == "exited":
            head = f"exit code: {self.code} [{shell}]"
        elif self.state == "quick":
            head = (f"exit code: {self.code} [{shell}] (it ended within {_secs(BACKGROUND_GRACE)} s, so it did not "
                    "stay in the background)")
        elif self.state in ("background", "started", "detached"):
            if self.state == "background":
                head = (f"STILL RUNNING after {_secs(self.wait)}s: continues in the background as job j{job_id} "
                        f"[{shell}]")
                no_output = "(no output yet)"
            elif self.state == "started":
                head = f"STARTED in the background as job j{job_id} [{shell}]"
                no_output = "(no output yet)"
            else:
                head = (f"exit code: {self.code} [{shell}]; processes it started keep running as background job "
                        f"j{job_id}")
            footers = FOOTERS_NEW if has_jobs_tool else FOOTERS_OLD
            template = footers.get(self.state, FOOTERS_NEW[self.state])
            minutes = int(getattr(cfg, "job_max_minutes", _SETTING_DEFAULTS["job_max_minutes"]) or 0)
            footer = template.format(id=job_id, log=self.job.log_shown if self.job else "",
                                     max=fmt_duration(cfg.max_timeout, "en"),
                                     job_max=fmt_duration(minutes * 60, "en") if minutes > 0
                                     else "the end of the session",
                                     names=format_names(self.names), wait="wait" if has_wait else "wait_for_events")
        elif self.state == "leftovers_killed":
            head = f"exit code: {self.code} [{shell}]; the processes it left running were stopped"
            note = self._note_text(has_jobs_tool, cfg)
            footer = f"[{note}]" if note else ""
        elif self.state == "timeout":
            head = f"TIMEOUT after {_secs(self.wait)}s (process tree killed) [{shell}]"
            note = self._note_text(has_jobs_tool, cfg)
            footer = f"[{note}]" if note else ""
        else:  # killed
            why = "you are leaving the swarm" if self.note == "retired" else "the session is stopping"
            head = f"KILLED: {why} [{shell}]"
        body = output if output.strip() else no_output
        room = self.max_output - len(head) - (len(footer) + 2 if footer else 0) - 1
        if len(body) > room:  # truncate_middle adds its "[N characters skipped]" line beyond the limit it gets
            body = truncate_middle(body, max(room - len(f"\n... [{len(body)} characters skipped] ...\n"), 0))
        return f"{head}\n{body}" + (f"\n\n{footer}" if footer else "")


# --- the manager ---------------------------------------------------------------------------------------------------

class JobManager:
    """Every job of one swarm, and the foreground `shell` commands that may still become one."""

    def __init__(self, swarm: Any) -> None:
        self.swarm = swarm
        self.closing = False                       # stop_all ran or runs: no new command, no new job
        self._jobs: dict[int, Job] = {}
        self._foreground: dict[int, _Foreground] = {}
        self._forbidden: set[str] = set()          # members being retired (D24)
        self._finishes: set[asyncio.Task] = set()  # finishers in flight (never cancelled)
        self._helpers: set[asyncio.Task] = set()   # stops, shell-exit reports: awaited by stop_all
        self._monitor: asyncio.Task | None = None
        self._stopped_all = False
        # jobs that ended by themselves while the session stops (no commit of their own then): owner -> [(id,
        # command, how it ended)], committed by stop_all
        self._uncommitted: dict[str, list[tuple[int, str, str]]] = {}
        # owner -> its jobs that kept it busy and have ended, until their end notice is out (_finish_job)
        self._ending: dict[str, int] = {}

    # settings, read live
    def _setting(self, name: str) -> Any:
        return getattr(self.swarm.config.shell, name, _SETTING_DEFAULTS[name])

    def _log_max_bytes(self) -> int:
        mb = min(max(int(self._setting("job_log_mb") or 1), 1), JOB_LOG_MB_MAX)
        return mb * 2 ** 20

    def _stopping(self) -> bool:
        return self.closing or bool(getattr(self.swarm, "stopping", False))

    def _may_adopt(self, owner: str) -> bool:
        """Not the limits: the session goes on and the owner stays (D24)."""
        return not self._stopping() and owner not in self._forbidden and self.swarm.is_member(owner)

    # --- a shell call -------------------------------------------------------------------------------------------
    async def run(self, actor: str, command: str, *, timeout: int, env: dict[str, str], max_output: int,
                  background: bool = False) -> ShellOutcome:
        """Run one `shell` call (docs/SPEC.md §7.1): wait for the command up to `timeout` seconds (2 s with
        `background`; an AI agent at most [shell] foreground_max while its command could become a job, an external
        member at most EXTERNAL_WAIT_MAX), then return its result, or keep it as a job and return its output so far.
        ToolError when no command may start (the session stops, the actor leaves) or `background` is not allowed
        now. In the user's wrap-up mode no command becomes a new job: `background` is refused, a command still
        running at its timeout is killed then (as without background_on_timeout), and processes it leaves running
        are killed at its end."""
        if self._stopping():
            raise ToolError(STOPPING_ERROR)
        if actor in self._forbidden:
            raise ToolError(LEAVING_ERROR)
        cfg = self.swarm.config.shell
        if background and self.wrapping_up(actor):
            raise ToolError(WRAP_UP_ERROR)
        if background:
            note = self.check_start(actor)
            if note is not None:
                raise ToolError(BACKGROUND_ERRORS[note].format(**self._note_args(actor, note)))
        requested = BACKGROUND_GRACE if background else float(timeout)
        wait = self._wait_for(actor, requested)
        env = self.command_env(env)
        cmd = await start_command(command, cwd=self.swarm.workspace.root, program=cfg.program, env=env,
                                  max_output=max_output, token=secrets.token_hex(8))
        fg = _Foreground(actor, cmd, asyncio.get_running_loop().create_future())
        self._foreground[cmd.serial] = fg
        adopted = False

        def outcome(state: str, **kw: Any) -> ShellOutcome:
            return ShellOutcome(state, kw.pop("code", None), kw.pop("output", "") or "", cmd.name, wait,
                                max_output=max_output, **kw)

        try:
            if not self._may_start(actor):  # stop_all or forbid() came while the command started
                fg.killed = self._refusal(actor)
                cmd.tree.kill()
            exited = await cmd.wait_exit(wait)
            # foreground_max only moves an AI agent's command to the background sooner: one that cannot become a job
            # now (the limits, jobs turned off, wrap-up mode switched on during the call) gets the rest of its own
            # timeout, as before the cap, and goes on as a job as soon as a slot frees. (An external member's cap
            # stays: it protects the MCP transport.)
            while (not exited and fg.killed is None and wait < requested and self._may_adopt(actor)
                   and self.swarm.kind_of(actor) == AgentKind.LLM
                   and (self.check_start(actor) is not None or (not background and self.wrapping_up(actor)))):
                step = min(JOB_TICK, requested - wait)
                exited = await cmd.wait_exit(step)
                wait += step
            if fg.killed is None and exited:
                empty = await cmd.settle(LEFTOVER_GRACE)
                if fg.killed is None and empty:
                    await cmd.wait_eof(READ_SETTLE)
                    return outcome("quick" if background else "exited", code=cmd.returncode, output=cmd.output())
                if fg.killed is None and not self._may_adopt(actor):
                    fg.killed = self._refusal(actor)
                if fg.killed is None:  # processes the command started run on after its shell exited
                    code = cmd.returncode
                    # wrapping up: what a command leaves running dies with it (a background=true call was accepted
                    # before wrap-up began: its program is the job it asked for)
                    note = "wrap_up_left" if not background and self.wrapping_up(actor) else self.check_start(actor)
                    if note is None:
                        output = cmd.output()
                        job = self._adopt(actor, command, cmd, "background" if background else "detached",
                                          exit_code=code, command_done=True)
                        adopted = True
                        return outcome("started" if background else "detached", code=code, output=output, job=job,
                                       names=list(job.processes))
                    names = cmd.tree.processes()
                    await cmd.kill()
                    await cmd.wait_eof(READ_SETTLE)
                    return outcome("leftovers_killed", code=code, output=cmd.output(), note=note, names=names,
                                   note_args=self._note_args(actor, note))
            elif fg.killed is None and not self._may_adopt(actor):
                fg.killed = self._refusal(actor)
            if fg.killed is not None:
                if not await cmd.wait_dead(0):
                    await cmd.kill()
                await cmd.wait_eof(READ_SETTLE)
                return outcome("killed", output=cmd.output(), note=fg.killed)
            # the shell still runs when the wait ended (first match wins)
            if background:
                note = self.check_start(actor)
            elif not self._setting("background_on_timeout"):
                note = ""  # exactly the old result: killed at its timeout
            elif self.wrapping_up(actor):
                note = "wrap_up"  # killed at its timeout, as without background_on_timeout, saying why
            elif wait >= cfg.max_timeout:
                note = "no_time"  # a timeout job would have no lifetime left
            else:
                note = self.check_start(actor)
            if note is None:
                output = cmd.output()
                job = self._adopt(actor, command, cmd, "background" if background else "timeout")
                adopted = True
                return outcome("started" if background else "background", output=output, job=job)
            await cmd.kill()
            await cmd.wait_eof(READ_SETTLE)
            return outcome("timeout", output=cmd.output(), note=note, note_args=self._note_args(actor, note))
        except asyncio.CancelledError:
            if not adopted:  # an agent was stopped or retired: its command must not go on writing meanwhile
                await kill_after_cancel(cmd, KILL_WAIT)
            raise
        except Exception:
            if not adopted:  # the call failed (e.g. the database): the command must not run on unwatched
                cmd.tree.kill()
            raise
        finally:
            self._foreground.pop(cmd.serial, None)
            if not adopted:
                cmd.close()
            if not fg.done.done():
                fg.done.set_result(None)

    def _wait_for(self, actor: str, wait: float) -> float:
        """How long a call of `actor` waits for its command before the command goes on as a job. With
        background_on_timeout an AI agent waits at most [shell] foreground_max: blocked in a long command it would
        see no message, comment or review until the command ends (run() waits on when the command cannot become a
        job). An external member waits at most EXTERNAL_WAIT_MAX: the MCP proxy must never cut the call. Without
        background_on_timeout the timeout is the wait, as before (the command is killed then)."""
        if not self._setting("background_on_timeout"):
            return wait
        kind = self.swarm.kind_of(actor)
        if kind == AgentKind.LLM:
            # also in wrap-up mode: run() extends the wait tick by tick while the mode lasts, so switching it off
            # during the call moves the command to the background at once instead of blocking to its timeout
            return min(wait, float(max(1, int(self._setting("foreground_max") or 1))))
        if kind == AgentKind.EXTERNAL:
            return min(wait, EXTERNAL_WAIT_MAX)
        return wait

    def command_env(self, env: dict[str, str]) -> dict[str, str]:
        """The environment of a command: `env` plus live output (PYTHONUNBUFFERED), no MSBuild node reuse, and the
        thread caps of numeric libraries (cpu_threads()). The caps are defaults: a value inherited from the hub's
        environment stays, and one the command sets itself wins anyway (the shell assigns it later)."""
        env = {**env, "PYTHONUNBUFFERED": "1"}  # "output so far" is live
        for key, value in DAEMON_ENV.items():
            env.setdefault(key, value)
        threads = self.cpu_threads()
        if threads:
            for key in THREAD_ENV:
                env.setdefault(key, str(threads))
        return env

    def cpu_threads(self) -> int:
        """The thread cap of a command's numeric libraries, from [shell] cpu_threads: "auto" shares the cores
        among the active AI agents (at least 1 each), N is N, 0 sets no cap."""
        value = self._setting("cpu_threads")
        if isinstance(value, str):
            text = value.strip().lower()
            value = int(text) if text.isdigit() else None  # "auto" (anything else is treated as "auto")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return max(1, (os.cpu_count() or 1) // max(1, self._agent_count()))
        return max(0, int(value))

    def _agent_count(self) -> int:
        count = getattr(self.swarm, "agent_count", None)
        if callable(count):
            return int(count())
        return sum(1 for m in self.swarm.members() if m["kind"] == AgentKind.LLM)

    def wrapping_up(self, actor: str) -> bool:
        """The user's wrap-up mode (Swarm.wrapping_up) holds for `actor`'s commands: every member's, AI agent or
        external; never the user's own."""
        return actor != USER and bool(getattr(self.swarm, "wrapping_up", False))

    def _may_start(self, actor: str) -> bool:
        return not self._stopping() and actor not in self._forbidden

    def _refusal(self, actor: str) -> str:
        """Why a foreground command is killed instead of kept: the member leaves ("retired") or the session stops."""
        return "retired" if actor in self._forbidden or not self.swarm.is_member(actor) else "session"

    def _note_args(self, actor: str, note: str | None) -> dict:
        if note == "limit_member":
            mine = self.running_of(actor)
            return {"n": len(mine), "ids": ", ".join(j.label for j in mine)}
        if note == "limit_session":
            return {"n": len(self._jobs), "ids": ""}
        return {}

    def check_start(self, actor: str) -> str | None:
        """Why a new job of `actor` is not allowed now ("disabled", "limit_member", "limit_session"), or None."""
        per = int(self._setting("max_jobs_per_member") or 0)
        total = int(self._setting("max_jobs") or 0)
        if per <= 0 or total <= 0:
            return "disabled"
        if len(self.running_of(actor)) >= per:
            return "limit_member"
        if len(self._jobs) >= total:
            return "limit_session"
        return None

    # --- adoption -----------------------------------------------------------------------------------------------
    def _scratch_is_local(self, root: Path) -> bool:
        folder = root / SCRATCH_DIR
        try:
            if not folder.is_dir() or folder.is_symlink():
                return False
            return os.path.normcase(str(folder.resolve())) == os.path.normcase(str(root.resolve() / SCRATCH_DIR))
        except OSError:
            return False

    def _log_location(self, owner: str, job_id: int) -> tuple[str, Path]:
        """(the `log` column, the file) of job `job_id`: `.scratch/<member>/jobs/j<N>.log`, or
        `<data_dir>/jobs/j<N>.log` when `.scratch` is not a plain folder of the workspace (a link, a file)."""
        name = f"j{job_id}.log"
        workspace = self.swarm.workspace
        root = Path(workspace.root)
        with contextlib.suppress(Exception):
            workspace.ensure_scratch(owner)
        if self._scratch_is_local(root):
            folder = root / scratch_rel(owner) / "jobs"
            try:
                folder.mkdir(parents=True, exist_ok=True)
                return f"{scratch_rel(owner)}jobs/{name}", folder / name
            except OSError as exc:
                log.warning("cannot create %s: %s", folder, exc)
        folder = Path(self.swarm.config.data_dir) / "jobs"
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # the job runs on without a log (attach_log fails and warns)
            log.warning("cannot create %s: %s", folder, exc)
        return str(folder / name), folder / name

    def _log_file(self, log_value: str | None) -> Path | None:
        if not log_value:
            return None
        path = Path(log_value)
        return path if path.is_absolute() else Path(self.swarm.workspace.root) / log_value

    def _adopt(self, owner: str, command: str, cmd: RunningCommand, origin: str, *, exit_code: int | None = None,
               command_done: bool = False) -> Job:
        """Keep `cmd` as a job of `origin` (synchronous: the limit check before it and this INSERT are atomic).
        `command_done`: its shell already ended by itself with `exit_code` (the call's result reported it), and
        only processes it started run on."""
        now = time.time()
        names: list[str] = []
        with contextlib.suppress(Exception):
            names = cmd.tree.processes()
        tree = cmd.tree
        code = exit_code if command_done else None
        job_id = self.swarm.db.insert(
            "INSERT INTO jobs(owner, command, shell, origin, status, exit_code, pid, pgid, token, hub_pid, processes,"
            " started_at, background_at) VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?)",
            (owner, command, cmd.name, origin, code, tree.pid, tree.pgid, tree.token, os.getpid(), dumps(names),
             cmd.started_at, now))
        log_value, log_path = self._log_location(owner, job_id)
        self.swarm.db.execute("UPDATE jobs SET log = ? WHERE id = ?", (log_value, job_id))
        job = Job(id=job_id, owner=owner, command=command, origin=origin, cmd=cmd, log=log_value, log_path=log_path,
                  started_at=cmd.started_at, background_at=now, done=asyncio.get_running_loop().create_future(),
                  command_done=command_done, exit_code=code, command_ended_at=now if command_done else None,
                  shell=cmd.name, processes=names)
        header = (f"# job j{job_id} of {owner} [{cmd.name}]: {short_command(command, 2000)}\n"
                  f"# started {_clock(cmd.started_at)}, in the background since {_clock(now)} "
                  f"({ORIGIN_PHRASES[origin]})\n")
        try:
            cmd.capture.attach_log(log_path, header, self._log_max_bytes())
        except OSError as exc:
            log.warning("cannot open the job log %s: %s", log_path, exc)
        cmd.capture.shrink(JOB_HEAD_CHARS, JOB_TAIL_CHARS)
        self._jobs[job_id] = job
        self._ensure_monitor()
        try:
            self._prune_logs(owner)
            self._announce_started(job)
        except Exception:  # noqa: BLE001 - the job is kept: the cleanup and the announcement are best effort
            log.exception("after adopting job j%s", job_id)
        return job

    def _prune_logs(self, owner: str) -> None:
        """Delete the logs of the owner's ended jobs beyond JOB_LOGS_KEPT. Chosen by rows, never by listing the
        folder: two names may share a scratch folder ("Анна Смирнова" and "Anna Smirnova")."""
        rows = self.swarm.db.all("SELECT log FROM jobs WHERE owner = ? AND status != 'running' AND log IS NOT NULL"
                                 " ORDER BY id DESC LIMIT -1 OFFSET ?", (owner, JOB_LOGS_KEPT))
        for row in rows:
            path = self._log_file(row["log"])
            if path is None:
                continue
            for file in (path, path.with_name(f"{path.stem}.prev{path.suffix}")):
                with contextlib.suppress(OSError):  # gone already, or held open by a reader (Windows)
                    file.unlink()

    # --- the monitor -----------------------------------------------------------------------------------------------
    def _ensure_monitor(self) -> None:
        if self.closing or (self._monitor is not None and not self._monitor.done()):
            return
        self._monitor = asyncio.get_running_loop().create_task(self._watch(), name="jobs-monitor")

    async def _watch(self) -> None:
        """Every JOB_TICK: ended jobs, shells that exited while their processes run on, lifetimes. Ends when no job
        is left (the next adoption starts it again)."""
        while self._jobs and not self.closing:
            await asyncio.sleep(JOB_TICK)
            try:
                self._tick()
            except Exception:  # noqa: BLE001 - the monitor must go on
                log.exception("job monitor")

    def _tick(self) -> None:
        now = time.time()
        jobs = [j for j in self._jobs.values() if not j.finishing and j.stopping is None]
        if not jobs:
            return
        for job, live in zip(jobs, alive_all(j.cmd.tree for j in jobs)):
            if not live:
                self._claim(job, ENDED, None, None, commit=True)
                continue
            if job.cmd.exited and not job.command_done:
                if job.exited_seen_at is None:
                    job.exited_seen_at = now
                elif now - job.exited_seen_at >= LEFTOVER_GRACE:
                    self._command_done(job)
                    continue
            deadline = self._deadline(job)
            if deadline is not None and now >= deadline:
                self._begin_stop(job, "lifetime")

    def _deadline(self, job: Job) -> float | None:
        if job.origin == "timeout":
            return job.started_at + float(self.swarm.config.shell.max_timeout)
        minutes = float(self._setting("job_max_minutes") or 0)
        return job.started_at + minutes * 60 if minutes > 0 else None

    def _command_done(self, job: Job) -> None:
        """The shell of a timeout/background job exited by itself while processes it started run on (a build that
        left a daemon): the command's own end is reported now, and the rest goes on as a detached job."""
        now = time.time()
        names: list[str] = []
        with contextlib.suppress(Exception):
            names = job.cmd.tree.processes()
        previous = job.origin
        job.command_done = True
        job.exit_code = job.cmd.returncode
        job.command_ended_at = now
        job.origin = "detached"
        job.processes = names or job.processes
        job.cmd.capture.log_line(f"# command ended {_clock(now)}: exit code {job.exit_code}; still running: "
                                 f"{format_names(names)}")
        self.swarm.db.execute("UPDATE jobs SET origin = ?, exit_code = ?, processes = ? WHERE id = ?",
                              (job.origin, job.exit_code, dumps(job.processes), job.id))
        self._spawn(self._after_command_done(job, previous, names))

    async def _after_command_done(self, job: Job, previous: str, names: list[str]) -> None:
        if not self._stopping():
            detail = (f"{job.command}\n\n{job.label}: the command ended with exit code {job.exit_code}; processes it "
                      f"started still run: {format_names(names)}")
            await self._commit(job.owner, f"job {job.label}", detail)
        self._announce_command_done(job, previous, names)

    async def _commit(self, owner: str, kind: str, detail: str) -> None:
        """The autocommit of what a job changed, under its owner: the subject names the changed files (`kind`: "job
        j3: src/a.py, ..."), the body keeps the command and how the job ended (workspace.commit_message). Never
        raises: a failed commit must not cost the owner its notice (the next autocommit takes the files)."""
        try:
            await self.swarm.workspace.autocommit(owner, kind=kind, detail=detail)
        except Exception:  # noqa: BLE001
            log.exception("autocommit of %s", kind)

    def _spawn(self, coro: Any) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._helpers.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self._helpers.discard(task)
        self._finishes.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("job task failed", exc_info=task.exception())

    # --- the end of a job --------------------------------------------------------------------------------------------
    def _claim(self, job: Job, status: str, reason: str | None, stopped_by: str | None, *,
               commit: bool = True) -> asyncio.Task | None:
        """Make this caller the job's one finisher (D23): synchronous, so two enders can never both finish it. None
        when another one already claimed it."""
        if job.finishing:
            return None
        job.finishing = True
        task = asyncio.get_running_loop().create_task(self._finish_job(job, status, reason, stopped_by, commit))
        self._finishes.add(task)
        task.add_done_callback(self._task_done)
        return task

    async def _finish(self, job: Job, status: str, reason: str | None = None, stopped_by: str | None = None, *,
                      commit: bool = True) -> bool:
        """End `job` (or wait for whoever is ending it). True when this call was the finisher."""
        claimed = self._claim(job, status, reason, stopped_by, commit=commit) is not None
        await asyncio.wait({job.done})
        return claimed

    async def _finish_job(self, job: Job, status: str, reason: str | None, stopped_by: str | None,
                          commit: bool) -> None:
        ending = False
        try:
            cmd = job.cmd
            await cmd.wait_eof(READ_SETTLE)
            now = time.time()
            # the code of the job's own end: its shell's, when the shell was the last to end; a job whose shell had
            # ended before (detached, or `start /b`/`&` with background=true) ends with no code of its own: that
            # shell's code was reported when it ended, and a crash of what ran on must not read as "exit code 0"
            exit_code = cmd.returncode if status == ENDED and not job.command_done else None
            if status == STOPPED:
                footer = f"# stopped {_clock(now)} ({reason})"
            elif job.command_done:
                footer = f"# ended {_clock(now)}: all its processes exited"
            else:
                footer = f"# ended {_clock(now)}: exit code {exit_code}"
            with contextlib.suppress(Exception):
                cmd.capture.detach_log(footer)
            job.tail = cmd.capture.tail(10 ** 6, END_TAIL_CHARS)
            cmd.close()
            job.status, job.reason, job.stopped_by = status, reason, stopped_by
            job.exit_code, job.ended_at = exit_code, now
            self.swarm.db.execute(
                "UPDATE jobs SET status = ?, reason = ?, stopped_by = ?, exit_code = ?, ended_at = ?, tail = ?,"
                " processes = ?, origin = ? WHERE id = ?",
                (status, reason, stopped_by, exit_code, now, job.tail, dumps(job.processes), job.origin, job.id))
            # the owner stays busy as it was until the end notice is out, after the commit below (seconds under
            # load): idle with nothing pending meanwhile, it would get a stalled nudge, an LLM call for nothing
            ending = self.busy(job.owner)
            if ending:
                self._ending[job.owner] = self._ending.get(job.owner, 0) + 1
            self._jobs.pop(job.id, None)
            self._announce_ended(job)
            phrase = self._end_phrase(job.public(now), now)
            if commit and not self._stopping():
                await self._commit(job.owner, f"job {job.label}", f"{job.command}\n\n{job.label}: {phrase}")
            elif commit:  # it ended while the session stops: stop_all commits what it changed, under its owner
                self._uncommitted.setdefault(job.owner, []).append((job.id, job.command, phrase))
            if self.swarm.is_member(job.owner):  # a retired owner or a non-member gets no notice
                self._announce_end_notice(job)
        except Exception:  # noqa: BLE001 - the end must be recorded; the waiters must be released
            log.exception("finishing job j%s", job.id)
            self._jobs.pop(job.id, None)
            with contextlib.suppress(Exception):
                job.cmd.close()  # idempotent: the handles and the log are given back in any case
        finally:
            if ending:
                left = self._ending.pop(job.owner, 1) - 1
                if left > 0:
                    self._ending[job.owner] = left
            if job.done is not None and not job.done.done():
                job.done.set_result(None)

    def _begin_stop(self, job: Job, reason: str, stopped_by: str | None = None,
                    force: bool = False) -> asyncio.Task | None:
        """Start stopping `job` (synchronous mark: the monitor leaves it alone from now on). None when another stop
        or the finisher is already at it."""
        if job.finishing or job.stopping is not None:
            return None
        job.stopping = reason
        with contextlib.suppress(Exception):
            job.processes = job.cmd.tree.processes() or job.processes
        return self._spawn(self._kill_and_finish(job, reason, stopped_by, force))

    async def _kill_and_finish(self, job: Job, reason: str, stopped_by: str | None, force: bool) -> bool:
        await job.cmd.kill(term_grace=0.0 if force else STOP_GRACE)
        return await self._finish(job, STOPPED, reason, stopped_by)

    # --- stopping ----------------------------------------------------------------------------------------------
    async def stop(self, job_id: int, by: str, *, force: bool = False) -> str:
        """Stop job `job_id` for `by` (its owner or the user), synchronously: terminate, STOP_GRACE, kill, then the
        job is finished. ToolError: no such job, or not `by`'s job. A job that had already ended is not an error."""
        job = self._jobs.get(job_id)
        if job is None:
            row = self._row(job_id)
            if row is None:
                raise ToolError(self._unknown(job_id))
            return self._already_ended(job_id)
        if by != USER and by != job.owner:
            raise ToolError(f"Job {job.label} belongs to {job.owner}; only its owner or the user can stop it (ask them "
                            "with send_message).")
        task = self._begin_stop(job, "user" if by == USER else "owner", stopped_by=by, force=force)
        if task is not None:
            await asyncio.wait({task})
        await asyncio.wait({job.done})
        finished_here = task is not None and not task.cancelled() and task.exception() is None and task.result()
        if not finished_here:
            return self._already_ended(job_id)
        last = job.tail.strip("\n")
        last = "\n".join(last.split("\n")[-STOP_RESULT_LINES:]) if last else "(no output)"
        return (f"Stopped job {job.label} ({short_command(job.command, 120)}) of {job.owner} after "
                f"{fmt_duration((job.ended_at or time.time()) - job.started_at)}, together with the processes it "
                f"started. Last output:\n{last}")

    def _unknown(self, job_id: int) -> str:
        running = ", ".join(j.label for j in sorted(self._jobs.values(), key=lambda j: j.id))
        return f"There is no job j{job_id}. " + (f"Running jobs: {running}." if running else "No jobs are running.")

    def _row(self, job_id: int) -> Any:
        return self.swarm.db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))

    def _already_ended(self, job_id: int) -> str:
        row = self._row(job_id)
        what = self._end_phrase(dict(row)) if row is not None else "it ended"
        return f"Job j{job_id} had already ended ({what}); nothing to stop."

    def forbid(self, owner: str) -> None:
        """D24: no new command and no new job for `owner` from now on (it is being retired). Synchronous."""
        self._forbidden.add(owner)

    async def stop_owner(self, owner: str, reason: str = "retired") -> list[int]:
        """Stop every job of `owner` and kill its foreground commands (an external member's call in flight), and
        wait for them. Returns the ids of the stopped jobs."""
        jobs = [j for j in self._jobs.values() if j.owner == owner]
        foreground = [fg for fg in self._foreground.values() if fg.actor == owner]
        for fg in foreground:
            fg.killed = fg.killed or reason
            fg.cmd.tree.kill()
        tasks = {t for t in (self._begin_stop(j, reason) for j in jobs) if t is not None}
        waits = tasks | {j.done for j in jobs if j.done is not None} | {fg.done for fg in foreground}
        if waits:
            await asyncio.wait(waits, timeout=STOP_GRACE + KILL_SETTLE + READ_SETTLE + KILL_WAIT)
        return [j.id for j in jobs]

    async def stop_all(self, reason: str = "session") -> None:
        """The session stops (docs/SPEC.md §7.1): kill every job and every foreground command, finish the jobs
        (quiet notices), wait for every finish and foreground call in flight, commit once per owner (its stopped
        jobs, and those that ended by themselves since the session began to stop), release every tree and log. Bounded (about STOP_ALL_TERM + STOP_ALL_KILL + READ_SETTLE seconds plus git). Nothing of this
        manager touches git or the database after it returned. Idempotent."""
        if self._stopped_all:
            return
        self._stopped_all = True
        self.closing = True
        if self._monitor is not None and not self._monitor.done():
            self._monitor.cancel()
        jobs = list(self._jobs.values())
        foreground = list(self._foreground.values())
        for fg in foreground:
            fg.killed = fg.killed or reason
        ours = [j for j in jobs if not j.finishing]
        for job in ours:
            with contextlib.suppress(Exception):
                job.processes = job.cmd.tree.processes() or job.processes
        trees = [j.cmd.tree for j in ours] + [fg.cmd.tree for fg in foreground]
        for tree in trees:
            tree.terminate()
        if not await self._all_dead(trees, STOP_ALL_TERM):
            for tree in trees:
                tree.kill()
            await self._all_dead(trees, STOP_ALL_KILL)
        stopped: dict[str, list[int]] = {}
        for job in ours:
            if self._claim(job, STOPPED, reason, None, commit=False) is not None:
                stopped.setdefault(job.owner, []).append(job.id)
        pending = set(self._finishes) | set(self._helpers) | {fg.done for fg in foreground}
        pending |= {j.done for j in jobs if j.done is not None}
        if pending:
            await asyncio.wait(pending, timeout=READ_SETTLE + STOP_ALL_KILL)
        by_id = {j.id: j for j in ours}
        ended, self._uncommitted = self._uncommitted, {}  # ended by themselves since the session began to stop
        for owner in dict.fromkeys([*stopped, *ended]):
            ids, late = sorted(stopped.get(owner, [])), sorted(ended.get(owner, []))
            labels = [f"j{i}" for i in sorted(ids + [i for i, _, _ in late])]
            parts = (["Stopped with the session:\n" + "\n".join(f"j{i}: {by_id[i].command}" for i in ids)] if ids
                     else [])
            if late:
                parts.append("Ended while the session stopped:\n"
                             + "\n".join(f"j{i}: {command} ({phrase})" for i, command, phrase in late))
            await self._commit(owner, ("job " if len(labels) == 1 else "jobs ") + ", ".join(labels), "\n".join(parts))
        for job in jobs:
            job.cmd.close()
        for fg in foreground:
            fg.cmd.tree.release()

    @staticmethod
    async def _all_dead(trees: list, timeout: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            if not any(alive_all(trees)):
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(SETTLE_POLL)

    def recover(self) -> None:
        """Swarm.start: rows a previous process left `running` are `lost` (its jobs died with it: the Job Object
        closed, the watchdog fired). On Linux, processes still carrying such a row's token are killed when the
        process that ran them is gone. Each owner still a member gets a quiet notice."""
        self.closing = False
        self._stopped_all = False
        self._uncommitted = {}
        now = time.time()
        for row in self.swarm.db.all("SELECT * FROM jobs WHERE status = 'running' ORDER BY id"):
            if row["id"] in self._jobs:
                continue
            self.swarm.db.execute("UPDATE jobs SET status = ?, reason = 'crash', ended_at = ? WHERE id = ?",
                                  (LOST, now, row["id"]))
            if sys.platform.startswith("linux") and row["token"] and (row["hub_pid"] == os.getpid()
                                                                      or not _pid_alive(row["hub_pid"])):
                with contextlib.suppress(Exception):
                    sweep_token(row["token"])
            with contextlib.suppress(Exception):
                if self.swarm.is_member(row["owner"]):
                    self._announce_lost(dict(row))

    # --- liveness and wakes -------------------------------------------------------------------------------------
    def running_of(self, owner: str | None = None) -> list[Job]:
        jobs = sorted(self._jobs.values(), key=lambda j: j.id)
        return jobs if owner is None else [j for j in jobs if j.owner == owner]

    def _holds_in_progress(self, name: str) -> bool:
        board = getattr(self.swarm, "board", None)
        if board is None:
            return False
        return any(t.status == Status.IN_PROGRESS for t in board.open_tasks_of(name))

    def busy(self, name: str) -> bool:
        """D18: a running `timeout` job, or a `background` job younger than max_timeout while the member holds an
        in_progress task or a review. A busy member is not idle for the scheduler."""
        if self._ending.get(name):  # one of those jobs has just ended, its end notice is not out yet
            return True
        jobs = self.running_of(name)
        if any(j.origin == "timeout" for j in jobs):
            return True
        now = time.time()
        limit = float(self.swarm.config.shell.max_timeout)
        if not any(j.origin == "background" and now - j.started_at < limit for j in jobs):
            return False
        board = getattr(self.swarm, "board", None)
        return bool(self._holds_in_progress(name) or (board is not None and board.reviews_of(name)))

    def _goal_status(self) -> str | None:
        goal = self.swarm.goal()
        return goal.get("status") if goal else None

    def wakes(self, job: Job) -> bool:
        """Would the end of `job` wake its owner now (docs/SPEC.md §7.1)? timeout/background: unless the goal is
        done; detached: only under an active goal while the owner holds an in_progress task."""
        status = self._goal_status()
        if job.origin in ("timeout", "background"):
            return status != "done"
        return status == "active" and self._holds_in_progress(job.owner)

    def awaited(self, owner: str) -> list[Job]:
        """The owner's running jobs whose end will wake it (the `wait` result names them)."""
        return [j for j in self.running_of(owner) if self.wakes(j)]

    # --- views -------------------------------------------------------------------------------------------------------
    def get(self, job_id: int) -> dict | None:
        job = self._jobs.get(job_id)
        if job is not None:
            return job.public()
        row = self._row(job_id)
        return _row_public(row) if row is not None else None

    def snapshot(self) -> list[dict]:
        """The running jobs, then those that ended in the last RECENT_SECONDS (one query)."""
        now = time.time()
        running = [j.public(now) for j in self.running_of()]
        rows = self.swarm.db.all("SELECT * FROM jobs WHERE status != 'running' AND ended_at >= ?"
                                 " ORDER BY id DESC LIMIT 50", (now - RECENT_SECONDS,))
        return running + [_row_public(r) for r in rows]

    def _end_phrase(self, info: dict, now: float | None = None, *, ago: bool = False) -> str:
        """How an ended job ended: "exit code 0 after 4 min", "stopped by the user after 3 min", ..."""
        now = now or time.time()
        took = fmt_duration((info.get("ended_at") or now) - (info.get("started_at") or now))
        since = f", {fmt_duration(now - (info.get('ended_at') or now))} ago" if ago else ""
        status, reason = info.get("status"), info.get("reason")
        if status == LOST:
            return "lost: the session's process ended unexpectedly"
        if status == STOPPED:
            phrase = {"owner": f"stopped by its owner after {took}",
                      "user": f"stopped by the user after {took}",
                      "lifetime": f"stopped at its time limit after {took}",
                      "session": "stopped with the session",
                      "retired": f"stopped when {info.get('owner')} left the swarm"}.get(reason)
            phrase = phrase or f"stopped after {took}"
            return phrase + since
        if info.get("exit_code") is None:  # its shell had ended before (detached, ...): no code of its own
            return f"ended after {took} (all its processes exited)" + since
        return f"ended with exit code {info.get('exit_code')} after {took}" + since

    def _running_phrase(self, job: Job, now: float) -> str:
        if job.command_done and job.command_ended_at is not None and job.exit_code is not None:
            return (f"ended with exit code {job.exit_code} after {fmt_duration(job.command_ended_at - job.started_at)}"
                    f"; processes it started still run: {format_names(job.processes)}")
        return f"running for {fmt_duration(now - job.started_at)} ({ORIGIN_PHRASES.get(job.origin, job.origin)})"

    def listing(self, actor: str) -> str:
        """The `jobs` tool without job_id: the swarm's running jobs, the caller's marked "you", then the jobs that
        ended in the last hour (the caller's own, or everyone's for the user)."""
        now = time.time()
        running = self.running_of()
        lines: list[str] = []
        if running:
            lines.append(f"Running background jobs ({len(running)}):")
            for job in running:
                mine = job.owner == actor
                who = f"{job.owner} (you)" if mine else job.owner
                text = (f"- {job.label} {short_command(job.command, 60)}: {who}, running "
                        f"{fmt_duration(now - job.started_at)}, {ORIGIN_PHRASES.get(job.origin, job.origin)}")
                if mine or actor == USER:
                    text += f"; log {job.log_shown}"
                lines.append(text)
        else:
            lines.append("No background jobs are running.")
        sql = "SELECT * FROM jobs WHERE status != 'running' AND ended_at >= ?"
        params: list[Any] = [now - RECENT_SECONDS]
        if actor != USER:
            sql += " AND owner = ?"
            params.append(actor)
        rows = self.swarm.db.all(sql + " ORDER BY id DESC LIMIT 50", params)
        if rows:
            lines.append("Jobs that ended in the last hour:" if actor == USER
                         else "Your jobs that ended in the last hour:")
            for row in rows:
                info = dict(row)
                who = f"{info['owner']}, " if actor == USER else ""
                if info["status"] == ENDED and info["exit_code"] is not None:
                    end = (f"exit code {info['exit_code']} after "
                           f"{fmt_duration(info['ended_at'] - info['started_at'])}, ended "
                           f"{fmt_duration(now - info['ended_at'])} ago")
                else:
                    end = self._end_phrase(info, now, ago=info["status"] != LOST)
                lines.append(f"- j{info['id']} {short_command(info['command'], 60)}: {who}{end}")
        return "\n".join(lines)

    def describe(self, job_id: int, *, actor: str, lines: int = DETAIL_LINES, max_chars: int = 12000) -> str:
        """The `jobs` tool with job_id: the job's state and the end of its output (from memory while it runs, else
        from the end of its log, else the stored tail). Any member may read any job."""
        now = time.time()
        lines = min(max(int(lines or DETAIL_LINES), 1), DETAIL_LINES_MAX)
        job = self._jobs.get(job_id)
        if job is not None:
            info = job.public(now)
            state = self._running_phrase(job, now)
        else:
            row = self._row(job_id)
            if row is None:
                raise ToolError(self._unknown(job_id))
            info = dict(row)
            state = self._end_phrase(info, now, ago=info["status"] in (ENDED, STOPPED))
        shell = job.shell if job is not None else info.get("shell")
        out = [f"Job j{job_id} of {info['owner']}: {short_command(info['command'], 200)}",
               f"State: {state}" + (f" [{shell}]" if shell else "")]
        if job is not None and info.get("processes"):
            out.append(f"Processes: {format_names(info['processes'])}")
        path = self._log_file(info.get("log"))
        shown = shown_log(info.get("log"))
        text = ""
        if job is not None:
            text = job.cmd.tail(lines, max_chars)
            if path is not None and path.exists():
                out.append(f"Log: {shown} ({format_size(path.stat().st_size)}; below is its end)")
        elif path is not None and path.is_file():
            text = _read_log_end(path, lines, max_chars)
            out.append(f"Log: {shown} ({format_size(path.stat().st_size)}; below is its end)")
        else:
            text = (info.get("tail") or "").rstrip("\n")
            text = "\n".join(text.split("\n")[-lines:]) if text else ""
            if shown:
                out.append(f"Log: {shown} (log file not found)")
        head = "\n".join(out)
        if not text.strip():
            return head + "\n(no output)"
        count = text.count("\n") + 1
        room = max(200, max_chars - len(head) - 40)
        if len(text) > room:
            text = text[-room:]
        return f"{head}\n--- last {count} lines ---\n{text}"

    # --- the console intercept ---------------------------------------------------------------------------------
    async def console(self, actor: str, command: str, *, max_chars: int = 12000) -> str | None:
        """`jobs`, `jobs j3` or `kill j3` as a whole `shell` command line: answered here, nothing is spawned (None:
        the command goes to the real shell). The first line ends with " [dreamteam]"."""
        parsed = parse_console(command)
        if parsed is None:
            return None
        action, ids, force = parsed
        try:
            if action == "list":
                text = self.listing(actor)
            elif action == "show":
                text = self.describe(ids[0], actor=actor, lines=DETAIL_LINES, max_chars=max_chars)
            else:
                results: list[str] = []
                for job_id in ids:
                    try:
                        results.append(await self.stop(job_id, actor, force=force))
                    except ToolError as exc:
                        if len(ids) == 1:
                            raise
                        results.append(f"Error: {exc}")
                text = "\n".join(results)
        except ToolError as exc:
            raise ToolError(_mark_first_line(str(exc))) from None
        return _mark_first_line(text)

    # --- notices and events (docs/SPEC.md §7.1 "Job end"); none of them ever raises ------------------------------
    def _announce_started(self, job: Job) -> None:
        """A job was adopted: the job.started event."""
        try:
            self.swarm.bus.emit("job.started", job.owner, job=job.public())
        except Exception:  # noqa: BLE001 - the job runs on whatever the journal says
            log.exception("job.started of j%s", job.id)

    def _announce_ended(self, job: Job) -> None:
        """A job's row was finished: the job.ended event (the public job plus its `duration`)."""
        try:
            data = job.public()
            data["duration"] = round(max(0.0, (job.ended_at or time.time()) - job.started_at), 3)
            self.swarm.bus.emit("job.ended", job.owner, job=data)
        except Exception:  # noqa: BLE001
            log.exception("job.ended of j%s", job.id)

    def _announce_end_notice(self, job: Job) -> None:
        """The owner's notice at a job's end (only for a member: the caller checks). Ended by itself: n_job_done;
        stopped by the user: n_job_stopped; at its lifetime: n_job_limit (all three wake under wakes()); with the
        session: a quiet n_job_gone; stopped by its owner (the tool result says it) or at its retirement: none."""
        try:
            lang = self.swarm.language_of(job.owner)
            duration = fmt_duration((job.ended_at or time.time()) - job.started_at, lang)
            command = short_command(job.command, 120)
            if job.status == ENDED:
                if job.exit_code is None:  # its shell had ended before: that code was already reported
                    result = prompts.t(lang, "job_gone")
                else:
                    result = prompts.t(lang, "job_exit", code=job.exit_code)
                tail = job.notice_tail() or prompts.t(lang, "job_no_output")
                self.swarm.notify(job.owner, "n_job_done", wake=self.wakes(job), id=job.id, result=result,
                                  duration=duration, command=command, tail=tail, log=job.log_shown)
            elif job.status == STOPPED and job.reason == "user":
                self.swarm.notify(job.owner, "n_job_stopped", wake=self.wakes(job), id=job.id, duration=duration,
                                  command=command, log=job.log_shown)
            elif job.status == STOPPED and job.reason == "lifetime":
                self.swarm.notify(job.owner, "n_job_limit", wake=self.wakes(job), id=job.id, duration=duration,
                                  command=command, log=job.log_shown)
            elif job.status == STOPPED and job.reason == "session":
                self.swarm.notify(job.owner, "n_job_gone", wake=False, id=job.id, command=command, log=job.log_shown)
        except Exception:  # noqa: BLE001 - the job is recorded; only its notice is lost
            log.exception("the end notice of j%s", job.id)

    def _announce_command_done(self, job: Job, previous_origin: str, names: list[str]) -> None:
        """The shell of a timeout/background job exited while its processes run on: n_job_command_done with the
        shell's code (the job goes on), waking the owner as an ended timeout/background job would (unless the goal is
        done; not while the session stops)."""
        try:
            if not self.swarm.is_member(job.owner):
                return
            lang = self.swarm.language_of(job.owner)
            ended = job.command_ended_at or time.time()
            tail = job.notice_tail() or prompts.t(lang, "job_no_output")
            wake = self._goal_status() != "done" and not self._stopping()
            self.swarm.notify(job.owner, "n_job_command_done", wake=wake, id=job.id,
                              result=prompts.t(lang, "job_exit", code=job.exit_code),
                              duration=fmt_duration(ended - job.started_at, lang), names=format_names(names),
                              command=short_command(job.command, 120), tail=tail, log=job.log_shown)
        except Exception:  # noqa: BLE001
            log.exception("the command-done notice of j%s", job.id)

    def _announce_lost(self, row: dict) -> None:
        """recover(): a job of a previous process is lost (its processes died with that process): a quiet
        n_job_gone, shown at the owner's next wake-up."""
        try:
            self.swarm.notify(row["owner"], "n_job_gone", wake=False, id=row["id"],
                              command=short_command(row.get("command") or "", 120), log=shown_log(row.get("log")))
        except Exception:  # noqa: BLE001
            log.exception("the lost notice of j%s", row.get("id"))


def _mark_first_line(text: str) -> str:
    first, sep, rest = text.partition("\n")
    return first + CONSOLE_MARK + sep + rest


def _read_log_end(path: Path, lines: int, max_chars: int) -> str:
    """The last `lines` lines of a log file, at most `max_chars` characters (read from its end only)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_chars * 4))
            data = f.read()
    except OSError:
        return ""
    text = data.decode("utf-8", "replace").rstrip("\n")
    out = "\n".join(text.split("\n")[-lines:])
    return out[-max_chars:]

