"""The shared fixture of the board's stacks of subtasks (docs/SPEC.md §18.4 "Subtasks", §4.2 "Surfaces"): which
tasks are cards and which are rows of whose stack, at which level. The web board (app.js `boardCards`, tests/test_ui.py)
is pinned by it; the TUI's board list shows a subtask as its own line only where the web shows it as its own card, so
its test takes the same fixture."""

from __future__ import annotations

from typing import Any


def _task(tid: int, status: str, parent_id: int | None = None, **extra: Any) -> dict[str, Any]:
    task = {"id": tid, "title": f"Task {tid}", "goal": "", "description": "", "status": status, "assignee": None,
            "reviewer": None, "parent_id": parent_id, "priority": 1, "tags": [], "depends_on": [], "blocked_by": [],
            "comments": 0, "crew": 0, "crew_working": [], "created_at": 0.0, "updated_at": float(tid)}
    task.update(extra)
    return task


# plain #1; parent #2 (todo) with #3 done, #4 in_progress (#6 under it), #5 todo; a done parent #7 with an open #8
# (its own card: open work never hides in «done») and a done #9; a parent_id loop #10 <-> #11 (no root: a card from
# its lowest id); #12 under a parent missing from the snapshot (a card whatever its status)
STACK_TASKS: list[dict[str, Any]] = [
    _task(1, "todo"),
    _task(2, "todo"),
    _task(3, "done", 2),
    _task(4, "in_progress", 2),
    _task(5, "todo", 2),
    _task(6, "todo", 4),
    _task(7, "done"),
    _task(8, "in_progress", 7),
    _task(9, "done", 7),
    _task(10, "todo", 11),
    _task(11, "todo", 10),
    _task(12, "done", 99),
]

# card id -> its stack: [(row id, level)], depth-first by id; level 0 = a direct subtask of the card
EXPECTED_CARDS: dict[int, list[tuple[int, int]]] = {
    1: [],
    2: [(3, 0), (4, 0), (6, 1), (5, 0)],
    7: [(9, 0)],
    8: [],
    10: [(11, 0)],
    12: [],
}
