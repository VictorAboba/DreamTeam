"""Team, goal, notes, waiting and swarm control."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any

from .. import prompts, roles
from ..db import loads
from ..jobs import fmt_duration, short_command
from ..models import USER, AgentKind, Status, ToolError
from . import ToolContext, tool
from .fmt import ago, budget_line, goal_line

MAX_NOTES = 20000
WAIT_DEFAULT = 50
WAIT_MAX = 600
# wait_for_events(progress=true): the journal events that are board progress, the lines shown per result (the rest
# is counted) and the events read per result
PROGRESS_TYPES = ("task.created", "task.updated", "goal.set", "goal.completed", "goal.reopened", "agent.hired",
                  "agent.retired")
PROGRESS_MAX = 20
PROGRESS_SCAN = 1000
PROGRESS_TITLE = 80
_PROGRESS = {
    "en": {
        "header": "Board progress:",
        "created": "#{id} {title}: created by {by}",
        "assignee": " (assignee {name})",
        "status": "#{id} {title}: {old} → {new} ({by})",
        "goal_set": "goal set by {by}: {title}",
        "goal_done": "goal achieved ({by})",
        "goal_reopened": "goal reopened by {by}",
        "hired": "{name} hired as \"{role}\" by {by}",
        "joined": "{name} joined as \"{role}\" (external member)",
        "retired": "{name} left the swarm ({by})",
        "more": "… and {n} more: see `board`",
    },
    "ru": {
        "header": "Прогресс на доске:",
        "created": "#{id} {title}: создана ({by})",
        "assignee": " (исполнитель {name})",
        "status": "#{id} {title}: {old} → {new} ({by})",
        "goal_set": "цель задана ({by}): {title}",
        "goal_done": "цель достигнута ({by})",
        "goal_reopened": "цель возвращена в работу ({by})",
        "hired": "{name} нанят(а) на роль «{role}» ({by})",
        "joined": "{name} присоединился(ась) к рою в роли «{role}» (внешний участник)",
        "retired": "{name} покинул(а) рой ({by})",
        "more": "… и ещё {n}: смотри `board`",
    },
}


def _job_list(jobs: list, *, parens: bool = True) -> str:
    """Running jobs for a tool result (English): "j3 (Anna Smith: npm run dev, 25 min); j5 (...)", or without
    `parens` "j3 Anna Smith: npm run dev, 25 min; j5 ..."."""
    now = time.time()
    items = [(j.label, f"{j.owner}: {short_command(j.command, 60)}, {fmt_duration(now - j.started_at)}") for j in jobs]
    return "; ".join(f"{label} ({text})" if parens else f"{label} {text}" for label, text in items)


def _team_size(members: list[dict], limits: dict) -> str:
    """The team size against max_agents ("AI agents: 4 of max 5."), and the external members it does not count
    (Swarm.agent_count)."""
    agents = sum(1 for m in members if m["kind"] == AgentKind.LLM)
    external = len(members) - agents
    text = f"AI agents: {agents} of max {limits['max_agents']}"
    if external:
        text += f", plus {external} external member{'s' if external != 1 else ''} (not counted)"
    return text + "."


@tool(
    "agents",
    en="Show the swarm roster: every member's name, role, status, model, open tasks and spending.",
    ru="Показать состав роя: имя, роль, статус, модель, открытые задачи и расходы каждого участника.",
)
async def agents(ctx: ToolContext, args: dict[str, Any]) -> str:
    swarm = ctx.swarm
    limits = swarm.limits()
    members = swarm.members()
    lines = [_team_size(members, limits) + " " + budget_line(swarm.spent_usd(), limits["max_cost_usd"])]
    for m in members:
        open_ids = [t.id for t in swarm.board.open_tasks_of(m["name"])]
        you = " (you)" if m["name"] == ctx.actor else ""
        kind = "" if m["kind"] == AgentKind.LLM else f", {m['kind']}"
        model = f", model {m['model']}" if m.get("model") else ""
        running = swarm.jobs.running_of(m["name"])
        jobs = f", background jobs: {', '.join(j.label for j in running)}" if running else ""
        lines.append(
            f"- {m['name']}{you}: {m['role']} [{m['status']}{kind}]{model}, "
            f"tasks: {', '.join('#' + str(i) for i in open_ids) or 'none'}, ${m.get('cost_usd') or 0:.2f}, "
            f"active {ago(m.get('last_active'))}{jobs}"
        )
    # the role catalog (docs/SPEC.md §17.3), in the caller's language: what hire_agent offers
    catalog = roles.catalog(swarm.config.roles, swarm.language_of(ctx.actor))
    if catalog:
        lines.append("Role catalog (hire_agent role=<name>; a catalog role brings its description and model profile, "
                     "and instructions may then be empty; any other name is a new role you describe in "
                     "instructions):")
        for role in catalog:
            model = f" (model {role.model})" if role.model else ""
            lines.append(f"- {role.name}{model}: {roles.summary(role.description)}")
    return "\n".join(lines)


@tool(
    "hire_agent",
    en="Hire a new AI agent into the swarm with a role and instructions (what they should focus on). "
       "Optional model profile name and human name. Limited by max agents and the budget.",
    ru="Нанять в рой нового ИИ-агента с ролью и инструкциями (на чём сосредоточиться). Можно указать "
       "профиль модели (model) и человеческое имя. Ограничено максимумом агентов и бюджетом.",
    properties={
        "role": {"type": "string", "description": "e.g. Backend developer, Researcher, QA engineer"},
        "instructions": {"type": "string", "description": "What they should do and focus on"},
        "model": {"type": "string", "description": "Model profile from the config (default if omitted)"},
        "name": {"type": "string", "description": "Human first and last name (generated if omitted)"},
    },
    required=["role", "instructions"],
    allow_empty=("instructions",),  # validation only (the schema is unchanged): "" for a catalog role
)
async def hire_agent(ctx: ToolContext, args: dict[str, Any]) -> str:
    swarm = ctx.swarm
    role = roles.normalize(args["role"])
    instructions = (args.get("instructions") or "").strip()
    # a catalog role brings its description and model profile; any other name is a new role, which only the
    # instructions describe (the guard is here, not in Swarm.hire: the starting team needs no instructions)
    entry = roles.find_role(swarm.config.roles, role, swarm.language)
    close = [] if entry else roles.close_roles(swarm.config.roles, role, swarm.language)
    similar = f" Similar catalog roles: {', '.join(close)}." if close else ""
    if entry is None and not instructions:
        if ctx.is_user:  # the board's hire form, the TUI or the console: the user's tools and views
            raise ToolError(f"'{role}' is not in the role catalog: describe the new role in the instructions (in the "
                            f"terminal: /hire {role} | instructions), or pick a catalog role (the list of the board's "
                            f"hire form, /roles in the terminal).{similar}")
        raise ToolError(f"'{role}' is not in the role catalog: describe the new role's responsibilities in "
                        f"instructions, or pick a catalog role (`agents` lists them).{similar}")
    name = await swarm.hire(ctx.actor, role, instructions, model=args.get("model") or None,
                            name=args.get("name") or None)
    member = swarm.member(name) or {}
    shown = member.get("role") or role
    if ctx.is_user:  # no agent-facing advice (task_update, welcome messages) for the user
        if entry is not None:
            return f"Hired {name} as \"{shown}\" (catalog role, model profile {member.get('model')})."
        return (f"Hired {name} as \"{shown}\": a new role, not in the role catalog; your instructions are all they "
                f"know about it.{similar}")
    if entry is not None:
        source = f" They got the catalog description of the role; model profile {member.get('model')}."
    else:
        source = (f" \"{shown}\" is a new role (not in the role catalog): your instructions are all they know about "
                  f"it.{similar}")
    return (f"Hired {name} as \"{shown}\".{source} They are woken now: assign them a task (task_update assignee) "
            "or leave ready tasks for them to claim; no welcome message is needed.")


@tool(
    "retire_agent",
    en="Remove a member from the swarm (yourself by default, or a member you hired). Their open tasks go "
       "back to todo.",
    ru="Вывести участника из роя (по умолчанию себя или нанятого тобой). Его открытые задачи вернутся в todo.",
    properties={"name": {"type": "string"}, "reason": {"type": "string"}},
)
async def retire_agent(ctx: ToolContext, args: dict[str, Any]) -> str:
    if args.get("name"):
        name = ctx.swarm.resolve_member(args["name"], allow_user=False)
    elif ctx.is_user:
        raise ToolError("Pass the name of the member to retire.")
    else:
        name = ctx.actor
    released = await ctx.swarm.retire(ctx.actor, name, args.get("reason") or "")
    tail = f" Released tasks: {', '.join('#' + str(i) for i in released)}." if released else ""
    return f"{name} left the swarm.{tail}"


@tool(
    "update_notes",
    en="Replace your personal notebook with content ('' clears it). It is shown to you on every wake-up and "
       "survives context compaction: keep key facts, decisions, plans and file paths there.",
    ru="Заменить содержимое твоего блокнота на content ('' — очистить). Он показывается при каждом пробуждении "
       "и переживает сжатие контекста: храни там ключевые факты, решения, планы и пути к файлам.",
    properties={"content": {"type": "string", "description": "The whole new notebook text"}},
    required=["content"],
    audience=("llm",),
    allow_empty=("content",),
)
async def update_notes(ctx: ToolContext, args: dict[str, Any]) -> str:
    content = args["content"].strip()
    if len(content) > MAX_NOTES:
        raise ToolError(f"Notes are limited to {MAX_NOTES} characters (got {len(content)}); keep only what matters.")
    ctx.swarm.db.execute("UPDATE agents SET notes = ? WHERE name = ?", (content, ctx.actor))
    return f"Notes saved ({len(content)} characters)." if content else "Notes cleared."


@tool(
    "wait",
    en="End your turn and sleep until something happens (a message, an assignment, a discussion post, an "
       "unblocked task, a trigger or the end of one of your background jobs). Call it when there is nothing useful "
       "left for you to do now. End every turn with it. Never poll the board, messages or jobs in a loop instead: "
       "these events wake you.",
    ru="Завершить ход и спать, пока что-нибудь не произойдёт (сообщение, назначение, пост в обсуждении, "
       "разблокировка задачи, триггер или завершение твоего фонового процесса). Вызывай, когда полезных дел для "
       "тебя сейчас нет. Заканчивай им каждый ход. Не опрашивай доску, сообщения и фоновые процессы в цикле: эти "
       "события тебя разбудят.",
    properties={"reason": {"type": "string", "description": "What you are waiting for"}},
    audience=("llm",),
)
async def wait(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.runtime is not None:
        ctx.runtime.end_turn()
    # the caller's running jobs whose end will wake it (never one whose end is quiet, e.g. once the goal is done)
    awaited = [j.label for j in ctx.swarm.jobs.awaited(ctx.actor)]
    if len(awaited) == 1:
        return f"OK, waiting. You will be woken up by new events, also when your background job {awaited[0]} ends."
    if awaited:
        return (f"OK, waiting. You will be woken up by new events, also when your background jobs "
                f"{', '.join(awaited)} end.")
    return "OK, waiting. You will be woken up by new events."


@tool(
    "goal_complete",
    en="Declare the global goal achieved (only after verifying the result) with a summary for the user: "
       "what was delivered and where to find it.",
    ru="Объявить глобальную цель достигнутой (только после проверки результата) с итогом для пользователя: "
       "что сделано и где это найти.",
    properties={"summary": {"type": "string"}},
    required=["summary"],
    audience=("llm", "external"),
)
async def goal_complete(ctx: ToolContext, args: dict[str, Any]) -> str:
    swarm = ctx.swarm
    goal = swarm.goal()
    if goal and goal.get("status") == "done":
        raise ToolError("The global goal is already marked as achieved. If more work is needed, tell the user "
                        "(send_message to \"user\"); only the user can reopen the goal.")
    if swarm.board.independent_review:
        waiting = swarm.board.list(status=Status.REVIEW)
        if waiting:
            listed = ", ".join(f"#{t.id} ({t.reviewer or 'user'})" for t in waiting[:20])
            raise ToolError(f"Tasks are still waiting for review: {listed}. The goal can be completed only after "
                            "every task is reviewed: call `wait` (when all tasks are finished, a member is woken to "
                            "verify the result and complete the goal).")
    swarm.complete_goal(ctx.actor, args["summary"])
    text = "The global goal is marked as achieved and the user was notified."
    still_open = swarm.board.list()
    if still_open:
        ids = ", ".join(f"#{t.id}" for t in still_open[:20]) + (" ..." if len(still_open) > 20 else "")
        text += f" Note: {len(still_open)} task(s) are still open ({ids}); finish or cancel them."
    running = swarm.jobs.running_of()
    if running:  # they go on (a dev server may be for the user): the caller may stop the ones nobody needs
        how = "job_stop" if ctx.has_tool("job_stop") else "the shell command kill jN (as a command of its own)"
        text += (f" Note: background jobs are still running: {_job_list(running)}. Stop the ones nobody needs with "
                 f"{how}; the rest stop with the session.")
    return text


@tool(
    "join_swarm",
    en="Join the swarm as a working member (you get a unique human name). Afterwards pass "
       "as_agent=\"<your name>\" in every call to act as that member; without as_agent you act as the user. "
       "If name is an existing external member (e.g. after a reconnect), you continue as that member. "
       "Members who join this way do not count toward the swarm's agent limit (max_agents).",
    ru="Вступить в рой как рабочий участник (получишь уникальное человеческое имя). Затем передавай "
       "as_agent=\"<твоё имя>\" в каждом вызове, чтобы действовать от имени участника; без as_agent ты "
       "действуешь как пользователь. Если name — уже существующий внешний участник (например, после "
       "переподключения), ты продолжаешь работать от его имени. Вступившие так участники не учитываются "
       "в лимите агентов роя (max_agents).",
    properties={
        "role": {"type": "string"},
        "name": {"type": "string", "description": "Desired human first and last name (optional)"},
        "instructions": {"type": "string", "description": "What you will focus on"},
    },
    required=["role"],
    audience=("user", "external"),
)
async def join_swarm(ctx: ToolContext, args: dict[str, Any]) -> str:
    swarm = ctx.swarm
    if ctx.kind == AgentKind.EXTERNAL:
        return f"You are already a member: {ctx.actor}. Pass as_agent=\"{ctx.actor}\" in every call."
    wanted = " ".join(str(args.get("name") or "").split())
    existing = next((m for m in swarm.members() if wanted and m["name"].lower() == wanted.lower()), None)
    if existing is not None:
        if existing["kind"] != AgentKind.EXTERNAL:
            raise ToolError(f"'{existing['name']}' is an internal swarm agent; choose another name.")
        return (f"Welcome back: you continue as {existing['name']} (role: {existing['role']}). "
                f"Pass as_agent=\"{existing['name']}\" in every call; wait_for_events returns what you missed.")
    name = await swarm.hire(
        USER, args["role"], args.get("instructions") or "", name=wanted or None, kind=AgentKind.EXTERNAL,
    )
    member = swarm.member(name) or {}
    role = member.get("role") or args["role"].strip()
    text = (
        f"You joined the swarm as {name} (role: {role}). Pass as_agent=\"{name}\" in every call. "
        "Look at `board`, claim tasks with `task_claim`, talk with `send_message`, and call `wait_for_events` "
        "to receive messages, assignments and trigger notifications (progress=true: board progress too). "
        "You do not count toward the agent limit (max_agents)."
    )
    # an external member has no system prompt: a catalog role's description reaches it here
    entry = roles.find_role(swarm.config.roles, role, member.get("language") or swarm.language)
    if entry is not None and entry.description:
        text += f"\nYour role ({role}): {entry.description}"
    return text


def _stopping(swarm) -> bool:
    return bool(getattr(swarm, "stopping", False))  # (the test double has no such flag)


def _progress_key(actor: str) -> str:
    return f"wait_progress:{actor}"


def _progress_cursor(swarm, actor: str) -> int:
    """The last journal event whose board progress `actor` was given by wait_for_events(progress=true); at its
    first such call, the journal's end (progress from then on)."""
    cursor = swarm.db.get(_progress_key(actor))
    if not isinstance(cursor, int):
        cursor = int(swarm.db.scalar("SELECT COALESCE(MAX(id), 0) FROM events") or 0)
        swarm.db.set(_progress_key(actor), cursor)
    return cursor


def _clip(text: Any, limit: int = PROGRESS_TITLE) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _progress_line(texts: dict[str, str], event: dict, actor: str) -> str | None:
    """One line about a journal event that is board progress made by someone else than `actor`; None otherwise."""
    by, data = event["actor"], event["data"]
    if by == actor:
        return None  # what the caller did itself is no news to it
    by = by or "DreamTeam"
    kind = event["type"]
    if kind in ("task.created", "task.updated"):
        task = data.get("task") or {}
        fields = {"id": task.get("id"), "title": _clip(task.get("title")), "by": by}
        if kind == "task.created":
            line = texts["created"].format(**fields)
            return line + (texts["assignee"].format(name=task["assignee"]) if task.get("assignee") else "")
        change = (data.get("changes") or {}).get("status")
        if not isinstance(change, (list, tuple)) or len(change) != 2:
            return None  # an edit, a new assignee... the assignee is told by its own notification
        return texts["status"].format(old=change[0], new=change[1], **fields)
    if kind == "goal.set":
        return texts["goal_set"].format(by=by, title=_clip((data.get("goal") or {}).get("title")))
    if kind == "goal.completed":
        return texts["goal_done"].format(by=by)
    if kind == "goal.reopened":
        return texts["goal_reopened"].format(by=by)
    name = data.get("name")
    if not name or name == actor:
        return None
    if kind == "agent.hired":
        key = "joined" if data.get("kind") == AgentKind.EXTERNAL else "hired"
        return texts[key].format(name=name, role=_clip(data.get("role"), 60), by=by)
    if kind == "agent.retired":
        return texts["retired"].format(name=name, by=by)
    return None


def _progress(swarm, actor: str, cursor: int) -> tuple[list[str], int]:
    """Board progress after journal event `cursor`, as lines in `actor`'s language, and the last event read."""
    rows = swarm.db.all(f"SELECT id, type, actor, data FROM events WHERE id > ? AND type IN "
                        f"({', '.join('?' * len(PROGRESS_TYPES))}) ORDER BY id LIMIT ?",
                        (cursor, *PROGRESS_TYPES, PROGRESS_SCAN))
    texts = _PROGRESS.get(swarm.language_of(actor)) or _PROGRESS["en"]
    lines = []
    for row in rows:
        event = {"type": row["type"], "actor": row["actor"], "data": loads(row["data"], {})}
        line = _progress_line(texts, event, actor) if isinstance(event["data"], dict) else None
        if line:
            lines.append(line)
    return lines, (rows[-1]["id"] if rows else cursor)


async def _signal_or_progress(swarm, actor: str, queue: asyncio.Queue, timeout: float) -> None:
    """Wait until `actor` is signalled, a board progress event arrives on the bus `queue` or `timeout` passes."""
    async def progress_event() -> None:
        while (await queue.get()).get("type") not in PROGRESS_TYPES:
            pass

    waiters = {asyncio.ensure_future(swarm.wait_signal(actor, timeout)), asyncio.ensure_future(progress_event())}
    try:
        await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)


@tool(
    "wait_for_events",
    en=f"Wait (up to timeout seconds, default {WAIT_DEFAULT}, max {WAIT_MAX}) for new messages, notifications, "
       "assignments or triggers addressed to you, and return them. Returns immediately if something is already "
       "pending. With progress=true it also returns on board progress made by others (tasks created or changing "
       "status, the goal set, achieved or reopened, members hired, joined or leaving), as short lines.",
    ru=f"Подождать (до timeout секунд, по умолчанию {WAIT_DEFAULT}, максимум {WAIT_MAX}) новых сообщений, "
       "уведомлений, назначений или срабатываний триггеров, адресованных тебе, и вернуть их. Сразу "
       "возвращается, если что-то уже есть. С progress=true возвращается и при прогрессе на доске, сделанном "
       "другими (задачи созданы или сменили статус, цель задана, достигнута или возвращена в работу, участники "
       "наняты, вступили или ушли), короткими строками.",
    properties={"timeout": {"type": "integer", "description": f"Seconds, 0-{WAIT_MAX}"},
                "progress": {"type": "boolean",
                             "description": "Also return on board progress (default false: only what is addressed "
                                            "to you)"}},
    audience=("external", "user"),
)
async def wait_for_events(ctx: ToolContext, args: dict[str, Any]) -> str:
    swarm = ctx.swarm
    timeout = args.get("timeout")
    timeout = max(0, min(WAIT_DEFAULT if timeout is None else timeout, WAIT_MAX))
    progress = bool(args.get("progress"))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    queue = swarm.bus.subscribe() if progress else None  # board progress wakes the wait too
    try:
        cursor = _progress_cursor(swarm, ctx.actor) if progress and not _stopping(swarm) else 0
        # a signal can be left over from an event that was already consumed (e.g. by read_messages):
        # keep waiting until something is really pending or the time is up
        while not _stopping(swarm) and not swarm.has_pending(ctx.actor):
            if progress:
                lines, last = _progress(swarm, ctx.actor, cursor)
                if lines:
                    break
                cursor = last  # only the caller's own changes or edits so far: nothing to show for them
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            if queue is None:
                await swarm.wait_signal(ctx.actor, remaining)
            else:
                await _signal_or_progress(swarm, ctx.actor, queue, remaining)
    finally:
        if queue is not None:
            swarm.bus.unsubscribe(queue)
    if _stopping(swarm):  # its database may already be closed
        raise ToolError("The swarm was stopped while waiting; call wait_for_events again once it runs again.")
    text = swarm.render_wake(ctx.actor, incoming=True)
    if progress:
        lines, last = _progress(swarm, ctx.actor, cursor)
        swarm.db.set(_progress_key(ctx.actor), last)
        if lines:
            texts = _PROGRESS.get(swarm.language_of(ctx.actor)) or _PROGRESS["en"]
            shown = [f"• {line}" for line in lines[:PROGRESS_MAX]]
            if len(lines) > PROGRESS_MAX:
                shown.append(f"• {texts['more'].format(n=len(lines) - PROGRESS_MAX)}")
            if not text:
                stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
                text = prompts.t(swarm.language_of(ctx.actor), "incoming_header", time=stamp)
            text += "\n" + "\n".join([texts["header"], *shown])
    return text or f"No new events in {timeout}s. Call wait_for_events again to keep listening."


@tool(
    "set_goal",
    en="Set or replace the global goal of the swarm. The team is woken up to plan the work. A multi-line title is "
       "split: its first line becomes the title, the other lines open the description.",
    ru="Задать или заменить глобальную цель роя. Команда будет разбужена, чтобы спланировать работу. Многострочный "
       "title делится: первая строка становится заголовком, остальные открывают описание.",
    properties={
        "title": {"type": "string", "description": "The goal: one line, or several lines (the first one is the title)"},
        "description": {"type": "string", "description": "Details, success criteria, constraints (optional)"},
    },
    required=["title"],
    audience=("user",),
)
async def set_goal(ctx: ToolContext, args: dict[str, Any]) -> str:
    goal = ctx.swarm.set_goal(ctx.actor, args["title"], args.get("description") or "")
    return "Goal set. " + goal_line(goal)


@tool(
    "swarm_status",
    en="Overview of the swarm: goal, budget, pause state, members, task counts and your unread items.",
    ru="Обзор роя: цель, бюджет, пауза, участники, счётчики задач и твои непрочитанные.",
    audience=("external", "user"),
)
async def swarm_status(ctx: ToolContext, args: dict[str, Any]) -> str:
    swarm = ctx.swarm
    limits = swarm.limits()
    members = swarm.members()
    counts = swarm.board.counts()
    lines = [
        f"Swarm \"{swarm.config.swarm.name}\" — {'PAUSED: ' + swarm.pause_reason if swarm.paused else 'running'}",
        goal_line(swarm.goal()),
        f"{budget_line(swarm.spent_usd(), limits['max_cost_usd'])} {_team_size(members, limits)}",
        "Members: " + (", ".join(f"{m['name']} ({m['role']}, {m['status']})" for m in members) or "none"),
        "Tasks: " + (", ".join(f"{k}={v}" for k, v in counts.items() if v) or "none"),
    ]
    running = swarm.jobs.running_of()
    if running:
        lines.append(f"Background jobs: {len(running)} running ({_job_list(running, parens=False)}).")
    if not ctx.is_user:
        mine = swarm.board.open_tasks_of(ctx.actor)
        lines.append(f"You are {ctx.actor}. Your open tasks: "
                     + ("; ".join(t.short() for t in mine) if mine else "none"))
        own = [j.label for j in running if j.owner == ctx.actor]
        if own:
            lines.append(f"Your background jobs: {', '.join(own)}.")
    pending = " New events are pending: call wait_for_events." if swarm.has_pending(ctx.actor) else ""
    lines.append(f"Your unread messages: {swarm.mail.unread_count(ctx.actor)}.{pending}")
    return "\n".join(lines)


@tool(
    "swarm_control",
    en="Control the swarm: action=pause|resume, change limits (max_cost_usd, 0 = no limit; max_agents), or "
       "reopen a completed goal with reopen_goal_comment.",
    ru="Управление роем: action=pause|resume, изменение лимитов (max_cost_usd, 0 — без лимита; max_agents) "
       "или возврат завершённой цели в работу через reopen_goal_comment.",
    properties={
        "action": {"type": "string", "enum": ["pause", "resume"]},
        "max_cost_usd": {"type": "number", "description": "Budget in USD, 0 = no limit"},
        "max_agents": {"type": "integer",
                       "description": "Maximum number of AI agents (members who joined through MCP do not count)"},
        "reopen_goal_comment": {"type": "string", "description": "Reopen the completed goal with this comment"},
    },
    audience=("user",),
)
async def swarm_control(ctx: ToolContext, args: dict[str, Any]) -> str:
    swarm = ctx.swarm
    done = []
    if args.get("max_cost_usd") is not None or args.get("max_agents") is not None:
        limits = swarm.set_limits(ctx.actor, args.get("max_cost_usd"), args.get("max_agents"))
        budget = f"${limits['max_cost_usd']:.2f}" if limits["max_cost_usd"] > 0 else "no budget limit"
        done.append(f"limits: {budget}, at most {limits['max_agents']} AI agents")
    if args.get("reopen_goal_comment"):
        goal = swarm.goal()
        if goal and goal.get("status") != "done":
            raise ToolError("The global goal is not completed, so there is nothing to reopen" +
                            (f" ({'; '.join(done)} applied)." if done else "."))
        swarm.reopen_goal(ctx.actor, args["reopen_goal_comment"])
        done.append("goal reopened")
    action = args.get("action")
    if action == "pause":
        swarm.pause(ctx.actor, "paused by the user")
        done.append("paused")
    elif action == "resume":
        swarm.resume(ctx.actor)
        done.append("resumed")
    if not done:
        raise ToolError("Nothing to do: pass action (pause/resume), a limit or reopen_goal_comment.")
    return "Done: " + "; ".join(done) + "."
