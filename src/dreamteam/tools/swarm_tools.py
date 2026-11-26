"""Team, goal, notes, waiting and swarm control."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .. import netoff, prompts, roles
from ..db import loads
from ..forum import rule_name, vote_line
from ..jobs import fmt_duration, short_command
from ..models import USER, AgentKind, Status, ToolError
from ..swarm import USER_PAUSE
from ..workspace import shown_list
from . import ToolContext, tool
from .fmt import STAMP_SPAN, ago, budget_line, goal_line, item_time, stamp_range

MAX_NOTES = 20000
WAIT_DEFAULT = 50
WAIT_MAX = 600
# `wait` with an in_progress task, nothing of the caller's set to wake it and the rest of the team asleep (docs/SPEC.md
# §12 `wait`): read after the next wake-up (wait ends the activation), hence "the next time"
WAIT_UNWATCHED = ("OK, waiting. You will be woken up by new events, but nothing of yours is set to wake you and "
                  "nobody else in the team is at work. A process started with & or nohup that `jobs` does not list "
                  "is not tracked: its end will not wake you. If you are waiting for one, the next time set a trigger "
                  "with a deadline before you wait (trigger_create, e.g. file_exists(\"out/result.json\") or "
                  "elapsed(1800)), and start such runs with background=true, without & and nohup.")
# wait_for_events(progress=true): the journal events that are progress, by the session's mode (docs/SPEC.md §22.5),
# the lines shown per result (the rest is counted) and the events read per result
PROGRESS_TYPES = {
    "board": ("task.created", "task.updated", "goal.set", "goal.completed", "goal.reopened", "agent.hired",
              "agent.retired", "swarm.wrap_up"),
    "thread": ("post.created", "post.comment", "goal.set", "goal.completed", "goal.reopened", "agent.hired",
               "agent.retired", "swarm.wrap_up"),
}
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
        "wrap_up_on": "wrap-up mode on ({by}): finish your tasks with what you have, no new computations or background "
                      "jobs",
        "wrap_up_off": "wrap-up mode off ({by})",
        "more": "… and {n} more: see `board`",
        # a thread session's lines (with goal_set, hired, joined, retired and wrap_up_off above)
        "posted": "#{id} \"{title}\": posted by {by}",
        "commented": "c{cid} on #{id} \"{title}\" by {by}: {text}",
        "goal_done_vote": "goal reached by the vote ({rule})",
        "goal_done_user": "goal completed by the user",
        "goal_reopened_thread": "goal reopened by {by}: the votes on #1 were cleared",
        "wrap_up_on_thread": "wrap-up mode on ({by}): post your results and what is left, stop jobs nobody needs, "
                             "start nothing new, then like #1",
        "header_thread": "Feed:",
        "more_thread": "… and {n} more: see `feed`",
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
        "wrap_up_on": "режим завершения включён ({by}): заверши свои задачи с тем, что есть, без новых вычислений и "
                      "фоновых процессов",
        "wrap_up_off": "режим завершения выключен ({by})",
        "more": "… и ещё {n}: смотри `board`",
        "posted": "#{id} «{title}»: опубликовал(а) {by}",
        "commented": "c{cid} к #{id} «{title}» от {by}: {text}",
        "goal_done_vote": "цель достигнута голосованием ({rule})",
        "goal_done_user": "цель завершена пользователем",
        "goal_reopened_thread": "цель возвращена в работу ({by}): голоса на #1 сброшены",
        "wrap_up_on_thread": "режим завершения включен ({by}): опубликуй результаты и то, что осталось, останови "
                             "ненужные фоновые процессы, не начинай нового, затем поставь like на #1",
        "header_thread": "Лента:",
        "more_thread": "… и еще {n}: смотри `feed`",
    },
}


def _job_list(jobs: list, *, parens: bool = True) -> str:
    """Running jobs for a tool result (English): "j3 (Anna Smith: npm run dev, 25 min); j5 (...)", or without
    `parens` "j3 Anna Smith: npm run dev, 25 min; j5 ...". A silent job (jobs.SILENT_MINUTES) adds "no output for
    12 min"."""
    now = time.time()
    items = [(j.label, f"{j.owner}: {short_command(j.command, 60)}, {fmt_duration(now - j.started_at)}"
              + (f", {silent}" if (silent := j.silent_phrase("en", now)) else "")) for j in jobs]
    return "; ".join(f"{label} ({text})" if parens else f"{label} {text}" for label, text in items)


def _mode_kw(mode: str) -> dict[str, str]:
    """The `mode` argument of the roles functions in a thread session (docs/SPEC.md §22.6); none for a board one,
    whose calls stay as they were."""
    return {"mode": mode} if mode != "board" else {}


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
    thread={"en": "Show the swarm roster: every member's name, role, status, model and spending.",
            "ru": "Показать состав роя: имя, роль, статус, модель и расходы каждого участника."},
)
async def agents(ctx: ToolContext, args: dict[str, Any]) -> str:
    swarm = ctx.swarm
    thread = ctx.mode == "thread"
    limits = swarm.limits()
    members = swarm.members()
    lines = [_team_size(members, limits) + " " + budget_line(swarm.spent_usd(), limits["max_cost_usd"])]
    for m in members:
        you = " (you)" if m["name"] == ctx.actor else ""
        kind = "" if m["kind"] == AgentKind.LLM else f", {m['kind']}"
        model = f", model {m['model']}" if m.get("model") else ""
        running = swarm.jobs.running_of(m["name"])
        jobs = f", background jobs: {', '.join(j.label for j in running)}" if running else ""
        tasks = ""
        if not thread:  # a thread session has no tasks
            open_ids = [t.id for t in swarm.board.open_tasks_of(m["name"])]
            tasks = f"tasks: {', '.join('#' + str(i) for i in open_ids) or 'none'}, "
        lines.append(
            f"- {m['name']}{you}: {m['role']} [{m['status']}{kind}]{model}, "
            f"{tasks}${m.get('cost_usd') or 0:.2f}, active {ago(m.get('last_active'))}{jobs}"
        )
    # the role catalog (docs/SPEC.md §17.3) of the session's mode, in the caller's language: what hire_agent offers
    catalog = roles.catalog(swarm.config.roles, swarm.language_of(ctx.actor), **_mode_kw(ctx.mode))
    if catalog:
        lines.append("Role catalog (hire_agent role=<name>; a catalog role brings its description and model profile, "
                     "and instructions may then be empty; any other name is a new role you describe in "
                     "instructions):")
        for role in catalog:
            notes = ([f"model {role.model}"] if role.model else []) + (
                [f"skills: {', '.join(role.skills)}"] if role.skills else [])
            extra = f" ({'; '.join(notes)})" if notes else ""
            lines.append(f"- {role.name}{extra}: {roles.summary(role.description)}")
    attached = _skill_names(swarm)
    if attached:
        lines.append(f"Skills attached to the swarm: {', '.join(attached)} (the wake-up lists them; read their "
                     "SKILL.md with read_file).")
    return "\n".join(lines)


def _skill_names(swarm) -> list[str]:
    skills = getattr(swarm, "skills", None)  # (the test double has none)
    return skills.names() if skills is not None else []


def _role_skills_note(swarm, name: str, member: dict) -> str:
    """What the hire did with the skills the new member's role lists (Swarm.hire, SessionSkills.ensure_role_skills):
    "" when its role lists none."""
    skills = getattr(swarm, "skills", None)
    if skills is None:
        return ""
    report = skills.hire_report(name) or {}
    listed = skills.role_skills(member)
    if not listed:
        return ""
    role = member.get("role") or ""
    got = [n for n in listed if n in set(skills.names())]
    parts = [f" They also got the skills of their role: {', '.join(got)}."] if got else []
    for key, text in (("missing", "Not found in the skills library (the user was told)"),
                      ("detached", "Detached by the user, so not attached"),
                      ("over_cap", f"Not attached, the swarm has the maximum of {swarm.config.skills.max_attached} "
                                   "skills")):
        names = (report.get(key) or {}).get(role)
        if names:
            parts.append(f" {text}: {', '.join(names)}.")
    return "".join(parts)


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
    mode = _mode_kw(ctx.mode)
    entry = roles.find_role(swarm.config.roles, role, swarm.language, **mode)
    close = [] if entry else roles.close_roles(swarm.config.roles, role, swarm.language, **mode)
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
    skills = _role_skills_note(swarm, name, member)
    if ctx.is_user:  # no agent-facing advice (task_update, welcome messages) for the user
        if entry is not None:
            return f"Hired {name} as \"{shown}\" (catalog role, model profile {member.get('model')}).{skills}"
        return (f"Hired {name} as \"{shown}\": a new role, not in the role catalog; your instructions are all they "
                f"know about it.{similar}")
    if entry is not None:
        source = f" They got the catalog description of the role; model profile {member.get('model')}.{skills}"
    else:
        source = (f" \"{shown}\" is a new role (not in the role catalog): your instructions are all they know about "
                  f"it.{similar}")
    if ctx.mode == "thread":
        return (f"Hired {name} as \"{shown}\".{source} They are woken now: they will read the feed and #2; no welcome "
                "message is needed.")
    return (f"Hired {name} as \"{shown}\".{source} They are woken now: assign them a task (task_update assignee) "
            "or leave ready tasks for them to claim; no welcome message is needed.")


@tool(
    "retire_agent",
    en="Remove a member from the swarm (yourself by default, or a member you hired). Their open tasks go "
       "back to todo.",
    ru="Вывести участника из роя (по умолчанию себя или нанятого тобой). Его открытые задачи вернутся в todo.",
    properties={"name": {"type": "string"}, "reason": {"type": "string"}},
    thread={"en": "Remove a member from the swarm (yourself by default, or a member you hired).",
            "ru": "Вывести участника из роя (по умолчанию себя или нанятого тобой)."},
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
    thread={
        "en": "End your turn and sleep until something happens (a message, an @mention of you, what your subscriptions "
              "catch, a trigger or the end of one of your background jobs). Call it when there is nothing useful left "
              "for you to do now. End every turn with it. Never poll the feed, messages or jobs in a loop instead: "
              "these events wake you.",
        "ru": "Завершить ход и спать, пока что-нибудь не произойдет (сообщение, @упоминание тебя, то, что ловят твои "
              "подписки, триггер или завершение твоего фонового процесса). Вызывай, когда полезных дел для тебя сейчас "
              "нет. Заканчивай им каждый ход. Не опрашивай ленту, сообщения и фоновые процессы в цикле: эти события "
              "тебя разбудят.",
    },
)
async def wait(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.runtime is not None:
        ctx.runtime.end_turn()
    # the caller's running jobs whose end will wake it (never one whose end is quiet, e.g. once the goal is done), and
    # the files it was refused whose release will (n_lock_free)
    awaited = [j.label for j in ctx.swarm.jobs.awaited(ctx.actor)]
    workspace = getattr(ctx.swarm, "workspace", None)  # (a test double may have none)
    refused = workspace.awaited_locks(ctx.actor) if workspace is not None else []
    also = []
    if len(awaited) == 1:
        also.append(f"your background job {awaited[0]} ends")
    elif awaited:
        also.append(f"your background jobs {', '.join(awaited)} end")
    if refused:
        also.append(f"{shown_list(refused)}, which you were refused, {'is' if len(refused) == 1 else 'are'} free")
    if also:
        return "OK, waiting. You will be woken up by new events, also when " + " and when ".join(also) + "."
    if _unwatched(ctx):
        return WAIT_UNWATCHED
    return "OK, waiting. You will be woken up by new events."


def _unwatched(ctx: ToolContext) -> bool:
    """The caller holds a stalled in_progress task (Swarm.stall_ids: not a clarify card waiting for the user, not a
    crew task with live directions; a `waiting` task alone does not count: answers wake it), has no active trigger of
    its own, and nobody else can act or be woken by something set (Swarm.team_asleep); the goal is active. The job
    and refused-file waits are ruled out by the caller. A test double without these gives False."""
    swarm = ctx.swarm
    stall_ids, team_asleep = getattr(swarm, "stall_ids", None), getattr(swarm, "team_asleep", None)
    triggers = getattr(swarm, "triggers", None)
    if stall_ids is None or team_asleep is None or triggers is None:
        return False
    goal = swarm.goal() or {}
    return (goal.get("status") == "active" and bool(stall_ids(ctx.actor))
            and not triggers.list(owner=ctx.actor) and team_asleep(ctx.actor))


@tool(
    "goal_complete",
    en="Declare the global goal achieved (only after verifying the result) with a summary for the user: "
       "what was delivered and where to find it.",
    ru="Объявить глобальную цель достигнутой (только после проверки результата) с итогом для пользователя: "
       "что сделано и где это найти.",
    properties={"summary": {"type": "string"}},
    required=["summary"],
    audience=("llm", "external"),
    modes=("board",),   # a thread goal is decided by the votes on #1 or by the user (docs/SPEC.md §22.4)
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
        text += (f" Note: background jobs are still running: {_job_list(running)}. Stop the ones nobody needs with "
                 "job_stop; the rest stop with the session.")
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
                f"Pass as_agent=\"{existing['name']}\" in every call; wait_for_events returns what you missed."
                + _skills_block(swarm, existing["name"]))
    name = await swarm.hire(
        USER, args["role"], args.get("instructions") or "", name=wanted or None, kind=AgentKind.EXTERNAL,
    )
    member = swarm.member(name) or {}
    role = member.get("role") or args["role"].strip()
    if getattr(swarm, "skills", None) is not None:
        swarm.skills.hire_report(name)  # its role's skills are marked in the list below
    if ctx.mode == "thread":
        digest = fmt_duration(swarm.config.agents.digest_seconds)
        text = (
            f"You joined the swarm as {name} (role: {role}). Pass as_agent=\"{name}\" in every call. Read the feed "
            "(`feed`, `post_read`), post with `post_create`, comment, like or dislike (`vote`; on #1 your vote is "
            "shown, only the swarm's AI agents' votes count), choose what wakes you with `subscriptions` / "
            "`subscribe`, talk with `send_message`, and call `wait_for_events` to receive @mentions, what your "
            "subscriptions catch, messages and trigger notifications (progress=true: new posts and comments and the "
            "goal vote too). You already follow the whole feed, #1, #2 and the reactions to your posts and comments "
            f"as a digest: it comes with your next wait_for_events result, at the latest about {digest} after its "
            "oldest item; votes never end the wait. You do not count toward the agent limit (max_agents)."
        )
    else:
        text = (
            f"You joined the swarm as {name} (role: {role}). Pass as_agent=\"{name}\" in every call. "
            "Look at `board`, claim tasks with `task_claim`, talk with `send_message`, and call `wait_for_events` "
            "to receive messages, assignments and trigger notifications (progress=true: board progress too). "
            "You do not count toward the agent limit (max_agents)."
        )
    # an external member has no system prompt: a catalog role's description reaches it here
    entry = roles.find_role(swarm.config.roles, role, member.get("language") or swarm.language, **_mode_kw(ctx.mode))
    if entry is not None and entry.description:
        text += f"\nYour role ({role}): {entry.description}"
    return text + _skills_block(swarm, name)


def _skills_block(swarm, name: str) -> str:
    """An external member has no system prompt nor a wake-up index: the skills are listed when it joins (and when it
    rejoins: a reconnected client may have lost the first list), changes come with wait_for_events."""
    if not _skill_names(swarm):
        return ""
    return ("\nSkills attached to the swarm: a skill is a SKILL.md with instructions for a kind of work; before work "
            "one fits, read it with read_file and follow it. You are told when skills are attached or detached; "
            "skill_propose proposes one you made.\n" + "\n".join(swarm.skills.item_lines(name, "en")))


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


def _thread_line(texts: dict[str, str], event: dict, by: str, lang: str, titles: dict[int, str]) -> str | None:
    """A thread session's line about a forum or goal event (None: not one of them, or no line for it)."""
    kind, data = event["type"], event["data"]
    if kind == "post.created":
        post = data.get("post") or {}
        if event["actor"] is None or not post.get("author"):
            return None  # a system post: goal.set covers it
        title = titles.get(post.get("id"), post.get("title"))
        return texts["posted"].format(id=post.get("id"), title=_clip(title), by=by)
    if kind == "post.comment":
        comment = data.get("comment") or {}
        post_id = data.get("post_id") or comment.get("post_id")
        return texts["commented"].format(cid=comment.get("id"), id=post_id, title=_clip(titles.get(post_id, "")),
                                         by=by, text=_clip(comment.get("text")))
    if kind == "goal.completed":
        decision = (data.get("goal") or {}).get("decision")
        if isinstance(decision, dict):
            if decision.get("rule") == "user":
                return texts["goal_done_user"]
            return texts["goal_done_vote"].format(rule=rule_name(lang, decision.get("rule")))
        return texts["goal_done"].format(by=by)
    if kind == "goal.reopened":
        return texts["goal_reopened_thread"].format(by=by)
    if kind == "swarm.wrap_up" and data.get("on"):
        return texts["wrap_up_on_thread"].format(by=by)
    return None


def _progress_line(texts: dict[str, str], event: dict, actor: str, mode: str = "board", lang: str = "en",
                   titles: dict[int, str] | None = None) -> str | None:
    """One line about a journal event that is progress made by someone else than `actor`; None otherwise."""
    by, data = event["actor"], event["data"]
    if by == actor:
        return None  # what the caller did itself is no news to it
    by = by or "DreamTeam"
    kind = event["type"]
    if mode == "thread" and kind in ("post.created", "post.comment", "goal.completed", "goal.reopened",
                                     "swarm.wrap_up"):
        line = _thread_line(texts, event, by, lang, titles or {})
        if line is not None or kind != "swarm.wrap_up":
            return line
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
    if kind == "swarm.wrap_up":
        return texts["wrap_up_on" if data.get("on") else "wrap_up_off"].format(by=by)
    name = data.get("name")
    if not name or name == actor:
        return None
    if kind == "agent.hired":
        key = "joined" if data.get("kind") == AgentKind.EXTERNAL else "hired"
        return texts[key].format(name=name, role=_clip(data.get("role"), 60), by=by)
    if kind == "agent.retired":
        return texts["retired"].format(name=name, by=by)
    return None


def _post_titles(swarm, events: list[dict]) -> dict[int, str]:
    """The current titles of the posts these events are about, read once per result."""
    ids = set()
    for event in events:
        data = event["data"]
        if event["type"] == "post.created":
            ids.add((data.get("post") or {}).get("id"))
        elif event["type"] == "post.comment":
            ids.add(data.get("post_id") or (data.get("comment") or {}).get("post_id"))
    ids = sorted(i for i in ids if isinstance(i, int))
    if not ids:
        return {}
    rows = swarm.db.all(f"SELECT id, title FROM posts WHERE id IN ({', '.join('?' * len(ids))})", tuple(ids))
    return {r["id"]: r["title"] for r in rows}


def _progress(swarm, actor: str, cursor: int) -> tuple[list[tuple[float, str]], int, bool]:
    """Progress after journal event `cursor`, as (event time, line in `actor`'s language), the last event read, and
    whether someone else voted on #1 in between (a thread session reads the votes too, which never make a line; on
    the board it is always False)."""
    mode = getattr(swarm, "mode", "board")
    types = PROGRESS_TYPES[mode] + (("post.vote",) if mode == "thread" else ())
    rows = swarm.db.all(f"SELECT id, ts, type, actor, data FROM events WHERE id > ? AND type IN "
                        f"({', '.join('?' * len(types))}) ORDER BY id LIMIT ?",
                        (cursor, *types, PROGRESS_SCAN))
    lang = swarm.language_of(actor)
    texts = _PROGRESS.get(lang) or _PROGRESS["en"]
    events = [{"ts": row["ts"], "type": row["type"], "actor": row["actor"], "data": loads(row["data"], {})}
              for row in rows]
    events = [e for e in events if isinstance(e["data"], dict)]
    titles = _post_titles(swarm, events) if mode == "thread" else {}
    lines, voted = [], False
    for event in events:
        if event["type"] == "post.vote":
            voted = voted or (event["data"].get("target") == "p1" and event["actor"] != actor)
            continue
        line = _progress_line(texts, event, actor, mode, lang, titles)
        if line:
            lines.append((event["ts"] or 0.0, line))
    return lines, (rows[-1]["id"] if rows else cursor), voted


async def _signal_or_progress(swarm, actor: str, queue: asyncio.Queue, timeout: float) -> None:
    """Wait until `actor` is signalled, a progress event of the session's mode arrives on the bus `queue` (never a
    vote) or `timeout` passes."""
    types = PROGRESS_TYPES[getattr(swarm, "mode", "board")]

    async def progress_event() -> None:
        while (await queue.get()).get("type") not in types:
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
       "status, the goal set, achieved or reopened, members hired, joined or leaving, wrap-up mode switched on or "
       "off), as short lines with their time (HH:MM). When the news spans more than a minute, every item has its time and the header the range.",
    ru=f"Подождать (до timeout секунд, по умолчанию {WAIT_DEFAULT}, максимум {WAIT_MAX}) новых сообщений, "
       "уведомлений, назначений или срабатываний триггеров, адресованных тебе, и вернуть их. Сразу "
       "возвращается, если что-то уже есть. С progress=true возвращается и при прогрессе на доске, сделанном "
       "другими (задачи созданы или сменили статус, цель задана, достигнута или возвращена в работу, участники "
       "наняты, вступили или ушли, режим завершения включён или выключен), короткими строками со временем (ЧЧ:ММ). Если новости охватывают больше "
       "минуты, время есть у каждой из них, а в заголовке — диапазон.",
    properties={"timeout": {"type": "integer", "description": f"Seconds, 0-{WAIT_MAX}"},
                "progress": {"type": "boolean",
                             "description": "Also return on board progress (default false: only what is addressed "
                                            "to you)"}},
    audience=("external", "user"),
    thread={
        "en": f"Wait (up to timeout seconds, default {WAIT_DEFAULT}, max {WAIT_MAX}) for new messages, notifications, "
              "@mentions, what your subscriptions catch or triggers addressed to you, and return them. Returns "
              "immediately if something is already pending. With progress=true it also returns when the feed moves "
              "on (new posts and comments by others, the goal set, reached or reopened, members hired, joined or "
              "leaving, wrap-up mode switched on or off), as short lines with their time (HH:MM), with the goal vote "
              "on #1 when it changed (votes alone never end the wait). When the news spans more than a minute, every "
              "item has its time and the header the range.",
        "ru": f"Подождать (до timeout секунд, по умолчанию {WAIT_DEFAULT}, максимум {WAIT_MAX}) новых сообщений, "
              "уведомлений, @упоминаний, того, что ловят твои подписки, или срабатываний триггеров, адресованных "
              "тебе, и вернуть их. Сразу возвращается, если что-то уже есть. С progress=true возвращается и когда "
              "лента движется (новые объявления и комментарии других, цель задана, достигнута или возвращена в "
              "работу, участники наняты, вступили или ушли, режим завершения включен или выключен), короткими "
              "строками со временем (ЧЧ:ММ), с голосованием о цели на #1, если оно изменилось (одни голоса ожидание не "
              "завершают). Если новости охватывают больше минуты, время есть у каждой из них, а в заголовке — "
              "диапазон.",
        "properties": {"timeout": {"type": "integer", "description": f"Seconds, 0-{WAIT_MAX}"},
                       "progress": {"type": "boolean",
                                    "description": "Also return when the feed moves on (default false: only what is "
                                                   "addressed to you)"}},
    },
)
async def wait_for_events(ctx: ToolContext, args: dict[str, Any]) -> str:
    swarm = ctx.swarm
    timeout = args.get("timeout")
    timeout = max(0, min(WAIT_DEFAULT if timeout is None else timeout, WAIT_MAX))
    progress = bool(args.get("progress"))
    workspace = getattr(swarm, "workspace", None)  # (the test double has none)
    if workspace is not None and not ctx.is_user and not _stopping(swarm):
        # an external member's wait ends its turn: the new files its last command left (held back for one more
        # command, docs/SPEC.md §7 "Held new files") are committed now, as at the end of an AI agent's activation
        await workspace.commit_held(ctx.actor, f"at the end of {ctx.actor}'s turn")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    queue = swarm.bus.subscribe() if progress else None  # progress wakes the wait too
    voted_any = False   # a vote on #1 by someone else (thread): one goal vote line, never one per vote
    try:
        cursor = _progress_cursor(swarm, ctx.actor) if progress and not _stopping(swarm) else 0
        # a signal can be left over from an event that was already consumed (e.g. by read_messages):
        # keep waiting until something is really pending or the time is up
        while not _stopping(swarm) and not swarm.has_pending(ctx.actor):
            if progress:
                lines, last, voted = _progress(swarm, ctx.actor, cursor)
                if lines:
                    break
                voted_any |= voted  # the cursor moves over these votes: keep that they came
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
    lines: list[tuple[float, str]] = []
    if progress:
        lines, last, voted = _progress(swarm, ctx.actor, cursor)
        voted_any |= voted
        swarm.db.set(_progress_key(ctx.actor), last)
    thread = ctx.mode == "thread"
    tally = swarm.forum.goal_vote() if thread and voted_any and getattr(swarm, "forum", None) is not None else None
    # one result may hold hours of news (progress collected since the last progress call, notifications queued
    # while the client was away): every progress line has its time, the rest too when the batch spans minutes
    since = min((ts for ts, _ in lines if ts), default=None)
    text = swarm.render_wake(ctx.actor, incoming=True, stamps=True, since=since)
    if lines or tally is not None:
        now = time.time()
        lang = swarm.language_of(ctx.actor)
        texts = _PROGRESS.get(lang) or _PROGRESS["en"]
        shown = [f"• {item_time(ts, now)} {line}" if ts else f"• {line}" for ts, line in lines[:PROGRESS_MAX]]
        if len(lines) > PROGRESS_MAX:
            more = texts["more_thread" if thread else "more"]
            shown.append(f"• {more.format(n=len(lines) - PROGRESS_MAX)}")
        if tally is not None:  # no time and no count toward PROGRESS_MAX: the vote as it stands now
            shown.append(f"• {vote_line(lang, tally)}")
        if not text:
            first = since if since is not None and now - since > STAMP_SPAN else None
            text = prompts.t(lang, "incoming_header", time=stamp_range(first, now))
        text += "\n" + "\n".join([texts["header_thread" if thread else "header"], *shown])
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
    text = "Goal set. " + goal_line(goal)
    if ctx.mode == "thread":
        text += " The feed has #1 \"Goal reached?\" (the vote) and #2 \"Let's plan the work\"."
    return text


@tool(
    "swarm_status",
    en="Overview of the swarm: goal, budget, pause and wrap-up state, members, task counts and your unread items. "
       "brief=true shows the goal as one line (title and status, without its description and summary).",
    ru="Обзор роя: цель, бюджет, пауза и режим завершения, участники, счётчики задач и твои непрочитанные. "
       "brief=true показывает цель одной строкой (заголовок и статус, без описания и итога).",
    properties={"brief": {"type": "boolean",
                          "description": "The goal as one line, without its description and summary (default false)"}},
    audience=("external", "user"),
    thread={
        "en": "Overview of the swarm: goal and its vote on #1, budget, pause and wrap-up state, members, the feed's "
              "counts and your unread items. brief=true shows the goal as one line (title and status, without its "
              "description and summary).",
        "ru": "Обзор роя: цель и голосование на #1, бюджет, пауза и режим завершения, участники, счетчики ленты и твои "
              "непрочитанные. brief=true показывает цель одной строкой (заголовок и статус, без описания и итога).",
    },
)
async def swarm_status(ctx: ToolContext, args: dict[str, Any]) -> str:
    swarm = ctx.swarm
    thread = ctx.mode == "thread"
    limits = swarm.limits()
    members = swarm.members()
    goal = swarm.goal()
    if args.get("brief") and goal:  # a client that checks often needs no repeat of a long goal text
        goal = {"title": goal.get("title"), "status": goal.get("status", "active")}
    state = "PAUSED: " + swarm.pause_text() if swarm.paused else "running"
    question = swarm.pending_question() if ctx.is_user else None
    if question is not None:  # the user's next message to the asker answers it (Mailbox.send)
        state += (f" (your next send_message to {question['asker']} or to all is the answer: every member gets it with "
                  "the question; swarm_control action=resume goes on without it)")
    if getattr(swarm, "clarifying", False) and thread:  # docs/SPEC.md §22.4 "Clarify"
        state += ("; CLARIFY MODE: at a new goal the first AI agent asks the user the goal's forks and comments the "
                  "answers on #2, and a member's question to the user with pause_swarm=true pauses the swarm until "
                  "the user answers")
    elif getattr(swarm, "clarifying", False):  # docs/SPEC.md §3 "Clarify mode"
        state += ("; CLARIFY MODE: the planning task first asks the user, and a member's question to the user with "
                  "pause_swarm=true pauses the swarm until the user answers")
    if swarm.wrapping_up and thread:
        state += ("; WRAP-UP MODE: the agents post their results and what is left, start nothing new, and a like on #1 "
                  "now means \"finish with what we have\"")
    elif swarm.wrapping_up:
        state += ("; WRAP-UP MODE: the agents finish their tasks with what they have, no new computations or "
                  "background jobs")
    if netoff.is_off(swarm.config):  # docs/SPEC.md §7.2
        state += "; NO NETWORK for the members' commands, jobs and MCP servers" + {
            "docker": " (the Docker sandbox's firewall)", "netns": " (a network namespace)",
            "env": " (best effort: proxy and offline variables only)",
            "container": " (best effort: proxy and offline variables only; the sandbox was started without its "
                         "firewall)"}.get(netoff.shown(netoff.detected()) or "", "")
    lines = [f"Swarm \"{swarm.config.swarm.name}\" — {state}", goal_line(goal)]
    tally = swarm.forum.goal_vote() if thread else None
    if tally is not None:
        lines.append(vote_line(ctx.lang, tally))
    lines += [
        f"{budget_line(swarm.spent_usd(), limits['max_cost_usd'])} {_team_size(members, limits)}",
        "Members: " + (", ".join(f"{m['name']} ({m['role']}, {m['status']})" for m in members) or "none"),
    ]
    if thread:
        counts = swarm.forum.counts()
        posts, comments = counts["posts"], counts["comments"]
        lines.append(f"Feed: {posts} post{'s' if posts != 1 else ''}, "
                     f"{comments} comment{'s' if comments != 1 else ''}.")
    else:
        counts = swarm.board.counts()
        lines.append("Tasks: " + (", ".join(f"{k}={v}" for k, v in counts.items() if v) or "none"))
    running = swarm.jobs.running_of()
    if running:
        lines.append(f"Background jobs: {len(running)} running ({_job_list(running, parens=False)}).")
    attached = _skill_names(swarm)
    if attached:
        pending = swarm.skills.pending()
        waiting = f"; {len(pending)} waiting for the user's library decision" if pending else ""
        for status, words in (("installing", "being installed"), ("failed", "failed")):
            names = [n for n in attached if any(d["status"] == status for d in swarm.skills.deps(n))]
            if names:
                waiting += f"; packages {words} for {len(names)} ({', '.join(names)})"
        lines.append(f"Skills: {len(attached)} attached ({', '.join(attached)}){waiting}.")
        # an external member reads them by these paths; a client that checks often (brief) has them from join_swarm
        if not ctx.is_user and not args.get("brief"):
            lines.extend(swarm.skills.item_lines(ctx.actor, "en"))
    if not ctx.is_user and thread:
        lines.append(f"You are {ctx.actor}. Your posts: {_own_posts(swarm, ctx.actor)}.")
    elif not ctx.is_user:
        mine = swarm.board.open_tasks_of(ctx.actor)
        lines.append(f"You are {ctx.actor}. Your open tasks: "
                     + ("; ".join(t.short() for t in mine) if mine else "none"))
        own = [j.label for j in running if j.owner == ctx.actor]
        if own:
            lines.append(f"Your background jobs: {', '.join(own)}.")
    pending = " New events are pending: call wait_for_events." if swarm.has_pending(ctx.actor) else ""
    lines.append(f"Your unread messages: {swarm.mail.unread_count(ctx.actor)}.{pending}")
    return "\n".join(lines)


def _own_posts(swarm, actor: str, limit: int = 5) -> str:
    """The member's most recently active posts with their counts: "#9 (likes 4, dislikes 0, comments 3), #12 (...)"."""
    ids = [r["id"] for r in swarm.db.all("SELECT id FROM posts WHERE author = ? ORDER BY active_at DESC, id DESC "
                                         "LIMIT ?", (actor, limit))]
    posts = [swarm.forum.get(i) for i in ids]
    return ", ".join(f"#{p['id']} (likes {p.get('likes', 0)}, dislikes {p.get('dislikes', 0)}, comments "
                     f"{p.get('comments', 0)})" for p in posts) or "none"


_CONTROL_PROPS = {
    "action": {"type": "string", "enum": ["pause", "resume"]},
    "wrap_up": {"type": "boolean",
                "description": "true: switch wrap-up mode on (each agent is told once); false: switch it off"},
    "max_cost_usd": {"type": "number", "description": "Budget in USD, 0 = no limit"},
    "max_agents": {"type": "integer",
                   "description": "Maximum number of AI agents (members who joined through MCP do not count)"},
    "reopen_goal_comment": {"type": "string", "description": "Reopen the completed goal with this comment"},
}


@tool(
    "swarm_control",
    en="Control the swarm: action=pause|resume, wrap_up=true|false (wrap-up mode: the agents finish their tasks with "
       "what they have, start no new computations, and no new background jobs start; a new or reopened goal ends "
       "it), change limits (max_cost_usd, 0 = no limit; max_agents), or reopen a completed goal with "
       "reopen_goal_comment.",
    ru="Управление роем: action=pause|resume, wrap_up=true|false (режим завершения: агенты завершают свои задачи с "
       "тем, что есть, не начинают новых вычислений, и новые фоновые процессы не запускаются; новая или "
       "возвращённая в работу цель его выключает), изменение лимитов (max_cost_usd, 0 — без лимита; max_agents) "
       "или возврат завершённой цели в работу через reopen_goal_comment.",
    properties=_CONTROL_PROPS,
    audience=("user",),
    # the user completes a thread goal directly (docs/SPEC.md §22.4); live texts: no frozen set has the tool
    thread={
        "en": "Control the swarm: action=pause|resume, wrap_up=true|false (wrap-up mode: the agents post their results "
              "and what is left, start no new computations, and no new background jobs start; a like on #1 then means "
              "\"finish with what we have\"; a new or reopened goal ends it), change limits (max_cost_usd, 0 = no "
              "limit; max_agents), reopen a completed goal with reopen_goal_comment (the votes on #1 are cleared), or "
              "complete the active goal yourself with complete_goal (optional goal_summary).",
        "ru": "Управление роем: action=pause|resume, wrap_up=true|false (режим завершения: агенты публикуют свои "
              "результаты и то, что осталось, не начинают новых вычислений, и новые фоновые процессы не запускаются; "
              "like на #1 тогда значит «завершить с тем, что есть»; новая или возвращенная в работу цель его "
              "выключает), изменение лимитов (max_cost_usd, 0 — без лимита; max_agents), возврат завершенной цели в "
              "работу через reopen_goal_comment (голоса на #1 сбрасываются) или завершение активной цели самим "
              "пользователем через complete_goal (goal_summary — по желанию).",
        "properties": {
            **_CONTROL_PROPS,
            "complete_goal": {"type": "boolean",
                              "description": "Thread sessions only: complete the active goal yourself, whatever the "
                                             "vote on #1"},
            "goal_summary": {"type": "string",
                             "description": "With complete_goal: the goal's summary (default: a generated one with "
                                            "the vote on #1)"},
        },
    },
)
async def swarm_control(ctx: ToolContext, args: dict[str, Any]) -> str:
    swarm = ctx.swarm
    thread = ctx.mode == "thread"
    complete = thread and bool(args.get("complete_goal"))
    if complete and args.get("reopen_goal_comment"):  # checked first: nothing is applied
        raise ToolError("Pass either complete_goal or reopen_goal_comment, not both.")
    if thread and args.get("goal_summary") and not complete:
        raise ToolError("goal_summary goes with complete_goal.")
    done = []
    wrap = args.get("wrap_up")
    if args.get("max_cost_usd") is not None or args.get("max_agents") is not None:
        limits = swarm.set_limits(ctx.actor, args.get("max_cost_usd"), args.get("max_agents"))
        budget = f"${limits['max_cost_usd']:.2f}" if limits["max_cost_usd"] > 0 else "no budget limit"
        done.append(f"limits: {budget}, at most {limits['max_agents']} AI agents")
    applied = f" ({'; '.join(done)} applied)." if done else "."
    if complete:
        try:
            swarm.complete_goal_by_user(args.get("goal_summary") or "")
        except ToolError as exc:
            raise ToolError(str(exc).rstrip(".") + applied) from None
        done.append("goal marked reached")
    if args.get("reopen_goal_comment"):
        goal = swarm.goal()
        if goal and goal.get("status") != "done":
            raise ToolError("The global goal is not completed, so there is nothing to reopen" + applied)
        swarm.reopen_goal(ctx.actor, args["reopen_goal_comment"])
        done.append("goal reopened: the votes on #1 were cleared" if thread else "goal reopened")
    action = args.get("action")
    kept: list = []   # the jobs a pause that paused leaves running (their own sentence after the joined parts, §12)
    if action == "pause":
        if swarm.pause(ctx.actor, USER_PAUSE):
            done.append("paused")
            kept = swarm.jobs.running_of()
        else:
            done.append(f"already paused ({swarm.pause_text()})")
    elif action == "resume":
        swarm.resume(ctx.actor)
        done.append("resumed")
    # after reopen_goal_comment, which ends wrap-up mode: both in one call means "reopen, then wrap up"
    if wrap is True:
        told = ("every agent was told to post what it has and what is left, start nothing new and like #1 to finish "
                "with it" if thread else "every agent was told to finish its tasks with what it has")
        done.append(f"wrap-up mode on: {told}, and no new background jobs start" if swarm.wrap_up(ctx.actor)
                    else "wrap-up mode was already on")
    elif wrap is False:
        done.append("wrap-up mode off" if swarm.end_wrap_up(ctx.actor) else "wrap-up mode was not on")
    if not done and thread:
        raise ToolError("Nothing to do: pass action (pause/resume), wrap_up, a limit, reopen_goal_comment or "
                        "complete_goal.")
    if not done:
        raise ToolError("Nothing to do: pass action (pause/resume), wrap_up, a limit or reopen_goal_comment.")
    text = "Done: " + "; ".join(done) + "."
    if kept:  # ids only: USER's u_paused_jobs (Swarm._set_pause) has the details
        text += (f" Background jobs keep running ({', '.join(j.label for j in kept)}): a pause stops only the agents' "
                 "model calls; job_stop stops a job.")
    return text
