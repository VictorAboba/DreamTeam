"""Swarm: the composition root that owns storage, the board, mail, workspace, triggers, MCP forge
and the agent runtimes, and implements team, goal, budget and notification logic."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import difflib
import hashlib
import logging
import random
import time
from pathlib import Path
from typing import Any, Callable

from . import __version__, hubauth, netoff, procs, prompts, roles
from .agent import AgentRuntime
from .autoreply import LAST_TO, PLANS, PLANS_THREAD, planner_question
from .board import MARK_FREE_DIRECTIONS, Board
from .config import Config, ModelProfile
from .db import MODE_KEY, MODES, Database, dumps, loads, made_by_earlier_version, remap_sql, utf8_safe
from .events import EventBus
from .forum import (PLAN_POST, SETTLE_KEY, Forum, decision_of, goal_tally, names_or_dash, rule_name, user_summary,
                    vote_line)
from .goal_text import split_goal
from .jobs import JobManager, fmt_duration, short_command
from .llm import create_llm
from .llm.base import LLMClient, Usage, cost_of, uncounted_fields
from .mailbox import Mailbox
from .mcp_forge import MCPForge
from .models import USER, AgentKind, AgentStatus, Status, Task, ToolError
from .names import generate_name
from .shell import resolve_shell, truncate_middle
from .swarm_skills import SessionSkills, role_skill_pairs
from .tools.fmt import STAMP_SPAN, item_time, stamp_range, trim
from .tracing import EXTERNAL_IDLE_SECONDS, Tracer
from .triggers import TriggerEngine
from .workspace import Workspace, scratch_rel

log = logging.getLogger(__name__)

USER_ALIASES = {"user", "пользователь", "the user", "human", "человек"}
QUIET_KEY = "_quiet"  # notifications.data flag of a member notification sent with wake=False
# ... and of a digest item of the forum's delivery (notify(..., batch=True), thread sessions; docs/SPEC.md §22.3)
DIGEST_KEY = "_digest"
DIGEST_SHOWN = 30  # the wake-up's digest group shows at most this many rows (§22.6)
SCHEDULER_SECONDS = 3.0
TRACE_PRUNE_SECONDS = 6 * 3600
# An agent nudged about ready tasks is "considering" them until it claims a task, goes idle again or
# this much time passes. No more agents consider offers than there are ready tasks, so one free task
# does not wake the whole idle team at once.
NUDGE_DECISION_SECONDS = 300.0
# A crew task with a free place and nothing a member may take now (docs/SPEC.md §3 item 8, §4.2 "A free place"): its
# owner is told once its state has lasted this many times agents.idle_nudge_seconds, at most CREW_GAP_MAX times per
# owner and crew task (counted from the notifications table), naming at most CREW_GAP_IDLE_MAX free members
CREW_GAP_FACTOR = 10
CREW_GAP_MAX = 2
CREW_GAP_IDLE_MAX = 8
# The whole team asleep with work open and nothing set to wake the team (docs/SPEC.md §3 item 9): one idle member is
# woken once the state has lasted TEAM_ASLEEP_FACTOR x 2 ** min(sent, TEAM_ASLEEP_DOUBLINGS) x
# agents.idle_nudge_seconds (sent: the n_team_asleep notices under the current goal, from the notifications table), at
# most TEAM_ASLEEP_MAX times per board state
TEAM_ASLEEP_FACTOR = 5
TEAM_ASLEEP_DOUBLINGS = 5
TEAM_ASLEEP_MAX = 2
# A member's critical question (send_message pause_swarm; docs/SPEC.md §3 "Clarify mode")
QUESTION = "question"            # the pause reason of a member's critical question (send_message pause_swarm)
# the pause reason of the user's own pause (swarm_control action=pause, docs/SPEC.md §3 `_set_pause`, §12); the
# readable texts key it by this string (eventtext._reason, static/app.js "reason_paused by the user")
USER_PAUSE = "paused by the user"
QUESTION_CHARS = 4000            # the question and the answer as quoted to the other members (n_user_answer)
QUESTION_PREVIEW_CHARS = 600     # snapshot swarm.question.text
AUTO_REPLY_GRACE = 90.0          # s a `dreamteam -p` client with auto-reply still counts after its last sign
                                 # (> printmode.LOST_LIMIT, 60 s: a client that lost the hub gives up first)
# (sender, recipient, since): the newest message from sender that reached recipient (autoreply.py, which also holds
# the rule of a clarify planner that waits for the user: planner_question)
_LAST_TO = LAST_TO
_monotonic = time.monotonic      # the lease's clock; tests patch swarm._monotonic, never time.monotonic itself
# the refusal of a swarm whose database was made by an earlier version (db.made_by_earlier_version); {dir} is the
# project (or session) folder, {version} this version
PROJECT_EARLIER_VERSION_TEXT = ("The swarm in {dir} was made by an earlier version of DreamTeam and cannot be "
                                "opened by this one ({version}). Open it with the version that made it.")
# the liveness key of a member (docs/SPEC.md §3 items 2-3): (its stalled in_progress task ids, its marks as
# (task id, kind), Board.stall_marks); the two parts are compared apart, never sorted together
StallKey = tuple[frozenset[int], frozenset[tuple[int, str]]]
# the free-place key of a crew task (docs/SPEC.md §3 item 8): (its owner, its crew, the ids of its open directions,
# the ids of the held ones the notice names)
CrewGapKey = tuple[str, int, frozenset[int], frozenset[int]]
# item 1's ready tasks, each with its offer key (task id, finish state) in _nudged (docs/SPEC.md §4.2 "Offers")
Offers = list[tuple[Task, tuple[int, str | None]]]


class EarlierVersionError(ValueError):
    """The swarm's database was made by an earlier DreamTeam: it is not opened and nothing is written to it."""


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
        # before the logs folder and Database(...), whose schema would be written into the old file
        if made_by_earlier_version(config.data_dir / "swarm.db"):
            raise EarlierVersionError(PROJECT_EARLIER_VERSION_TEXT.format(dir=config.project_dir,
                                                                          version=__version__))
        self.config = config
        self.project_dir = config.project_dir
        (config.data_dir / "logs").mkdir(parents=True, exist_ok=True)
        self.db = Database(config.data_dir / "swarm.db")
        try:
            config = self.config = self._open_mode(config)
            self._init(config, llm_factory)
        except BaseException:  # e.g. a corrupt database: the connection must not stay open (Windows file locks)
            self.db.close()
            raise

    def _open_mode(self, config: Config) -> Config:
        """`config` with the database's mode (kv swarm_mode, docs/SPEC.md §22.1), right after Database(...): the
        first open writes it (a database with tasks, messages or events is board whatever the config says, an empty
        one gets config.swarm.mode); a stored mode this version does not know is a ValueError. A thread database
        gets its forum tables at every open."""
        self._mode_warned = False
        stored = self.db.get(MODE_KEY)
        if stored is None:
            used = any(self.db.one(f"SELECT 1 FROM {table} LIMIT 1") for table in ("tasks", "messages", "events"))
            stored = "board" if used else config.swarm.mode
            if stored not in MODES:
                raise ValueError(f"swarm.mode must be 'board' or 'thread', got {stored!r}")
            self.db.set(MODE_KEY, stored)
        if stored not in MODES:
            raise ValueError(f"the session's mode {stored!r} is unknown: it was made by a newer DreamTeam")
        self._mode: str = stored
        if stored == "thread":
            self.db.ensure_forum()
        return self._pin_mode(config)

    def _pin_mode(self, config: Config) -> Config:
        """`config` with swarm.mode = the session's mode: a config that names the other mode is ignored, with one
        warning per swarm (docs/SPEC.md §22.1)."""
        if config.swarm.mode == self._mode:
            return config
        if not self._mode_warned:
            self._mode_warned = True
            log.warning("the session's mode is %s, chosen when it was created; [swarm] mode is ignored", self._mode)
        return dataclasses.replace(config, swarm=dataclasses.replace(config.swarm, mode=self._mode))

    @property
    def mode(self) -> str:
        """The session's mode, "board" | "thread": the database's (kv swarm_mode), fixed at its creation."""
        return self._mode

    @property
    def is_thread(self) -> bool:
        return self._mode == "thread"

    @property
    def goal_rule(self) -> str:
        """The thread mode's goal rule, "democracy" | "veto" (config.swarm.goal_rule, read live; docs/SPEC.md §22.4);
        board sessions ignore it."""
        return self.config.swarm.goal_rule

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
        # (ready task id, its finish state) -> members offered it (docs/SPEC.md §4.2 "Offers"): a parent whose
        # subtasks have all finished gets a fresh key, so it is offered again to everyone
        self._nudged: dict[tuple[int, str | None], set[str]] = {}
        self._last_nudge: dict[str, float] = {}
        # agent -> (nudged at, its open task ids then, task ids offered)
        self._considering: dict[str, tuple[float, frozenset[int], frozenset[int]]] = {}
        self._all_done_key: tuple | None = None
        # agent -> the liveness key it was woken about: (stalled task ids, liveness marks), see _stall_key
        self._stall_woken: dict[str, StallKey] = {}
        # planning task id -> {holder, since (first seen holding it), steps (its runtime's llm_steps then), hints sent,
        # hinted (its llm_steps at the last hint)}: hint_planner; a thread session's one watch on #2's asker is "p2"
        self._planning_watch: dict[int | str, dict[str, Any]] = {}
        self._paused_at: float | None = None  # when this process paused the swarm: the hint's clock stops meanwhile
        # the auto-reply lease of a `dreamteam -p` client (models.AUTO_REPLY_HEADER; never persisted): its open event
        # streams, and the _monotonic() deadline of the grace after its last sign
        self._auto_reply_streams = 0
        self._auto_reply_until = 0.0
        # the hub answers for the user itself (hold_auto_reply: a v1 session with `answers: auto`), for this object's
        # whole life
        self._auto_reply_held = False
        # agent idle (not busy, nothing pending) holding in-progress tasks or with liveness marks -> (that liveness
        # key, idle with it since)
        self._idle_holding: dict[str, tuple[StallKey, float]] = {}
        # crew task id -> (its free-place key, in that state since): _tell_crew_gaps, rebuilt at each pass from
        # Board.crew_gaps like _idle_holding
        self._crew_gaps: dict[int, tuple[CrewGapKey, float]] = {}
        # crew task id -> the free-place key its owner was told about, or that reached the cap (never cleared, like
        # _stall_woken: a key that comes back is not told again)
        self._crew_gap_told: dict[int, CrewGapKey] = {}
        # the whole team asleep (_wake_team_asleep): (the board key, asleep with it since), dropped by a pass that does
        # not find the team asleep and by resume; (the board key, the n_team_asleep sent since it was first seen)
        self._team_asleep: tuple[tuple, float] | None = None
        self._team_asleep_woken: tuple[tuple, int] | None = None
        # a thread session's quiet check (_check_quiet, docs/SPEC.md §22.7): (the thread key, quiet with it since),
        # dropped like _team_asleep; (the thread key, the nudges sent since it was first seen, USER told for it)
        self._thread_quiet: tuple[tuple, float] | None = None
        self._thread_quiet_woken: tuple[tuple, int, bool] | None = None
        # external member -> time.time() of its last hub call but wait_for_events, stamped by hub_mcp.hub_call before
        # and after the call (team_asleep: one that called in the last EXTERNAL_IDLE_SECONDS may still act)
        self._external_seen: dict[str, float] = {}
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
        # the time of the last start() or resume(): a digest item ages from it at the earliest (digest_deadline)
        self._digest_floor = 0.0
        self._missing_profiles: set[str] = set()  # members' profiles gone from the settings (warned once)
        # (profile, provider, base_url, model, missing prices) whose $0 calls the user was told about since the start
        self._cost_warned: set[tuple[str, str, str, str, tuple[str, ...]]] = set()
        self.stopping = False  # stop() runs or ran: long polls (wait_for_events) end without touching the db
        self.hub_url: str | None = None
        self.board = Board(self)
        # the thread mode's posts, votes and subscriptions (docs/SPEC.md §22.2); None in a board session
        self.forum: Forum | None = Forum(self) if self.is_thread else None
        if self.forum is not None:
            self.bus.listen(self.forum._on_event)  # the file subscriptions' delivery (docs/SPEC.md §22.3)
        self.mail = Mailbox(self)
        self.workspace = Workspace(self)
        self.workspace.commit_events = self.is_thread  # files.committed (docs/SPEC.md §22.2)
        self.triggers = TriggerEngine(self)
        self.forge = MCPForge(self)
        self.jobs = JobManager(self)  # the background jobs of the members' shell commands
        self.skills = SessionSkills(self)  # the skills attached to this session (docs/SPEC.md §19)
        self.skills.load()

    # ---- lifecycle ----------------------------------------------------------------------------
    async def start(self, run_agents: bool = True) -> None:
        self.stopping = False
        self._cost_warned.clear()  # u_cost_unknown comes again once per start
        self.tracer.recover()  # traces left open by a previous process
        self.jobs.recover()  # jobs a previous process left running died with it: lost (quiet notices)
        self._prune_traces()
        # the isolation first: DreamTeam's git in init() already runs like the members' commands (hubgit.py)
        await self._prepare_offline()
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
        self._digest_floor = time.time()  # time spent stopped does not age a digest item (docs/SPEC.md §22.3)
        self._thread_quiet = self._thread_quiet_woken = None  # a restart ends the quiet period (§22.7)
        if self.forum is not None:
            # digest rows a batch released before the stop and nobody read are digest news again: a member whose
            # only news they are gets no n_restart, the next batch wakes it (docs/SPEC.md §22.3)
            self.db.execute(f"UPDATE notifications SET data = json_set(data, '$.{QUIET_KEY}', json('true')) "
                            f"WHERE delivered_at IS NULL AND json_extract(data, '$.{DIGEST_KEY}') IS NOT NULL "
                            f"AND json_extract(data, '$.{QUIET_KEY}') IS NULL")
        if run_agents:
            for m in self.members():
                if m["kind"] == AgentKind.LLM:
                    if m["status"] in (AgentStatus.WORKING, AgentStatus.ERROR, AgentStatus.PAUSED):
                        self.db.execute("UPDATE agents SET status = ? WHERE name = ?", (AgentStatus.IDLE, m["name"]))
                    self._start_runtime(m["name"])
        self._background.append(asyncio.create_task(self.triggers.run(), name="triggers"))
        self._background.append(asyncio.create_task(self._scheduler(), name="scheduler"))
        if self.forum is not None:
            # a rule change or a retire while the session was stopped may have met the vote on #1 (docs/SPEC.md
            # §22.4): decided before anyone is woken for it
            self.forum.rewrite_goal_rule()
            self.forum.recheck_goal(None)
        goal = self.goal()
        goal_active = bool(goal) and goal.get("status") == "active"
        if self.config.swarm.goal and not goal:
            self.set_goal(USER, self.config.swarm.goal, self.config.swarm.goal_description)
        elif goal_active and not existing and not self.paused:  # the goal was set before the very first start
            if self.forum is not None:  # its system posts were written by that set_goal; nobody was told yet
                self._kickoff_thread(USER, goal, True)
            else:
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
        if not self.config.swarm.clarify:
            # [swarm] clarify may have been switched off while the session was stopped: a clarify planner waiting for
            # the user is told once per card, a question pause is lifted (its n_resumed covers the skipped n_restart)
            self._clarify_switched_off(once=True)

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
                    # every member's held new files (docs/SPEC.md §7 "Held new files"), each under its owner, once
                    # a commit a stopped agent started has ended; never raises
                    if self.workspace.held():
                        await self.workspace.settle()
                        await self.workspace.commit_held(None, "when the session stopped")
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
        language, the session's mode and the agents' frozen system prompts stay; LLM clients are rebuilt on their
        next call."""
        config = self._pin_mode(config)  # the mode never changes on a running swarm (docs/SPEC.md §22.1)
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
        if self._started and not self.stopping and old.swarm.clarify and not config.swarm.clarify:
            self._clarify_switched_off()
        if self._started and not self.stopping and role_skill_pairs(config) != role_skill_pairs(old):
            self._attach_role_skills()
        if self._started and not self.stopping and netoff.is_off(config) != netoff.is_off(old):
            self._network_changed()
        if (self.forum is not None and self._started and not self.stopping
                and old.swarm.goal_rule != config.swarm.goal_rule):
            self._goal_rule_changed()

    def _goal_rule_changed(self) -> None:
        """The session's goal rule changed under a running thread swarm (docs/SPEC.md §22.4): #1's text names the new
        rule, and the votes as they stand are checked against it."""
        self.forum.rewrite_goal_rule()
        self.forum.recheck_goal(None)

    async def _prepare_offline(self) -> None:
        """A session without network (docs/SPEC.md §7.2): the isolation detected (off the loop: on Linux it starts a
        probe once per process) and, in the Docker sandbox, what root created in the workspace opened to the offline
        user (netoff.prepare_workspace; at a start before Workspace.init, which creates what is missing with the
        offline user's group); then, under "docker" or "netns", DreamTeam's git there runs isolated too
        (Workspace.git_isolated)."""
        if not netoff.is_off(self.config):
            return
        await procs.daemon_call(netoff.isolation)
        if netoff.detected() == "docker":
            failed = await procs.daemon_call(netoff.prepare_workspace, self.workspace.root)
            if failed:
                log.warning("%d entries of %s stay closed to the offline user", failed, self.workspace.root)
        # from now on DreamTeam's own git in the workspace runs like the members' commands (hubgit.py)
        self.workspace.git_isolated = netoff.detected() in ("docker", "netns")

    def _network_changed(self) -> None:
        """[swarm] network changed under a running session: commands started from now on get the new setting (running
        jobs keep theirs), the MCP servers start again with it at their next use, the skills' dependencies are
        planned again (a member's proposal gets no install without network) and each member is told at its next
        wake-up (_tell_network)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # not called from the event loop: the next start applies it
            return
        # the members' next commands are offline at once: DreamTeam's git waits until it may run isolated too (else
        # it would run as itself, with the network, beside them until _prepare_offline is done)
        held = [netoff.is_off(self.config)]
        if held[0]:
            self.workspace.hold_git()

        def release(_task: Any = None) -> None:
            if held[0]:
                held[0] = False
                self.workspace.release_git()

        async def apply() -> None:
            try:
                try:
                    await self._prepare_offline()
                finally:
                    release()
                await self.forge.close_connections()
                await self.skills.replan()
            except Exception:  # noqa: BLE001 - the setting itself is in place for the next commands
                log.exception("could not apply the new network setting")

        self._background = [t for t in self._background if not t.done()]
        task = loop.create_task(apply(), name="network-changed")
        task.add_done_callback(release)   # also when it is cancelled before it ran
        self._background.append(task)

    def _tell_network(self, name: str) -> None:
        """Tell a member once that the session has no network (n_network_off), and again when it has it back
        (n_network_on): the last of the two it got is the state it knows, also across restarts."""
        off = netoff.is_off(self.config)
        off_key = prompts.mode_key("n_network_off", self.mode)
        last = self.db.scalar("SELECT kind FROM notifications WHERE recipient = ? AND kind IN (?, 'n_network_on') "
                              "ORDER BY id DESC LIMIT 1", (name, off_key))
        if off and last != off_key:
            self.notify(name, off_key, wake=False)
        elif not off and last == off_key:
            self.notify(name, "n_network_on", wake=False)

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
    def notify(self, recipient: str, key: str, *, wake: bool = True, batch: bool = False, defer: bool = False,
               **kw: Any) -> None:
        """wake=False for a member: news for its next wake-up, which does not count as pending (has_pending)
        and so neither wakes it nor keeps the scheduler from offering it tasks. batch (with wake=False, never for
        USER): also a digest item (DIGEST_KEY) of the forum's delivery; defer (with wake=True, never for USER):
        waking but not signalled, defer_wake(recipient) instead (docs/SPEC.md §22.3)."""
        text = utf8_safe(prompts.t(self.language_of(recipient), key, **kw))  # may quote model output
        quiet = not wake and recipient != USER
        data = {**kw, QUIET_KEY: True, **({DIGEST_KEY: True} if batch else {})} if quiet else kw
        self.db.insert(
            "INSERT INTO notifications(ts, recipient, kind, text, data) VALUES (?, ?, ?, ?, ?)",
            (time.time(), recipient, key, text, dumps(data)),
        )
        self.bus.emit("notification", None, recipient=recipient, kind=key, text=text)
        if wake and defer and recipient != USER:
            self.defer_wake(recipient)  # its job's end shows it, else the scheduler's _wake_deferred
        elif wake:
            self.signal(recipient)
        elif quiet and batch and recipient not in self.runtimes:
            # an external member's wait_for_events caps its sleep at its digest_deadline: it computes it again
            self._signals.setdefault(recipient, asyncio.Event()).set()

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

    def digest_deadline(self, name: str) -> float | None:
        """When the oldest undelivered digest item of `name` other than a vote falls due: max(ts, _digest_floor) +
        [agents] digest_seconds; None while the goal is not active or there is none (docs/SPEC.md §22.3)."""
        if not self.is_thread:
            return None
        goal = self.goal()
        if not goal or goal.get("status") != "active":
            return None
        first = self.db.scalar(f"SELECT MIN(ts) FROM notifications WHERE recipient = ? AND delivered_at IS NULL AND "
                               f"json_extract(data, '$.{DIGEST_KEY}') IS NOT NULL AND kind != 'f_vote'", (name,))
        return None if first is None else max(float(first), self._digest_floor) + self._digest_seconds()

    def _digest_seconds(self) -> float:
        """[agents] digest_seconds, read at each use (at least 10)."""
        return max(10.0, float(self.config.agents.digest_seconds))

    def _due_digests(self, now: float) -> list[str]:
        """The members whose digest_deadline has passed (one query for the team; the caller checks the goal)."""
        rows = self.db.all(f"SELECT recipient, MIN(ts) AS first FROM notifications WHERE delivered_at IS NULL AND "
                           f"json_extract(data, '$.{DIGEST_KEY}') IS NOT NULL AND kind != 'f_vote' GROUP BY recipient")
        seconds = self._digest_seconds()
        return [r["recipient"] for r in rows if now >= max(float(r["first"]), self._digest_floor) + seconds]

    def _release_digest(self, name: str) -> None:
        """A due digest becomes waking news: `_quiet` dropped from every undelivered digest row of `name` (its votes
        ride along; `_digest` stays, so render_wake shows them in the digest group), then signal(name)."""
        self.db.execute(f"UPDATE notifications SET data = json_remove(data, '$.{QUIET_KEY}') WHERE recipient = ? AND "
                        f"delivered_at IS NULL AND json_extract(data, '$.{DIGEST_KEY}') IS NOT NULL", (name,))
        self.signal(name)

    def _release_settled(self, now: float) -> list[str]:
        """Every undelivered waking f_file whose run of changes has settled (no new change for [agents]
        file_settle_seconds, or file_max_wait_seconds (0: no cap) since the first) loses `_quiet` and `_settle`,
        and its member is signalled (docs/SPEC.md §22.3 "Settling"); returns those members."""
        rows = self.db.all(f"SELECT n.id, n.recipient, json_extract(n.data, '$.{SETTLE_KEY}.first') AS first, "
                           f"json_extract(n.data, '$.{SETTLE_KEY}.last') AS last FROM notifications n "
                           f"JOIN agents a ON a.name = n.recipient AND a.status != 'retired' "
                           f"WHERE n.delivered_at IS NULL AND json_extract(n.data, '$.{SETTLE_KEY}') IS NOT NULL")
        if not rows:
            return []
        quiet = max(0.0, float(self.config.agents.file_settle_seconds))
        cap = max(0.0, float(self.config.agents.file_max_wait_seconds))
        settled = [r for r in rows if now - float(r["last"] or 0) >= quiet
                   or (cap > 0 and now - float(r["first"] or 0) >= cap)]
        for row in settled:
            self.db.execute(f"UPDATE notifications SET data = json_remove(data, '$.{QUIET_KEY}', '$.{SETTLE_KEY}') "
                            "WHERE id = ?", (row["id"],))
        names = list(dict.fromkeys(r["recipient"] for r in settled))
        for name in names:
            self.signal(name)
        return names

    def _deliver_digests(self) -> None:
        """T1 of a thread session's pass (docs/SPEC.md §22.7): the settled file items released, whatever the goal's
        state; then, while the goal is active and no staggered notice is due (_timers empty), each idle AI agent
        (_idle_llm_members) whose digest is due woken once with all it has gathered. The scheduler calls it unless
        paused."""
        now = time.time()
        self._release_settled(now)
        goal = self.goal()
        if not goal or goal.get("status") != "active" or self._timers:
            return
        due = set(self._due_digests(now))
        if not due:
            return
        for m in self._idle_llm_members():
            if m["name"] in due:
                self._release_digest(m["name"])

    def team_asleep(self, caller: str | None = None) -> bool:
        """Nobody in the team can act or be woken by something already set (docs/SPEC.md §3 `team_asleep`; scheduler
        item 9, `wait`'s result): every LLM member with a runtime is idle (_idle_llm_members: nothing pending, not
        busy) or in error with no recovery scheduled, at least one idle; none waits for a job whose end wakes it or for
        a file it was refused; no delayed notification is pending; no trigger may fire unattended
        (TriggerEngine.unattended); no external member called the hub in the last EXTERNAL_IDLE_SECONDS. `caller`: the
        member calling `wait`, still working, counts as idle with nothing pending. A thread session (§22.7): also not
        while the goal is active and an idle LLM member other than `caller` holds an undelivered digest row other than
        a vote (a batch is about to wake it), nor while any member holds an undelivered `_settle` row."""
        if self._timers:  # staggered kickoffs not delivered yet
            return False
        members = self.members()
        idle = False
        idle_names: list[str] = []
        for m in members:
            name = m["name"]
            if m["kind"] != AgentKind.LLM or name not in self.runtimes:
                continue
            if name == caller or (m["status"] == AgentStatus.IDLE and not self.has_pending(name)
                                  and not self.jobs.busy(name)):
                idle = True
                if name != caller:
                    idle_names.append(name)
            elif m["status"] != AgentStatus.ERROR or getattr(self.runtimes[name], "_recovery", None) is not None:
                return False  # at work, with news, busy, paused, or in error with a retry on its way
            if self.jobs.awaited(name) or self.workspace.waits_for_lock(name):
                return False
        if not idle:
            return False
        if self.is_thread and self._thread_news(idle_names):
            return False
        if self.triggers.unattended(self.triggers.clock()):  # the clock the engine stamps its triggers with
            return False
        now = time.time()  # hub_call's stamps
        externals = {m["name"] for m in members if m["kind"] == AgentKind.EXTERNAL}
        return not any(now - seen < EXTERNAL_IDLE_SECONDS for name, seen in self._external_seen.items()
                       if name in externals)

    def _thread_news(self, idle: list[str]) -> bool:
        """team_asleep's thread clauses (docs/SPEC.md §22.7): a member of `idle` holds an undelivered digest row other
        than a vote while the goal is active (T1 wakes it), or any member holds an undelivered `_settle` row (T1
        releases it once its changes settle). Members in error are not in `idle`: their digest keeps nobody awake."""
        if self.db.scalar(f"SELECT 1 FROM notifications n JOIN agents a ON a.name = n.recipient AND a.status != "
                          f"'retired' WHERE n.delivered_at IS NULL AND json_extract(n.data, '$.{SETTLE_KEY}') IS NOT "
                          f"NULL LIMIT 1"):
            return True
        goal = self.goal()
        if not idle or not goal or goal.get("status") != "active":
            return False
        return bool(self.db.scalar(
            f"SELECT 1 FROM notifications WHERE delivered_at IS NULL AND json_extract(data, '$.{DIGEST_KEY}') IS NOT "
            f"NULL AND kind != 'f_vote' AND recipient IN ({','.join('?' * len(idle))}) LIMIT 1", tuple(idle)))

    def stall_ids(self, name: str) -> frozenset[int]:
        """The in_progress task ids of `name` that count as stalled (_stall_ids: scheduler items 2-3, `wait`'s result,
        docs/SPEC.md §3); always empty in a thread session (§22.7)."""
        if self.is_thread:
            return frozenset()
        return self._stall_ids(name)

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
        if name != USER:
            self._tell_network(name)  # once per change of the session's network setting (docs/SPEC.md §7.2)
            # the fires its repeating triggers held back ([agents] trigger_wake_seconds), quietly: it reads its news now
            self.triggers.release_held(name)
        rows = self.db.all("SELECT id, ts, kind, text, data FROM notifications WHERE recipient = ? AND "
                           "delivered_at IS NULL ORDER BY id", (name,))
        messages = self.mail.unread(name, mark_read=True)
        if incoming and not rows and not messages:
            return None
        now = time.time()
        if rows:
            self.db.execute(
                f"UPDATE notifications SET delivered_at = ? WHERE id IN ({','.join('?' * len(rows))})",
                [now] + [n["id"] for n in rows],
            )
        # the digest rows form their own group (a thread session's, docs/SPEC.md §22.6 item 5): other rows first,
        # votes last, each part in (ts, id) order; every one is marked delivered above, the ones not shown too
        digest = sorted((n for n in rows if (loads(n["data"]) or {}).get(DIGEST_KEY)),
                        key=lambda n: (n["kind"] == "f_vote", n["ts"], n["id"]))
        in_digest = {n["id"] for n in digest}
        notes = [n for n in rows if n["id"] not in in_digest]
        first = None
        if stamps:
            times = [t for t in [*(n["ts"] for n in rows), *(m["ts"] for m in messages), since] if t]
            if times and now - min(times) > STAMP_SPAN:
                first = min(times)

        def at(ts: float | None) -> str:
            return f"{item_time(ts, now)} " if first is not None and ts else ""

        thread = self.is_thread
        lines = [prompts.t(lang, "incoming_header" if incoming else "wake_header", time=stamp_range(first, now))]
        if not incoming and self.wrapping_up:
            lines.append(prompts.t(lang, prompts.mode_key("wrap_up_line", self.mode)))
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
                lines.append(prompts.t(lang, prompts.mode_key("all_hint", self.mode)))
        if digest:  # incoming renders too: a working member gets its digest with its next news
            lines.append(prompts.t(lang, "digest"))
            lines.extend(f"• {at(n['ts'])}{n['text']}" for n in digest[:DIGEST_SHOWN])
            if len(digest) > DIGEST_SHOWN:
                lines.append(prompts.t(lang, "digest_more", n=len(digest) - DIGEST_SHOWN))
        if not incoming:
            goal = self.goal()
            if goal:
                lines.append(prompts.t(lang, "goal", title=goal["title"], status=goal.get("status", "active")))
                tally = self.forum.goal_vote() if thread and goal.get("status") == "active" else None
                if tally is not None:  # the vote on #1 under the session's rule (docs/SPEC.md §22.6 item 6)
                    lines.append(vote_line(lang, tally))
            else:
                lines.append(prompts.t(lang, "goal_none"))
            member = self.member(name) or {}
            limit = self.limits()["max_cost_usd"]
            if member.get("kind") == AgentKind.LLM and limit > 0:  # what is spent, next to the goal (§6.3)
                spent = self.spent_usd()
                lines.append(prompts.t(lang, "money", spent=spent, limit=limit,
                                       pct=min(round(100 * spent / limit), 99)))
            if not thread:  # a thread session has no tasks: the board's queries are not run (§22.6)
                mine = self.board.open_tasks_of(name)
                lines.append(prompts.t(lang, "my_tasks", tasks="; ".join(t.short() for t in mine))
                             if mine else prompts.t(lang, "my_tasks_none"))
                # a held task blocked by unfinished ones: its assignee may drop what it can do without
                # (Board._plan_deps); not in review, where the result is already produced
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
            if not thread:
                busy = self.board.busy_crews(name)
                ready = [t for t in self.board.ready_unassigned() if t.parent_id not in busy][:8]
                if ready:
                    lines.append(prompts.t(lang, "ready", tasks="; ".join(f"#{t.id} {t.title}" for t in ready)))
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
        # a new goal is new work (its kickoff follows the quiet notice); the votes on #1 are reset with the posts
        self.end_wrap_up(actor, clear_votes=False)
        if self.forum is not None:
            # a thread session (docs/SPEC.md §22.4): no planning task, ever; #1 and #2 are written or rewritten
            first = self.forum.ensure_system_posts(goal)
            if self._started:
                self._kickoff_thread(actor, goal, first)
            log.info("goal set by %s (previous: %s)", actor, (previous or {}).get("title"))
            return goal
        planning = None
        if not self.board.list():
            # a session that clarifies the goal first gets the clarify card (its tag drives the kickoff and the nag
            # guard, asks_user); it is never rewritten when the setting changes later
            clarify = self.clarifying
            p_title, p_goal, p_desc = prompts.planning_task(self.language, clarify)
            body = f"{p_desc}\n\n{title}" + (f"\n{goal['description']}" if goal["description"] else "")
            planning = self.board.create(actor, p_title, goal=p_goal, description=body, priority=2,
                                         tags=["planning", prompts.CLARIFY_TAG] if clarify else ["planning"])
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
        clarify = planning is not None and prompts.CLARIFY_TAG in planning.tags
        for i, m in enumerate(llm_members):
            if planning is not None:
                # clarify: one member asks the user and plans; the others get the kickoff quietly and spend nothing
                # until its first tasks wake them (n_ready)
                quiet = clarify and i > 0
                if not quiet:
                    # the kickoff is everybody's offer of the planning task: no "ready task" nudge about it
                    # before (stagger) or after the kickoff
                    self._nudged.setdefault((planning.id, None), set()).add(m["name"])
                self.notify_later(0 if quiet else i * stagger, m["name"], "n_kickoff", wake=not quiet, by=actor,
                                  title=goal["title"], description=goal.get("description", ""), team=team,
                                  task_id=planning.id, task_title=planning.title)
            else:
                self.notify_later(i * stagger, m["name"], "n_goal_updated", by=actor, title=goal["title"],
                                  description=goal.get("description", ""))
        if clarify and llm_members:
            planner = llm_members[0]["name"]
            # nobody else is offered the card while the planner decides (_nudge_idle); if it never claims, the card
            # is offered to the next idle member once it is idle without it or NUDGE_DECISION_SECONDS passed
            self._considering[planner] = (time.time(), frozenset(t.id for t in self.board.open_tasks_of(planner)),
                                          frozenset({planning.id}))

    def _llm_order(self, seed: float) -> list[str]:
        """The active LLM members in the order of one notice round: shuffled with `seed` (the goal's updated_at), so
        no member is always the first to read #2 (docs/SPEC.md §22.4)."""
        names = [m["name"] for m in self.members() if m["kind"] == AgentKind.LLM]
        random.Random(seed).shuffle(names)
        return names

    def _kickoff_thread(self, actor: str, goal: dict, first: bool) -> None:
        """A thread session's kickoff (docs/SPEC.md §22.4): every LLM member gets n_kickoff_thread (the first goal)
        or n_goal_updated_thread (a new one), staggered in a shuffled order. With a clarify #2 only its asker, the
        first LLM member, gets the waking n_kickoff_thread_clarify at once; the others get their notice quietly."""
        members = self.members()
        llm = [m["name"] for m in members if m["kind"] == AgentKind.LLM]
        if not llm:
            return
        stagger = max(0.0, self.config.agents.stagger_seconds)
        team = "; ".join(f"{m['name']} — {m['role']}" for m in members)
        description = goal.get("description", "")
        plan = self.forum.plan_data() or {}
        asker = None
        if plan.get("clarify"):
            asker = plan.get("asker")
            if asker not in llm:  # #2 was written before the team existed (or its asker left): the first one asks
                asker = llm[0]
                self.forum.set_plan_asker(asker)
        key = "n_kickoff_thread" if first else "n_goal_updated_thread"
        extra = {"team": team} if first else {}
        for i, name in enumerate(self._llm_order(goal["updated_at"])):
            if asker is None:
                self.notify_later(i * stagger, name, key, by=actor, title=goal["title"], description=description,
                                  **extra)
            elif name == asker:
                self.notify(name, "n_kickoff_thread_clarify", by=actor, title=goal["title"], description=description,
                            team=team)
            else:  # they wake on the asker's comment on #2 (their automatic #2 subscription, docs/SPEC.md §22.3)
                self.notify(name, key, wake=False, by=actor, title=goal["title"], description=description, **extra)

    def complete_goal(self, actor: str | None, summary: str, *, decision: dict | None = None) -> dict:
        """`decision` (a thread session, docs/SPEC.md §22.4): how the goal was decided, stored with it; actor None
        when no voter met the rule. With a decision USER gets u_goal_decided in place of u_goal_done (nothing for
        the user's own completion, rule "user") and every LLM member the quiet n_goal_decided."""
        goal = self.goal()
        if not goal:
            raise ToolError("There is no global goal to complete.")
        if goal.get("status") == "done":  # the user would be told "goal achieved" twice
            raise ToolError("The global goal is already marked as achieved.")
        if not (summary or "").strip():
            raise ToolError("Give a summary for the user.")
        goal.update(status="done", summary=summary.strip(), completed_by=actor, updated_at=time.time())
        if decision is not None:
            goal["decision"] = decision
        self.db.set("goal", goal)
        self.bus.emit("goal.completed", actor, goal=goal)
        if decision is None:
            self.notify(USER, "u_goal_done", by=actor, summary=summary.strip())
        elif decision["rule"] != "user":
            self.notify(USER, "u_goal_decided", rule=decision["rule"], counted=decision["counted"],
                        summary=summary.strip(), **{"for": decision["for"], "against": decision["against"]})
        running = self.jobs.running_of()
        if running:  # they go on (a dev server may be for the user); their later ends wake nobody
            self.notify(USER, "u_jobs_running", wake=False, jobs=self._user_job_list(running))
        if decision is not None:
            names = {"for": names_or_dash(decision["for"]), "against": names_or_dash(decision["against"])}
            for m in self.members():
                if m["kind"] == AgentKind.LLM:
                    rule = rule_name(self.language_of(m["name"]), decision["rule"])
                    self.notify(m["name"], "n_goal_decided", wake=False, rule=rule, **names)
        return goal

    def complete_goal_by_user(self, summary: str = "") -> dict:
        """The user marks a thread session's goal reached whatever the vote (docs/SPEC.md §22.4): decision rule
        "user" with the counted votes as they stand; the summary is the user's, else a generated one."""
        if self.forum is None:
            raise ToolError("In a board session the members complete the goal with goal_complete.")
        goal = self.goal()
        if not goal:
            raise ToolError("There is no global goal to complete.")
        if goal.get("status") == "done":
            raise ToolError("The global goal is already marked as achieved.")
        tally = goal_tally(self.db, self.goal_rule)
        text = (summary or "").strip() or user_summary(self.language, tally)
        return self.complete_goal(USER, text, decision=decision_of(tally, self.wrapping_up, rule="user"))

    def _user_job_list(self, running: list) -> str:
        """Running jobs for a user notice in the swarm language (u_jobs_running, u_paused_jobs): "j3 (Anna Smith: npm
        run dev, 25 min); j5 (...)"."""
        now = time.time()
        return "; ".join(f"{j.label} ({j.owner}: {short_command(j.command, 60)}, "
                         f"{fmt_duration(now - j.started_at, self.language)})" for j in running)

    def reopen_goal(self, actor: str, comment: str = "") -> dict:
        goal = self.goal()
        if not goal:
            raise ToolError("There is no global goal.")
        if goal.get("status") != "done":
            raise ToolError("The global goal is not completed; nothing to reopen.")
        goal.update(status="active", updated_at=time.time())
        if self.forum is not None:  # the decision is shown only while the goal is done (docs/SPEC.md §22.4)
            goal.pop("decision", None)
            goal["summary"] = ""
        self.db.set("goal", goal)
        self.bus.emit("goal.reopened", actor, goal=goal, comment=comment)
        self.end_wrap_up(actor, clear_votes=False)  # the missing parts are to be done now
        if self.forum is not None:
            # the votes on #1 cleared, the comment posted there; every LLM member is woken, staggered in the
            # kickoff's shuffled order (nobody re-plans for the others)
            self.forum.reopen(actor, comment)
            stagger = max(0.0, self.config.agents.stagger_seconds)
            for i, name in enumerate(self._llm_order(goal["updated_at"])):
                self.notify_later(i * stagger, name, "n_goal_reopened_thread", title=goal["title"],
                                  comment=comment or "-")
            return goal
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
        # a thread session takes the thread texts of the built-in roles (the reviewer is the Critic, §22.6)
        entry = roles.find_role(self.config.roles, role, language, mode=self.mode)
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
                language, mode=self.mode, name=name, swarm=self.config.swarm.name, role=role,
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
        if self.forum is not None:  # every member follows the feed, its reactions, #1 and #2 (docs/SPEC.md §22.3)
            self.forum.auto_subscribe(name)
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
            self.notify(name, prompts.mode_key("n_hired", self.mode), by=by, role=role)
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
        if self.forum is not None:
            self.forum.drop_subscriptions(name)  # every row, the feed one too (docs/SPEC.md §22.3)
        self.db.execute("UPDATE agents SET status = ? WHERE name = ?", (AgentStatus.RETIRED, name))
        self.bus.emit("agent.retired", by, name=name, reason=reason, released=released, jobs=jobs)
        q = self.pending_question()
        if q is not None and q["asker"] == name:
            self._drop_question(by, "left")  # a question cannot outlive its asker
        hirer = member.get("hired_by")
        if hirer and hirer not in (USER, by, name) and self.is_member(hirer):
            self.notify(hirer, prompts.mode_key("n_member_retired", self.mode), name=name, reason=reason or "-",
                        tasks=", ".join(f"#{i}" for i in released) or "-")
        if self.forum is not None:
            # one counted voter less: the votes on #1 that stand may meet the rule now (docs/SPEC.md §22.4)
            self.forum.recheck_goal(None)
        runtime = self.runtimes.pop(name, None)
        if runtime is not None:
            if runtime._task is asyncio.current_task():
                runtime.request_stop()
            else:
                await runtime.stop()  # an LLM agent's foreground command dies with its cancellation
        # its jobs and its foreground commands (an external member's call in flight): stopped, committed under its
        # name, no notice
        await self.jobs.stop_owner(name, "retired")
        # the new files its last command left (held back for one more command, docs/SPEC.md §7): it is no suspect any
        # more, so they are committed now unless another member's command may have written them too
        await self.workspace.commit_held(name, f"when {name} left the team", leaving=True)
        return released

    def rename_profiles(self, renames: dict[str, str]) -> None:
        """The settings renamed model profiles ({old: new}, settings.profile_renames): the members hired on one keep
        it (agents.model, the runtimes' profile_name). No client is reset here: the reconfigure with the new settings
        that follows (SessionManager.update_settings) rebuilds them all."""
        if not renames:
            return
        # one step: in a swap or a chain a name is both an old and a new one
        self.db.execute(*remap_sql("agents", "model", renames))
        for runtime in self.runtimes.values():
            if runtime.profile_name in renames:
                runtime.profile_name = renames[runtime.profile_name]
        # the same profile under its new name: its u_cost_unknown is not sent again in this start; the old keys stay
        # for a call that was in flight with the old client (the key also holds provider, base_url and model)
        self._cost_warned |= {(renames[key[0]], *key[1:]) for key in self._cost_warned if key[0] in renames}

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
        fields = uncounted_fields(profile, usage, model)
        if fields:
            self._cost_unknown(profile, model or profile.model, fields)
        try:  # the notice must not fail the model call that paid for it
            self._budget_low()
        except Exception:
            log.exception("the money notice failed")
        return cost

    def _budget_low(self) -> None:
        """Once per money limit and [swarm] budget_notice_at (kv budget_notice): when the share is spent and the
        limit is not yet, every AI agent gets n_budget_low (waking by resume's rule, else quiet) and USER the quiet
        u_budget_low. At the limit u_budget covers it; a new limit or share differs from the record and re-arms it
        (docs/SPEC.md §3 record_usage)."""
        limit = self.limits()["max_cost_usd"]
        at = float(self.config.swarm.budget_notice_at or 0)
        if not (limit > 0 and 0 < at < 1):
            return
        spent = self.spent_usd()
        if spent < at * limit - 1e-9 or spent >= limit:
            return
        told = self.db.get("budget_notice") or {}
        if told.get("limit") == limit and told.get("at") == at:
            return
        self.db.set("budget_notice", {"limit": limit, "at": at, "spent": spent, "ts": time.time()})
        pct = min(round(100 * spent / limit), 99)
        for m in self.members():
            if m["kind"] != AgentKind.LLM:
                continue
            name = m["name"]
            runtime = self.runtimes.get(name)
            busy = (m["status"] == AgentStatus.WORKING or (runtime is not None and runtime.state == AgentStatus.WORKING)
                    or self.board.open_tasks_of(name) or self.board.reviews_of(name) or self.has_pending(name))
            self.notify(name, prompts.mode_key("n_budget_low", self.mode), spent=spent, limit=limit, pct=pct,
                        wake=bool(busy))
        self.notify(USER, "u_budget_low", spent=spent, limit=limit, pct=pct, wake=False)

    def _cost_unknown(self, profile: ModelProfile, model: str, fields: tuple[str, ...]) -> None:
        """A call of `profile` counted (some of) its tokens as $0: no price and no cost from the provider. The user
        is told once per start for each profile, endpoint, model and set of missing prices (docs/SPEC.md §10)."""
        key = (profile.name, (profile.provider or "").strip().lower(), (profile.base_url or "").strip().rstrip("/"),
               model, fields)
        if key in self._cost_warned:
            return
        self._cost_warned.add(key)
        log.warning("model profile '%s' (model %s) has no %s and its provider reports no cost: those tokens count "
                    "as $0", profile.name, model, ", ".join(fields))
        self.notify(USER, "u_cost_unknown", profile=profile.name, model=model, fields=", ".join(fields), wake=False)

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

    def pause(self, actor: str, reason: str, **details: Any) -> bool:
        """No-op (False) when already paused; details go into kv `paused` and the swarm.paused event."""
        if self.paused:
            return False
        self._set_pause(actor, reason, **details)
        return True

    def _set_pause(self, actor: str, reason: str, *, outcome: dict | None = None, **details: Any) -> None:
        """Write the pause slot even while paused (a question turned into a budget pause). `outcome`: the event's
        `question` data of a question that ends with this pause. A fresh user pause (USER_PAUSE) while jobs run tells
        USER that they go on (quiet u_paused_jobs); a budget or question pause does not (u_budget / u_question cover
        it, and a stop_when_done session stops its jobs right after a budget pause), docs/SPEC.md §3 `_set_pause`."""
        was_paused = self.paused
        self.db.set("paused", {"paused": True, "reason": reason, **details})
        if not was_paused or self._paused_at is None:
            self._paused_at = time.time()
        self.bus.emit("swarm.paused", actor, reason=reason, **details, **({"question": outcome} if outcome else {}))
        if reason == "budget":
            self.notify(USER, "u_budget", spent=self.spent_usd(), limit=self.limits()["max_cost_usd"], wake=False)
        elif reason == QUESTION:
            self.notify(USER, "u_question", name=details["asker"], id=details["message_id"], wake=False)
        elif reason == USER_PAUSE and not was_paused and (running := self.jobs.running_of()):
            self.notify(USER, "u_paused_jobs", wake=False, jobs=self._user_job_list(running))

    def resume(self, actor: str, *, question: dict | None = None) -> None:
        """`question`: the event's data of the question this resume ends (answered, or dropped by its caller); a
        resume without it while a question holds the pause (Resume, /pause, swarm_control) drops the question."""
        if not self.paused:
            return
        if self.budget_exceeded():
            raise ToolError("The budget is exhausted: raise max_cost_usd before resuming.")
        pending = self.pending_question()
        if pending is not None and question is None:
            question = {"asker": pending["asker"], "message_id": pending["message_id"], "outcome": "dropped",
                        "why": "resumed"}
        self.db.set("paused", {"paused": False, "reason": ""})
        if self._paused_at is not None:
            # a pause is no time spent planning: the planning hint's clock (since) moves on by its length
            now = time.time()
            shift = now - self._paused_at
            for watch in self._planning_watch.values():
                watch["since"] += shift
            # nor time to decide on an offer (_nudge_idle; the clarify planner's from its kickoff): an offer made
            # during the pause counts from the resume
            for agent, (since, owned, offered) in list(self._considering.items()):
                self._considering[agent] = (since + now - max(since, self._paused_at), owned, offered)
            # nor does a crew task's state age while paused (_tell_crew_gaps): every free-place clock moves on
            for task_id, (key, since) in list(self._crew_gaps.items()):
                self._crew_gaps[task_id] = (key, since + now - max(since, self._paused_at))
            self._paused_at = None
        self._team_asleep = None  # the team-asleep clock starts again: n_resumed below wakes the holders
        # a pause ends a thread session's quiet period: USER never gets u_thread_quiet because of it (§22.7)
        self._thread_quiet = self._thread_quiet_woken = None
        self._digest_floor = time.time()  # nor does time spent paused age a digest item (docs/SPEC.md §22.3)
        self.bus.emit("swarm.resumed", actor, **({"question": question} if question else {}))
        told = None
        if (question and question["outcome"] == "dropped" and question.get("why") != "left"
                and self.is_member(question["asker"])):
            # the asker takes its defaults: told in place of n_resumed (a wake, also for an external member)
            told = question["asker"]
            self.notify(told, prompts.mode_key("n_question_unanswered", self.mode), id=question["message_id"])
        for m in self.members():
            if m["kind"] == AgentKind.LLM and m["name"] != told:
                runtime = self.runtimes.get(m["name"])
                # an agent whose activation the pause cut short continues it even with nothing pending
                if (self.board.open_tasks_of(m["name"]) or self.board.reviews_of(m["name"])
                        or self.has_pending(m["name"]) or (runtime is not None and runtime.interrupted)):
                    self.notify(m["name"], "n_resumed")
                elif runtime is not None:
                    runtime.wake()  # paused -> idle

    # ---- a member's question to the user (docs/SPEC.md §3 "Clarify mode") -----------------------------------
    def pending_question(self) -> dict | None:
        """{asker, message_id, since} of the question that holds the pause, else None."""
        p = self.db.get("paused") or {}
        if p.get("paused") and p.get("reason") == QUESTION and p.get("asker") and isinstance(p.get("message_id"), int):
            return {"asker": p["asker"], "message_id": p["message_id"], "since": p.get("since")}
        return None

    def last_message_id(self, sender: str, recipient: str, since: float = 0.0) -> int | None:
        """The id of the newest message from `sender` that reached `recipient` (named, or through "all": the inbox
        row), sent at `since` (a timestamp) or later; None when there is none (docs/SPEC.md §21.5 `message`)."""
        found = self.db.scalar(_LAST_TO, (sender, recipient, since))
        return int(found) if found is not None else None

    def pause_text(self) -> str:
        """The pause reason as readable text (swarm_status, an agent's trace)."""
        q = self.pending_question()
        if q is not None:
            return f"waiting for the user's answer to {q['asker']}'s question #{q['message_id']}"
        return self.pause_reason or "no reason given"

    def ask_user(self, asker: str, message: dict) -> tuple[str, dict]:
        """send_message(pause_swarm=true) after `message` was stored: the outcome and the details of its result note.
        Synchronous: nothing can slip in between the send and the pause."""
        if asker == USER:
            return "user", {}
        if USER not in message["recipients"]:
            return "no_user", {}
        if not self.config.swarm.clarify:
            return "off", {}
        if self.auto_reply_active:
            return "auto", {}
        q = self.pending_question()
        if q is not None:
            return ("own" if q["asker"] == asker else "pending"), {"asker": q["asker"], "id": q["message_id"]}
        if self.paused:
            return "paused", {"reason": self.pause_text()}
        if self.budget_exceeded():
            return "budget", {}
        self.pause(asker, "question", asker=asker, message_id=message["id"], since=time.time())
        return "asked", {"id": message["id"]}

    def question_answered(self, message: dict, question: dict) -> None:
        """Mailbox.send hook: the user's `message` answers `question`. Tell the others, then resume (or turn into a
        budget pause). Never raises: the user's message is stored already."""
        try:
            self._tell_answer(message, question)
        except Exception:
            log.exception("could not tell the members the answer to question #%s", question["message_id"])
        try:
            if self.pending_question() != question:
                return  # something ended this pause meanwhile
            outcome = {"asker": question["asker"], "message_id": question["message_id"], "outcome": "answered",
                       "answer_id": message["id"]}
            if self.budget_exceeded():
                self._set_pause(USER, "budget", outcome=outcome)
            else:
                self.resume(USER, question=outcome)
        except Exception:
            log.exception("could not lift the question pause #%s", question["message_id"])

    def _tell_answer(self, message: dict, question: dict) -> None:
        """Every other member gets the question with the answer: quietly for AI agents (they are paused; the resume
        wakes those with work), with a wake for external members (they are not paused)."""
        asked = self.db.scalar("SELECT text FROM messages WHERE id = ?", (question["message_id"],)) or ""
        q_text = truncate_middle(asked, QUESTION_CHARS)
        a_text = truncate_middle(message["text"], QUESTION_CHARS)
        for m in self.members():
            name = m["name"]
            if name == question["asker"]:
                continue                            # it has its question and gets the answer as the message itself
            wake = m["kind"] != AgentKind.LLM       # external members are not paused: wait_for_events returns
            if name in message["recipients"]:       # e.g. the user answered "all": no second copy of the answer
                self.notify(name, prompts.mode_key("n_user_answer_seen", self.mode), wake=wake,
                            asker=question["asker"], id=question["message_id"], question=q_text,
                            answer_id=message["id"])
            else:
                self.notify(name, prompts.mode_key("n_user_answer", self.mode), wake=wake, asker=question["asker"],
                            id=question["message_id"], question=q_text, answer=a_text)

    def _drop_question(self, actor: str, why: str) -> None:
        """End a question pause without an answer: why "clarify_off" | "left"."""
        q = self.pending_question()
        if q is None:
            return
        outcome = {"asker": q["asker"], "message_id": q["message_id"], "outcome": "dropped", "why": why}
        if self.budget_exceeded():
            self._set_pause(actor, "budget", outcome=outcome)
            if why != "left" and self.is_member(q["asker"]):
                self.notify(q["asker"], prompts.mode_key("n_question_unanswered", self.mode), id=q["message_id"])
        else:
            self.resume(actor, question=outcome)
        if why == "left" and actor != USER:
            self.notify(USER, "u_question_left", name=q["asker"], id=q["message_id"], wake=False)

    def _plans(self, name: str, task: Any) -> bool:
        """`name` has created a task since the planning card `task`: it plans already (as _hint_planner's check)."""
        return bool(self.db.scalar(PLANS, (name, task.id)))

    def _awaits_user_answer(self, name: str, task: Any = None) -> bool:
        """`task` is a clarify planning card, `name` has created no task since it (it plans already: the ordinary nags
        apply again), and `name`'s last message to the user since the card was created is newer than the user's last
        message to it (named or through "all"). Both bounds keep an old message (an earlier goal) or a note to the user
        after planning started from silencing the nags for ever. A question the user went on without (a manual
        resume, a budget pause in its place: n_question_unanswered told the planner so) waits for nothing.
        (autoreply.planner_question: the requests to the user read the same rule from a session's file.)
        A thread session (docs/SPEC.md §22.4 "Clarify"): `name` is the kickoff asker of a clarify #2, and the bounds
        count from #2's last write; `task` is not read."""
        if self.forum is not None:
            if self._clarify_asker() != name:
                return False
            return planner_question(self.db.scalar, name, PLAN_POST, self.forum.plan_since(),
                                    plans=PLANS_THREAD) is not None
        if task is None or "planning" not in task.tags or prompts.CLARIFY_TAG not in task.tags:
            return False
        return planner_question(self.db.scalar, name, task.id, task.created_at) is not None

    def asks_user(self, name: str, task: Any = None) -> bool:
        """The nag guard (docs/SPEC.md §3 "Clarify mode"): the clarify planner waits for the user's answers, so it
        gets no n_stalled, n_planning_hint or in-activation nudge about its planning card meanwhile. A thread
        session: the kickoff asker of a clarify #2 waits for them (§22.4)."""
        return bool(self.config.swarm.clarify) and self._awaits_user_answer(name, task)

    def _clarify_asker(self) -> str | None:
        """A thread session's kickoff asker: #2's data `asker` when its `clarify` is true, else None."""
        data = self.forum.plan_data() if self.forum is not None else None
        return data.get("asker") if data and data.get("clarify") and data.get("asker") else None

    def _in_clarify_phase(self, name: str) -> bool:
        """The clarify phase of a thread session's asker `name` (docs/SPEC.md §22.4): since #2's last write it has
        neither messaged the user nor commented on #2."""
        if self._clarify_asker() != name:
            return False
        since = self.forum.plan_since()
        return not self.db.scalar(_LAST_TO, (name, USER, since)) and not self.db.scalar(
            PLANS_THREAD, (name, PLAN_POST, since))

    def _clarify_switched_off(self, once: bool = False) -> None:
        """[swarm] clarify is off: a clarify planner that waits for the user is told to plan now (n_clarify_off), and a
        question pause is lifted. `once` (at start(): nothing records whether the setting changed while the session was
        stopped): not again for a card whose holder already got n_clarify_off about it. A thread session: the asker
        of a clarify #2 in its clarify phase or waiting for the answers gets n_clarify_off_thread, with `once` not
        again since #2's last write (docs/SPEC.md §22.4 "Clarify")."""
        if self.forum is not None:
            asker = self._clarify_asker()
            if asker and self.is_member(asker) and (self._in_clarify_phase(asker)
                                                    or self._awaits_user_answer(asker)):
                told = once and self.db.scalar(
                    "SELECT 1 FROM notifications WHERE recipient = ? AND kind = 'n_clarify_off_thread' AND ts >= ?"
                    " LIMIT 1", (asker, self.forum.plan_since()))
                if not told:
                    self.notify(asker, "n_clarify_off_thread")
            self._drop_question(USER, "clarify_off")
            return
        for t in self.board.list(tag="planning"):
            # it waits for the user: the card is `waiting` (also after a reply that answered nothing) or the guard
            # holds; a holder that created a task plans already, so its `waiting` card waits for something else
            if not (prompts.CLARIFY_TAG in t.tags and t.assignee and self.is_member(t.assignee)
                    and not self._plans(t.assignee, t)
                    and (t.status == Status.WAITING or self._awaits_user_answer(t.assignee, t))):
                continue
            if once and self.db.scalar("SELECT 1 FROM notifications WHERE recipient = ? AND kind = 'n_clarify_off'"
                                       " AND json_extract(data, '$.task_id') = ? LIMIT 1", (t.assignee, t.id)):
                continue
            self.notify(t.assignee, "n_clarify_off", task_id=t.id)
        self._drop_question(USER, "clarify_off")

    # the auto-reply lease of a `dreamteam -p` client (docs/SPEC.md §3 "Clarify mode"): in memory only, so a hub or
    # session restart forgets it, and it ends AUTO_REPLY_GRACE seconds after the client's last sign
    def auto_reply_seen(self) -> None:
        self._auto_reply_until = max(self._auto_reply_until, _monotonic() + AUTO_REPLY_GRACE)

    @contextlib.contextmanager
    def auto_reply_stream(self):
        """An event stream (WebSocket) of such a client: the lease holds while it is open, and for the grace after."""
        self._auto_reply_streams += 1
        self.auto_reply_seen()
        try:
            yield
        finally:
            self._auto_reply_streams = max(0, self._auto_reply_streams - 1)
            self.auto_reply_seen()

    def hold_auto_reply(self) -> None:
        """The hub itself answers what the members ask the user (HTTP API v1 `answers: "auto"`, docs/SPEC.md §21.5):
        the lease holds from now until this Swarm is unloaded, with no client sign. SessionManager._start_locked
        takes it before `swarm.start()`, which may itself set a goal from the settings."""
        self._auto_reply_held = True

    @property
    def auto_reply_active(self) -> bool:
        return self._auto_reply_held or self._auto_reply_streams > 0 or _monotonic() < self._auto_reply_until

    @property
    def clarifying(self) -> bool:
        """Effective clarify: [swarm] clarify, unless a `dreamteam -p` client answers questions automatically."""
        return bool(self.config.swarm.clarify) and not self.auto_reply_active

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
        key = prompts.mode_key("n_wrap_up", self.mode)
        for m in self.members():
            if m["kind"] == AgentKind.LLM:
                self.notify(m["name"], key)
        return True

    def end_wrap_up(self, actor: str, clear_votes: bool = True) -> bool:
        """Switch wrap-up mode off (the user, a new or reopened goal): a quiet notice for the next wake-up. False when
        it was not on. A thread session with an active goal and `clear_votes` (the user's switch-off; set_goal and
        reopen_goal reset the votes themselves) also clears the votes on #1, whose likes meant "finish with what we
        have" (docs/SPEC.md §22.4), and sends n_wrap_up_votes_cleared in place of n_wrap_up_end."""
        if not self.wrapping_up:
            return False
        self.db.set("wrap_up", {"on": False})
        self.bus.emit("swarm.wrap_up", actor, on=False)
        cleared = (self.forum is not None and clear_votes
                   and (self.goal() or {}).get("status") == "active")
        if cleared:
            self.forum.reset_goal_votes(actor, "wrap_up_end")
        key = "n_wrap_up_votes_cleared" if cleared else "n_wrap_up_end"
        for m in self.members():
            if m["kind"] == AgentKind.LLM:
                self.notify(m["name"], key, wake=False)
        return True

    def make_llm(self, profile_name: str | None, agent: str) -> LLMClient:
        if profile_name and profile_name not in self.config.models:
            # the member's profile (agents.model) was deleted in the settings, or renamed by a hand edit of
            # dreamteam.toml (a rename saved through the settings follows: rename_profiles): an error here would crash
            # the agent at every activation, so it falls back to the default profile
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
                self.workspace.settle_waiters()  # wakes the members refused what is free now; tells long holders
                await self.skills.check()  # a mirror copy changed by a command is rebuilt (at most once a minute)
                await self.skills.touch_envs()  # hourly: the environments' `used` stamps, retries, repairs
                if time.time() - self._last_prune >= TRACE_PRUNE_SECONDS:
                    self._prune_traces()
                # an external member that stopped calling: its session trace ends at its last call
                self.tracer.close_external(idle_seconds=EXTERNAL_IDLE_SECONDS)
                if not self.paused and self.mode == "thread":
                    # no board item runs in a thread session (docs/SPEC.md §22.7): digest batching, then the quiet
                    # check, last as item 9 (a member that the steps before woke has news pending, so it is not idle)
                    self._wake_deferred()
                    self._deliver_digests()
                    self._check_quiet()
                elif not self.paused:
                    self._wake_deferred()
                    self._nudge_idle()
                    self._wake_stalled()
                    self._wake_idle_holders()
                    self.hint_planner()
                    self._remind_reviewers()
                    self._check_all_done()
                    self._remind_user_reviews()
                    self._tell_crew_gaps()  # after items 1-7: it reads item 1's offers of this pass, and whom they woke
                    self._wake_team_asleep()  # last: a member that items 1-8 woke has news pending, so it is not idle
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

    def _offers(self) -> Offers:
        """The ready tasks (Board.ready_unassigned), each with its offer key: a ready task is offered to each member
        once while it stays ready with the same finish state (docs/SPEC.md §4.2 "Offers")."""
        ready = self.board.ready_unassigned()
        states = self.board.finish_states(t.id for t in ready) if ready else {}
        return [(t, (t.id, states.get(t.id))) for t in ready]

    def _nudge_idle(self) -> None:
        offers = self._offers()
        ready = [t for t, _ in offers]
        ready_ids = {t.id for t in ready}
        offer_key = {t.id: key for t, key in offers}
        live = set(offer_key.values())  # a key no longer among the ready ones is dropped
        self._nudged = {k: v for k, v in self._nudged.items() if k in live}
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
            # never a direction of a crew task it works on already: its claim would be refused
            busy = self.board.busy_crews(name)
            fresh = [t for t in ready if name not in self._nudged.get(offer_key[t.id], set())
                     and t.parent_id not in busy][:6]
            if not fresh:
                continue
            owned = frozenset(t.id for t in self.board.open_tasks_of(name))
            self.notify(name, "n_ready", tasks="; ".join(f"#{t.id} {t.title}" for t in fresh))
            for t in fresh:
                self._nudged.setdefault(offer_key[t.id], set()).add(name)
            self._last_nudge[name] = now
            self._considering[name] = (now, owned, frozenset(t.id for t in fresh))
            break

    def _wake_stalled(self) -> None:
        """An agent that stopped with plain text holding in-progress tasks even after the in-activation nudge
        is idle with nothing pending, so no event would ever wake it: wake it once per liveness key, those of its
        stalled tasks it still holds and its marks (_stall_key; not while the end of one of its jobs, or a file it
        was refused becoming free, will wake it)."""
        now = time.time()
        for m in self._idle_llm_members():
            name = m["name"]
            runtime = self.runtimes.get(name)
            stalled = getattr(runtime, "stalled", frozenset())
            if not stalled or now - runtime.stalled_at < self.config.agents.idle_nudge_seconds:
                continue
            if self.jobs.awaited(name) or self.workspace.waits_for_lock(name):
                continue  # the end of its job (or n_lock_free) will wake it: it waits, it has not stalled
            runtime.stalled = frozenset()
            still = self._stall_ids(name, stalled)
            if not still:
                continue
            key = self._stall_key(name, still)
            if self._stall_woken.get(name) == key:
                continue
            self._wake_about(name, key)

    def _wake_idle_holders(self) -> None:
        """An agent that sleeps (idle, not busy, nothing pending) while it holds in-progress tasks, e.g. after
        `wait` with nothing that will wake it, would sleep until some unrelated event: after
        agents.idle_nudge_seconds it is woken once per liveness key (its in-progress task ids and its marks,
        _stall_key; shared with _wake_stalled, so a key is never woken about twice), also with no such task when
        it has marks (a `waiting` or `todo` parent whose subtasks have all finished, a direction it can take now).
        The clock starts when it was last seen
        going idle with that key. A member whose running job's end will wake it (JobManager.awaited, what `wait`
        promised it) is waiting for that job, and one refused a file waits for it to be free
        (Workspace.waits_for_lock, what the refusal promised): no clock runs for it meanwhile."""
        now, wait = time.time(), self.config.agents.idle_nudge_seconds
        holding: dict[str, tuple[StallKey, float]] = {}
        for m in self._idle_llm_members():
            name = m["name"]
            if self.jobs.awaited(name) or self.workspace.waits_for_lock(name):
                continue
            key = self._stall_key(name, self._stall_ids(name))
            if not key[0] and not key[1]:
                continue
            seen, since = self._idle_holding.get(name, (None, now))
            if seen != key:
                since = now
            # an activation between two ticks (woken and idle again) restarts the clock: last_active is when it
            # last went idle
            since = max(since, float(m.get("last_active") or 0))
            holding[name] = (key, since)
            if now - since < wait or self._stall_woken.get(name) == key:
                continue
            self._wake_about(name, key)
        self._idle_holding = holding

    def _stall_ids(self, name: str, among: frozenset[int] | None = None) -> frozenset[int]:
        """The in_progress task ids of `name` that count as stalled (`among` them, when given): not a clarify
        planning task whose holder waits for the user's answers (asks_user), nor a crew task with a direction that can
        still move (Board.has_live_directions: it waits for them; a free one is the "free directions" mark). A crew
        task whose open directions all wait for a failed or cancelled task counts. A `waiting` task counts only through
        its marks."""
        return frozenset(t.id for t in self.board.open_tasks_of(name)
                         if t.status == Status.IN_PROGRESS and (among is None or t.id in among)
                         and not self.asks_user(name, t) and not self.board.has_live_directions(t))

    def _stall_key(self, name: str, ids: frozenset[int]) -> StallKey:
        """The liveness key both scheduler items compare and record (docs/SPEC.md §3 items 2-3): the stalled ids
        and the member's marks read from the board now (Board.stall_marks)."""
        return frozenset(ids), self.board.stall_marks(name)

    def _wake_about(self, name: str, key: StallKey) -> None:
        """Wake `name` about its liveness key: n_stalled naming the ids that carry no mark followed by one sentence
        per mark, or the mark sentences alone (n_marks_stalled). The key is recorded only once the text is built,
        so a failed render leaves it to the next pass (logged; the other scheduler items still run)."""
        ids, marks = key
        marked = {tid for tid, _ in marks}
        unmarked = sorted(i for i in ids if i not in marked)
        try:
            text = self.board.mark_text(name, marks) if marks else ""
        except Exception:
            log.exception("could not render the liveness marks of %s", name)
            return
        if not unmarked and not text:
            return  # the marks no longer apply (changed since they were read): the next pass reads them again
        self._stall_woken[name] = key
        if unmarked:
            self.notify(name, "n_stalled", tasks=", ".join(f"#{i}" for i in unmarked), marks=text)
        else:
            self.notify(name, "n_marks_stalled", text=text.lstrip())

    def crew_nudge(self, name: str, tasks: list[Any]) -> tuple[set[int], str]:
        """The in-activation nudge's crew part (docs/SPEC.md §6.2) for `name`'s in_progress `tasks`: the ids of the
        crew tasks with a direction that can still move (Board.has_live_directions; left out of the nudge: they wait
        for their directions), and the "free directions" sentences of those with a free direction its owner, working
        on none of them, could take ("" when there is none)."""
        crew = {t.id for t in tasks if self.board.has_live_directions(t)}
        if not crew:
            return set(), ""
        free = [(tid, kind) for tid, kind in self.board.stall_marks(name)
                if tid in crew and kind == MARK_FREE_DIRECTIONS]
        return crew, self.board.mark_text(name, free) if free else ""

    def hint_planner(self, name: str | None = None) -> None:
        """An LLM member holding a `planning` task in progress that has created no task `agents.planning_hint_steps`
        LLM steps or `planning_hint_minutes` after it was first seen holding it gets `n_planning_hint` (the others
        wait for the plan's first tasks meanwhile); once more at twice either value, after at least one step since the
        first hint; never more (a re-claim or a restart included), never with nobody else on the team, while paused
        (the clock stops: resume) or in wrap-up mode. While [swarm] clarify is on, the holder of a clarify card that
        has not messaged the user since the card was created gets `n_clarify_hint` in its place (ask now, or plan if
        there is nothing to ask); the two kinds share the count. `name`: after one of that member's steps (the
        hint is in the same activation's `incoming`); None: the scheduler, for every holder (the time trigger, or a
        holder that went idle). Never raises: it runs inside activations."""
        try:
            if self.is_thread:
                # a thread session has no planning task: only the clarify asker's hint (docs/SPEC.md §22.4), after
                # its own steps (the scheduler does not call it there, §22.7)
                if name is not None:
                    self._hint_asker(name)
                return
            self._hint_planner(name)
        except Exception:
            log.exception("planning hint failed")

    def _hint_asker(self, name: str) -> None:
        """hint_planner in a thread session: one watch ("p2") on the kickoff asker of a clarify #2 while it is in its
        clarify phase; `n_clarify_hint_thread` after `planning_hint_steps` steps or `planning_hint_minutes`, once more
        at twice either value after at least one step since the first, never a third time (the hints sent since the
        goal was set count when the watch starts). The watch is dropped once the asker asked (asks_user) or left its
        clarify phase (also when #2 is written again: a new goal)."""
        asker, plan = self._clarify_asker(), self.forum.plan_since()
        watch = self._planning_watch.get("p2")
        if watch is not None and (watch["holder"] != asker or watch.get("plan") != plan):
            self._planning_watch.pop("p2", None)
            watch = None
        if name != asker:
            return
        agents = self.config.agents
        steps_n, minutes = agents.planning_hint_steps, agents.planning_hint_minutes
        goal = self.goal()
        if not (bool(steps_n or minutes) and self.config.swarm.clarify and goal and goal.get("status") == "active"
                and not self.paused and not self.wrapping_up):
            return  # the watch stays (a pause moves its start on: resume)
        runtime = self.runtimes.get(name)
        if (runtime is None or self.kind_of(name) != AgentKind.LLM or not self._in_clarify_phase(name)
                or self.asks_user(name)):
            self._planning_watch.pop("p2", None)
            return
        now = time.time()
        if watch is None:
            sent = self.db.scalar("SELECT COUNT(*) FROM notifications WHERE recipient = ?"
                                  " AND kind = 'n_clarify_hint_thread' AND ts >= ?",
                                  (name, float(goal.get("updated_at") or 0)))
            watch = self._planning_watch["p2"] = {"holder": name, "since": now, "steps": runtime.llm_steps,
                                                  "hints": int(sent or 0), "hinted": -1, "plan": plan}
        level = watch["hints"] + 1
        if level > 2 or runtime.llm_steps == watch["hinted"]:
            return  # hinted twice already, or no step since the last hint
        steps, elapsed = runtime.llm_steps - watch["steps"], now - watch["since"]
        if not ((steps_n and steps >= level * steps_n) or (minutes and elapsed >= level * minutes * 60)):
            return
        if len(self.members()) < 2:
            return  # nobody waits for it
        watch["hints"], watch["hinted"] = level, runtime.llm_steps
        self.notify(name, prompts.mode_key("n_clarify_hint", self.mode), steps=steps,
                    elapsed=fmt_duration(elapsed, self.language_of(name)))

    def _hint_planner(self, name: str | None) -> None:
        agents = self.config.agents
        steps_n, minutes = agents.planning_hint_steps, agents.planning_hint_minutes
        goal = self.goal()
        # paused: no step counts (a step that ends after the pause began is not planning time); wrap-up: the team
        # finishes with what it has, so no member is told to create tasks
        active = (bool(steps_n or minutes) and bool(goal) and goal.get("status") == "active" and not self.paused
                  and not self.wrapping_up)
        if not active:
            tasks = []
        else:
            tasks = self.board.open_tasks_of(name) if name else self.board.list(status=Status.IN_PROGRESS, tag="planning")
        held = {t.id: t for t in tasks
                if t.status == Status.IN_PROGRESS and "planning" in t.tags and t.assignee in self.runtimes}
        # a clarify planner that waits for the user's answers is not planning yet (asks_user): its watch is dropped,
        # so the clock starts again once the answer came
        for task_id in [k for k, t in held.items() if self.asks_user(t.assignee, t)]:
            del held[task_id]
            self._planning_watch.pop(task_id, None)
        if name is None:  # the scheduler sees every holder: forget the tasks nobody holds any more
            self._planning_watch = {k: w for k, w in self._planning_watch.items() if k in held}
        now = time.time()
        for task in held.values():
            holder, runtime = task.assignee, self.runtimes[task.assignee]
            watch = self._planning_watch.get(task.id)
            if watch is None or watch["holder"] != holder:  # claimed (or claimed by another member) since the last look
                # the hints it already had for this task count (a re-claim, a restart): two at most, ever, of
                # both kinds
                sent = self.db.scalar("SELECT COUNT(*) FROM notifications WHERE recipient = ?"
                                      " AND kind IN ('n_planning_hint', 'n_clarify_hint')"
                                      " AND json_extract(data, '$.id') = ?", (holder, task.id))
                watch = self._planning_watch[task.id] = {"holder": holder, "since": now, "steps": runtime.llm_steps,
                                                         "hints": int(sent or 0), "hinted": -1}
            level = watch["hints"] + 1
            if level > 2 or runtime.llm_steps == watch["hinted"]:
                continue  # hinted twice already, or no step since the last hint
            steps, elapsed = runtime.llm_steps - watch["steps"], now - watch["since"]
            if not ((steps_n and steps >= level * steps_n) or (minutes and elapsed >= level * minutes * 60)):
                continue
            if self.db.scalar("SELECT COUNT(*) FROM tasks WHERE created_by = ? AND id > ?", (holder, task.id)):
                continue  # the planner already creates tasks: the others have work
            if len(self.members()) < 2:
                continue  # nobody waits for its tasks
            watch["hints"], watch["hinted"] = level, runtime.llm_steps
            # a clarify planner that has not asked the user yet is still finding the forks: it is told to ask now
            # (or, with nothing to ask, to plan), never to create tasks before the questions
            asking = (bool(self.config.swarm.clarify) and prompts.CLARIFY_TAG in task.tags
                      and not self.db.scalar(_LAST_TO, (holder, USER, task.created_at)))
            key = "n_clarify_hint" if asking else "n_planning_hint"
            self.notify(holder, key, id=task.id, steps=steps, elapsed=fmt_duration(elapsed, self.language_of(holder)))

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
        # a member busy with a job (its command went on in the background) is at work like a WORKING one
        if (any(m["status"] == AgentStatus.WORKING or self.has_pending(m["name"]) or self.jobs.busy(m["name"])
                for m in llm)
                or any(t.reviewer != USER for t in reviews) or self._has_task_in_progress()
                or self._has_actionable_todo()):
            return
        key = frozenset(t.id for t in waiting)
        if key != self._user_reviews_key:
            self._user_reviews_key = key
            self.notify(USER, "u_reviews_waiting", tasks=", ".join(f"#{t.id}" for t in waiting))

    def _has_task_in_progress(self) -> bool:
        """An `in_progress` task its holder can go on with: one without open subtasks (a parent with an open subtask
        waits for it, and cannot be completed before it finishes; the open subtasks count on their own), or a crew
        task whose holder, a member, has its "free directions" mark (Board.stall_marks: it works on none of its
        directions and may take a free one; its owner's claim takes no place, so a full crew task counts)."""
        tasks = self.board.list(status=Status.IN_PROGRESS)
        waits = self.board.open_parents(t.id for t in tasks)
        marks: dict[str, frozenset] = {}
        for t in tasks:
            if t.id not in waits:
                return True
            if t.crew >= 2 and t.assignee not in (None, USER):
                if t.assignee not in marks:
                    marks[t.assignee] = self.board.stall_marks(t.assignee)
                if (t.id, MARK_FREE_DIRECTIONS) in marks[t.assignee]:
                    return True
        return False

    def _has_actionable_todo(self) -> bool:
        """A `todo` task someone can go on with, never a parent with an open subtask (it waits for them): one
        ready_unassigned returns (a direction only while its crew task takes directions), or one a member holds that
        no blocker stops; a held direction of a crew task only when its holder's claim would take it now
        (Board.directions_to_take: not while the crew task is blocked, full, in review or terminal)."""
        ready = self.board.ready_unassigned()
        waits = self.board.open_parents(t.id for t in ready)
        if any(t.id not in waits for t in ready):
            return True
        held = [t for t in self.board.list(status=Status.TODO)
                if t.assignee is not None and t.assignee != USER and not t.blocked_by]
        waits = self.board.open_parents(t.id for t in held)
        crews: dict[int, bool] = {}
        takeable: dict[str, set[int]] = {}
        for t in held:
            if t.id in waits:
                continue
            if t.parent_id is None:
                return True
            if t.parent_id not in crews:
                crews[t.parent_id] = self.board.get(t.parent_id).crew >= 2
            if not crews[t.parent_id]:
                return True
            if t.assignee not in takeable:
                takeable[t.assignee] = {d.id for d in self.board.directions_to_take(t.assignee)}
            if t.id in takeable[t.assignee]:
                return True
        return False

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
        key = self._board_key()
        if key == self._all_done_key:
            return
        idle = sorted(self._idle_llm_members(), key=lambda m: m.get("last_active") or 0, reverse=True)
        if not idle:
            return
        self._all_done_key = key
        # wrapping up: the goal is closed with what there is, no new tasks for what is missing
        self.notify(idle[0]["name"], "n_all_done_wrap_up" if self.wrapping_up else "n_all_done")

    def _tell_crew_gaps(self) -> None:
        """Item 8 (docs/SPEC.md §4.2 "A free place"): the owner of a crew task with a free place, a working direction
        and none a member may take now (Board.crew_gaps) gets n_crew_gap once the crew task has kept that state (its
        key: owner, crew, open directions, held ones) for CREW_GAP_FACTOR x agents.idle_nudge_seconds, while the
        owner is an idle LLM member (one that waits for its own job or a file included) and other members are free
        (_free_members); once per key (_crew_gap_told), at most CREW_GAP_MAX times per owner and crew task (a key due
        at the cap is recorded at once, with no free member looked for). While the goal is not active or in wrap-up
        mode nothing is sent and the clocks are dropped; a pause stops them (resume moves them on). It runs after items
        1-7: it reads item 1's offers of the pass (_nudged), and a member they woke has news pending, so it is not idle;
        nor is an owner told earlier in the same pass a free member."""
        goal = self.goal()
        if not goal or goal.get("status") != "active" or self.wrapping_up:
            self._crew_gaps = {}
            return
        now, wait = time.time(), self.config.agents.idle_nudge_seconds
        clocks: dict[int, tuple[CrewGapKey, float]] = {}
        due: list[tuple[Task, list[tuple[int, str, int]], CrewGapKey]] = []
        for task, subs, held in self.board.crew_gaps():
            key = (task.assignee, task.crew, frozenset(s.id for s in subs), frozenset(sub for sub, _, _ in held))
            seen, since = self._crew_gaps.get(task.id, (None, now))
            since = since if seen == key else now
            clocks[task.id] = (key, since)
            if now - since >= CREW_GAP_FACTOR * wait and self._crew_gap_told.get(task.id) != key:
                due.append((task, held, key))
        self._crew_gaps = clocks
        if not due:
            return
        idle = self._idle_llm_members()  # read once: an owner of two crew tasks due is told of both in this pass
        owners = {m["name"] for m in idle}
        told: set[str] = set()  # the owners told in this pass: news pending, so no free member of the next crew task
        offers: Offers | None = None  # item 1's ready tasks, read once a free member is looked for
        for task, held, key in due:
            owner = task.assignee
            if owner not in owners:
                continue  # USER, an external member, or a member at work, busy, with news, in error or paused
            sent = self.db.scalar("SELECT COUNT(*) FROM notifications WHERE recipient = ? AND kind = 'n_crew_gap'"
                                  " AND json_extract(data, '$.id') = ?", (owner, task.id))
            if int(sent or 0) >= CREW_GAP_MAX:
                self._crew_gap_told[task.id] = key  # the cap: this key is not counted again, nor free members sought
                continue
            if offers is None:
                offers = self._offers()
            free = self._free_members(task, [m for m in idle if m["name"] not in told], now, offers)
            if not free:
                continue
            lang = self.language_of(owner)
            try:
                directions, held_text = self.board.crew_gap_parts(task, held, lang)
            except Exception:
                log.exception("could not render the free place of crew task #%s", task.id)
                continue  # the key stays unrecorded: the next pass tries again
            names = [f"{m['name']} ({trim(m.get('role'), 60)})" for m in free[:CREW_GAP_IDLE_MAX]]
            self.notify(owner, "n_crew_gap", id=task.id, title=task.title, working=len(task.crew_working),
                        crew=task.crew, names=", ".join(task.crew_working),
                        idle=", ".join(names + (["…"] if len(free) > CREW_GAP_IDLE_MAX else [])),
                        directions=directions, held=held_text)
            self._crew_gap_told[task.id] = key
            told.add(owner)

    def _free_members(self, task: Task, idle: list[dict], now: float, offers: Offers) -> list[dict]:
        """The free members of crew task `task` (docs/SPEC.md §4.2 "A free place"), the longest idle first: the
        `idle` LLM members outside its working set that have been idle for agents.idle_nudge_seconds at least
        (last_active), wait neither for a job of their own nor for a file, have no review waiting for them, hold
        nothing they can go on with (no in_progress task and no todo one without blockers that is no direction, a
        parent with an open subtask left out; no liveness mark), and were offered by item 1 every ready task it would
        offer them (`offers` but the directions of their busy_crews, each in _nudged under its offer key)."""
        wait = self.config.agents.idle_nudge_seconds
        working = set(task.crew_working)
        crews: dict[int, bool] = {}  # parent id -> a crew task: its todo subtasks are directions
        free: list[dict] = []
        for m in sorted(idle, key=lambda m: m.get("last_active") or 0):
            name = m["name"]
            if name in working or now - float(m.get("last_active") or 0) < wait:
                continue
            if self.jobs.awaited(name) or self.workspace.waits_for_lock(name) or self.board.reviews_of(name):
                continue
            work = []
            for t in self.board.open_tasks_of(name):
                if t.status == Status.TODO and not t.blocked_by and t.parent_id is not None:
                    if t.parent_id not in crews:
                        crews[t.parent_id] = self.board.get(t.parent_id).crew >= 2
                    if crews[t.parent_id]:
                        continue  # a direction: its liveness mark says when its claim would take it
                if t.status == Status.IN_PROGRESS or (t.status == Status.TODO and not t.blocked_by):
                    work.append(t.id)
            waits = self.board.open_parents(work)  # a parent with an open subtask waits for it
            if any(tid not in waits for tid in work):
                continue
            busy = self.board.busy_crews(name) if offers else set()
            if any(name not in self._nudged.get(key, set()) for t, key in offers if t.parent_id not in busy):
                continue  # item 1 offers it a ready task first
            if self.board.stall_marks(name):
                continue
            free.append(m)
        return free

    def _wake_team_asleep(self) -> None:
        """Item 9 (docs/SPEC.md §3): the whole team sleeps while work is open and nothing set will wake the team
        (_team_may_sleep, team_asleep), e.g. a member waits for a process `jobs` does not list. Once that has lasted
        TEAM_ASLEEP_FACTOR x 2 ** min(sent, TEAM_ASLEEP_DOUBLINGS) x agents.idle_nudge_seconds (sent: the n_team_asleep
        notices under the current goal) with one board key (item 6's), one idle LLM member is woken (n_team_asleep), at
        most TEAM_ASLEEP_MAX times per key, and USER is told with the first (u_team_asleep); with every open task held
        by USER or external members, only USER (u_team_waits, once per key). The clock is dropped by a pass that does
        not find the team asleep and by resume. It runs last: a member that items 1-8 woke has news pending."""
        goal = self.goal()
        if not goal or goal.get("status") != "active" or not self._team_may_sleep():
            self._team_asleep = None
            return
        now, wait = time.time(), self.config.agents.idle_nudge_seconds
        key = self._board_key()
        idle = self._idle_llm_members()
        seen = self._team_asleep
        since = seen[1] if seen is not None and seen[0] == key else now
        # a member's activation between two passes restarts the clock (not a failing retry: those in error are left out)
        since = max([since] + [float(m.get("last_active") or 0) for m in idle])
        self._team_asleep = (key, since)
        per_key = self._team_asleep_woken[1] if self._team_asleep_woken and self._team_asleep_woken[0] == key else 0
        if per_key >= TEAM_ASLEEP_MAX:
            return
        sent = int(self.db.scalar("SELECT COUNT(*) FROM notifications WHERE kind = 'n_team_asleep' AND ts >= ?",
                                  (float(goal.get("updated_at") or 0),)) or 0)
        if now - since < TEAM_ASLEEP_FACTOR * 2 ** min(sent, TEAM_ASLEEP_DOUBLINGS) * wait:
            return
        open_tasks = self.board.list(status=[Status.TODO, Status.IN_PROGRESS, Status.WAITING])
        name = self._team_asleep_whom(idle, open_tasks)
        if name is None:
            if open_tasks and all(t.assignee is not None and self.kind_of(t.assignee) != AgentKind.LLM
                                  for t in open_tasks):
                # nothing a member could take: only USER (or an external member) can move the run
                self._team_asleep_woken = (key, TEAM_ASLEEP_MAX)
                self.notify(USER, "u_team_waits", elapsed=fmt_duration(now - since, self.language_of(USER)),
                            tasks=", ".join(f"#{t.id}" for t in open_tasks))
            return  # else open tasks held only by members in error: USER was told of the error (u_agent_error)
        awaited = {j.id for j in self.jobs.awaited(name)}
        quiet = [j.label for j in self.jobs.running_of(name) if j.id not in awaited]
        lang = self.language_of(name)
        self.notify(name, "n_team_asleep", elapsed=fmt_duration(now - since, lang),
                    jobs=prompts.t(lang, "team_asleep_jobs", jobs=", ".join(quiet)) if quiet else "")
        self._team_asleep_woken = (key, per_key + 1)
        if per_key == 0:
            try:
                self.notify(USER, "u_team_asleep", elapsed=fmt_duration(now - since, self.language_of(USER)), name=name)
            except Exception:
                log.exception("could not tell the user that the team is asleep")

    def _team_may_sleep(self) -> bool:
        """Item 9's conditions besides the active goal (docs/SPEC.md §3 item 9 *When*): an open task (todo,
        in_progress, waiting), or every task terminal after item 6 spoke for this board state; no review that item 5
        still works on (independent review and an LLM reviewer) and not item 7's state (USER's reviews wait with
        nothing in progress or actionable); no clarify planner waiting for the user's answers; team_asleep()."""
        counts = self.board.counts()
        total = sum(counts.values())
        if total == 0:
            return False
        if not any(counts.get(s, 0) for s in (Status.TODO, Status.IN_PROGRESS, Status.WAITING)):
            if any(counts.get(s, 0) for s in Status.OPEN) or self._all_done_key != self._board_key():
                return False
        if not self.team_asleep():  # first among the rest: false at once while anyone works
            return False
        reviews = self.board.list(status=Status.REVIEW) if counts.get(Status.REVIEW, 0) else []
        if self.board.independent_review and any(
                t.reviewer and self.kind_of(t.reviewer) == AgentKind.LLM and self.is_member(t.reviewer)
                for t in reviews):
            return False
        if (any(t.reviewer == USER for t in reviews) and not self._has_task_in_progress()
                and not self._has_actionable_todo()):
            return False
        if self.config.swarm.clarify:
            for t in self.board.list(tag="planning"):
                if t.assignee and self.kind_of(t.assignee) == AgentKind.LLM and self.asks_user(t.assignee, t):
                    return False
        return True

    def _board_key(self) -> tuple:
        """Items 6 and 9's board key: (task count, MAX(tasks.updated_at)); a claim or a status change makes a new one,
        a comment does not."""
        return (self.db.scalar("SELECT COUNT(*) FROM tasks"), self.db.scalar("SELECT MAX(updated_at) FROM tasks"))

    def _team_asleep_whom(self, idle: list[dict], open_tasks: list[Task]) -> str | None:
        """The idle LLM member item 9 wakes, the one active last of the first group that has one: holding an
        in_progress or waiting task; holding a todo one; any, when an open task has no assignee or none is left."""
        held: dict[str, set[str]] = {}
        for t in open_tasks:
            if t.assignee:
                held.setdefault(t.assignee, set()).add(t.status)
        groups = [[m for m in idle if held.get(m["name"], set()) & {Status.IN_PROGRESS, Status.WAITING}],
                  [m for m in idle if Status.TODO in held.get(m["name"], set())]]
        if not open_tasks or any(t.assignee is None for t in open_tasks):
            groups.append(idle)
        for group in groups:
            if group:
                return max(group, key=lambda m: float(m.get("last_active") or 0))["name"]
        return None

    def _check_quiet(self) -> None:
        """T2 of a thread session's pass, the counterpart of item 9 (docs/SPEC.md §22.7): the goal is active, the team
        asleep (team_asleep with its thread clauses) and the clarify asker does not wait for the user's answers. Once
        that has lasted 2 ** min(sent, TEAM_ASLEEP_DOUBLINGS) x agents.idle_nudge_seconds (sent: the n_thread_quiet*
        notices under the current goal) with one thread key, one idle LLM member gets n_thread_quiet (its wrap-up
        form in wrap-up mode), at most TEAM_ASLEEP_MAX times per key; USER gets u_thread_quiet once per key with a
        nudge that follows an earlier one of this goal, or with the first in wrap-up mode. The clock is dropped by a
        pass that does not find the team quiet, by start() and by resume()."""
        goal = self.goal()
        asker = self._clarify_asker()
        idle = self._idle_llm_members()
        if (not goal or goal.get("status") != "active" or not idle or not self.team_asleep()
                or (asker is not None and self.asks_user(asker))):
            self._thread_quiet = None
            return
        now, wait = time.time(), max(0.0, float(self.config.agents.idle_nudge_seconds))
        key = self._thread_key()
        seen = self._thread_quiet
        since = seen[1] if seen is not None and seen[0] == key else now
        # a member's activation between two passes restarts the clock (not a failing retry: those in error are left out)
        since = max([since] + [float(m.get("last_active") or 0) for m in idle])
        self._thread_quiet = (key, since)
        woken = self._thread_quiet_woken
        per_key, told = (woken[1], woken[2]) if woken is not None and woken[0] == key else (0, False)
        if per_key >= TEAM_ASLEEP_MAX:
            return
        sent = int(self.db.scalar("SELECT COUNT(*) FROM notifications WHERE kind IN ('n_thread_quiet', "
                                  "'n_thread_quiet_wrap_up') AND ts >= ?", (float(goal.get("updated_at") or 0),)) or 0)
        if now - since < 2 ** min(sent, TEAM_ASLEEP_DOUBLINGS) * wait:
            return
        name = self._quiet_whom(idle, asker)
        awaited = {j.id for j in self.jobs.awaited(name)}
        quiet = [j.label for j in self.jobs.running_of(name) if j.id not in awaited]
        lang = self.language_of(name)
        wrapping_up = self.wrapping_up
        self.notify(name, "n_thread_quiet_wrap_up" if wrapping_up else "n_thread_quiet", vote=self._vote_text(lang),
                    jobs=prompts.t(lang, "thread_quiet_jobs", jobs=", ".join(quiet)) if quiet else "")
        tell = (sent >= 1 or wrapping_up) and not told
        self._thread_quiet_woken = (key, per_key + 1, told or tell)
        if tell:
            try:
                lang = self.language_of(USER)
                self.notify(USER, "u_thread_quiet", vote=self._vote_text(lang), elapsed=fmt_duration(now - since, lang))
            except Exception:
                log.exception("could not tell the user that the thread is quiet")

    def _thread_key(self) -> tuple:
        """T2's thread key: (MAX(posts.active_at), the comments, the votes, the newest file.changed or files.committed
        event, the newest message); a post, an edit, a comment, a new vote, a workspace change or a message makes a new
        one, a vote turned from like to dislike does not."""
        one = self.db.scalar
        return (one("SELECT MAX(active_at) FROM posts"), one("SELECT COUNT(*) FROM post_comments"),
                one("SELECT COUNT(*) FROM votes"),
                one("SELECT MAX(id) FROM events WHERE type IN ('file.changed', 'files.committed')"),
                one("SELECT MAX(id) FROM messages"))

    def _quiet_whom(self, idle: list[dict], asker: str | None) -> str:
        """The idle LLM member T2 wakes: the clarify asker while it is idle in its clarify phase, else the one least
        recently active (never active counts as oldest), so successive nudges go round the team."""
        if asker is not None and any(m["name"] == asker for m in idle) and self._in_clarify_phase(asker):
            return asker
        return min(idle, key=lambda m: float(m.get("last_active") or 0))["name"]

    def _vote_text(self, language: str) -> str:
        """The vote line on #1 under the session's rule in `language` (goal_vote_line or its veto form)."""
        return vote_line(language, goal_tally(self.db, self.goal_rule))

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
        # the thread mode (docs/SPEC.md §22.8 "Snapshot and REST"): one query each, nothing in a board session
        posts = self.forum.listing(USER) if self.forum is not None else []
        subscriptions = self.forum.subscriptions_by_member() if self.forum is not None else {}
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
                "subscriptions": subscriptions.get(m["name"], []),  # [{id, kind, target, by, mode, auto}]
            })
        pause = self.db.get("paused") or {}
        goal = self.goal()
        question = self.pending_question()
        if question is not None:
            asked = self.db.scalar("SELECT text FROM messages WHERE id = ?", (question["message_id"],)) or ""
            question["text"] = truncate_middle(asked, QUESTION_PREVIEW_CHARS)
        # the clarify planner waits for the user's answers (asks_user): the board's quiet banner
        clarify_wait = next(({"name": t.assignee, "task_id": t.id} for t in tasks
                             if t.status in Status.OPEN and "planning" in t.tags and prompts.CLARIFY_TAG in t.tags
                             and t.assignee and self.asks_user(t.assignee, t)), None)
        if self.forum is not None:  # a thread session: #2's asker (docs/SPEC.md §22.4 "Clarify")
            asker = self._clarify_asker()
            clarify_wait = {"name": asker, "post_id": PLAN_POST} if asker and self.asks_user(asker) else None
        return {
            "swarm": {
                "name": self.config.swarm.name, "language": self.language, "paused": bool(pause.get("paused")),
                "pause_reason": pause.get("reason", ""), "wrap_up": self.wrapping_up, "limits": self.limits(),
                "agent_count": self.agent_count(),  # what limits.max_agents counts: external members do not
                "spent_usd": round(self.spent_usd(), 4), "goal": goal, "counts": counts,
                "mcp_url": self.hub_url, "models": list(self.config.models),
                # the hub asks every peer for its token (hubauth.py): the board's MCP command needs the header;
                # never the token itself
                "mcp_token": hubauth.hub_digest() is not None,
                "session": None if self.session_id is None else {
                    "id": self.session_id, "title": self.session_title or self.session_id,
                    "status": "running" if self._started else "stopped",
                    "pending_goal": None if goal else self.session_pending_goal},
                "tracing": bool(self.tracer.enabled),
                "independent_review": self.board.independent_review,
                "workspace": self.workspace_info(),
                "network": netoff.describe(self.config),  # {"mode": "on"|"off", "isolation": docker|netns|env}
                # a member's question that holds the pause: {asker, message_id, since, text} | None
                "question": question,
                "clarify": bool(self.config.swarm.clarify),  # the setting ([swarm] clarify)
                "auto_reply": self.auto_reply_active,  # a `dreamteam -p` client answers questions automatically
                "clarify_wait": clarify_wait,  # {name, task_id} | {name, post_id: 2} (a thread session) | None
                "mode": self.mode,  # the session's work mode, "board" | "thread" (docs/SPEC.md §22.8)
                "goal_rule": self.goal_rule,
            },
            "tasks": [t.to_dict() for t in tasks],
            "posts": posts,  # every post without its text, in the feed's order ([] in a board session)
            "goal_vote": self.forum.goal_vote() if self.forum is not None else None,  # forum.goal_tally | None
            "agents": members,
            "triggers": self.triggers.list(include_inactive=False),
            "jobs": jobs,
            "mcp": self.forge.list(USER),
            "skills": self.skills.public(),  # from memory: no query
            "locks": self.workspace.locks(),
            "user": {"unread": unread.get(USER, 0)},
        }


__all__ = ["Swarm", "loads"]
