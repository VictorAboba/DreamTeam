"""Append-only event journal + in-process pub/sub (web UI, triggers, console)."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from typing import Any, Callable

from .db import Database, dumps, loads

log = logging.getLogger(__name__)

Listener = Callable[[dict[str, Any]], None]
# the ids one read_page examines at most (docs/SPEC.md §21.6 "Polling"); read at each call, so a test can lower it
SCAN = 5000


def read_page(conn: sqlite3.Connection, after: int, limit: int, types: tuple[str, ...] | None = None,
              scan: int | None = None, *, recipient: str | None = None,
              decode: bool = True) -> tuple[list[dict[str, Any]], int, bool]:
    """One page of the journal in id order (docs/SPEC.md §21.6 "Polling"): (events, last, more). The newest id (`head`)
    is read first, then the rows with `after < id <= min(head, after + scan)` (`scan`: SCAN when None; the database
    reads that id range by its primary key, so a page never examines more than `scan` rows) that `types` keeps (an
    exact `type IN (...)`, never a LIKE pattern; None keeps every type), at most `limit` of them (at least 1).
    `recipient`: a `notification` counts only when it is addressed to that one (`swarm.notify` journals one per
    recipient). `last` is the last row's id when the page is full, else the upper end of the window, the highest id
    examined, so a page the filter emptied still moves on; `more`: the head lies beyond `last`. An `after` past the head
    gives no events and `last` = the head. Each event is {id, type, ts, actor, data} (data {} when its JSON cannot be
    read: event_data); `decode` False leaves `data` the journaled text, for a caller that decodes it after releasing
    the Database's lock. `conn`: a connection the caller holds (under the Database's lock for a loaded session), its
    own row factory left alone."""
    scan = SCAN if scan is None else scan
    row = conn.execute("SELECT MAX(id) FROM events").fetchone()
    head = int(row[0] or 0) if row is not None else 0
    if after >= head:
        return [], head, False
    upper = min(head, after + max(1, scan))
    sql = "SELECT id, ts, type, actor, data FROM events WHERE id > ? AND id <= ?"
    params: list[Any] = [after, upper]
    if types is not None:
        plain = [t for t in types if recipient is None or t != "notification"]
        clauses = []
        if plain:
            clauses.append(f"type IN ({', '.join('?' * len(plain))})")
            params += plain
        if recipient is not None and "notification" in types:
            # (CASE: json_extract raises on JSON it cannot read; it is evaluated only for a notification that has some)
            clauses.append("CASE WHEN type = 'notification' AND json_valid(data) "
                           "THEN json_extract(data, '$.recipient') END = ?")
            params.append(recipient)
        sql += f" AND ({' OR '.join(clauses)})" if clauses else " AND 0"
    sql += " ORDER BY id LIMIT ?"
    params.append(max(1, limit))
    rows = conn.execute(sql, params).fetchall()
    events = [{"id": r[0], "type": r[2], "ts": r[1], "actor": r[3], "data": event_data(r[4]) if decode else r[4]}
              for r in rows]
    last = int(rows[-1][0]) if len(rows) >= max(1, limit) else upper
    return events, last, head > last


def event_data(value: Any) -> Any:
    """A journaled event's `data` text decoded ({} for none, and for a text that is not JSON: logged at debug level)."""
    try:
        return loads(value, {})
    except ValueError:
        log.debug("an event whose data is not JSON: %r", str(value)[:80])
        return {}


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
