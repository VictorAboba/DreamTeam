"""Helpers of the crew task tests (docs/SPEC.md §4.2): build crew tasks and set task rows directly, without the
board's write rules, and read what members were told. The signatures stay fixed; later tests only add functions."""

from __future__ import annotations

import time
from typing import Any

from dreamteam.board import Board
from dreamteam.models import Task


def set_raw(board: Board, tid: int, **values: Any) -> Task:
    """Write columns of a task row as they are (no checks, no events, no notifications); the task, filled."""
    cols = {**values, "updated_at": time.time()}
    board.db.execute("UPDATE tasks SET " + ", ".join(f"{c} = ?" for c in cols) + " WHERE id = ?",
                     [*cols.values(), tid])
    return board.get(tid)


def make_crew_raw(board: Board, actor: str, crew: int, titles: list[str], **kw: Any) -> tuple[Task, list[Task]]:
    """A parent created by `actor` (`kw` go to its create) with one subtask per title, created by `actor` with
    parent_id, then `crew` written into the parent's row: (the crew task, its subtasks in creation order)."""
    parent = board.create(actor, kw.pop("title", "crew task"), **kw)
    subs = [board.create(actor, title, parent_id=parent.id) for title in titles]
    set_raw(board, parent.id, crew=crew)
    return board.get(parent.id), [board.get(s.id) for s in subs]


def finish_raw(board: Board, tid: int, status: str, result: str | None = None, **values: Any) -> Task:
    """Put a task straight into `status` with `result` (other columns, e.g. assignee, in `values`)."""
    return set_raw(board, tid, status=status, result=result, **values)


def notes_of(swarm: Any, name: str, key: str) -> list[dict[str, Any]]:
    """The placeholders of each `key` notification `name` got, oldest first."""
    return [kw for k, kw in swarm.notifications_for(name) if k == key]


def keys_for(swarm: Any, name: str) -> list[str]:
    """The keys of the notifications `name` got, oldest first."""
    return [k for k, _ in swarm.notifications_for(name)]
