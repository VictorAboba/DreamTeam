"""Compact text formatting of swarm objects for tool results."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from ..models import PRIORITIES, Status, Task


def trim(text: str | None, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def clock(ts: float | None) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


# Swarm.render_wake(stamps=True) (wait_for_events): news older than this gets its own time, and the header a range
STAMP_SPAN = 60.0


def item_time(ts: float, now: float) -> str:
    """The time of one item of a wake-up text: HH:MM, with the date when it is not the date of `now`."""
    at = datetime.fromtimestamp(ts)
    return at.strftime("%H:%M" if at.date() == datetime.fromtimestamp(now).date() else "%Y-%m-%d %H:%M")


def stamp_range(first: float | None, now: float) -> str:
    """The time of a wake-up header: `now`, or the range from `first` to `now` ("2026-10-11 10:50–12:50")."""
    if first is None:
        return datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M")
    return f"{datetime.fromtimestamp(first):%Y-%m-%d %H:%M}–{item_time(now, first)}"


def ago(ts: float | None) -> str:
    if not ts:
        return "never"
    delta = max(0, time.time() - ts)
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def budget_line(spent: float, limit: float) -> str:
    if limit > 0:
        return f"Spent ${spent:.2f} of ${limit:.2f}."
    return f"Spent ${spent:.2f} (no budget limit)."


def goal_line(goal: dict[str, Any] | None) -> str:
    if not goal:
        return "Global goal: not set."
    line = f"Global goal [{goal.get('status', 'active')}]: {goal.get('title', '')}"
    if goal.get("description"):
        line += f"\n  {trim(goal['description'], 600)}"
    if goal.get("summary"):
        line += f"\n  Summary: {trim(goal['summary'], 600)}"
    return line


def task_line(task: Task, with_goal: bool = True, indent: str = "") -> str:
    """The task's short line; `indent` goes before it (a subtask under its parent: "  ↳ "), and the goal line is
    indented by as much more."""
    line = indent + task.short()
    if task.comments:
        line += f" 💬{task.comments}"
    if with_goal and task.goal:
        line += f"\n{' ' * len(indent)}    goal: {trim(task.goal, 160)}"
    return line


# the free directions named in a crew task's card (docs/SPEC.md §4.2 "Texts for agents"); its Subtasks: lines list all
CREW_FREE_LISTED = 20


def subtask_line(task: Task) -> str:
    """One subtask in its parent's card: #15 [done] "title" @Anna Smith — <the result's first line>."""
    line = f'#{task.id} [{task.status}] "{trim(task.title, 80)}"'
    if task.assignee:
        line += f" @{task.assignee}"
    first = next((part for part in (task.result or "").splitlines() if part.strip()), "")
    if first:
        line += f" — {trim(first, 120)}"
    return line


def crew_line(task: Task, subtasks: list[Task]) -> str:
    """The crew of a crew task: `Crew task: up to 3 members at once, the owner included; 2 working now (owner X #16;
    Anna Smith #15). Free directions: #17 "oidc".` (`subtasks`: its directions; `task.crew_working` filled)."""
    held: dict[str, list[int]] = {}
    for sub in subtasks:
        if sub.assignee and sub.status in (Status.IN_PROGRESS, Status.WAITING):
            held.setdefault(sub.assignee, []).append(sub.id)

    def who(name: str) -> str:
        label = f"owner {name}" if name == task.assignee else name
        ids = held.get(name, [])
        return label + (" " + ", ".join(f"#{i}" for i in ids) if ids else "")

    working = list(task.crew_working)
    line = f"Crew task: up to {task.crew} members at once, the owner included; {len(working)} working now"
    if working:
        line += f" ({'; '.join(who(name) for name in working)})"
    takeable = task.status in (Status.TODO, Status.IN_PROGRESS, Status.WAITING) and not task.blocked_by
    free = [s for s in subtasks if takeable and s.status == Status.TODO and s.assignee is None]
    names = [f'#{s.id} "{trim(s.title, 80)}"' for s in free[:CREW_FREE_LISTED]]
    if len(free) > CREW_FREE_LISTED:
        names.append("…")
    return line + ". Free directions: " + (", ".join(names) or "none") + "."


def task_detail(task: Task) -> str:
    rows = [
        f"Task #{task.id}: {task.title}",
        f"Status: {task.status}" + (f" | blocked by {', '.join('#' + str(b) for b in task.blocked_by)}" if task.blocked_by else ""),
        f"Goal: {task.goal or '(not set)'}",
        f"Assignee: {task.assignee or '-'} | Reviewer: {task.reviewer or '-'} | Created by: {task.created_by or '-'}",
        f"Priority: {PRIORITIES.get(task.priority, task.priority)} | Tags: {', '.join(task.tags) or '-'}",
    ]
    if task.parent_id:
        rows.append(f"Parent: #{task.parent_id}")
    if task.depends_on:
        rows.append("Depends on: " + ", ".join(f"#{d}" for d in task.depends_on))
    rows.append(f"Created: {clock(task.created_at)} | Updated: {clock(task.updated_at)}")
    if task.description:
        rows.append("Description:\n" + task.description)
    if task.result:
        rows.append("Result:\n" + task.result)
    rows.append(f"Discussion: {task.comments} post(s)")
    return "\n".join(rows)


def post_line(post: dict[str, Any], limit: int = 4000) -> str:
    reply = f" (reply to #{post['reply_to']})" if post.get("reply_to") else ""
    text = post.get("text") or ""
    if len(text) > limit:
        text = text[:limit] + "…"
    return f"#{post['id']} {post['author']} at {clock(post.get('ts'))}{reply}:\n{text}"


def message_line(msg: dict[str, Any], me: str | None = None, limit: int = 4000) -> str:
    to = ", ".join("you" if r == me else r for r in msg.get("recipients", []))
    reply = f" (reply to #{msg['reply_to']})" if msg.get("reply_to") else ""
    text = msg.get("text") or ""
    if len(text) > limit:
        text = text[:limit] + "…"
    return f"#{msg['id']} {clock(msg.get('ts'))} {msg['sender']} → {to}{reply}:\n{text}"
