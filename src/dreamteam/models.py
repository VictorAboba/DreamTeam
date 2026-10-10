"""Core domain constants and records shared by all modules."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any

from .db import loads

USER = "user"
ALL = "all"


class ToolError(Exception):
    """An error reported back to the caller (agent, user or MCP client) as a tool result."""


class Status:
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    WAITING = "waiting"
    REVIEW = "review"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    ALL = ("todo", "in_progress", "waiting", "review", "done", "failed", "cancelled")
    TERMINAL = frozenset({"done", "failed", "cancelled"})
    OPEN = frozenset({"todo", "in_progress", "waiting", "review"})


PRIORITIES = {0: "low", 1: "normal", 2: "high", 3: "critical"}


class AgentKind:
    LLM = "llm"  # runs inside the swarm on an LLM (ReAct loop)
    EXTERNAL = "external"  # connected through MCP (e.g. Claude Code)


class AgentStatus:
    IDLE = "idle"
    WORKING = "working"
    PAUSED = "paused"
    ERROR = "error"
    RETIRED = "retired"


@dataclass
class Task:
    id: int
    title: str
    goal: str
    description: str
    status: str
    assignee: str | None
    reviewer: str | None
    created_by: str | None
    parent_id: int | None
    depends_on: list[int]
    priority: int
    tags: list[str]
    result: str | None
    created_at: float
    updated_at: float
    blocked_by: list[int] = field(default_factory=list)
    comments: int = 0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Task":
        return cls(
            id=row["id"],
            title=row["title"],
            goal=row["goal"] or "",
            description=row["description"] or "",
            status=row["status"],
            assignee=row["assignee"],
            reviewer=row["reviewer"],
            created_by=row["created_by"],
            parent_id=row["parent_id"],
            depends_on=loads(row["depends_on"], []),
            priority=row["priority"] if row["priority"] is not None else 1,
            tags=loads(row["tags"], []),
            result=row["result"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def short(self) -> str:
        bits = [f"#{self.id} [{self.status}] {self.title}"]
        if self.assignee:
            bits.append(f"@{self.assignee}")
        if self.priority != 1:
            bits.append(f"({PRIORITIES.get(self.priority, self.priority)})")
        if self.tags:
            bits.append("tags:" + ",".join(self.tags))
        if self.blocked_by:
            bits.append("blocked by " + ", ".join(f"#{d}" for d in self.blocked_by))
        if self.parent_id:
            bits.append(f"parent #{self.parent_id}")
        return " ".join(bits)
