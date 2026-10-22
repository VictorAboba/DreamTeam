"""Swarm: the composition root that owns storage, the board, mail, workspace, triggers, MCP forge
and the agent runtimes, and implements team, goal, budget and notification logic."""

from __future__ import annotations

import asyncio
import dataclasses
import difflib
import hashlib
import logging
import time
from pathlib import Path
from typing import Any, Callable

from . import prompts, roles
from .agent import AgentRuntime
from .board import Board
from .config import Config, ModelProfile
from .db import Database, dumps, loads, utf8_safe
from .events import EventBus
from .goal_text import split_goal
from .jobs import JobManager, fmt_duration, short_command
from .llm import create_llm
from .llm.base import LLMClient, Usage, cost_of
from .mailbox import Mailbox
from .mcp_forge import MCPForge
from .models import USER, AgentKind, AgentStatus, Status, ToolError
from .names import generate_name
from .shell import resolve_shell
from .swarm_skills import SessionSkills, role_skill_pairs
from .tools.fmt import STAMP_SPAN, item_time, stamp_range
from .tracing import EXTERNAL_IDLE_SECONDS, Tracer
from .triggers import TriggerEngine
from .workspace import Workspace, scratch_rel

log = logging.getLogger(__name__)

USER_ALIASES = {"user", "пользователь", "the user", "human", "человек"}
QUIET_KEY = "_quiet"  # notifications.data flag of a member notification sent with wake=False
SCHEDULER_SECONDS = 3.0
TRACE_PRUNE_SECONDS = 6 * 3600
# An agent nudged about ready tasks is "considering" them until it claims a task, goes idle again or
# this much time passes. No more agents consider offers than there are ready tasks, so one free task
# does not wake the whole idle team at once.
NUDGE_DECISION_SECONDS = 300.0


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def resolve_member_in(pool: Callable[[], list[dict]], name: str, allow_user: bool = True) -> str:
    """Name resolution shared by Swarm.resolve_member and the test double (tests/conftest.py), so leaf
    modules are tested against the real rules: case-insensitive, whitespace-tolerant, '@' prefix, a unique
    first name alone, user aliases. `pool()` returns the candidate agents rows."""
    raw = str(name or "").strip().lstrip("@")
    key = " ".join(raw.split()).lower()
    if not key:
        raise ToolError("Empty member name.")
    if key in USER_ALIASES:
        if allow_user:
            return USER
        raise ToolError("The user cannot be used here; pass a member name.")
    members = pool()
    for m in members:
        if m["name"].lower() == key:
            return m["name"]
    names = [m["name"] for m in members]
    firsts = [m["name"] for m in members if m["name"].split()[0].lower() == key]
    if len(firsts) == 1:
        return firsts[0]
    close = difflib.get_close_matches(raw, names, n=3, cutoff=0.5)
    hint = f" Did you mean: {', '.join(close)}?" if close else ""
    return_names = ", ".join(names) or "(none)"
    raise ToolError(f"Unknown member '{raw}'.{hint} Members: {return_names}" + (", user" if allow_user else ""))


class Swarm:
    def __init__(self, project_dir: str | Path | None = None, config: Config | None = None,
                 llm_factory: Callable[[ModelProfile, str], LLMClient] | None = None):
        if config is None:
            if project_dir is None:
                raise ValueError("project_dir or config is required")
            config = Config.load(project_dir)
        self.config = config
        self.project_dir = config.project_dir
        (config.data_dir / "logs").mkdir(parents=True, exist_ok=True)
        self.db = Database(config.data_dir / "swarm.db")
        try:
            self._init(config, llm_factory)
        except BaseException:  # e.g. a corrupt database: the connection must not stay open (Windows file locks)
            self.db.close()
            raise

    def _init(self, config: Config, llm_factory: Callable[[ModelProfile, str], LLMClient] | None) -> None:
        self.bus = EventBus(self.db)
        self.tracer = Tracer(self.db, self.bus, enabled=config.tracing.enabled,
                             max_payload_chars=config.tracing.max_payload_chars)
        self.session_id: str | None = None  # set by sessions.SessionManager
        self.session_title: str | None = None
        # the goal given when the session was created; it becomes the real goal at its first start
        self.session_pending_goal: dict | None = None
        self._last_prune = 0.0
        self.language = config.swarm.language
        self.llm_semaphore = asyncio.Semaphore(max(1, config.agents.max_parallel_llm_calls))
        self._llm_factory = llm_factory
        self.runtimes: dict[str, AgentRuntime] = {}
        self._signals: dict[str, asyncio.Event] = {}
        self._background: list[asyncio.Task] = []
        self._role_skills_task: asyncio.Task | None = None  # a reconfigure attaching the roles' skills (one at a time)
        self._role_skills_again = False   # the roles changed again: that task runs once more
        self._timers: set[asyncio.TimerHandle] = set()
        self._started = False
        self._run_agents = True
        self._nudged: dict[int, set[str]] = {}
        self._last_nudge: dict[str, float] = {}
        # agent -> (nudged at, its open task ids then, task ids offered)
        self._considering: dict[str, tuple[float, frozenset[int], frozenset[int]]] = {}
        self._all_done_key: tuple | None = None
        self._stall_woken: dict[str, frozenset[int]] = {}  # agent -> stalled task ids it was woken about
        # agent idle (not busy, nothing pending) holding in-progress tasks -> (their ids, idle with them since)
        self._idle_holding: dict[str, tuple[frozenset[int], float]] = {}
        # reviewer -> (ids of the reviews it was reminded about, or that waited while it was in error; since when)
        self._review_reminded: dict[str, tuple[frozenset[int], float]] = {}
        self._review_lapsed: dict[int, set[str]] = {}  # task in review -> reviewers it was taken from (hand-over)
        self._user_reviews_key: frozenset[int] | None = None  # the user's reviews it was last told about
        self._notes_shown: dict[str, str] = {}  # agent -> digest of the notes last put into its context
        # agent -> True: a full Skills index is in its context (a wake-up or a compaction); False: "none" was shown
        self._skills_shown: dict[str, bool] = {}
        # agents a broadcast reached without a wake while they wait for their own job (Mailbox.send): woken by the
        # scheduler if that wait ends without the job's end waking them (_wake_deferred)
        self._deferred: set[str] = set()
        self._missing_profiles: set[str] = set()  # members' profiles gone from the settings (warned once)
        self.stopping = False  # stop() runs or ran: long polls (wait_for_events) end without touching the db
        self.hub_url: str | None = None
        self.board = Board(self)
        self.mail = Mailbox(self)
        self.workspace = Workspace(self)
        self.triggers = TriggerEngine(self)
        self.forge = MCPForge(self)
        self.jobs = JobManager(self)  # the background jobs of the members' shell commands
        self.skills = SessionSkills(self)  # the skills attached to this session (docs/SPEC.md §19)
        self.skills.load()

    # ---- lifecycle ----------------------------------------------------------------------------
    async def start(self, run_agents: bool = True) -> None:
        self.stopping = False
        self.tracer.recover()  # traces left open by a previous process
        self.jobs.recover()  # jobs a previous process left running died with it: lost (quiet notices)
        self._prune_traces()
        await self.workspace.init()
        await self.forge.start()
        await self.skills.start()  # the agents' mirror is rebuilt from the session copies at every start
        # the skills' script dependencies (docs/SPEC.md §19): built in the background, never waited for
        self._background.append(asyncio.create_task(self.skills.ensure_deps(), name="skill-deps"))
        existing = self.members(include_retired=True)
        if not existing:
            # member i gets starting_roles[i], the rest are generalists (the role catalog resolves the names)
            for role in roles.starting_team(self.config.swarm.starting_roles, self.config.swarm.start_agents):
                self._create_member(USER, role, kind=AgentKind.LLM)
        # the skills the members' roles list (docs/SPEC.md §19), untold: every first wake-up after a start shows the
        # full index anyway
        await self.skills.ensure_role_skills(announce=False)
        self._started = True
        self._run_agents = run_agents
        if run_agents:
            for m in self.members():
                if m["kind"] == AgentKind.LLM:
                    if m["status"] in (AgentStatus.WORKING, AgentStatus.ERROR, AgentStatus.PAUSED):
                        self.db.execute("UPDATE agents SET status = ? WHERE name = ?", (AgentStatus.IDLE, m["name"]))
                    self._start_runtime(m["name"])
        self._background.append(asyncio.create_task(self.triggers.run(), name="triggers"))
        self._background.append(asyncio.create_task(self._scheduler(), name="scheduler"))
        goal = self.goal()
        goal_active = bool(goal) and goal.get("status") == "active"
        if self.config.swarm.goal and not goal:
            self.set_goal(USER, self.config.swarm.goal, self.config.swarm.goal_description)
        elif goal_active and not existing and not self.paused:  # the goal was set before the very first start
            planning = next((t for t in self.board.list() if "planning" in t.tags), None)
            self._kickoff(USER, goal, planning)
        elif existing and not self.paused:
            for m in self.members():
                if m["kind"] != AgentKind.LLM:
                    continue
                runtime = self.runtimes.get(m["name"])
                # woken once: open work or reviews waiting for it under an active goal, or an activation the
                # restart cut short (e.g. a reply to the user or a review in progress, which leave nothing pending)
                if ((runtime is not None and runtime.interrupted)
                        or (goal_active and (self.board.open_tasks_of(m["name"]) or self.board.reviews_of(m["name"])
                                             or self.has_pending(m["name"])))):
                    self.notify(m["name"], "n_restart")

    async def stop(self) -> None:
        self._wake_waiters()
        for handle in self._timers:  # staggered kickoff notifications not sent yet
            handle.cancel()
        self._timers.clear()
        for task in self._background:
            task.cancel()
        for task in self._background:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._background.clear()
        try:
            self.tracer.close_external()
        except Exception:  # noqa: BLE001 - tracing must never keep the session from stopping
            log.exception("could not close the traces of external members")
        try:
            failure: Exception | None = None
            for runtime in list(self.runtimes.values()):
                try:
                    await runtime.stop()  # also closes its LLM client
                except Exception as exc:  # noqa: BLE001 - the other agents stop all the same; raised below
                    failure = failure or exc
            self.runtimes.clear()
            if failure is not None:
                raise failure
        finally:
            # even when a step above failed: every job and every foreground command (an external member's call) is
            # killed, and every finish and call in flight has returned: nothing of them touches git or the database
            # after this (logs, handles closed), and no process outlives its session
            try:
                await self.jobs.stop_all()
            finally:
                try:
                    await self.forge.stop()
                    await self.workspace.settle()  # a commit a stopped agent started finishes before files may move
                finally:
                    await self.skills.stop_deps()  # never raises: no more build news, the in-use set released
                    if self.config.workspace_adopted:
                        # the user's own folder: no copy of a library or a private Claude Code skill stays in it,
                        # whatever failed above (remove_mirror never raises)
                        await self.skills.remove_mirror()
                    self._started = False

    def reconfigure(self, config: Config) -> None:
        """Hot-apply new settings (docs/SPEC.md §16.1). The project directory, the workspace folder, the swarm
        language and the agents' frozen system prompts stay; LLM clients are rebuilt on their next call."""
        if config.project_dir != self.project_dir:
            config = dataclasses.replace(config, project_dir=self.project_dir)
        if (config.workspace_path, config.workspace_adopted) != (self.config.workspace_path,
                                                                 self.config.workspace_adopted):
            # the workspace (its root, the forged MCP servers' folders) moves only with a restart (sessions.py)
            config = dataclasses.replace(config, workspace_path=self.config.workspace_path,
                                         workspace_adopted=self.config.workspace_adopted)
        if self._started and self.config.workspace_adopted and config.workspace.git != self.config.workspace.git:
            # a folder of the user: its repository is checked (clean, on a branch) when the session starts, so git
            # is switched on or off there at the next start only (sessions.py), never under running agents
            config = dataclasses.replace(config, workspace=dataclasses.replace(config.workspace,
                                                                               git=self.config.workspace.git))
        old = self.config
        self.config = config
        self._missing_profiles.clear()
        self.tracer.enabled = config.tracing.enabled
        self.tracer.max_payload_chars = config.tracing.max_payload_chars
        if config.agents.max_parallel_llm_calls != old.agents.max_parallel_llm_calls:
            # calls holding the old semaphore finish with it; new calls use the new limit
            self.llm_semaphore = asyncio.Semaphore(max(1, config.agents.max_parallel_llm_calls))
        for runtime in self.runtimes.values():
            runtime.reset_llm()
            if runtime.state == AgentStatus.ERROR and runtime.interrupted:
                runtime.wake()  # e.g. the API key was fixed: retry the failed activation now
        if self.paused and self.pause_reason == "budget" and not self.budget_exceeded():
            self.resume(USER)  # the new default budget is enough again (as with set_limits)
        if self._started and not self.stopping and role_skill_pairs(config) != role_skill_pairs(old):
            self._attach_role_skills()

    def _attach_role_skills(self) -> None:
        """The roles' skills changed under a running swarm: the missing ones are attached in the background (stop()
        cancels it). A run still going is never cancelled (its copy thread would go on past the lock): it runs once
        more with the newer roles."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # not called from the event loop: the next start attaches them
            return
        self._role_skills_again = True
        old = self._role_skills_task
        if old is not None and not old.done():
            return
        if old in self._background:
            self._background.remove(old)
        self._role_skills_task = loop.create_task(self._role_skills_runs(), name="role-skills")
        self._background.append(self._role_skills_task)

    async def _role_skills_runs(self) -> None:
        while self._role_skills_again:
            self._role_skills_again = False
            await self.skills.ensure_role_skills()

    def _prune_traces(self) -> None:
        self._last_prune = time.time()
        days = float(self.config.tracing.retention_days or 0)
        if days > 0:
            removed = self.tracer.prune(days)
            if removed:
                log.info("pruned %d traces older than %g days", removed, days)

    def close(self) -> None:
        self._wake_waiters()
        self.db.close()

    def _wake_waiters(self) -> None:
        """End the long polls (wait_for_events) of external members and MCP clients: the swarm is stopping, and
        its database may be closed before their timeout."""
        self.stopping = True
        for event in self._signals.values():
            event.set()

    def _start_runtime(self, name: str) -> AgentRuntime:
        runtime = self.runtimes.get(name)
        if runtime is None:
            runtime = AgentRuntime(self, name)
            self.runtimes[name] = runtime
        runtime.start()
        if self.has_pending(name):
            runtime.wake()
        return runtime

    # ---- members ------------------------------------------------------------------------------
    def member(self, name: str) -> dict | None:
        row = self.db.one("SELECT * FROM agents WHERE name = ?", (name,))
        return dict(row) if row else None

    def members(self, include_retired: bool = False) -> list[dict]:
        sql = "SELECT * FROM agents" + ("" if include_retired else " WHERE status != 'retired'")
        # rowid breaks ties: members created within one tick of the clock keep their hiring order
        return [dict(r) for r in self.db.all(sql + " ORDER BY created_at, rowid")]

    def is_member(self, name: str) -> bool:
        m = self.member(name)
        return bool(m) and m["status"] != AgentStatus.RETIRED

    def agent_count(self) -> int:
        """The members counted against max_agents: the active AI (LLM) agents. External members (MCP clients that
        joined with join_swarm) do not count: a client joining must not use up a place of the team."""
        return int(self.db.scalar("SELECT COUNT(*) FROM agents WHERE kind = ? AND status != ?",
                                  (AgentKind.LLM, AgentStatus.RETIRED)) or 0)

    def kind_of(self, name: str) -> str | None:
        if name == USER:
            return "user"
        m = self.member(name)
        return m["kind"] if m else None

    def resolve_member(self, name: str, allow_user: bool = True, allow_retired: bool = False) -> str:
        return resolve_member_in(lambda: self.members(include_retired=allow_retired), name, allow_user)

    def language_of(self, name: str) -> str:
        if name == USER:
            return self.language
        m = self.member(name)
        return (m and m.get("language")) or self.language

    # ---- notifications & waking ---------------------------------------------------------------
    def notify(self, recipient: str, key: str, *, wake: bool = True, **kw: Any) -> None:
        """wake=False for a member: news for its next wake-up, which does not count as pending (has_pending)
        and so neither wakes it nor keeps the scheduler from offering it tasks."""
        text = utf8_safe(prompts.t(self.language_of(recipient), key, **kw))  # may quote model output
        data = kw if wake or recipient == USER else {**kw, QUIET_KEY: True}
        self.db.insert(
            "INSERT INTO notifications(ts, recipient, kind, text, data) VALUES (?, ?, ?, ?, ?)",
            (time.time(), recipient, key, text, dumps(data)),
        )
        self.bus.emit("notification", None, recipient=recipient, kind=key, text=text)
        if wake:
            self.signal(recipient)

    def notify_later(self, delay: float, recipient: str, key: str, **kw: Any) -> None:
        if delay <= 0:
            self.notify(recipient, key, **kw)
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.notify(recipient, key, **kw)
            return
        holder: list[asyncio.TimerHandle] = []

        def fire() -> None:
            self._timers.discard(holder[0])
            self._safe_notify(recipient, key, kw)

        holder.append(loop.call_later(delay, fire))
        self._timers.add(holder[0])

    def _safe_notify(self, recipient: str, key: str, kw: dict[str, Any]) -> None:
        try:
            if recipient == USER or self.is_member(recipient):
                self.notify(recipient, key, **kw)
        except Exception:
            log.exception("delayed notification failed")

    def signal(self, name: str) -> None:
        self._signals.setdefault(name, asyncio.Event()).set()
        runtime = self.runtimes.get(name)
        if runtime is not None:
            runtime.wake()

    async def wait_signal(self, name: str, timeout: float) -> bool:
        event = self._signals.setdefault(name, asyncio.Event())
        if event.is_set():
            event.clear()
            return True
        try:
            await asyncio.wait_for(event.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            event.clear()

    def waits_for_job(self, name: str) -> bool:
        """An AI agent asleep (idle) while a command of its own runs on as a job it waits for (JobManager.busy) and
        whose end will wake it (JobManager.awaited): a broadcast of another member reaches it without a wake."""
        runtime = self.runtimes.get(name)
        return (runtime is not None and runtime.state == AgentStatus.IDLE and self.jobs.busy(name)
                and bool(self.jobs.awaited(name)))

    def defer_wake(self, name: str) -> None:
        """`name` got news without a wake (waits_for_job): its job's end shows it; should that wait end otherwise,
        the scheduler wakes it (_wake_deferred)."""
        self._deferred.add(name)

    def has_pending(self, name: str) -> bool:
        if self.db.scalar("SELECT 1 FROM inbox WHERE recipient = ? AND read_at IS NULL LIMIT 1", (name,)):
            return True
        return bool(self.db.scalar(
            "SELECT 1 FROM notifications WHERE recipient = ? AND delivered_at IS NULL"
            f" AND json_extract(data, '$.{QUIET_KEY}') IS NULL LIMIT 1", (name,)))

    def render_wake(self, name: str, incoming: bool = False, stamps: bool = False,
                    since: float | None = None) -> str | None:
        """The wake-up text of `name` (incoming: only the news, None when there is none). `stamps` (only
        wait_for_events: an MCP client's batch may span hours, unlike an agent's wake-up, whose history must not
        grow): when the oldest item, or `since` (the oldest of other lines shown with this text), is more than
        STAMP_SPAN seconds old, each notification and message gets its own time and the header the range."""
        lang = self.language_of(name)
        notes = self.db.all(
            "SELECT id, ts, text FROM notifications WHERE recipient = ? AND delivered_at IS NULL ORDER BY id", (name,))
        messages = self.mail.unread(name, mark_read=True)
        if incoming and not notes and not messages:
            return None
        now = time.time()
        if notes:
            self.db.execute(
                f"UPDATE notifications SET delivered_at = ? WHERE id IN ({','.join('?' * len(notes))})",
                [now] + [n["id"] for n in notes],
            )
        first = None
        if stamps:
            times = [t for t in [*(n["ts"] for n in notes), *(m["ts"] for m in messages), since] if t]
            if times and now - min(times) > STAMP_SPAN:
                first = min(times)

        def at(ts: float | None) -> str:
            return f"{item_time(ts, now)} " if first is not None and ts else ""

        lines = [prompts.t(lang, "incoming_header" if incoming else "wake_header", time=stamp_range(first, now))]
        if not incoming and self.wrapping_up:
            lines.append(prompts.t(lang, "wrap_up_line"))
        if notes or messages:
            lines.append(prompts.t(lang, "reasons"))
            lines.extend(f"• {at(n['ts'])}{n['text']}" for n in notes)
            you = prompts.t(lang, "you")
            for m in messages:
                to = ", ".join(you if r == name else r for r in m["recipients"])
                text = prompts.t(lang, "msg", id=m["id"], sender=m["sender"], to=to, text=m["text"])
                if m.get("reply_to"):
                    head, _, rest = text.partition("\n")
                    text = head.rstrip(":") + prompts.t(lang, "msg_reply", reply_to=m["reply_to"]) + ":\n" + rest
                lines.append("• " + at(m["ts"]) + text)
            # every member reads a message to all at once: only with one, not at every wake-up
            if any(m.get("to_all") for m in messages):
                lines.append(prompts.t(lang, "all_hint"))
        if not incoming:
            goal = self.goal()
            if goal:
                lines.append(prompts.t(lang, "goal", title=goal["title"], status=goal.get("status", "active")))
            else:
                lines.append(prompts.t(lang, "goal_none"))
            mine = self.board.open_tasks_of(name)
            lines.append(prompts.t(lang, "my_tasks", tasks="; ".join(t.short() for t in mine))
                         if mine else prompts.t(lang, "my_tasks_none"))
            # a held task blocked by unfinished ones: its assignee may drop what it can do without (Board._plan_deps);
            # not in review, where the result is already produced
            lines.extend(prompts.t(lang, "my_blocked", id=t.id, blockers=", ".join(f"#{d}" for d in t.blocked_by))
                         for t in mine if t.blocked_by and t.status != Status.REVIEW)
            reviews = self.board.reviews_of(name)
            if reviews:
                lines.append(prompts.t(lang, "my_reviews", tasks="; ".join(
                    f"#{t.id} {t.title} ({t.assignee or '-'})" for t in reviews)))
            jobs = self.jobs.running_of(name)
            if jobs:
                now = time.time()
                lines.append(prompts.t(lang, "my_jobs", jobs="; ".join(self._my_job(j, lang, now) for j in jobs)))
            ready = self.board.ready_unassigned()[:8]
            if ready:
                lines.append(prompts.t(lang, "ready", tasks="; ".join(f"#{t.id} {t.title}" for t in ready)))
            member = self.member(name) or {}
            notes_text = (member.get("notes") or "").strip()
            if member.get("kind") == AgentKind.LLM:
                # The full Skills index (up to ~15 KB) once per agent memory (the first wake-up, after a restart, in
                # a compaction text); every later wake-up one line of names: changes arrive as notifications with
                # their descriptions, and the history is append-only.
                index = self.skills.index_text(name)
                if index:
                    if self._skills_shown.get(name) is True:
                        lines.append(self.skills.names_line(name))
                    else:  # never shown, or only "none" since
                        lines.append(index)
                        self.skills_shown(name, True)
                elif self._skills_shown.get(name) is True:  # it had seen some: all were detached since
                    lines.append(prompts.t(lang, "skills_none"))
                    self.skills_shown(name, False)
                # The history is append-only: the full notebook (up to 20000 chars) at every wake-up would grow
                # it by that much each time. It is shown again only when it changed since it was last shown
                # (once more after a restart; a compaction puts it into the compacted context).
                if not notes_text:
                    lines.append(prompts.t(lang, "notes_empty"))
                elif self._notes_shown.get(name) == _digest(notes_text):
                    lines.append(prompts.t(lang, "notes_unchanged"))
                else:
                    lines.append(prompts.t(lang, "notes", notes=notes_text))
                    self.notes_shown(name, notes_text)
        return "\n".join(lines)

    @staticmethod
    def _my_job(job: Any, lang: str, now: float) -> str:
        """One job of the my_jobs line; a silent one (jobs.SILENT_MINUTES) says for how long."""
        kw = {"id": job.id, "command": short_command(job.command, 60),
              "duration": fmt_duration(now - job.started_at, lang)}
        silent = job.silent_for(now)
        if silent is None:
            return prompts.t(lang, "my_job", **kw)
        return prompts.t(lang, "my_job_silent", silent=fmt_duration(silent, lang), **kw)

    def notes_shown(self, name: str, notes: str) -> None:
        """The agent's context now holds these notes (a wake-up text or a compaction summary)."""
        self._notes_shown[name] = _digest((notes or "").strip())

    def skills_shown(self, name: str, value: bool | None) -> None:
        """What the agent's context holds of the Skills index: True the full index, False "none attached", None
        nothing known (its next wake-up shows the full index)."""
        if value is None:
            self._skills_shown.pop(name, None)
        else:
            self._skills_shown[name] = value

    # ---- goal ---------------------------------------------------------------------------------
    def goal(self) -> dict | None:
        return self.db.get("goal")

    def set_goal(self, actor: str, title: str, description: str = "") -> dict:
        # every goal comes through here: the first line of a multi-line title is the title, the rest opens the
        # description (goal_text.split_goal), so the goal line of every wake-up stays one line
        title, description = split_goal(title, description)
        if not title:
            raise ToolError("The goal title is empty.")
        previous = self.goal()
        goal = {"title": title, "description": description, "status": "active", "summary": "",
                "set_by": actor, "updated_at": time.time()}
        self.db.set("goal", goal)
        self.bus.emit("goal.set", actor, goal=goal)
        self.end_wrap_up(actor)  # a new goal is new work (its kickoff follows the quiet notice)
        planning = None
        if not self.board.list():
            p_title, p_goal, p_desc = prompts.PLANNING_TASK[self.language]
            body = f"{p_desc}\n\n{title}" + (f"\n{goal['description']}" if goal["description"] else "")
            planning = self.board.create(actor, p_title, goal=p_goal, description=body, priority=2, tags=["planning"])
        if self._started:
            self._kickoff(actor, goal, planning)
        log.info("goal set by %s (previous: %s)", actor, (previous or {}).get("title"))
        return goal

    def _kickoff(self, actor: str, goal: dict, planning) -> None:
        stagger = max(0.0, self.config.agents.stagger_seconds)
        members = self.members()
        llm_members = [m for m in members if m["kind"] == AgentKind.LLM]
        # who has which role: the planner sees the team without a tool call
        team = "; ".join(f"{m['name']} — {m['role']}" for m in members)
        for i, m in enumerate(llm_members):
            if planning is not None:
                # the kickoff is everybody's offer of the planning task: no "ready task" nudge about it
                # before (stagger) or after the kickoff
                self._nudged.setdefault(planning.id, set()).add(m["name"])
                self.notify_later(i * stagger, m["name"], "n_kickoff", by=actor, title=goal["title"],
                                  description=goal.get("description", ""), team=team, task_id=planning.id,
                                  task_title=planning.title)
            else:
                self.notify_later(i * stagger, m["name"], "n_goal_updated", by=actor, title=goal["title"],
                                  description=goal.get("description", ""))

    def complete_goal(self, actor: str, summary: str) -> dict:
        goal = self.goal()
        if not goal:
            raise ToolError("There is no global goal to complete.")
        if goal.get("status") == "done":  # the user would be told "goal achieved" twice
            raise ToolError("The global goal is already marked as achieved.")
        if not (summary or "").strip():
            raise ToolError("Give a summary for the user.")
        goal.update(status="done", summary=summary.strip(), completed_by=actor, updated_at=time.time())
        self.db.set("goal", goal)
        self.bus.emit("goal.completed", actor, goal=goal)
        self.notify(USER, "u_goal_done", by=actor, summary=summary.strip())
        running = self.jobs.running_of()
        if running:  # they go on (a dev server may be for the user); their later ends wake nobody
            now = time.time()
            listed = (f"{j.label} ({j.owner}: {short_command(j.command, 60)}, "
                      f"{fmt_duration(now - j.started_at, self.language)})" for j in running)
            self.notify(USER, "u_jobs_running", wake=False, jobs="; ".join(listed))
        return goal

    def reopen_goal(self, actor: str, comment: str = "") -> dict:
        goal = self.goal()
        if not goal:
            raise ToolError("There is no global goal.")
        if goal.get("status") != "done":
            raise ToolError("The global goal is not completed; nothing to reopen.")
        goal.update(status="active", updated_at=time.time())
        self.db.set("goal", goal)
        self.bus.emit("goal.reopened", actor, goal=goal, comment=comment)
        self.end_wrap_up(actor)  # the missing parts are to be done now
        # One member re-plans: the one who declared the goal achieved, else the most recently active. Waking
        # everyone at once made them all create tasks for the same missing parts; the others are offered the
        # new tasks by the scheduler (n_ready) and see the reopened goal in their next wake-up.
        llm = [m for m in self.members() if m["kind"] == AgentKind.LLM]
        if llm:
            names = {m["name"] for m in llm}
            chosen = goal.get("completed_by")
            if chosen not in names:
                chosen = max(llm, key=lambda m: m.get("last_active") or 0)["name"]
            self.notify(chosen, "n_goal_reopened", title=goal["title"], comment=comment or "-")
        return goal

    # ---- team ---------------------------------------------------------------------------------
    def _validate_name(self, name: str) -> str:
        name = " ".join(name.split())
        parts = name.split(" ")
        if len(parts) < 2 or not all(p.replace("-", "").replace("'", "").isalpha() for p in parts) or len(name) > 60:
            raise ToolError("The name must be a human first and last name, e.g. 'John Smith'.")
        if name.lower() in USER_ALIASES:
            raise ToolError("This name is reserved.")
        taken = {m["name"].lower() for m in self.members(include_retired=True)}
        if name.lower() in taken:
            raise ToolError(f"The name '{name}' is already taken in this swarm; choose another one.")
        return name

    def _create_member(self, by: str, role: str, instructions: str = "", model: str | None = None,
                       name: str | None = None, language: str | None = None, kind: str = AgentKind.LLM) -> str:
        role = roles.normalize(role)
        if not role:
            raise ToolError("The role is empty.")
        language = language or self.language
        if language not in ("ru", "en"):
            raise ToolError("language must be 'ru' or 'en'.")
        # a catalog role (docs/SPEC.md §17.3) is stored by its canonical name in the member's language ("tester" ->
        # "Tester"/«Тестировщик») and brings its description, instructions and model profile; any other name is a
        # new role that only the hirer's instructions describe
        entry = roles.find_role(self.config.roles, role, language)
        if entry is not None:
            role = entry.name
        profile_name = None
        if kind == AgentKind.LLM:
            # model precedence: the explicit profile > the role's > agents.default_model
            wanted = model or (entry.model if entry else None)
            if not model and wanted and wanted not in self.config.models:
                # the settings validation forbids it; a missing profile must not make the role unhirable
                log.warning("role '%s' uses the missing model profile '%s'; using the default", role, wanted)
                wanted = None
            try:
                profile_name = self.config.model(wanted).name
            except KeyError:
                profiles = ", ".join(self.config.models)
                raise ToolError(f"Unknown model profile '{wanted}'. Profiles: {profiles}") from None
        if name:
            name = self._validate_name(name)
        else:
            taken = {m["name"] for m in self.members(include_retired=True)} | {USER}
            name = generate_name(taken, language)
        system = ""
        if kind == AgentKind.LLM:
            # the catalog's instructions of the role come before the hirer's; the agents row keeps the hirer's own
            extra = entry.instructions if entry else ""
            system = prompts.system_prompt(
                language, name=name, swarm=self.config.swarm.name, role=role,
                role_description=entry.description if entry else "",
                instructions="\n\n".join(t for t in (extra, instructions.strip()) if t),
                shell=resolve_shell(self.config.shell.program)[0], max_agents=self.limits()["max_agents"],
                scratch=scratch_rel(name), foreground_max=self.config.shell.foreground_max,
            )
        now = time.time()
        self.db.execute(
            "INSERT INTO agents(name, kind, role, instructions, model, language, status, hired_by, created_at,"
            " last_active, system_prompt) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, kind, role, instructions.strip(), profile_name, language, AgentStatus.IDLE, by, now, now, system),
        )
        self.bus.emit("agent.hired", by, name=name, role=role, kind=kind, model=profile_name, hired_by=by)
        return name

    async def hire(self, by: str, role: str, instructions: str = "", model: str | None = None,
                   name: str | None = None, language: str | None = None, kind: str = AgentKind.LLM) -> str:
        if kind not in (AgentKind.LLM, AgentKind.EXTERNAL):
            raise ToolError("kind must be 'llm' or 'external'.")
        if by != USER and not self.is_member(by):
            raise ToolError("Only swarm members or the user can hire.")
        limits = self.limits()
        # max_agents limits the AI agents: external members (MCP clients that joined) neither count nor are limited
        if kind == AgentKind.LLM and self.agent_count() >= limits["max_agents"]:
            raise ToolError(f"The swarm already has the maximum of {limits['max_agents']} AI agents (members who "
                            "joined through MCP do not count). Retire someone or ask the user to raise max_agents.")
        if kind == AgentKind.LLM and self.budget_exceeded():
            raise ToolError("The budget is exhausted; hiring is not possible.")
        name = self._create_member(by, role, instructions, model, name, language, kind)
        role = (self.member(name) or {}).get("role") or role  # canonical: "tester" -> "Tester"
        # its role's skills before it is woken: its first wake-up lists them (hire_agent reports them)
        self.skills.note_hire(name, await self.skills.ensure_role_skills([name]))
        if by != USER:
            self.notify(USER, "u_member_joined", name=name, role=role, by=by, wake=False)
        if kind == AgentKind.LLM:
            self.notify(name, "n_hired", by=by, role=role)
            if self._started and self._run_agents:
                self._start_runtime(name)
        return name

    async def retire(self, by: str, name: str, reason: str = "") -> list[int]:
        name = self.resolve_member(name, allow_user=False)
        member = self.member(name)
        if by not in (USER, name) and member.get("hired_by") != by:
            raise ToolError(f"Only the user, {name} or whoever hired them ({member.get('hired_by')}) can retire {name}.")
        # before the first await: no command of the member may become a job while it leaves (D24)
        jobs = [j.id for j in self.jobs.running_of(name)]
        self.jobs.forbid(name)
        released = self.board.release_all(name, by)
        self.workspace.release_all(name)
        self.db.execute("UPDATE triggers SET active = 0 WHERE owner = ?", (name,))
        self.db.execute("UPDATE agents SET status = ? WHERE name = ?", (AgentStatus.RETIRED, name))
        self.bus.emit("agent.retired", by, name=name, reason=reason, released=released, jobs=jobs)
        hirer = member.get("hired_by")
        if hirer and hirer not in (USER, by, name) and self.is_member(hirer):
            self.notify(hirer, "n_member_retired", name=name, reason=reason or "-",
                        tasks=", ".join(f"#{i}" for i in released) or "-")
        runtime = self.runtimes.pop(name, None)
        if runtime is not None:
            if runtime._task is asyncio.current_task():
                runtime.request_stop()
            else:
                await runtime.stop()  # an LLM agent's foreground command dies with its cancellation
        # its jobs and its foreground commands (an external member's call in flight): stopped, committed under its
        # name, no notice
        await self.jobs.stop_owner(name, "retired")
        return released

    # ---- budget & control ---------------------------------------------------------------------
    def limits(self) -> dict:
        stored = self.db.get("limits") or {}
        return {
            "max_cost_usd": float(stored.get("max_cost_usd", self.config.swarm.max_cost_usd)),
            "max_agents": int(stored.get("max_agents", self.config.swarm.max_agents)),
        }

    def set_limits(self, actor: str, max_cost_usd: float | None = None, max_agents: int | None = None) -> dict:
        limits = self.limits()
        if max_cost_usd is not None:
            if max_cost_usd < 0:
                raise ToolError("max_cost_usd must be >= 0 (0 = unlimited).")
            limits["max_cost_usd"] = float(max_cost_usd)
        if max_agents is not None:
            if max_agents < 1:
                raise ToolError("max_agents must be at least 1.")
            limits["max_agents"] = int(max_agents)
        self.db.set("limits", limits)
        self.bus.emit("swarm.limits", actor, **limits)
        if self.paused and self.pause_reason == "budget" and not self.budget_exceeded():
            self.resume(actor)
        return limits

    def spent_usd(self) -> float:
        return float(self.db.scalar("SELECT COALESCE(SUM(cost_usd), 0) FROM agents") or 0.0)

    def record_usage(self, agent: str, usage: Usage, profile: ModelProfile, model: str | None = None) -> float:
        cost = cost_of(profile, usage, model)
        self.db.execute(
            "UPDATE agents SET cost_usd = cost_usd + ?, tokens_in = tokens_in + ?, tokens_out = tokens_out + ?,"
            " last_active = ? WHERE name = ?",
            (cost, usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens, usage.output_tokens,
             time.time(), agent),
        )
        self.bus.emit("agent.usage", agent, agent=agent, cost=cost, input_tokens=usage.input_tokens,
                      output_tokens=usage.output_tokens, cache_read_tokens=usage.cache_read_tokens,
                      spent=self.spent_usd())
        return cost

    def budget_exceeded(self) -> bool:
        limit = self.limits()["max_cost_usd"]
        return limit > 0 and self.spent_usd() >= limit

    @property
    def started(self) -> bool:
        """start() ran and stop() did not: a loaded swarm that is only viewed is not started."""
        return self._started

    @property
    def paused(self) -> bool:
        return bool((self.db.get("paused") or {}).get("paused"))

    @property
    def pause_reason(self) -> str:
        return (self.db.get("paused") or {}).get("reason", "")

    def pause(self, actor: str, reason: str) -> None:
        if self.paused:
            return
        self.db.set("paused", {"paused": True, "reason": reason})
        self.bus.emit("swarm.paused", actor, reason=reason)
        if reason == "budget":
            self.notify(USER, "u_budget", spent=self.spent_usd(), limit=self.limits()["max_cost_usd"], wake=False)

    def resume(self, actor: str) -> None:
        if not self.paused:
            return
        if self.budget_exceeded():
            raise ToolError("The budget is exhausted: raise max_cost_usd before resuming.")
        self.db.set("paused", {"paused": False, "reason": ""})
        self.bus.emit("swarm.resumed", actor)
        for m in self.members():
            if m["kind"] == AgentKind.LLM:
                runtime = self.runtimes.get(m["name"])
                # an agent whose activation the pause cut short continues it even with nothing pending
                if (self.board.open_tasks_of(m["name"]) or self.board.reviews_of(m["name"])
                        or self.has_pending(m["name"]) or (runtime is not None and runtime.interrupted)):
                    self.notify(m["name"], "n_resumed")
                elif runtime is not None:
                    runtime.wake()  # paused -> idle

    @property
    def wrapping_up(self) -> bool:
        """The user's wrap-up mode: the team finishes with what it has. Every wake-up says so (render_wake), and no
        command of a member becomes a new background job (jobs.py). Nothing is paused."""
        return bool((self.db.get("wrap_up") or {}).get("on"))

    def wrap_up(self, actor: str) -> bool:
        """Switch wrap-up mode on; every AI agent is told once (n_wrap_up). False when it was on already."""
        if self.wrapping_up:
            return False
        self.db.set("wrap_up", {"on": True, "by": actor, "since": time.time()})
        self.bus.emit("swarm.wrap_up", actor, on=True)
        for m in self.members():
            if m["kind"] == AgentKind.LLM:
                self.notify(m["name"], "n_wrap_up")
        return True

    def end_wrap_up(self, actor: str) -> bool:
        """Switch wrap-up mode off (the user, a new or reopened goal): a quiet notice for the next wake-up. False when
        it was not on."""
        if not self.wrapping_up:
            return False
        self.db.set("wrap_up", {"on": False})
        self.bus.emit("swarm.wrap_up", actor, on=False)
        for m in self.members():
            if m["kind"] == AgentKind.LLM:
                self.notify(m["name"], "n_wrap_up_end", wake=False)
        return True

    def make_llm(self, profile_name: str | None, agent: str) -> LLMClient:
        if profile_name and profile_name not in self.config.models:
            # the member's profile (agents.model) was renamed or deleted in the settings: an error here would
            # crash the agent at every activation, so it falls back to the default profile
            if profile_name not in self._missing_profiles:
                self._missing_profiles.add(profile_name)
                log.warning("model profile '%s' no longer exists; %s uses the default profile '%s'",
                            profile_name, agent, self.config.agents.default_model)
            profile_name = None
        profile = self.config.model(profile_name)
        if self._llm_factory is not None:
            return self._llm_factory(profile, agent)
        return create_llm(profile)

    # ---- scheduler ----------------------------------------------------------------------------
    async def _scheduler(self) -> None:
        while True:
            await asyncio.sleep(SCHEDULER_SECONDS)
            try:
                self.workspace.expire_locks()
                await self.skills.check()  # a mirror copy changed by a command is rebuilt (at most once a minute)
                await self.skills.touch_envs()  # hourly: the environments' `used` stamps, retries, repairs
                if time.time() - self._last_prune >= TRACE_PRUNE_SECONDS:
                    self._prune_traces()
                # an external member that stopped calling: its session trace ends at its last call
                self.tracer.close_external(idle_seconds=EXTERNAL_IDLE_SECONDS)
                if not self.paused:
                    self._wake_deferred()
                    self._nudge_idle()
                    self._wake_stalled()
                    self._wake_idle_holders()
                    self._remind_reviewers()
                    self._check_all_done()
                    self._remind_user_reviews()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scheduler tick failed")

    def _idle_llm_members(self) -> list[dict]:
        """LLM agents that sleep with nothing pending and are not busy: a member whose command went on as a job it
        waits for (JobManager.busy) is at work, so it gets no nudge, reminder or hand-over meanwhile."""
        return [m for m in self.members()
                if m["kind"] == AgentKind.LLM and m["status"] == AgentStatus.IDLE
                and m["name"] in self.runtimes and not self.has_pending(m["name"]) and not self.jobs.busy(m["name"])]

    def _wake_deferred(self) -> None:
        """A broadcast reached an agent without a wake while it waited for its own job (defer_wake): if that wait
        ended without a wake (the job stopped being busy, e.g. past its busy window, or its end is quiet now), the
        news must not sit unread: wake it now."""
        for name in list(self._deferred):
            if name not in self.runtimes or not self.has_pending(name):
                self._deferred.discard(name)  # shown already (or the member left)
            elif not self.waits_for_job(name):
                self._deferred.discard(name)
                self.signal(name)

    def _nudge_idle(self) -> None:
        ready = self.board.ready_unassigned()
        ready_ids = {t.id for t in ready}
        self._nudged = {k: v for k, v in self._nudged.items() if k in ready_ids}
        goal = self.goal()
        if not ready or not goal or goal.get("status") != "active":
            return
        now = time.time()
        idle = self._idle_llm_members()
        idle_names = {m["name"] for m in idle}
        for name, (since, owned_before, offered) in list(self._considering.items()):
            decided = (name in idle_names or name not in self.runtimes or now - since > NUDGE_DECISION_SECONDS
                       or not offered & ready_ids  # what it was offered is taken
                       or bool({t.id for t in self.board.open_tasks_of(name)} - owned_before))
            if decided:
                del self._considering[name]
        if len(self._considering) >= len(ready):
            return  # every ready task is already being considered by someone
        independent = self.board.independent_review
        for m in sorted(idle, key=lambda m: m.get("last_active") or 0):
            name = m["name"]
            if now - self._last_nudge.get(name, 0) < self.config.agents.idle_nudge_seconds:
                continue
            if independent and self.board.reviews_of(name):
                continue  # its reviews come first: it is reminded of them (_remind_reviewers), not offered more work
            fresh = [t for t in ready if name not in self._nudged.get(t.id, set())][:6]
            if not fresh:
                continue
            owned = frozenset(t.id for t in self.board.open_tasks_of(name))
            self.notify(name, "n_ready", tasks="; ".join(f"#{t.id} {t.title}" for t in fresh))
            for t in fresh:
                self._nudged.setdefault(t.id, set()).add(name)
            self._last_nudge[name] = now
            self._considering[name] = (now, owned, frozenset(t.id for t in fresh))
            break

    def _wake_stalled(self) -> None:
        """An agent that stopped with plain text holding in-progress tasks even after the in-activation nudge
        is idle with nothing pending, so no event would ever wake it: wake it once per such set of tasks (not while
        the end of one of its jobs will wake it)."""
        now = time.time()
        for m in self._idle_llm_members():
            name = m["name"]
            runtime = self.runtimes.get(name)
            stalled = getattr(runtime, "stalled", frozenset())
            if not stalled or now - runtime.stalled_at < self.config.agents.idle_nudge_seconds:
                continue
            if self.jobs.awaited(name):
                continue  # the end of its job will wake it: it waits for that job, it has not stalled
            runtime.stalled = frozenset()
            still = [t for t in self.board.open_tasks_of(name) if t.status == Status.IN_PROGRESS and t.id in stalled]
            if not still or self._stall_woken.get(name) == stalled:
                continue
            self._stall_woken[name] = stalled
            self.notify(name, "n_stalled", tasks=", ".join(f"#{t.id}" for t in still))

    def _wake_idle_holders(self) -> None:
        """An agent that sleeps (idle, not busy, nothing pending) while it holds in-progress tasks, e.g. after
        `wait` with nothing that will wake it, would sleep until some unrelated event: after
        agents.idle_nudge_seconds it is woken once per such set of tasks (n_stalled, shared with _wake_stalled, so
        a set is never woken about twice). The clock starts when it was last seen going idle with that set. A
        member whose running job's end will wake it (JobManager.awaited, what `wait` promised it) is waiting for
        that job: no clock runs for it meanwhile."""
        now, wait = time.time(), self.config.agents.idle_nudge_seconds
        holding: dict[str, tuple[frozenset[int], float]] = {}
        for m in self._idle_llm_members():
            name = m["name"]
            held = frozenset(t.id for t in self.board.open_tasks_of(name) if t.status == Status.IN_PROGRESS)
            if not held or self.jobs.awaited(name):
                continue
            key, since = self._idle_holding.get(name, (None, now))
            if key != held:
                since = now
            # an activation between two ticks (woken and idle again) restarts the clock: last_active is when it
            # last went idle
            since = max(since, float(m.get("last_active") or 0))
            holding[name] = (held, since)
            if now - since < wait or self._stall_woken.get(name) == held:
                continue
            self._stall_woken[name] = held
            self.notify(name, "n_stalled", tasks=", ".join(f"#{i}" for i in sorted(held)))
        self._idle_holding = holding

    def _remind_reviewers(self) -> None:
        """Liveness of reviews: an idle LLM reviewer whose reviews wait longer than agents.idle_nudge_seconds is
        reminded once per set of task ids; when the same set still waits idle_nudge_seconds after the reminder
        (or the reviewer has been in error that long), the reviews are handed to another member or to the user.
        A review never goes back to a member it was taken from, so two idle members cannot pass it back and forth:
        once every agent let it lapse, it is the user's."""
        if not self.board.independent_review:
            return
        reviews: dict[str, list] = {}  # reviewer -> its tasks in review (one query for the whole team)
        for t in self.board.list(status=Status.REVIEW):
            reviews.setdefault(t.reviewer or "", []).append(t)
        in_review = {t.id for tasks in reviews.values() for t in tasks}
        self._review_lapsed = {k: v for k, v in self._review_lapsed.items() if k in in_review}
        if not in_review:
            self._review_reminded.clear()
            return
        now, wait = time.time(), self.config.agents.idle_nudge_seconds
        idle = {m["name"] for m in self._idle_llm_members()}
        for m in self.members():
            if m["kind"] != AgentKind.LLM:
                continue
            name = m["name"]
            due = [t for t in reviews.get(name, []) if now - (t.updated_at or 0) >= wait]
            key = frozenset(t.id for t in due)
            seen = self._review_reminded.get(name)
            if not key:
                self._review_reminded.pop(name, None)
                continue
            if seen is not None and seen[0] == key and self.jobs.busy(name):
                # e.g. its test run went on as a job: the hand-over clock restarts when it stops being busy
                self._review_reminded[name] = (key, now)
                continue
            if seen is None or seen[0] != key:
                if m["status"] == AgentStatus.ERROR:
                    self._review_reminded[name] = (key, now)  # it cannot act: start the clock only
                elif name in idle:
                    self._review_reminded[name] = (key, now)
                    self.notify(name, "n_review_reminder", tasks="; ".join(f"#{t.id} {t.title}" for t in due))
                continue
            stuck = ((m["status"] == AgentStatus.ERROR and now - seen[1] >= 2 * wait)
                     or (name in idle and now - seen[1] >= wait))
            if stuck:
                for t in due:
                    lapsed = self._review_lapsed.setdefault(t.id, set())
                    lapsed.add(name)
                    self.board.hand_over_review(t.id, exclude=lapsed)
                self._review_reminded.pop(name, None)

    def _remind_user_reviews(self) -> None:
        """Only the user's reviews keep the swarm going: tell the user once per set of such tasks."""
        goal = self.goal()
        reviews = self.board.list(status=Status.REVIEW) if goal and goal.get("status") == "active" else []
        waiting = [t for t in reviews if t.reviewer == USER]
        if not waiting:
            self._user_reviews_key = None
            return
        llm = [m for m in self.members() if m["kind"] == AgentKind.LLM]
        actionable = [t for t in self.board.list(status=Status.TODO)  # claimable or assigned, not blocked
                      if not t.blocked_by and t.assignee != USER]
        # a member busy with a job (its command went on in the background) is at work like a WORKING one
        if (any(m["status"] == AgentStatus.WORKING or self.has_pending(m["name"]) or self.jobs.busy(m["name"])
                for m in llm)
                or actionable or self.board.list(status=Status.IN_PROGRESS)
                or any(t.reviewer != USER for t in reviews)):
            return
        key = frozenset(t.id for t in waiting)
        if key != self._user_reviews_key:
            self._user_reviews_key = key
            self.notify(USER, "u_reviews_waiting", tasks=", ".join(f"#{t.id}" for t in waiting))

    def _check_all_done(self) -> None:
        goal = self.goal()
        if not goal or goal.get("status") != "active":
            return
        counts = self.board.counts()
        total = sum(counts.values())
        if total == 0 or any(counts.get(s, 0) for s in Status.OPEN):
            self._all_done_key = None
            return
        members = self.members()
        if any(m["status"] == AgentStatus.WORKING for m in members):
            return
        # a command that went on as a job may still change the result (with no open task only timeout jobs count)
        if any(m["kind"] == AgentKind.LLM and self.jobs.busy(m["name"]) for m in members):
            return
        key = (total, self.db.scalar("SELECT MAX(updated_at) FROM tasks"))
        if key == self._all_done_key:
            return
        idle = sorted(self._idle_llm_members(), key=lambda m: m.get("last_active") or 0, reverse=True)
        if not idle:
            return
        self._all_done_key = key
        # wrapping up: the goal is closed with what there is, no new tasks for what is missing
        self.notify(idle[0]["name"], "n_all_done_wrap_up" if self.wrapping_up else "n_all_done")

    # ---- snapshot -----------------------------------------------------------------------------
    def workspace_info(self) -> dict:
        """The folder the members work in: {"path", "default" (the project's own workspace/), "adopted" (a
        folder of the user that a session adopted), "exists"} (the same shape as a session summary's)."""
        root = self.workspace.root
        return {"path": str(root), "default": self.config.workspace_path is None,
                "adopted": bool(self.config.workspace_adopted), "exists": root.is_dir()}

    def snapshot(self) -> dict:
        # Polled by the UI after every event: a fixed number of queries, whatever the team size.
        tasks = self.board.list(include_closed=True)
        open_tasks: dict[str, list[int]] = {}
        counts = {s: 0 for s in Status.ALL}
        for t in tasks:
            counts[t.status] = counts.get(t.status, 0) + 1
            if t.assignee and t.status in Status.OPEN:
                open_tasks.setdefault(t.assignee, []).append(t.id)
        unread = {r["recipient"]: r["n"] for r in self.db.all(
            "SELECT recipient, COUNT(*) AS n FROM inbox WHERE read_at IS NULL GROUP BY recipient")}
        jobs = self.jobs.snapshot()  # the running ones (from memory), then those ended in the last hour (one query)
        running_jobs: dict[str, int] = {}
        for job in jobs:
            if job["status"] == "running":
                running_jobs[job["owner"]] = running_jobs.get(job["owner"], 0) + 1
        members = []
        for m in self.members(include_retired=True):
            members.append({
                "name": m["name"], "kind": m["kind"], "role": m["role"], "status": m["status"],
                "model": m.get("model"), "language": m.get("language"), "cost_usd": round(m.get("cost_usd") or 0, 4),
                "tokens_in": m.get("tokens_in") or 0, "tokens_out": m.get("tokens_out") or 0,
                "hired_by": m.get("hired_by"), "created_at": m.get("created_at"), "last_active": m.get("last_active"),
                "instructions": m.get("instructions") or "", "context_tokens": m.get("context_tokens") or 0,
                "open_tasks": open_tasks.get(m["name"], []),
                "unread": unread.get(m["name"], 0),
                "jobs": running_jobs.get(m["name"], 0),  # its running background jobs
            })
        pause = self.db.get("paused") or {}
        goal = self.goal()
        return {
            "swarm": {
                "name": self.config.swarm.name, "language": self.language, "paused": bool(pause.get("paused")),
                "pause_reason": pause.get("reason", ""), "wrap_up": self.wrapping_up, "limits": self.limits(),
                "agent_count": self.agent_count(),  # what limits.max_agents counts: external members do not
                "spent_usd": round(self.spent_usd(), 4), "goal": goal, "counts": counts,
                "mcp_url": self.hub_url, "models": list(self.config.models),
                "session": None if self.session_id is None else {
                    "id": self.session_id, "title": self.session_title or self.session_id,
                    "status": "running" if self._started else "stopped",
                    "pending_goal": None if goal else self.session_pending_goal},
                "tracing": bool(self.tracer.enabled),
                "independent_review": self.board.independent_review,
                "workspace": self.workspace_info(),
            },
            "tasks": [t.to_dict() for t in tasks],
            "agents": members,
            "triggers": self.triggers.list(include_inactive=False),
            "jobs": jobs,
            "mcp": self.forge.list(USER),
            "skills": self.skills.public(),  # from memory: no query
            "locks": self.workspace.locks(),
            "user": {"unread": unread.get(USER, 0)},
        }


__all__ = ["Swarm", "loads"]
