"""Compact text formatting of swarm objects for tool results."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from ..models import PRIORITIES, Task


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


def task_line(task: Task, with_goal: bool = True) -> str:
    line = task.short()
    if task.comments:
        line += f" 💬{task.comments}"
    if with_goal and task.goal:
        line += f"\n    goal: {trim(task.goal, 160)}"
    return line


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
