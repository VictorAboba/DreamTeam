"""Shared test fixtures. `FakeSwarm` implements the subset of the Swarm API (docs/SPEC.md §3) that
leaf modules (board, mailbox, workspace, triggers, forge, tools) depend on, backed by a real
SQLite database and EventBus in a temporary project directory."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from dreamteam import prompts
from dreamteam.board import Board
from dreamteam.config import Config
from dreamteam.db import Database, dumps, loads
from dreamteam.events import EventBus
from dreamteam.goal_text import split_goal
from dreamteam.models import USER, AgentKind, AgentStatus, ToolError
from dreamteam.swarm import QUIET_KEY, resolve_member_in


class FakeSwarm:
    def __init__(self, project_dir: Path, raw_config: dict[str, Any] | None = None):
        project_dir.mkdir(parents=True, exist_ok=True)
        self.config = Config.from_dict(project_dir, raw_config or {})
        self.config.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(self.config.data_dir / "swarm.db")
        self.bus = EventBus(self.db)
        self.language = self.config.swarm.language
        self.notified: list[tuple[str, str, dict[str, Any]]] = []
        self.signals: list[str] = []
        self._events: dict[str, asyncio.Event] = {}
        self.board = None
        self.mail = None
        self.workspace = None
        self.triggers = None
        self.forge = None
        self.runtimes: dict[str, Any] = {}
        self.paused = False
        self.pause_reason = ""

    # --- members -------------------------------------------------------------------------
    def add_member(self, name: str, kind: str = AgentKind.LLM, role: str = "Generalist",
                   language: str | None = None, status: str = AgentStatus.IDLE) -> str:
        self.db.execute(
            "INSERT INTO agents(name, kind, role, model, language, status, hired_by, created_at, last_active)"
            " VALUES (?, ?, ?, 'default', ?, ?, ?, ?, ?)",
            (name, kind, role, language or self.language, status, USER, time.time(), time.time()),
        )
        return name

    def member(self, name: str) -> dict | None:
        row = self.db.one("SELECT * FROM agents WHERE name = ?", (name,))
        return dict(row) if row else None

    def members(self, include_retired: bool = False) -> list[dict]:
        sql = "SELECT * FROM agents" + ("" if include_retired else " WHERE status != 'retired'")
        return [dict(r) for r in self.db.all(sql + " ORDER BY created_at, name")]

    def is_member(self, name: str) -> bool:
        m = self.member(name)
        return bool(m) and m["status"] != AgentStatus.RETIRED

    def kind_of(self, name: str) -> str | None:
        if name == USER:
            return "user"
        m = self.member(name)
        return m["kind"] if m else None

    def resolve_member(self, name: str, allow_user: bool = True, allow_retired: bool = False) -> str:
        # the real resolver: leaf modules must agree with how production resolves names
        return resolve_member_in(lambda: self.members(include_retired=allow_retired), name, allow_user)

    def language_of(self, name: str) -> str:
        m = self.member(name) if name != USER else None
        return (m and m.get("language")) or self.language

    # --- notifications -----------------------------------------------------------------------
    def notify(self, recipient: str, key: str, *, wake: bool = True, **kw: Any) -> None:
        text = prompts.t(self.language_of(recipient), key, **kw)
        self.notified.append((recipient, key, kw))
        data = kw if wake or recipient == USER else {**kw, QUIET_KEY: True}  # like Swarm.notify
        self.db.insert(
            "INSERT INTO notifications(ts, recipient, kind, text, data) VALUES (?, ?, ?, ?, ?)",
            (time.time(), recipient, key, text, dumps(data)),
        )
        self.bus.emit("notification", None, recipient=recipient, kind=key, text=text)
        if wake:
            self.signal(recipient)

    def notifications_for(self, recipient: str) -> list[tuple[str, dict]]:
        return [(k, kw) for r, k, kw in self.notified if r == recipient]

    def signal(self, name: str) -> None:
        self.signals.append(name)
        self._events.setdefault(name, asyncio.Event()).set()

    async def wait_signal(self, name: str, timeout: float) -> bool:
        ev = self._events.setdefault(name, asyncio.Event())
        try:
            await asyncio.wait_for(ev.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            ev.clear()

    def has_pending(self, name: str) -> bool:
        unread = self.db.scalar("SELECT COUNT(*) FROM inbox WHERE recipient = ? AND read_at IS NULL", (name,))
        notes = self.db.scalar(
            "SELECT COUNT(*) FROM notifications WHERE recipient = ? AND delivered_at IS NULL"
            f" AND json_extract(data, '$.{QUIET_KEY}') IS NULL", (name,))
        return bool(unread or notes)

    # --- goal ----------------------------------------------------------------------------------
    def goal(self) -> dict | None:
        return self.db.get("goal")

    def set_goal(self, actor: str, title: str, description: str = "") -> dict:
        # validates, splits and plans like Swarm.set_goal (no agents are kicked off: there are no runtimes)
        title, description = split_goal(title, description)
        if not title:
            raise ToolError("The goal title is empty.")
        goal = {"title": title, "description": description, "status": "active", "summary": "",
                "set_by": actor, "updated_at": time.time()}
        self.db.set("goal", goal)
        self.bus.emit("goal.set", actor, goal=goal)
        if isinstance(self.board, Board) and not self.board.list():
            p_title, p_goal, p_desc = prompts.PLANNING_TASK[self.language]
            body = f"{p_desc}\n\n{title}" + (f"\n{goal['description']}" if goal["description"] else "")
            self.board.create(actor, p_title, goal=p_goal, description=body, priority=2, tags=["planning"])
        return goal

    def complete_goal(self, actor: str, summary: str) -> dict:
        goal = self.goal()
        if not goal:
            raise ToolError("There is no global goal to complete.")
        if goal.get("status") == "done":
            raise ToolError("The global goal is already marked as achieved.")
        if not (summary or "").strip():
            raise ToolError("Give a summary for the user.")
        goal.update(status="done", summary=summary.strip(), completed_by=actor, updated_at=time.time())
        self.db.set("goal", goal)
        self.bus.emit("goal.completed", actor, goal=goal)
        self.notify(USER, "u_goal_done", by=actor, summary=summary.strip())
        return goal


@pytest.fixture
def fake_swarm(tmp_path: Path) -> FakeSwarm:
    swarm = FakeSwarm(tmp_path / "project")
    yield swarm
    swarm.db.close()


@pytest.fixture
def make_fake_swarm(tmp_path: Path):
    created: list[FakeSwarm] = []

    def factory(raw_config: dict[str, Any] | None = None, subdir: str = "project") -> FakeSwarm:
        swarm = FakeSwarm(tmp_path / subdir, raw_config)
        created.append(swarm)
        return swarm

    yield factory
    for s in created:
        s.db.close()


__all__ = ["FakeSwarm", "loads"]
