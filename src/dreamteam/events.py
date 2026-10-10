"""Append-only event journal + in-process pub/sub (web UI, triggers, console)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from .db import Database, dumps, loads

log = logging.getLogger(__name__)

Listener = Callable[[dict[str, Any]], None]


class EventBus:
    def __init__(self, db: Database):
        self.db = db
        self._listeners: list[Listener] = []
        self._queues: set[asyncio.Queue] = set()

    def emit(self, type: str, actor: str | None = None, **data: Any) -> dict[str, Any]:
        ts = time.time()
        event_id = self.db.insert(
            "INSERT INTO events(ts, type, actor, data) VALUES (?, ?, ?, ?)", (ts, type, actor, dumps(data))
        )
        event = {"id": event_id, "ts": ts, "type": type, "actor": actor, "data": data}
        self._dispatch(event)
        return event

    def publish(self, type: str, actor: str | None = None, **data: Any) -> dict[str, Any]:
        """Deliver a live-only event (not journaled, id None) to listeners and subscribers, e.g. trace
        progress for the web UI; durable state lives elsewhere."""
        event = {"id": None, "ts": time.time(), "type": type, "actor": actor, "data": data}
        self._dispatch(event)
        return event

    def _dispatch(self, event: dict[str, Any]) -> None:
        type = event["type"]
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:  # a broken listener must not break the swarm
                log.exception("event listener failed for %s", type)
        for queue in list(self._queues):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def listen(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def subscribe(self, maxsize: int = 2000) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._queues.discard(queue)

    def recent(self, since: int = 0, limit: int = 200, types: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM events WHERE id > ?"
        params: list[Any] = [since]
        if types:
            sql += " AND (" + " OR ".join("type LIKE ?" for _ in types) + ")"
            params += [t.replace("*", "%") for t in types]
        if since:
            sql += " ORDER BY id ASC LIMIT ?"
            rows = self.db.all(sql, params + [limit])
        else:
            sql += " ORDER BY id DESC LIMIT ?"
            rows = list(reversed(self.db.all(sql, params + [limit])))
        return [
            {"id": r["id"], "ts": r["ts"], "type": r["type"], "actor": r["actor"], "data": loads(r["data"], {})}
            for r in rows
        ]
