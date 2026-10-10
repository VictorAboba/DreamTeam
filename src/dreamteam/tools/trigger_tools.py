"""Triggers: wake up or act when a condition over the swarm state becomes true."""

from __future__ import annotations

import json
from typing import Any

from ..models import Status
from . import ToolContext, tool
from .fmt import ago

_DSL = (
    "Condition = a boolean expression with and/or/not, comparisons (== != < <= > >= in) and functions: "
    "status(id), done(*ids) [all done], any_done(*ids), finished(*ids) [all done/failed/cancelled], "
    "failed(*ids) [any failed], assignee(id), count(status=, tag=, assignee=, parent=), all_done(tag=, parent=), "
    "message(sender=, contains=) [a message to you arrived], comment(task=, sender=, contains=) [a new discussion "
    "post], file_exists(path), file_changed(path), after(\"2026-10-09 18:00\"), elapsed(seconds), "
    "every(seconds) [periodic, >= 10], agent_status(name), goal_done(). Status names (todo, in_progress, waiting, "
    "review, done, failed, cancelled) can be used bare. Examples: done(1, 5); status(3) in (failed, cancelled); "
    "done(2) and not failed(4); count(status=done, tag=\"api\") >= 3; message(sender=\"Anna Smith\"); every(1800)."
)
_DSL_RU = (
    "Условие — логическое выражение с and/or/not, сравнениями (== != < <= > >= in) и функциями: "
    "status(id), done(*ids) [все выполнены], any_done(*ids), finished(*ids) [все завершены как угодно], "
    "failed(*ids) [хотя бы одна провалена], assignee(id), count(status=, tag=, assignee=, parent=), "
    "all_done(tag=, parent=), message(sender=, contains=) [пришло сообщение тебе], comment(task=, sender=, "
    "contains=) [новый пост в обсуждении], file_exists(path), file_changed(path), after(\"2026-10-09 18:00\"), "
    "elapsed(секунды), every(секунды) [периодически, >= 10], agent_status(имя), goal_done(). Статусы (todo, "
    "in_progress, waiting, review, done, failed, cancelled) можно писать без кавычек. Примеры: done(1, 5); "
    "status(3) in (failed, cancelled); done(2) and not failed(4); count(status=done, tag=\"api\") >= 3; "
    "message(sender=\"Анна Смирнова\"); every(1800)."
)
_ACTIONS = (
    "By default the trigger wakes you with the note; actions can instead send a message, set a task status "
    "or create a task (add {\"type\": \"wake\"} to also wake you). once=true (default): the trigger is "
    "deactivated after it fires; once=false: it fires every time the condition becomes true again "
    "(every() triggers always repeat)."
)
_ACTIONS_RU = (
    "По умолчанию триггер будит тебя с заметкой note; actions позволяют вместо этого отправить сообщение, "
    "сменить статус задачи или создать задачу (добавь {\"type\": \"wake\"}, чтобы ещё и проснуться). "
    "once=true (по умолчанию): после срабатывания триггер отключается; once=false: срабатывает каждый раз, "
    "когда условие снова становится истинным (триггеры с every() повторяются всегда)."
)

_ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["wake", "message", "set_status", "create_task"]},
        "to": {"type": "array", "items": {"type": "string"}, "description": "message: recipient names, \"all\" or \"user\""},
        "text": {"type": "string", "description": "message: text"},
        "task_id": {"type": "integer", "description": "set_status: task id"},
        "status": {"type": "string", "enum": list(Status.ALL), "description": "set_status: new status"},
        "title": {"type": "string", "description": "create_task: title"},
        "goal": {"type": "string", "description": "create_task: goal"},
        "description": {"type": "string", "description": "create_task: description"},
        "assignee": {"type": "string", "description": "create_task: member name"},
        "tags": {"type": "array", "items": {"type": "string"}, "description": "create_task: tags"},
        "priority": {"type": "integer", "minimum": 0, "maximum": 3, "description": "create_task: 0-3"},
    },
    "required": ["type"],
}


@tool(
    "trigger_create",
    en="Create a trigger that fires when a condition over the swarm state becomes true. " + _DSL + " " + _ACTIONS,
    ru="Создать триггер, срабатывающий, когда условие над состоянием роя становится истинным. " + _DSL_RU + " "
       + _ACTIONS_RU,
    properties={
        "condition": {"type": "string"},
        "note": {"type": "string", "description": "Reminder shown when it fires (what to do then)"},
        "once": {"type": "boolean"},
        "actions": {"type": "array", "items": _ACTION_SCHEMA, "description": "Default: [{\"type\": \"wake\"}]"},
    },
    required=["condition"],
)
async def trigger_create(ctx: ToolContext, args: dict[str, Any]) -> str:
    trig = ctx.swarm.triggers.create(
        ctx.actor, args["condition"], note=args.get("note") or "", actions=args.get("actions") or None,
        once=True if args.get("once") is None else args["once"],
    )
    kinds = [a["type"] for a in trig["actions"]]
    text = f"Trigger #{trig['id']} created: {trig['condition']}" + (" (repeating)" if not trig["once"] else "")
    if kinds != ["wake"]:
        text += f"; actions: {', '.join(kinds)}"
    return text + "."


@tool(
    "trigger_list",
    en="List your active triggers (all=true: every member's triggers, including inactive ones).",
    ru="Показать твои активные триггеры (all=true — триггеры всех участников, включая неактивные).",
    properties={"all": {"type": "boolean"}},
)
async def trigger_list(ctx: ToolContext, args: dict[str, Any]) -> str:
    everything = bool(args.get("all"))
    items = ctx.swarm.triggers.list(owner=None if everything else ctx.actor, include_inactive=everything)
    if not items:
        return "No triggers." if everything else "You have no active triggers (all=true lists everyone's)."
    lines = []
    for t in items:
        state = "active" if t["active"] else "inactive"
        repeat = "" if t["once"] else ", repeating"
        line = f"#{t['id']} [{state}{repeat}] owner={t['owner']} when {t['condition']}"
        if t.get("note"):
            line += f" — {t['note']}"
        if t.get("actions") and t["actions"] != [{"type": "wake"}]:
            line += f" actions={json.dumps(t['actions'], ensure_ascii=False)}"
        line += f" (fired {t.get('fire_count', 0)}x, last {ago(t.get('last_fired'))})"
        if t.get("last_error"):
            line += f" error: {t['last_error']}"
        lines.append(line)
    return "\n".join(lines)


@tool(
    "trigger_delete",
    en="Delete one of your triggers.",
    ru="Удалить свой триггер.",
    properties={"trigger_id": {"type": "integer"}},
    required=["trigger_id"],
)
async def trigger_delete(ctx: ToolContext, args: dict[str, Any]) -> str:
    ctx.swarm.triggers.delete(ctx.actor, args["trigger_id"])
    return f"Trigger #{args['trigger_id']} deleted."
