"""Slash commands of the terminal UI (docs/TUI.md §4.3): a data-driven registry (it feeds `/help` and the
suggestions) and the async handlers. A handler gets a `CommandContext` (the app, the hub client, the
session id, the render context and the argument text); hub errors are reported by `execute()`."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from .. import roles
from . import render
from .client import HubError
from .i18n import t
from .input import ALL, CommandInfo, did_you_mean, number, parse_command, resolve_member

if TYPE_CHECKING:
    from .app import TUI

Handler = Callable[["CommandContext"], Awaitable[None]]

TRACES_LIMIT = 20
CONTEXT_PAGE = 30
TEAM_RESET = ("-", "default", "reset", "сброс")   # /team: back to the starting team of the settings
# "/approve 12", "/reject #12 ...": a task in review. Agent-built MCP servers are named like mcp_forge.NAME_RE
# (they start with a letter) and external servers are never pending, so a number is never an MCP server.
_TASK_REF = re.compile(r"#?\d+")

# CLI flags that become session overrides of a new session (the sections of the launcher's modal)
FLAT_OVERRIDES: dict[str, tuple[str, str]] = {
    "model": ("agents", "default_model"),
    "agents": ("swarm", "start_agents"),
    "start_agents": ("swarm", "start_agents"),
    "max_agents": ("swarm", "max_agents"),
    "budget": ("swarm", "max_cost_usd"),
    "max_cost_usd": ("swarm", "max_cost_usd"),
    "language": ("swarm", "language"),
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

    def description(self, lang: str) -> str:
        return t(lang, "cmd_" + self.name)

    def args_text(self, lang: str) -> str:
        return t(lang, "cmdargs_" + self.name) if self.args else ""

    def usage(self, lang: str) -> str:
        return f"/{self.name} {self.args_text(lang)}".rstrip()

    def help_row(self, lang: str) -> dict[str, Any]:
        return {"name": self.name, "args": self.args_text(lang), "description": self.description(lang),
                "aliases": list(self.aliases)}

    def info(self, lang: str) -> CommandInfo:
        return CommandInfo(self.name, self.description(lang), self.aliases, self.args_text(lang), self.required)


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
             "instructions": r.get("instructions") or "", "model": r.get("model"), "builtin": bool(r.get("builtin"))}
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
    c.print(render.help_view(c.ctx, [cmd.help_row(c.lang) for cmd in COMMANDS]))


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
    if not c.args:
        state = await c.state()
        c.print(render.goal_panel(c.ctx, (state.get("swarm") or {}).get("goal")))
        return
    await c.act("set_goal", {"title": c.args})  # the whole text: the hub splits it (first line = title)


async def _board(c: CommandContext) -> None:
    state = await c.state()
    c.print(render.board(c.ctx, state.get("tasks") or []))


async def _task(c: CommandContext) -> None:
    task_id = _int_arg(c.args)
    c.print(render.task_detail(c.ctx, await c.client.task(c.sid, task_id)))


async def _agents(c: CommandContext) -> None:
    state = await c.state()
    c.print(render.agents_table(c.ctx, state.get("agents") or []))


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
    """The starting team of the sessions this window creates next (like --team); the settings stay as they are."""
    arg = c.args.strip()
    swarm = c.app.overrides.get("swarm") or {}
    if arg.casefold() in TEAM_RESET:
        swarm.pop("starting_roles", None)
        swarm.pop("start_agents", None)
        if not swarm:
            c.app.overrides.pop("swarm", None)
        c.info(c.t("team_reset"))
        return
    view = await c.client.settings()
    catalog = _localized(c.lang, view.get("role_catalog"))
    if not arg:
        own = {k: swarm[k] for k in ("starting_roles", "start_agents") if k in swarm}
        team = {**((view.get("settings") or {}).get("swarm") or {}), **own}
        rows = roles.team_rows(team.get("starting_roles"), team.get("start_agents") or 0)
        c.info(c.t("team_set" if own else "team_line", team=_team_text(c, rows, catalog),
                   n=sum(n for _, n in rows)))
        return
    try:
        team = roles.parse_team(arg)
    except ValueError as exc:
        c.error(c.t("team_bad", error=exc))
        return
    swarm = c.app.overrides.setdefault("swarm", {})
    swarm["starting_roles"], swarm["start_agents"] = team, len(team)
    c.info(c.t("team_set", team=_team_text(c, roles.team_rows(team, len(team)), catalog), n=len(team)))


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


async def _cd(c: CommandContext) -> None:
    await c.app.change_folder(c.args)


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


async def _pause(c: CommandContext) -> None:
    await c.app.toggle_pause()


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
    if task_id is not None:
        await c.act("task_review", {"task_id": task_id, "approve": True, "comment": comment}, report=True)
        return
    await c.app.decide_mcp(c.args.split()[0], True)


async def _reject(c: CommandContext) -> None:
    task_id, comment = _task_ref(c.args)
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
    Command("board", _board, session=True),
    Command("task", _task, args=True, required=True, session=True),
    Command("agents", _agents, session=True),
    Command("roles", _roles, args=True),
    Command("to", _to, args=True, required=True, session=True),
    Command("hire", _hire, args=True, required=True, session=True),
    Command("trace", _trace, args=True, session=True),
    Command("span", _span, args=True, required=True, session=True),
    Command("files", _files, args=True, session=True),
    Command("cat", _files, args=True, required=True, session=True),
    Command("cd", _cd, args=True, launcher=True),   # without a session: the folder of the next new one
    Command("cost", _cost, session=True),
    Command("status", _status),
    Command("model", _model, args=True, launcher=True),
    Command("pause", _pause, session=True),
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


def infos(lang: str) -> list[CommandInfo]:
    """The registry as the suggestions need it."""
    return [cmd.info(lang) for cmd in COMMANDS]


def help_rows(lang: str) -> list[dict[str, Any]]:
    return [cmd.help_row(lang) for cmd in COMMANDS]


async def execute(app: "TUI", text: str) -> bool:
    """Run the slash command in `text`; False when `text` is not a command at all."""
    parsed = parse_command(text)
    if parsed is None:
        return False
    name, args = parsed
    lang = app.ctx.lang
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
        app.print(render.error(app.ctx, t(lang, "usage", usage=cmd.usage(lang))))
        return True
    try:
        await cmd.handler(CommandContext(app, cmd, args))
    except UsageError:
        app.print(render.error(app.ctx, t(lang, "usage", usage=cmd.usage(lang))))
    except HubError as exc:
        app.print(render.error(app.ctx, app.hub_error(exc)))
    return True


def session_overrides(options: dict | None) -> tuple[dict[str, dict], str | None]:
    """CLI options of a new session -> (overrides by section, title). Flat keys (model, agents,
    max_agents, budget, language) map to swarm.start_agents / swarm.max_agents / swarm.max_cost_usd /
    agents.default_model / swarm.language; `team` ("architect, developer*2", checked by the CLI with
    roles.parse_team) sets swarm.starting_roles, and swarm.start_agents unless `agents` gives it; dict values
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
