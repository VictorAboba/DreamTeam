"""Team, goal, notes, waiting and swarm control."""

from __future__ import annotations

import asyncio
from typing import Any

from .. import roles
from ..models import USER, AgentKind, Status, ToolError
from . import ToolContext, tool
from .fmt import ago, budget_line, goal_line

MAX_NOTES = 20000
WAIT_DEFAULT = 50
WAIT_MAX = 600


@tool(
    "agents",
    en="Show the swarm roster: every member's name, role, status, model, open tasks and spending.",
    ru="Показать состав роя: имя, роль, статус, модель, открытые задачи и расходы каждого участника.",
)
async def agents(ctx: ToolContext, args: dict[str, Any]) -> str:
    swarm = ctx.swarm
    limits = swarm.limits()
    members = swarm.members()
    lines = [f"Members: {len(members)} of max {limits['max_agents']}. "
             + budget_line(swarm.spent_usd(), limits["max_cost_usd"])]
    for m in members:
        open_ids = [t.id for t in swarm.board.open_tasks_of(m["name"])]
        you = " (you)" if m["name"] == ctx.actor else ""
        kind = "" if m["kind"] == AgentKind.LLM else f", {m['kind']}"
        model = f", model {m['model']}" if m.get("model") else ""
        lines.append(
            f"- {m['name']}{you}: {m['role']} [{m['status']}{kind}]{model}, "
            f"tasks: {', '.join('#' + str(i) for i in open_ids) or 'none'}, ${m.get('cost_usd') or 0:.2f}, "
            f"active {ago(m.get('last_active'))}"
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
       "unblocked task or a trigger). Call it when there is nothing useful left for you to do now. End every "
       "turn with it. Never poll the board or messages in a loop instead: these events wake you.",
    ru="Завершить ход и спать, пока что-нибудь не произойдёт (сообщение, назначение, пост в обсуждении, "
       "разблокировка задачи или триггер). Вызывай, когда полезных дел для тебя сейчас нет. Заканчивай им "
       "каждый ход. Не опрашивай доску и сообщения в цикле: эти события тебя разбудят.",
    properties={"reason": {"type": "string", "description": "What you are waiting for"}},
    audience=("llm",),
)
async def wait(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.runtime is not None:
        ctx.runtime.end_turn()
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
    return text


@tool(
    "join_swarm",
    en="Join the swarm as a working member (you get a unique human name). Afterwards pass "
       "as_agent=\"<your name>\" in every call to act as that member; without as_agent you act as the user. "
       "If name is an existing external member (e.g. after a reconnect), you continue as that member.",
    ru="Вступить в рой как рабочий участник (получишь уникальное человеческое имя). Затем передавай "
       "as_agent=\"<твоё имя>\" в каждом вызове, чтобы действовать от имени участника; без as_agent ты "
       "действуешь как пользователь. Если name — уже существующий внешний участник (например, после "
       "переподключения), ты продолжаешь работать от его имени.",
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
        "to receive messages, assignments and trigger notifications."
    )
    # an external member has no system prompt: a catalog role's description reaches it here
    entry = roles.find_role(swarm.config.roles, role, member.get("language") or swarm.language)
    if entry is not None and entry.description:
        text += f"\nYour role ({role}): {entry.description}"
    return text


def _stopping(swarm) -> bool:
    return bool(getattr(swarm, "stopping", False))  # (the test double has no such flag)


@tool(
    "wait_for_events",
    en=f"Wait (up to timeout seconds, default {WAIT_DEFAULT}, max {WAIT_MAX}) for new messages, notifications, "
       "assignments or triggers addressed to you, and return them. Returns immediately if something is already "
       "pending.",
    ru=f"Подождать (до timeout секунд, по умолчанию {WAIT_DEFAULT}, максимум {WAIT_MAX}) новых сообщений, "
       "уведомлений, назначений или срабатываний триггеров, адресованных тебе, и вернуть их. Сразу "
       "возвращается, если что-то уже есть.",
    properties={"timeout": {"type": "integer", "description": f"Seconds, 0-{WAIT_MAX}"}},
    audience=("external", "user"),
)
async def wait_for_events(ctx: ToolContext, args: dict[str, Any]) -> str:
    swarm = ctx.swarm
    timeout = args.get("timeout")
    timeout = max(0, min(WAIT_DEFAULT if timeout is None else timeout, WAIT_MAX))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    # a signal can be left over from an event that was already consumed (e.g. by read_messages):
    # keep waiting until something is really pending or the time is up
    while not _stopping(swarm) and not swarm.has_pending(ctx.actor):
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        await swarm.wait_signal(ctx.actor, remaining)
    if _stopping(swarm):  # its database may already be closed
        raise ToolError("The swarm was stopped while waiting; call wait_for_events again once it runs again.")
    text = swarm.render_wake(ctx.actor, incoming=True)
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
        f"{budget_line(swarm.spent_usd(), limits['max_cost_usd'])} Members: {len(members)} of {limits['max_agents']}.",
        "Members: " + (", ".join(f"{m['name']} ({m['role']}, {m['status']})" for m in members) or "none"),
        "Tasks: " + (", ".join(f"{k}={v}" for k, v in counts.items() if v) or "none"),
    ]
    if not ctx.is_user:
        mine = swarm.board.open_tasks_of(ctx.actor)
        lines.append(f"You are {ctx.actor}. Your open tasks: "
                     + ("; ".join(t.short() for t in mine) if mine else "none"))
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
        "max_agents": {"type": "integer", "description": "Maximum number of members"},
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
        done.append(f"limits: {budget}, {limits['max_agents']} agents")
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
