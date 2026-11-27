"""Slash commands of the terminal UI (docs/TUI.md §4.3): a data-driven registry (it feeds `/help` and the
suggestions) and the async handlers. A handler gets a `CommandContext` (the app, the hub client, the
session id, the render context and the argument text); hub errors are reported by `execute()`."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from .. import roles
from ..console import _SCP_URL, _SKILL_REF, match_addressee, resolve_folder, unquote
from ..models import USER
from . import render
from .client import HubError
from .dialogs import ConfirmDialog
from .i18n import TEXTS, t
from .input import ALL, CommandInfo, did_you_mean, number, parse_command, resolve_member

if TYPE_CHECKING:
    from .app import TUI

Handler = Callable[["CommandContext"], Awaitable[None]]

TRACES_LIMIT = 20
CONTEXT_PAGE = 30
# "/approve 12", "/reject #12 ...": a task in review. Agent-built MCP servers are named like mcp_forge.NAME_RE
# (they start with a letter) and external servers are never pending, so a number is never an MCP server.
_TASK_REF = re.compile(r"#?\d+")
# "/approve calc 3f2a9c01": the digest (or its start) the dialog, mcp_list or the MCP card show; the console takes it too
_DIGEST = re.compile(r"[0-9a-fA-F]{8,64}")
_JOB_REF = re.compile(r"[jJ%#]?(\d+)")          # "/jobs j3", "/jobs stop 3": a background job of an agent
JOB_STOP = ("stop", "kill", "стоп")
WRAP_UP_ON = ("", "on", "вкл")                  # /wrapup: the user's wrap-up mode on ...
WRAP_UP_OFF = ("off", "выкл", "stop", "стоп")   # ... or back off
CLARIFY_ON = ("on", "вкл")                      # /clarify: clarify mode ([swarm] clarify) on ...
CLARIFY_OFF = ("off", "выкл")                   # ... or off
# a thread session (docs/SPEC.md §22.8 "TUI"): a post's number (9, #9, p9), a comment's (c31), a vote's target
_POST_NUMBER = re.compile(r"(?:#|[pP])?(\d+)")
_COMMENT_NUMBER = re.compile(r"[cC](\d+)")
_VOTE_TARGET = re.compile(r"(#|[pPcC])?(\d+)")
USER_NAMES = ("user", "пользователь")           # /feed @user: the user's posts
RULE_WORDS = {"democracy": "democracy", "демократия": "democracy", "veto": "veto", "вето": "veto"}
MODE_WORDS = {"board": "board", "доска": "board", "thread": "thread", "тред": "thread"}
BOTH, BOARD, THREAD = ("board", "thread"), ("board",), ("thread",)

# CLI flags that become session overrides of a new session (the sections of the launcher's modal)
FLAT_OVERRIDES: dict[str, tuple[str, str]] = {
    "model": ("agents", "default_model"),
    "agents": ("swarm", "start_agents"),
    "start_agents": ("swarm", "start_agents"),
    "max_agents": ("swarm", "max_agents"),
    "budget": ("swarm", "max_cost_usd"),
    "max_cost_usd": ("swarm", "max_cost_usd"),
    "language": ("swarm", "language"),
    "network": ("swarm", "network"),   # --network off (docs/SPEC.md §7.2)
    "clarify": ("swarm", "clarify"),   # --clarify / --no-clarify (docs/SPEC.md §3 "Clarify mode")
    "mode": ("swarm", "mode"),         # --mode board|thread (docs/SPEC.md §22.8)
    "goal_rule": ("swarm", "goal_rule"),   # --goal-rule democracy|veto
}


class UsageError(Exception):
    """The arguments of a command are wrong: `execute()` prints its usage line."""


@dataclass(frozen=True)
class Command:
    name: str
    handler: Handler
    aliases: tuple[str, ...] = ()
    args: bool = False        # takes arguments (their text is the i18n key cmdargs_<name>)
    required: bool = False    # the arguments are required
    session: bool = False     # needs an attached session
    launcher: bool = False    # needs a launcher hub (sessions, settings)
    # the work modes whose /help and suggestions list it (docs/SPEC.md §22.8: a thread session lists its own
    # commands); it runs in either, and says what to use instead where it does not apply
    modes: tuple[str, ...] = BOTH

    def _text(self, prefix: str, lang: str, mode: str) -> str:
        """`<prefix><name>`, or its `_thread` variant in a thread session when there is one."""
        key = prefix + self.name
        if mode == "thread" and key + "_thread" in TEXTS.get(lang, TEXTS["en"]):
            key += "_thread"
        return t(lang, key)

    def description(self, lang: str, mode: str = "board") -> str:
        return self._text("cmd_", lang, mode)

    def args_text(self, lang: str, mode: str = "board") -> str:
        return self._text("cmdargs_", lang, mode) if self.args else ""

    def usage(self, lang: str, mode: str = "board") -> str:
        return f"/{self.name} {self.args_text(lang, mode)}".rstrip()

    def listed(self, mode: str = "board") -> bool:
        return mode in self.modes

    def help_row(self, lang: str, mode: str = "board") -> dict[str, Any]:
        return {"name": self.name, "args": self.args_text(lang, mode), "description": self.description(lang, mode),
                "aliases": list(self.aliases)}

    def info(self, lang: str, mode: str = "board") -> CommandInfo:
        return CommandInfo(self.name, self.description(lang, mode), self.aliases, self.args_text(lang, mode),
                           self.required)


@dataclass
class CommandContext:
    app: "TUI"
    command: Command
    args: str

    @property
    def client(self):
        return self.app.client

    @property
    def sid(self) -> str | None:
        return self.app.sid

    @property
    def ctx(self) -> render.RenderContext:
        return self.app.ctx

    @property
    def lang(self) -> str:
        return self.app.ctx.lang

    def t(self, key: str, **kw: Any) -> str:
        return t(self.lang, key, **kw)

    def print(self, renderable: Any) -> None:
        self.app.print(renderable)

    def info(self, text: str) -> None:
        self.app.print(render.info(self.ctx, text))

    def error(self, text: str) -> None:
        self.app.print(render.error(self.ctx, text))

    async def act(self, tool: str, args: dict | None = None, *, report: bool = False) -> bool:
        """Run a hub tool as the user; a refusal is printed as an error, the result as info if `report`."""
        ok, text = await self.client.act(self.sid, tool, args or {})
        if not ok:
            self.error(text)
        elif report and text:
            self.info(text)
        return ok

    async def state(self) -> dict:
        return await self.app.refresh_state()


def _mode(app: Any) -> str:
    """The attached session's work mode ("board" without one; a stand-in app without `mode` is a board)."""
    return getattr(app, "mode", "board") if getattr(app, "attached", False) else "board"


def _thread(app: Any) -> bool:
    return _mode(app) == "thread"


def _rule_word(c: "CommandContext", rule: Any) -> str:
    return c.t("ev_rule_" + rule) if rule in ("democracy", "veto") else str(rule or "?")


def _split(text: str) -> tuple[str, str]:
    head, _, tail = text.partition("|")
    return head.strip(), tail.strip()


def _int_arg(text: str) -> int:
    value = text.strip().lstrip("#")
    if not value.isdigit():
        raise UsageError
    return int(value)


def _task_ref(text: str) -> tuple[int | None, str]:
    """(task id, the rest) of "12 the rest" / "#12 the rest"; (None, "") when the first word is not a task. The rest
    is free text taken as typed, without one pair of quotes around all of it ("12 'needs tests'")."""
    head, _, rest = text.strip().partition(" ")
    if not _TASK_REF.fullmatch(head):
        return None, ""
    rest = rest.strip()
    if len(rest) >= 2 and rest[0] == rest[-1] and rest[0] in "'\"":
        rest = rest[1:-1].strip()
    return int(head.lstrip("#")), rest


async def _role_catalog(c: "CommandContext") -> list[dict] | None:
    """The visible role catalog [{key, name, description, instructions, model, builtin}]: the attached session's
    (in its language), else the launcher's settings (in the UI language); None without either."""
    if c.app.attached:
        return list((await c.client.roles(c.sid)).get("roles") or [])
    if c.client.mode != "launcher":
        return None
    return _localized(c.lang, (await c.client.settings()).get("role_catalog"))


def _localized(lang: str, catalog: Any) -> list[dict]:
    """GET /api/settings "role_catalog" (names and descriptions in both languages) in `lang`, hidden roles dropped."""
    def pick(value: Any) -> str:
        return str((value.get(lang) or value.get("en") or "") if isinstance(value, dict) else value or "")
    return [{"key": r.get("key"), "name": pick(r.get("name")), "description": pick(r.get("description")),
             "instructions": r.get("instructions") or "", "model": r.get("model"), "builtin": bool(r.get("builtin")),
             "skills": list(r.get("skills") or [])}
            for r in catalog or [] if isinstance(r, dict) and not r.get("hidden")]


def _team_text(c: "CommandContext", rows: list[tuple[str, int]], catalog: list[dict]) -> str:
    """The rows of a starting team as "Архитектор ×1 · Разработчик ×2", the roles named in the UI language."""
    names = {r.get("key"): r.get("name") for r in catalog}
    times = "×" if c.ctx.unicode else "x"

    def name(role: str) -> str:
        key = roles.role_key(role)
        if names.get(key):
            return str(names[key])
        preset = roles.ROLE_PRESETS.get(c.lang, roles.ROLE_PRESETS["en"]).get(key)
        return preset[0] if preset else role
    return f" {c.ctx.sym['dot']} ".join(f"{name(role)} {times}{count}" for role, count in rows)


# ---- handlers -------------------------------------------------------------------------------------
async def _help(c: CommandContext) -> None:
    c.print(render.help_view(c.ctx, help_rows(c.lang, _mode(c.app)), thread=_thread(c.app)))


async def _new(c: CommandContext) -> None:
    if c.args:
        await c.app.new_session(c.args)
    else:
        c.app.ask_goal()


async def _sessions(c: CommandContext) -> None:
    await c.app.open_picker()


async def _resume(c: CommandContext) -> None:
    if c.args:
        await c.app.attach_id(c.args.split()[0])
    else:
        await c.app.open_picker()


async def _delete(c: CommandContext) -> None:
    if c.args:
        await c.app.delete_session(c.args.split()[0])
    else:
        await c.app.open_picker(deleting=True)


async def _goal(c: CommandContext) -> None:
    """`/goal`: the goal panel (a thread session's adds #1's vote line, the rule and the decision); `/goal text`: a
    new goal, which in a thread session with a goal asks first (#1 and #2 are rewritten, the votes cleared)."""
    thread = _thread(c.app)
    if not c.args:
        state = await c.state()
        swarm = state.get("swarm") or {}
        if thread:
            c.print(render.goal_panel(c.ctx, swarm.get("goal"), vote=state.get("goal_vote"),
                                      rule=swarm.get("goal_rule")))
        else:
            c.print(render.goal_panel(c.ctx, swarm.get("goal")))
        return
    if thread and ((await c.state()).get("swarm") or {}).get("goal"):
        if not await c.app.ask(ConfirmDialog(c.lang, c.t("goal_rewrite_title"), c.t("goal_rewrite"),
                                             default=False)):
            c.info(c.t("goal_unchanged"))
            return
    await c.act("set_goal", {"title": c.args})  # the whole text: the hub splits it (first line = title)


async def _board(c: CommandContext) -> None:
    if _thread(c.app):   # a thread session has no board: its feed instead
        await _feed(c)
        return
    state = await c.state()
    c.print(render.board(c.ctx, state.get("tasks") or []))


async def _task(c: CommandContext) -> None:
    if _thread(c.app):
        c.error(c.t("thread_no_tasks"))
        return
    task_id = _int_arg(c.args)
    data = await c.client.task(c.sid, task_id)   # an unknown task errors before the snapshot is fetched
    try:                                         # the crew line and the subtasks come from the snapshot (§4.2)
        tasks: list[dict] | None = (await c.state()).get("tasks") or []
    except HubError:                             # without it the task still prints, with no crew line or subtasks
        tasks = None
    c.print(render.task_detail(c.ctx, data, tasks))


# ---- a thread session's commands (docs/SPEC.md §22.8 "TUI") ---------------------------------------------------
def _board_session(c: CommandContext) -> bool:
    """A thread command in a board session: says what to use there; True when it was refused so."""
    if _thread(c.app):
        return False
    c.error(c.t("board_no_thread"))
    return True


def _post_id(text: str) -> int:
    match = _POST_NUMBER.fullmatch(text.strip())
    if match is None or int(match.group(1)) <= 0:
        raise UsageError
    return int(match.group(1))


async def _feed(c: CommandContext) -> None:
    """`/feed [@Name] [text]`: the `feed` tool with `by` (a member, retired ones included, or the user) and `query`;
    an `@` that names nobody sends nothing."""
    if _board_session(c):
        return
    arg = c.args.strip()
    args: dict[str, Any] = {}
    if arg.startswith("@"):
        state = await c.state()
        names = [str(a["name"]) for a in state.get("agents") or [] if isinstance(a, dict) and a.get("name")]
        name, rest = match_addressee(arg, [*names, *USER_NAMES])
        if name is None:
            c.error(c.t("unknown_member", name=arg.split()[0].lstrip("@") or "@"))
            return
        args["by"] = USER if name in USER_NAMES else name
        if rest.strip():
            args["query"] = rest.strip()
    elif arg:
        args["query"] = arg
    ok, text = await c.client.act(c.sid, "feed", args)
    if not ok:
        c.error(text)
    else:
        c.print(render.feed_text(c.ctx, text))


async def _post(c: CommandContext) -> None:
    """`/post N`: the post with its vote and comment tree (GET /api/posts/N; its comments feed the `cM`
    suggestions); `/post` alone: the next input is a new post (its first line the title, the rest the text)."""
    if _board_session(c):
        return
    if not c.args.strip():
        c.app.ask_post()
        return
    data = await c.client.post(c.sid, _post_id(c.args))
    c.app.open_post = data
    c.print(render.post_view(c.ctx, data))


async def _comment(c: CommandContext) -> None:
    """`/comment N text`, `/comment N cM text` (a reply to comment M of post N)."""
    if _board_session(c):
        return
    match = re.match(r"(\S+)\s*(.*)\Z", c.args.strip(), re.S)
    if match is None:
        raise UsageError
    post_id, rest = _post_id(match.group(1)), match.group(2)
    args: dict[str, Any] = {"post_id": post_id}
    reply = re.match(r"(\S+)\s*(.*)\Z", rest, re.S)
    if reply is not None and _COMMENT_NUMBER.fullmatch(reply.group(1)):
        args["reply_to"] = f"c{int(reply.group(1)[1:])}"
        rest = reply.group(2)
    if not rest.strip():
        raise UsageError
    args["text"] = rest.strip()
    await c.app.ensure_running()   # as a message: a comment to a stopped session starts it
    await c.act("comment", args)   # the post.comment line says it


async def _vote(c: CommandContext, value: str) -> None:
    """`/like`, `/dislike`, `/unvote` with N (sent as #N: the forum refuses a bare number), #N, pN or cN."""
    if _board_session(c):
        return
    arg = c.args.strip()
    match = _VOTE_TARGET.fullmatch(arg)
    if match is None or int(match.group(2)) <= 0:
        raise UsageError
    target = arg if match.group(1) else f"#{int(match.group(2))}"
    await c.act("vote", {"target": target, "value": value}, report=True)


async def _like(c: CommandContext) -> None:
    await _vote(c, "like")


async def _dislike(c: CommandContext) -> None:
    await _vote(c, "dislike")


async def _unvote(c: CommandContext) -> None:
    await _vote(c, "none")


async def _done(c: CommandContext) -> None:
    """`/done [summary]`: the user completes the goal of a thread session (swarm_control complete_goal), after a
    confirmation with «No» preselected; on a board the agents complete it."""
    if not _thread(c.app):
        c.error(c.t("board_done"))
        return
    if not await c.app.ask(ConfirmDialog(c.lang, c.t("done_title"), c.t("done_text"), default=False)):
        c.info(c.t("goal_unchanged"))
        return
    summary = c.args.strip()
    await c.act("swarm_control", {"complete_goal": True, **({"goal_summary": summary} if summary else {})})


async def _rule(c: CommandContext) -> None:
    """`/rule [democracy|veto]`: a thread session's goal rule (shown, or changed live through PATCH); a board
    session has none; without a session the rule of the thread sessions this window creates (as /clarify)."""
    arg = c.args.strip().casefold()
    rule = RULE_WORDS.get(arg)
    if arg and rule is None:
        raise UsageError
    if c.app.attached:
        if not _thread(c.app):
            c.info(c.t("rule_board"))
            return
        if rule is None:
            state = await c.state()
            c.info(c.t("rule_session", rule=_rule_word(c, (state.get("swarm") or {}).get("goal_rule"))))
            return
        data = await c.client.update_session(c.sid, {"swarm": {"goal_rule": rule}})
        if isinstance(data.get("session"), dict):
            c.app.session = {**(c.app.session or {}), **data["session"]}
        c.info(c.t("rule_session_set", rule=_rule_word(c, rule)))
        try:   # the vote is counted again under the new rule: the panel and the status read the snapshot
            await c.state()
        except HubError:
            pass
        return
    swarm = c.app.overrides.get("swarm") or {}
    if rule is None:
        if "goal_rule" in swarm:
            value, source = swarm["goal_rule"], "clarify_window"
        else:
            view = (await c.client.settings()).get("settings") or {}
            value, source = (view.get("swarm") or {}).get("goal_rule") or "democracy", "clarify_inherited"
        c.info(c.t("rule_new_state", rule=_rule_word(c, value), source=c.t(source)))
        return
    c.app.overrides.setdefault("swarm", {})["goal_rule"] = rule
    c.info(c.t("rule_new_set", rule=_rule_word(c, rule)))


async def _mode_command(c: CommandContext) -> None:
    """`/mode [board|thread]`: a session's mode (chosen when it is created: an argument is refused), or without a
    session the mode of the sessions this window creates (as /clarify)."""
    arg = c.args.strip().casefold()
    mode = MODE_WORDS.get(arg)
    if arg and mode is None:
        raise UsageError
    if c.app.attached:
        if arg:
            c.error(c.t("mode_fixed"))
            return
        c.info(c.t("mode_session", mode=c.t("mode_" + _mode(c.app))))
        return
    swarm = c.app.overrides.get("swarm") or {}
    if mode is None:
        if swarm.get("mode") in MODE_WORDS.values():
            value, source = swarm["mode"], "clarify_window"
        else:
            view = (await c.client.settings()).get("settings") or {}
            value = "thread" if (view.get("swarm") or {}).get("mode") == "thread" else "board"
            source = "clarify_inherited"
        c.info(c.t("mode_new_state", mode=c.t("mode_" + value), source=c.t(source)))
        return
    c.app.overrides.setdefault("swarm", {})["mode"] = mode
    c.info(c.t("mode_new_set", mode=c.t("mode_" + mode)))


async def _agents(c: CommandContext) -> None:
    state = await c.state()
    c.print(render.agents_table(c.ctx, state.get("agents") or []))


async def _jobs(c: CommandContext) -> None:
    """`/jobs`: the agents' background jobs (from the state: the running ones, then those ended in the last hour);
    `/jobs jN`: one job's state and the end of its output (the `jobs` tool); `/jobs stop jN [jM …]`: stop jobs (the
    `job_stop` tool: the user may stop any member's job). Pausing the swarm does not stop them."""
    words = c.args.split()
    if not words:
        state = await c.state()
        c.print(render.jobs_table(c.ctx, state.get("jobs") or []))
        return
    stop = words[0].casefold() in JOB_STOP
    refs = [_JOB_REF.fullmatch(word) for word in (words[1:] if stop else words)]
    if not refs or not all(refs) or (not stop and len(refs) > 1):
        raise UsageError
    if stop:
        for ref in refs:
            await c.act("job_stop", {"job_id": f"j{ref.group(1)}"}, report=True)
        return
    ok, text = await c.client.act(c.sid, "jobs", {"job_id": f"j{refs[0].group(1)}"})
    if not ok:
        c.error(text)
    else:
        c.print(render.job_output(c.ctx, text))


SKILL_WORDS = {"lib": "lib", "library": "lib", "библиотека": "lib", "add": "add", "attach": "add",
               "update": "update", "rm": "rm", "detach": "rm", "remove": "rm", "show": "show", "pending": "pending",
               "approve": "approve", "reject": "reject", "reinstall": "reinstall", "log": "log",
               "clean-envs": "clean-envs"}
SKILL_ECOSYSTEMS = ("python", "node")


def _skill_add_args(c: CommandContext, text: str) -> tuple[dict, str | None]:
    """`add X [--pick a,b] [--replace]` -> (skill_attach arguments, the reference when X is one). A reference
    ("pdf", "claude-user:pdf") attaches; a folder or .zip path (resolved here when the hub is on this computer)
    or a Git URL is imported first."""
    words, replace, pick = text.split(), False, None
    if "--replace" in words:
        replace, words = True, [w for w in words if w != "--replace"]
    if "--pick" in words:
        at = words.index("--pick")
        if at + 1 >= len(words):
            raise UsageError
        pick = [p for p in words[at + 1].split(",") if p]
        del words[at:at + 2]
    target = unquote(" ".join(words))
    if not target:
        raise UsageError
    extra = {**({"replace": True} if replace else {}), **({"pick": pick} if pick else {})}
    if _SKILL_REF.fullmatch(target):
        return {"name": target, **extra}, target
    if not ("://" in target or _SCP_URL.match(target)):   # a folder or .zip path on the hub's machine
        path = resolve_folder(target, local=c.app.local)
        if path is None:
            c.error(c.t("cd_remote_relative"))
            return {}, None
        target = path
    return {"source": target, **extra}, None


async def _skills(c: CommandContext) -> None:
    """`/skills`: the session's skills (without a session: the next new one's); `lib`: the library and Claude Code's;
    `add X [--pick a,b] [--replace]` (a name, a folder or .zip path, a Git URL; without a session: for the next new
    one); `update X` (again from its own source); `rm X`; `show X`; `pending [id]` (proposals waiting, also of a
    stopped session); `approve X [replace]`; `reject X [reason]`; `reinstall X [python|node]`; `log X [python|node]`;
    `clean-envs` (the launcher removes unused skill environments)."""
    word, _, rest = c.args.strip().partition(" ")
    rest = rest.strip()
    if not word:
        if not c.app.attached:
            c.print(render.skills_next(c.ctx, c.app.skills_override))
            return
        state = await c.state()
        c.print(render.skills_table(c.ctx, state.get("skills") or []))
        return
    sub = SKILL_WORDS.get(word.casefold())
    if sub is None:
        raise UsageError
    if sub == "clean-envs":
        if c.client.mode != "launcher":
            c.error(c.t("launcher_only"))
            return
        report = await c.client.clean_skill_envs()
        kept = [k for k in report.get("kept") or [] if isinstance(k, dict)]
        text = c.t("skills_envs_cleaned", removed=len(report.get("removed") or []))
        if kept:   # each kept one says why: in use, or being built right now
            words = {"in use": c.t("skills_why_in_use"), "being built": c.t("skills_why_being_built")}
            why = ", ".join(dict.fromkeys(words.get(k.get("why"), str(k.get("why") or "?")) for k in kept))
            text += c.t("skills_envs_kept", kept=len(kept), why=why)
        c.info(text)
        return
    if sub == "lib":
        c.print(render.skills_library(c.ctx, await c.client.skills(c.sid if c.app.attached else None)))
        return
    if sub == "pending":
        sid = rest.split()[0] if rest else c.sid
        if c.client.mode != "launcher" or not sid:
            c.error(c.t("launcher_only") if c.client.mode != "launcher" else c.t("no_session"))
            return
        c.print(render.skills_pending(c.ctx, sid, (await c.client.session_skills(sid)).get("attached") or [],
                                      other=not (c.app.attached and sid == c.sid)))
        return
    if sub in ("approve", "reject"):
        await _skill_decide(c, sub == "approve", rest)
        return
    name, _, tail = rest.partition(" ")
    tail = tail.strip()
    if not name or (sub in ("reinstall", "log") and tail and tail not in SKILL_ECOSYSTEMS):
        raise UsageError
    if sub == "add":
        args, ref = _skill_add_args(c, rest)
        if not args:
            return
        if not c.app.attached:   # the next new session gets it (as `--skill`)
            c.app.skills_override.append(args.get("source") or ref)
            c.info(c.t("skills_added_next", name=args.get("source") or ref))
            return
        await c.act("skill_attach", args, report=True)
        return
    if sub == "show" and not c.app.attached:
        origin, _, bare = name.rpartition(":")
        c.print(render.skill_detail(c.ctx, await c.client.skill(None, bare, origin or "library"), origin or "library"))
        return
    if not c.app.attached:
        c.error(c.t("no_session"))
        return
    if sub == "show":
        await _skill_show(c, name)
    elif sub == "update":
        state = await c.state()
        skill = next((s for s in state.get("skills") or [] if isinstance(s, dict) and s.get("name") == name), None)
        if skill is None:
            c.error(c.t("skill_not_attached", name=name))
        elif skill.get("from") == "agent":   # a member's proposal has no source of its own
            c.error(c.t("skill_agent_update", name=name))
        else:
            await c.act("skill_attach", {"name": f"{skill.get('from')}:{name}", "replace": True}, report=True)
    elif sub == "rm":
        await c.act("skill_detach", {"name": name}, report=True)
    elif sub == "reinstall":
        await c.act("skill_reinstall", {"name": name, **({"ecosystem": tail} if tail else {})}, report=True)
    else:
        c.print(render.skill_log(c.ctx, await c.client.skill_log(c.sid, name, tail or None)))


async def _skill_decide(c: CommandContext, approve: bool, rest: str) -> None:
    """`/skills approve X [replace] [--session ID]`, `/skills reject X [reason] [--session ID]`: the attached
    session's proposal, or with --session another session's (also a stopped one, which is not started for it:
    the launcher's decide route)."""
    words = rest.split()
    sid = None
    if "--session" in words:
        at = words.index("--session")
        if at + 1 >= len(words):
            raise UsageError
        sid = words[at + 1]
        words = words[:at] + words[at + 2:]
    if not words:
        raise UsageError
    name, tail = words[0], " ".join(words[1:])
    if sid is not None and not (c.app.attached and sid == c.sid):
        if c.client.mode != "launcher":
            c.error(c.t("launcher_only"))
            return
    elif not c.app.attached:
        c.error(c.t("no_session"))
        return
    else:
        sid = None
    comment = "" if approve else tail.strip().strip("\"'").strip()
    await c.app.decide_skill(name, approve, comment, replace=approve and tail.casefold() == "replace", sid=sid)


async def _skill_show(c: CommandContext, name: str) -> None:
    """`/skills show X`: the session's copy of an attached skill, else the library's (`claude-user:x`: that one)."""
    origin, _, bare = name.rpartition(":")
    where = origin or "library"
    if not origin:
        state = await c.state()
        if any(isinstance(s, dict) and s.get("name") == bare for s in state.get("skills") or []):
            where = "session"
    c.print(render.skill_detail(c.ctx, await c.client.skill(c.sid, bare, where), where))


async def _roles(c: CommandContext) -> None:
    catalog = await _role_catalog(c)
    if catalog is None:
        c.error(c.t("no_session"))
        return
    name = c.args.strip()
    if not name:
        c.print(render.roles_table(c.ctx, catalog))
        return
    key, typed = roles.role_key(name), roles.normalize(name).casefold()
    role = next((r for r in catalog
                 if r.get("key") == key or roles.normalize(r.get("name")).casefold() == typed), None)
    if role is None:
        c.error(c.t("unknown_role", name=name))
        return
    c.print(render.role_detail(c.ctx, role))


async def _team(c: CommandContext) -> None:
    """The starting team of the sessions this window creates next (like --team): roles in order, or a team saved in the
    settings, copied now (docs/SPEC.md §17.3 "Saved teams"); the settings stay as they are."""
    # quotes as other commands take them, around the argument or around each item of a list
    arg = unquote(",".join(unquote(item) for item in c.args.split(",")))
    swarm = c.app.overrides.get("swarm") or {}
    if arg.casefold() in roles.TEAM_RESET:
        swarm.pop("starting_roles", None)
        swarm.pop("start_agents", None)
        if not swarm:
            c.app.overrides.pop("swarm", None)
        c.info(c.t("team_reset"))
        return
    view = await c.client.settings()
    catalog = _localized(c.lang, view.get("role_catalog"))
    settings = view.get("settings") or {}
    saved = settings.get("teams") or []
    if not arg:
        own = {k: swarm[k] for k in ("starting_roles", "start_agents") if k in swarm}
        team = {**(settings.get("swarm") or {}), **own}
        rows = roles.team_rows(team.get("starting_roles"), team.get("start_agents") or 0)
        c.info(c.t("team_set" if own else "team_line", team=_team_text(c, rows, catalog),
                   n=sum(n for _, n in rows)))
        if saved:
            c.info(c.t("team_saved", teams=", ".join(f"{x['name']} ({len(x['roles'])})" for x in saved),
                       example=saved[0]["name"]))
        return
    try:
        kind, value = roles.team_choice(arg, saved, settings.get("roles"))
    except ValueError as exc:
        c.error(c.t("team_bad", error=exc))
        return
    if kind == "unknown":
        known = roles.close_roles(settings.get("roles"), value, c.lang) + [x["name"] for x in saved]
        c.error(c.t("team_unknown", name=value, known=", ".join(dict.fromkeys(known))))
        return
    if kind == "mixed":
        c.error(c.t("team_mixed", name=value))
        return
    named = kind == "team"
    if named:
        missing = roles.missing_roles(value, settings.get("roles"))
        if missing:
            c.error(c.t("team_broken", name=value["name"], roles=", ".join(missing)))
            return
    team = list(value["roles"]) if named else value
    swarm = c.app.overrides.setdefault("swarm", {})
    swarm["starting_roles"], swarm["start_agents"] = team, len(team)
    text = _team_text(c, roles.team_rows(team, len(team)), catalog)
    c.info(c.t("team_picked", name=value["name"], team=text, n=len(team)) if named
           else c.t("team_set", team=text, n=len(team)))


async def _to(c: CommandContext) -> None:
    name = resolve_member(c.args, c.app.members)
    if name is None:
        c.error(c.t("unknown_member", name=c.args))
        return
    c.app.set_addressee(name)
    c.info(c.t("addressee_set", name=c.t("everyone") if name == ALL else name))


async def _hire(c: CommandContext) -> None:
    role, instructions = _split(c.args)
    if not role:
        raise UsageError
    # instructions stay a required parameter of the tool: "" is fine for a catalog role
    await c.act("hire_agent", {"role": role, "instructions": instructions}, report=True)


async def _trace(c: CommandContext) -> None:
    arg = c.args.strip()
    if arg.lstrip("#").isdigit():
        c.print(render.trace_detail(c.ctx, await c.client.trace(c.sid, int(arg.lstrip("#")))))
        return
    filters: dict[str, Any] = {"limit": TRACES_LIMIT}
    if arg:
        filters["agent"] = resolve_member(arg, c.app.members) or arg
    c.print(render.traces_list(c.ctx, await c.client.traces(c.sid, **filters)))


async def _span(c: CommandContext) -> None:
    parts = c.args.split()
    if not parts:
        raise UsageError
    span_id = _int_arg(parts[0])
    if len(parts) > 1 and parts[1].casefold() in ("ctx", "context", "контекст"):
        offset = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        data = await c.client.span_context(c.sid, span_id)
        c.print(render.context_view(c.ctx, data, offset=offset, limit=CONTEXT_PAGE))
    else:
        c.print(render.span_detail(c.ctx, await c.client.span(c.sid, span_id)))


async def _files(c: CommandContext) -> None:
    try:
        data = await c.client.files(c.sid, c.args.strip() or ".")
    except HubError as exc:
        data = {"error": c.app.hub_error(exc)}
    c.print(render.files_view(c.ctx, data))


async def _unlock(c: CommandContext) -> None:
    """`/unlock path`: remove a member's lock on a file or folder (file_unlock); the members waiting for it are
    woken. The path as it was locked (the suggestions list the locked ones)."""
    await c.act("file_unlock", {"path": unquote(c.args)}, report=True)


async def _cd(c: CommandContext) -> None:
    await c.app.change_folder(c.args)


async def _copy_from(c: CommandContext) -> None:
    c.app.choose_copy_source(c.args)


async def _cost(c: CommandContext) -> None:
    c.print(render.cost_table(c.ctx, await c.state()))


async def _status(c: CommandContext) -> None:
    c.print(render.status_report(c.ctx, await c.app.status_info()))


async def _model(c: CommandContext) -> None:
    view = (await c.client.settings()).get("settings") or {}
    name = c.args.strip()
    if not name:
        c.print(render.models_table(c.ctx, view))
        return
    models = view.get("models") or {}
    if name not in models:
        c.error(c.t("unknown_profile", name=name, names=", ".join(models) or "-"))
        return
    # a section in the update replaces the stored one: send the whole [agents] section back
    await c.client.update_settings({"agents": {**(view.get("agents") or {}), "default_model": name}})
    c.info(c.t("model_set", name=name))
    missing = render.missing_prices(models[name])
    if missing:
        c.print(render.warning(c.ctx, c.t("model_no_prices", name=name, fields=", ".join(missing))))


async def _pause(c: CommandContext) -> None:
    await c.app.toggle_pause()


async def _wrapup(c: CommandContext) -> None:
    arg = c.args.strip().casefold()
    if arg in WRAP_UP_ON:
        on = True
    elif arg in WRAP_UP_OFF:
        on = False
    else:
        raise UsageError
    ok, text = await c.client.act(c.sid, "swarm_control", {"wrap_up": on})
    if not ok:
        c.error(text)
    elif "already on" in text or "was not on" in text:  # no change, so no swarm.wrap_up event prints a line
        c.info(text)


async def _clarify(c: CommandContext) -> None:
    """`/clarify [on|off]`: clarify mode ([swarm] clarify, docs/SPEC.md §3): the attached session's own setting
    (changed at once, PATCH), or without a session that of the sessions this window creates next (like --clarify
    and /team); without an argument, the state and where it comes from."""
    arg = c.args.strip().casefold()
    if arg in CLARIFY_ON:
        on: bool | None = True
    elif arg in CLARIFY_OFF:
        on = False
    elif not arg:
        on = None
    else:
        raise UsageError
    if c.app.attached:
        from ..sessions import LEGACY_ID   # (here only: the TUI of another hub needs no session code otherwise)

        if on is None:   # also for main: it has no overrides, so its state is the general settings' one
            data = await c.client.session(c.sid)
            own = ((data.get("session") or {}).get("overrides") or {}).get("swarm") or {}
            value = own.get("clarify", ((data.get("config") or {}).get("swarm") or {}).get("clarify"))
            c.info(c.t("clarify_session_state", state=c.t("on" if value else "off"),
                       source=c.t("clarify_own" if "clarify" in own else "clarify_inherited")))
            return
        if c.sid == LEGACY_ID:
            c.info(c.t("clarify_main"))
            return
        data = await c.client.update_session(c.sid, {"swarm": {"clarify": on}})
        if isinstance(data.get("session"), dict):
            c.app.session = {**(c.app.session or {}), **data["session"]}
        # a thread session clarifies in #2, with questions to the user before the work starts (docs/SPEC.md §22.8)
        on_key = "clarify_session_on_thread" if _thread(c.app) else "clarify_session_on"
        c.info(c.t(on_key if on else "clarify_session_off"))
        return
    swarm = c.app.overrides.get("swarm") or {}
    if on is None:
        if "clarify" in swarm:
            value, source = swarm["clarify"], "clarify_window"
        else:
            view = (await c.client.settings()).get("settings") or {}
            value, source = (view.get("swarm") or {}).get("clarify"), "clarify_inherited"
        c.info(c.t("clarify_new_state", state=c.t("on" if value else "off"), source=c.t(source)))
        return
    c.app.overrides.setdefault("swarm", {})["clarify"] = on
    c.info(c.t("clarify_new_on" if on else "clarify_new_off"))


async def _budget(c: CommandContext) -> None:
    value = number(c.args)
    if value is None or value < 0:
        c.error(c.t("not_a_number", value=c.args))
        return
    await c.act("swarm_control", {"max_cost_usd": value})  # swarm.limits prints the new limits


async def _max(c: CommandContext) -> None:
    value = number(c.args)
    if value is None or value < 1 or value != int(value):
        c.error(c.t("not_a_number", value=c.args))
        return
    await c.act("swarm_control", {"max_agents": int(value)})


async def _approve(c: CommandContext) -> None:
    task_id, comment = _task_ref(c.args)
    if task_id is not None and _thread(c.app):   # a thread has no tasks; `/approve name` of MCP is unchanged
        c.error(c.t("thread_no_tasks"))
        return
    if task_id is not None:
        await c.act("task_review", {"task_id": task_id, "approve": True, "comment": comment}, report=True)
        return
    parts = c.args.split()
    # "/approve name <sha256>": approved only while the server still has that digest; without one, decide_mcp sends
    # the digest of the last dialog shown for that server (put off with Esc)
    digest = parts[1] if len(parts) > 1 and _DIGEST.fullmatch(parts[1]) else ""
    await c.app.decide_mcp(parts[0], True, digest=digest)


async def _reject(c: CommandContext) -> None:
    task_id, comment = _task_ref(c.args)
    if task_id is not None and _thread(c.app):
        c.error(c.t("thread_no_tasks"))
        return
    if task_id is not None:
        if not comment:  # a return without a reason gives the agent nothing to act on
            c.error(c.t("review_reason_required", id=task_id))
            return
        await c.act("task_review", {"task_id": task_id, "approve": False, "comment": comment}, report=True)
        return
    try:
        parts = shlex.split(c.args)
    except ValueError:
        parts = c.args.split()
    if not parts:
        raise UsageError
    await c.app.decide_mcp(parts[0], False, " ".join(parts[1:]))


async def _stop(c: CommandContext) -> None:
    await c.app.stop_session()


async def _web(c: CommandContext) -> None:
    c.app.open_url(c.app.web_url(session=True))


async def _settings(c: CommandContext) -> None:
    view = (await c.client.settings()).get("settings") or {}
    c.print(render.models_table(c.ctx, view))
    c.app.open_url(c.app.web_url(session=False) + "/#settings")


async def _verbose(c: CommandContext) -> None:
    c.app.cycle_verbosity()


async def _lang(c: CommandContext) -> None:
    lang = c.args.strip().lower()
    if lang not in ("ru", "en"):
        raise UsageError
    c.app.set_lang(lang)


async def _clear(c: CommandContext) -> None:
    c.app.clear_screen()


async def _exit(c: CommandContext) -> None:
    c.app.exit()


COMMANDS: tuple[Command, ...] = (
    Command("help", _help, aliases=("?",)),
    Command("new", _new, args=True, launcher=True),
    Command("team", _team, args=True, launcher=True),
    Command("sessions", _sessions, launcher=True),
    Command("resume", _resume, args=True, launcher=True),
    Command("delete", _delete, args=True, launcher=True),
    Command("goal", _goal, args=True, session=True),
    Command("board", _board, session=True, modes=BOARD),   # in a thread session: /feed
    Command("task", _task, args=True, required=True, session=True, modes=BOARD),
    Command("feed", _feed, args=True, session=True, modes=THREAD),
    Command("post", _post, args=True, session=True, modes=THREAD),
    Command("comment", _comment, args=True, required=True, session=True, modes=THREAD),
    Command("like", _like, args=True, required=True, session=True, modes=THREAD),
    Command("dislike", _dislike, args=True, required=True, session=True, modes=THREAD),
    Command("unvote", _unvote, args=True, required=True, session=True, modes=THREAD),
    Command("done", _done, args=True, session=True, modes=THREAD),
    Command("agents", _agents, session=True),
    Command("roles", _roles, args=True),
    Command("jobs", _jobs, args=True, session=True),
    Command("skills", _skills, args=True),   # without a session: the skills of the next new one, the library
    Command("to", _to, args=True, required=True, session=True),
    Command("hire", _hire, args=True, required=True, session=True),
    Command("trace", _trace, args=True, session=True),
    Command("span", _span, args=True, required=True, session=True),
    Command("files", _files, args=True, session=True),
    Command("cat", _files, args=True, required=True, session=True),
    Command("unlock", _unlock, args=True, required=True, session=True),
    Command("cd", _cd, args=True, launcher=True),   # without a session: the folder of the next new one
    Command("copy-from", _copy_from, args=True, launcher=True),   # always the files of the next new session
    Command("cost", _cost, session=True),
    Command("status", _status),
    Command("model", _model, args=True, launcher=True),
    Command("pause", _pause, session=True),
    Command("wrapup", _wrapup, args=True, session=True),
    Command("clarify", _clarify, args=True, launcher=True),   # without a session: the next new ones (as --clarify)
    Command("rule", _rule, args=True, launcher=True),         # the same for a thread session's goal rule
    Command("mode", _mode_command, args=True, launcher=True),  # without a session: the next new ones' mode
    Command("budget", _budget, args=True, required=True, session=True),
    Command("max", _max, args=True, required=True, session=True),
    Command("approve", _approve, args=True, required=True, session=True),
    Command("reject", _reject, args=True, required=True, session=True),
    Command("stop", _stop, session=True, launcher=True),
    Command("web", _web),
    Command("settings", _settings, launcher=True),
    Command("verbose", _verbose),
    Command("lang", _lang, args=True, required=True),
    Command("clear", _clear),
    Command("exit", _exit, aliases=("quit", "q")),
)

_BY_NAME: dict[str, Command] = {}
for _cmd in COMMANDS:
    for _name in (_cmd.name, *_cmd.aliases):
        _BY_NAME[_name] = _cmd


def find(name: str) -> Command | None:
    """The command called `name` (or one of its aliases), without the slash."""
    return _BY_NAME.get(name.strip().lstrip("/").casefold())


def names() -> list[str]:
    return list(_BY_NAME)


def infos(lang: str, mode: str = "board") -> list[CommandInfo]:
    """The registry as the suggestions need it: the commands of the session's work mode."""
    return [cmd.info(lang, mode) for cmd in COMMANDS if cmd.listed(mode)]


def help_rows(lang: str, mode: str = "board") -> list[dict[str, Any]]:
    return [cmd.help_row(lang, mode) for cmd in COMMANDS if cmd.listed(mode)]


async def execute(app: "TUI", text: str) -> bool:
    """Run the slash command in `text`; False when `text` is not a command at all."""
    parsed = parse_command(text)
    if parsed is None:
        return False
    name, args = parsed
    lang = app.ctx.lang
    mode = _mode(app)
    cmd = find(name)
    if cmd is None:
        app.print(render.error(app.ctx, t(lang, "unknown_command", cmd="/" + name)))
        close = did_you_mean(name, [c.name for c in COMMANDS])
        if close:
            app.print(render.info(app.ctx, t(lang, "did_you_mean", options=", ".join("/" + n for n in close))))
        return True
    if cmd.launcher and app.client.mode != "launcher":
        app.print(render.error(app.ctx, t(lang, "launcher_only")))
        return True
    if cmd.session and not app.attached:
        app.print(render.error(app.ctx, t(lang, "no_session")))
        return True
    if cmd.required and not args:
        app.print(render.error(app.ctx, t(lang, "usage", usage=cmd.usage(lang, mode))))
        return True
    try:
        await cmd.handler(CommandContext(app, cmd, args))
    except UsageError:
        app.print(render.error(app.ctx, t(lang, "usage", usage=cmd.usage(lang, mode))))
    except HubError as exc:
        app.print(render.error(app.ctx, app.hub_error(exc)))
    return True


def session_overrides(options: dict | None) -> tuple[dict[str, dict], str | None]:
    """CLI options of a new session -> (overrides by section, title). Flat keys (model, agents,
    max_agents, budget, language) map to swarm.start_agents / swarm.max_agents / swarm.max_cost_usd /
    agents.default_model / swarm.language; `team` ("architect, developer*2" or a saved team's roles as a list, checked
    by the CLI) sets swarm.starting_roles, and swarm.start_agents unless `agents` gives it; dict values
    are taken as sections as they are."""
    sections: dict[str, dict] = {}
    title: str | None = None
    team: list[str] | None = None
    for key, value in (options or {}).items():
        if value is None or value == "":
            continue
        if key == "title":
            title = str(value)
        elif key == "team":
            team = roles.parse_team(", ".join(map(str, value)) if isinstance(value, (list, tuple)) else value)
            sections.setdefault("swarm", {})["starting_roles"] = team
        elif isinstance(value, dict):
            sections.setdefault(key, {}).update(value)
        elif key in FLAT_OVERRIDES:
            section, field = FLAT_OVERRIDES[key]
            sections.setdefault(section, {})[field] = value
    if team is not None:
        sections["swarm"].setdefault("start_agents", len(team))   # --agents N wins over the team's size
    return sections, title


__all__ = [
    "Command", "CommandContext", "COMMANDS", "UsageError", "find", "names", "infos", "help_rows", "execute",
    "session_overrides",
]
