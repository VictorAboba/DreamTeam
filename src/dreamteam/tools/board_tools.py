"""Task board tools: view, create, claim, update, complete, review, discuss."""

from __future__ import annotations

from typing import Any

from ..board import MAX_POST, UNSET
from ..models import PRIORITIES, USER, Status, Task, ToolError
from . import ToolContext, tool
from .fmt import goal_line, post_line, task_detail, task_line, trim

_STATUS_DOC = "todo | in_progress | waiting | review | done | failed | cancelled"

_TASK_ID = {"type": "integer", "description": "Task number, e.g. 5"}
_MEMBER = {"type": "string", "description": "Member name, 'me' or 'user'"}
_PRIORITY = {"type": "integer", "minimum": 0, "maximum": 3, "description": "0 low, 1 normal, 2 high, 3 critical"}
_TAGS = {"type": "array", "items": {"type": "string"}}
_DEPENDS = {"type": "array", "items": {"type": "integer"}, "description": "Ids of tasks that must be done first"}

BOARD_GOALS_UP_TO = 25     # with more tasks the board shows one line per task (goals via task_get)
BOARD_MAX_TASKS = 100


def _member_arg(ctx: ToolContext, value: Any, allow_retired: bool = False) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    if value == "":
        return None
    if value.lower() in ("me", "self", "myself", "я"):
        return ctx.actor
    return ctx.swarm.resolve_member(value, allow_retired=allow_retired)


def _opt_id(value: Any) -> int | None:
    """Optional ids: models often send 0 for "none"."""
    return value or None


@tool(
    "board",
    en="Show the task board: the global goal, task counts and tasks (open ones by default). "
       "Filters: status (one or several, comma-separated), assignee ('me' for yourself), tag, include_closed. "
       "Your wake-up message already lists your open tasks and the ready ones; call this when you need the full "
       "picture (e.g. before creating tasks).",
    ru="Показать доску задач: глобальную цель, счётчики и задачи (по умолчанию открытые). "
       "Фильтры: status (один или несколько через запятую), assignee ('me' — свои), tag, include_closed. "
       "Сообщение при пробуждении уже перечисляет твои открытые и готовые задачи; вызывай, когда нужна полная "
       "картина (например, перед созданием задач).",
    properties={
        "status": {"type": "string", "description": "One status or several, comma-separated: " + _STATUS_DOC},
        "assignee": _MEMBER,
        "tag": {"type": "string"},
        "include_closed": {"type": "boolean", "description": "Also show done/failed/cancelled tasks"},
    },
)
async def board(ctx: ToolContext, args: dict[str, Any]) -> str:
    swarm = ctx.swarm
    tasks = swarm.board.list(
        status=args.get("status") or None,
        assignee=_member_arg(ctx, args.get("assignee"), allow_retired=True),
        tag=args.get("tag") or None,
        include_closed=bool(args.get("include_closed")),
    )
    counts = swarm.board.counts()
    lines = [goal_line(swarm.goal())]
    lines.append("Counts: " + (", ".join(f"{k}={v}" for k, v in counts.items() if v) or "no tasks"))
    if not tasks:
        lines.append("No matching tasks.")
        return "\n".join(lines)
    with_goal = len(tasks) <= BOARD_GOALS_UP_TO
    lines.extend(task_line(t, with_goal=with_goal) for t in tasks[:BOARD_MAX_TASKS])
    if len(tasks) > BOARD_MAX_TASKS:
        lines.append(f"... and {len(tasks) - BOARD_MAX_TASKS} more tasks: narrow the list with status, "
                     "assignee or tag.")
    if not with_goal:
        lines.append(f"({len(tasks)} tasks: goals are omitted; task_get shows a task in full.)")
    return "\n".join(lines)


@tool(
    "task_get",
    en="Show one task in full: goal, description, status, people, dependencies, subtasks, result and the "
       "latest discussion posts.",
    ru="Показать задачу полностью: цель, описание, статус, участники, зависимости, подзадачи, итог и "
       "последние посты обсуждения.",
    properties={"task_id": _TASK_ID},
    required=["task_id"],
)
async def task_get(ctx: ToolContext, args: dict[str, Any]) -> str:
    board_ = ctx.swarm.board
    task = board_.get(args["task_id"])
    text = task_detail(task)
    # not in review: its result is already produced and waits for the reviewer
    if task.blocked_by and task.assignee == ctx.actor and task.status in Status.OPEN - {Status.REVIEW}:
        text += ("\nYou hold it while it is blocked: if its result can be produced without some of "
                 f"{', '.join(f'#{d}' for d in task.blocked_by)}, remove those with task_update (depends_on = the "
                 "ones still needed) and say why in its discussion.")
    every = board_.list(include_closed=True)
    subtasks = [t for t in every if t.parent_id == task.id]
    if subtasks:
        text += "\nSubtasks: " + "; ".join(f"#{t.id} [{t.status}] {trim(t.title, 60)}" for t in subtasks)
    needed_by = [t for t in every if task.id in t.depends_on and t.status in Status.OPEN]
    if needed_by:
        text += "\nRequired by (open): " + ", ".join(f"#{t.id} [{t.status}]" for t in needed_by)
    posts = board_.discussion(task.id)[-5:]
    if posts:
        text += "\nLatest posts (task_discussion shows the whole thread):\n" + "\n".join(post_line(p, 600) for p in posts)
    return text


@tool(
    "task_create",
    en="Create a task on the board. Always give a goal: why the task exists and what counts as success. "
       "Optional: description, assignee (member name or 'me'; they are notified), reviewer, depends_on (task ids "
       "that must be done first), parent_id (for subtasks), priority (0 low, 1 normal, 2 high, 3 critical), tags. "
       "Check the board first so you do not duplicate a task. Write the description for a member who has none "
       "of your context (inputs, file paths, interfaces). Leave assignee empty to let free members claim it.",
    ru="Создать задачу на доске. Всегда указывай goal (цель): зачем нужна задача и что считается успехом. "
       "Необязательно: description, assignee (имя участника или 'me'; он получит уведомление), reviewer, "
       "depends_on (номера задач, которые должны быть выполнены раньше), parent_id (для подзадач), "
       "priority (0 низкий, 1 обычный, 2 высокий, 3 критичный), tags. Сначала проверь доску, чтобы не "
       "создать дубликат. Пиши description для участника без твоего контекста (входные данные, пути к файлам, "
       "интерфейсы). Оставь assignee пустым, чтобы задачу взял свободный участник.",
    properties={
        "title": {"type": "string", "description": "Short imperative title (max 200 characters)"},
        "goal": {"type": "string", "description": "Why the task exists / success criterion"},
        "description": {"type": "string", "description": "Details, context, acceptance criteria"},
        "assignee": _MEMBER,
        "reviewer": _MEMBER,
        "depends_on": _DEPENDS,
        "parent_id": {"type": "integer", "description": "Parent task id (makes this a subtask)"},
        "priority": _PRIORITY,
        "tags": _TAGS,
    },
    required=["title", "goal"],
)
async def task_create(ctx: ToolContext, args: dict[str, Any]) -> str:
    task = ctx.swarm.board.create(
        ctx.actor,
        args["title"],
        goal=args["goal"],
        description=args.get("description") or "",
        assignee=_member_arg(ctx, args.get("assignee")),
        reviewer=_member_arg(ctx, args.get("reviewer")),
        parent_id=_opt_id(args.get("parent_id")),
        depends_on=args.get("depends_on") or None,
        priority=1 if args.get("priority") is None else args["priority"],
        tags=args.get("tags") or None,
    )
    return f"Created task {task_line(task, with_goal=False)}"


@tool(
    "task_claim",
    en="Claim a todo task: you become its assignee and it moves to in_progress. Fails if someone else holds it "
       "or its dependencies are not done.",
    ru="Взять задачу в статусе todo: ты становишься исполнителем, статус in_progress. Не получится, если "
       "задачу держит кто-то другой или её зависимости не выполнены.",
    properties={"task_id": _TASK_ID},
    required=["task_id"],
    audience=("llm", "external"),
)
async def task_claim(ctx: ToolContext, args: dict[str, Any]) -> str:
    before = ctx.swarm.board.get(args["task_id"])
    task = ctx.swarm.board.claim(ctx.actor, args["task_id"])
    text = f"You now work on {task_line(task)}"
    if before.reviewer == ctx.actor and task.reviewer is None:
        text += "\nYou were its reviewer; since you execute it now, another member will review it."
    return text


@tool(
    "task_update",
    en="Change a task (fields of tasks you created or are assigned to): title, goal, description, priority, tags, "
       "depends_on, parent_id (0 = none), reviewer, assignee ('me', a name, '' to unassign), status. Status: "
       "in_progress, waiting (blocked: explain in comment), todo (releases the task), failed, cancelled; "
       "finish with task_complete, not with done/review. comment is posted to the task discussion.",
    ru="Изменить задачу (поля задач, которые ты создал или которые на тебе): title, goal, description, priority, "
       "tags, depends_on, parent_id (0 — без родителя), reviewer, assignee ('me', имя, '' — снять исполнителя), "
       "status. Статусы: in_progress, waiting (заблокирована: объясни в comment), todo (отпустить задачу), "
       "failed, cancelled; завершай через task_complete, а не через done/review. comment публикуется в "
       "обсуждении задачи.",
    properties={
        "task_id": _TASK_ID,
        "title": {"type": "string"},
        "goal": {"type": "string"},
        "description": {"type": "string"},
        "status": {"type": "string", "enum": list(Status.ALL)},
        "assignee": {"type": "string", "description": "Member name, 'me', 'user' or '' to unassign"},
        "reviewer": {"type": "string", "description": "Member name, 'me', 'user' or '' for no reviewer"},
        "priority": _PRIORITY,
        "tags": _TAGS,
        "depends_on": _DEPENDS,
        "parent_id": {"type": "integer", "description": "Parent task id; 0 = no parent"},
        "comment": {"type": "string", "description": "Why you change it (posted to the discussion)"},
    },
    required=["task_id"],
)
async def task_update(ctx: ToolContext, args: dict[str, Any]) -> str:
    board_ = ctx.swarm.board
    task_id = args["task_id"]
    changes: dict[str, Any] = {}
    for key in ("title", "goal", "description", "status", "priority", "tags", "depends_on"):
        if args.get(key) is not None:
            changes[key] = args[key]
    for key in ("assignee", "reviewer"):
        if args.get(key) is not None:
            changes[key] = _member_arg(ctx, args[key])
    if args.get("parent_id") is not None:
        changes["parent_id"] = _opt_id(args["parent_id"])
    if changes.get("status") == Status.REVIEW or (changes.get("status") == Status.DONE and not ctx.is_user):
        raise ToolError("Use task_complete to finish a task (it records the result and handles the review).")
    comment = (args.get("comment") or "").strip()
    if not changes and not comment:
        raise ToolError("Nothing to change: pass at least one field (or a comment).")
    if len(comment) > MAX_POST:
        raise ToolError(f"comment is too long ({len(comment)} chars, max {MAX_POST}).")
    old = board_.get(task_id)
    task = board_.update(ctx.actor, task_id, **changes) if changes else old
    post = board_.post(ctx.actor, task.id, comment) if comment else None
    diff = _diff(old, task)
    if diff:
        text = f"Updated task #{task.id}: {'; '.join(diff)}.\n{task_line(task, with_goal=False)}"
    elif post is None:
        text = f"No changes: task #{task.id} already has these values.\n{task_line(task, with_goal=False)}"
    else:
        text = f"No fields changed in task #{task.id}."
    if post is not None:
        text += f"\nPosted comment #{post['id']} in its discussion."
    removed = [d for d in old.depends_on if d not in task.depends_on]
    if removed and ctx.actor not in (USER, old.created_by):  # an assignee dropped what its creator planned
        ids = ", ".join(f"#{d}" for d in removed)
        if old.created_by and ctx.swarm.is_member(old.created_by):
            text += f"\nIts creator ({old.created_by}) was told that you removed {ids}, and the discussion records it."
        else:  # created by the user (who sees the discussion) or by a member who left
            text += f"\nThe discussion records that you removed {ids}."
        if post is None:
            text += " Say there why the task can do without them (task_comment)."
    return text


def _diff(old: Task, new: Task) -> list[str]:
    def show(field: str, value: Any) -> str:
        if field == "priority":
            return PRIORITIES.get(value, str(value))
        if field in ("depends_on", "parent_id"):
            ids = value if isinstance(value, list) else [value] if value else []
            return ", ".join(f"#{i}" for i in ids) or "-"
        if field == "tags":
            return ", ".join(value) or "-"
        return str(value) if value not in (None, "") else "-"

    out = []
    for field in ("status", "assignee", "reviewer", "priority", "tags", "depends_on", "parent_id"):
        before, after = getattr(old, field), getattr(new, field)
        if before != after:
            out.append(f"{field} {show(field, before)} → {show(field, after)}")
    for field in ("title", "goal", "description"):
        if getattr(old, field) != getattr(new, field):
            out.append(f"{field} changed")
    return out


@tool(
    "task_complete",
    en="Finish your task with a concise result: what was done, where it is (file paths), what is left. "
       "If the task has a reviewer it goes to review, otherwise to done.",
    ru="Завершить свою задачу с кратким итогом: что сделано, где лежит (пути к файлам), что осталось. "
       "Если у задачи есть ревьюер, она уйдёт на ревью, иначе — в done.",
    properties={"task_id": _TASK_ID,
                "result": {"type": "string", "description": "What was done, where it is, what is left"}},
    required=["task_id", "result"],
)
async def task_complete(ctx: ToolContext, args: dict[str, Any]) -> str:
    before = ctx.swarm.board.get(args["task_id"])
    task = ctx.swarm.board.complete(ctx.actor, args["task_id"], args["result"])
    if task.status != Status.REVIEW:
        return f"Task #{task.id} is done."
    if task.reviewer == before.reviewer:  # the task's own reviewer
        return f"Task #{task.id} was sent to {task.reviewer} for review."
    if task.reviewer == USER:
        return (f"Task #{task.id} was sent to the user for review: nobody else in the team can review it. You will "
                "be told the verdict; meanwhile take the next task or call wait.")
    return (f"Task #{task.id} was sent to {task.reviewer} for review: a member other than you checks it, and you "
            "will be told the verdict.")


@tool(
    "task_review",
    en="Review a task that is in review (you are its reviewer): approve=true moves it to done, approve=false "
       "returns it to the assignee with your comment.",
    ru="Проверить задачу на ревью (ты её ревьюер): approve=true переводит её в done, approve=false возвращает "
       "исполнителю с твоим комментарием.",
    properties={"task_id": _TASK_ID, "approve": {"type": "boolean"},
                "comment": {"type": "string", "description": "What is good / what must be fixed"}},
    required=["task_id", "approve"],
)
async def task_review(ctx: ToolContext, args: dict[str, Any]) -> str:
    task = ctx.swarm.board.review(ctx.actor, args["task_id"], args["approve"], args.get("comment") or "")
    return f"Task #{task.id} is now {task.status}" + (f" (assignee: {task.assignee})." if task.assignee else ".")


@tool(
    "task_comment",
    en="Post to a task's discussion thread (questions, proposals, findings, review remarks). Use reply_to to "
       "answer a specific post and @Full Name to mention a member. Participants are notified.",
    ru="Написать в обсуждение задачи (вопросы, предложения, находки, замечания ревью). reply_to — ответ на "
       "конкретный пост, @Имя Фамилия — упоминание участника. Участники обсуждения получат уведомление.",
    properties={"task_id": _TASK_ID, "text": {"type": "string"},
                "reply_to": {"type": "integer", "description": "Number of the post you answer"}},
    required=["task_id", "text"],
)
async def task_comment(ctx: ToolContext, args: dict[str, Any]) -> str:
    post = ctx.swarm.board.post(ctx.actor, args["task_id"], args["text"], _opt_id(args.get("reply_to")))
    return f"Posted #{post['id']} in the discussion of task #{post['task_id']}."


@tool(
    "task_discussion",
    en="Read a task's discussion thread (the latest 200 posts, or only those after since_id).",
    ru="Прочитать обсуждение задачи (последние 200 постов или только новее since_id).",
    properties={"task_id": _TASK_ID, "since_id": {"type": "integer", "description": "Show posts after this one"}},
    required=["task_id"],
)
async def task_discussion(ctx: ToolContext, args: dict[str, Any]) -> str:
    task = ctx.swarm.board.get(args["task_id"])
    posts = ctx.swarm.board.discussion(task.id, since_id=args.get("since_id") or 0)
    head = f"Discussion of task #{task.id} \"{task.title}\" ({len(posts)} post(s))"
    if not posts:
        return head + ": empty."
    return head + ":\n" + "\n\n".join(post_line(p) for p in posts)


__all__ = ["UNSET"]
