"""Requests to the user and automatic answers in the user's place (docs/TUI.md §2, docs/SPEC.md §21.5).

- What `dreamteam -p` sends when a member writes to the user (AUTO_REPLY) or a task waits for the user's review
  (AUTO_REVIEW), which messages and tasks call for one, and which sessions the hub answers for itself (`hub_answers`:
  `-p` sends nothing there).
- The requests to the user (`requests_of` for a loaded swarm, `read_requests` for a session's file, `requests_from`
  for a connection): what only the user can give now, in board-neutral words, and `blocking_requests`, those of them
  that hold the work. The one definition behind SessionAutoReply's sweep, a session summary's `requests`, the v1 state
  `waiting` and the v1 routes.
- `SessionAutoReply(swarm)`: the hub's own automatic answers for a v1 session with `answers: "auto"`.
- `follow_journal`: a session's journal read by id, woken by its bus (SessionAutoReply, stop_when_done).

Stdlib, models, db and prompts only: hub code imports it, and it never imports dreamteam.tui (whose package imports
prompt_toolkit), nor the swarm module (it is given a Swarm)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterable

from .db import loads, reading_file
from .models import API_VIA, USER, AgentStatus, Status, ToolError
from .prompts import CLARIFY_TAG

log = logging.getLogger(__name__)

AUTO_REPLY_MAX = 50      # a safety net against an endless question / auto-answer ping-pong (not approvals)

AUTO_REPLY = {
    "ru": "Пользователь недоступен (неинтерактивный режим): прими решение сам и продолжай работу.",
    "en": "The user is not available (non-interactive mode): decide yourself and continue.",
}
AUTO_REVIEW = {
    "ru": "Принято автоматически: пользователь недоступен (неинтерактивный режим), задачу никто не проверял.",
    "en": "Approved automatically: the user is not available (non-interactive mode); nobody reviewed the task.",
}


def reply_text(lang: str | None) -> str:
    """AUTO_REPLY in `lang`, English for a language it has no text in."""
    return AUTO_REPLY.get(lang or "en", AUTO_REPLY["en"])


def review_text(lang: str | None) -> str:
    """AUTO_REVIEW in `lang`, English for a language it has no text in."""
    return AUTO_REVIEW.get(lang or "en", AUTO_REVIEW["en"])


def wants_reply(msg: dict) -> bool:
    """A journaled message (a `message.sent` event's `message`) that a member sent to the user: it has an id, and the
    user is among its recipients."""
    return (isinstance(msg.get("id"), int) and msg.get("sender") != USER
            and USER in (msg.get("recipients") or []))


def review_key(task: dict) -> tuple[int, Any] | None:
    """A task (a snapshot's or a `task.updated` event's dict) that waits for the user's review: `(id, updated_at)`,
    the key `-p` approves it once under (a task submitted again has a new `updated_at`, so it is approved again).
    None for any other task.

    `-p`'s client-side key only: every write of the row changes `updated_at`, so it cannot name a review round on
    the hub. SessionAutoReply and requests_of key a round on the `task.updated` event that put the task in review
    for the user (the request id `r<task>-<event>`, docs/SPEC.md §21.5)."""
    if task.get("status") != Status.REVIEW or task.get("reviewer") != USER or not isinstance(task.get("id"), int):
        return None
    return task["id"], task.get("updated_at")


def hub_answers(summary: Any) -> bool:
    """Whether the hub itself answers in the user's place for the session of this summary (the hub's GET
    /api/sessions): one created through the HTTP API v1 with `answers: "auto"` (SessionAutoReply, docs/SPEC.md
    §21.5). `dreamteam -p` then sends no automatic replies or approvals of its own, so nothing is answered twice."""
    return isinstance(summary, dict) and summary.get("via") == API_VIA and summary.get("answers") == "auto"


# ---- requests to the user (docs/SPEC.md §21.5) ----------------------------------------------------------------------
REQUEST_KINDS = ("question", "message", "review", "mcp")
# the kinds that hold the work (the v1 state `waiting`, docs/SPEC.md §21.7); a `message` does not: reports to the
# user land there too
BLOCKING_KINDS = frozenset({"question", "review", "mcp"})
_QUESTION = "question"   # the pause reason of a member's question (swarm.QUESTION)
# the newest message from `sender` that reached `recipient` (named, or through "all": the inbox row), sent at `since`
# (a timestamp) or later: (sender, recipient, since). Swarm.last_message_id is its public form.
LAST_TO = ("SELECT MAX(m.id) FROM messages m JOIN inbox i ON i.message_id = m.id "
           "WHERE m.sender = ? AND i.recipient = ? AND m.ts >= ?")
# the question (its message id) ended without an answer: the swarm.resumed event that went on without it (a manual
# resume, clarify switched off, the asker left) or the swarm.paused event of the budget pause it turned into (the
# events_type index; few such events)
QUESTION_DROPPED = ("SELECT 1 FROM events WHERE type IN ('swarm.resumed', 'swarm.paused') "
                    "AND json_extract(data, '$.question.message_id') = ? "
                    "AND json_extract(data, '$.question.outcome') = 'dropped' LIMIT 1")
# `name` has created a task since the planning card with id `task_id`: it plans already (name, task_id)
PLANS = "SELECT 1 FROM tasks WHERE created_by = ? AND id > ? LIMIT 1"
# LAST_TO for every sender to the user (since 0): (sender, newest id) rows, read through the inbox_recipient index;
# (USER,)
_LAST_TO_USER = ("SELECT m.sender, MAX(m.id) FROM messages m JOIN inbox i ON i.message_id = m.id "
                 "WHERE i.recipient = ? GROUP BY m.sender")
# a message of the user reached `name` (named or through "all") after message `last`: (last, USER, name). A rowid range
# read from `last` on, so it costs what was written since, never a scan of the whole inbox (messages has no sender
# index).
_ANSWERED_SINCE = ("SELECT 1 FROM messages m WHERE m.id > ? AND m.sender = ? AND EXISTS (SELECT 1 FROM inbox i "
                   "WHERE i.message_id = m.id AND i.recipient = ?) LIMIT 1")
# the newest journaled task.updated of a task that put it in review for the user (the event's own task is in review
# with the user as its reviewer, and the event changed its status to review or its reviewer to the user): the round
# of a review request (r<task>-<event>), with its time. Walked back from the newest event on the events_type index, so
# it reads only the task.updated events since that round. The same rule as _puts_in_users_review, which reads one
# event. (task id)
_REVIEW_ROUND = ("SELECT id, ts FROM events WHERE type = 'task.updated' AND json_extract(data, '$.task.id') = ? "
                 "AND json_extract(data, '$.task.status') = 'review' AND json_extract(data, '$.task.reviewer') = 'user' "
                 "AND (json_extract(data, '$.changes.status[1]') = 'review' "
                 "OR json_extract(data, '$.changes.reviewer[1]') = 'user') ORDER BY id DESC LIMIT 1")
# a request's id (request_key): its numbers have at most 19 digits, as SQLite's integers (ID_MAX), so a client's id
# of thousands of digits never reaches int() (Python refuses a text of over 4300 digits: a ValueError)
_REQUEST_ID = re.compile(r"(?:(?P<kind>[qm])(?P<mid>[1-9][0-9]{0,18})"
                         r"|r(?P<task>[1-9][0-9]{0,18})-(?P<event>0|[1-9][0-9]{0,18})"
                         r"|s-(?P<name>.+)-(?P<digest>[0-9a-f]{12})?)\Z")
ID_MAX = (1 << 63) - 1   # the largest id SQLite holds (a message's, a task's, an event's)


def planner_question(scalar: Callable[[str, tuple], Any], name: str, task_id: int, since: Any) -> int | None:
    """The question the clarify planner `name` waits for the user to answer, about its clarify planning card (`task_id`,
    created at `since`): the id of `name`'s last message to the user since the card was created, when it is newer than
    the user's last message to `name` (named or through "all"), `name` has created no task since the card (it plans
    already: the ordinary nags apply again), and the user did not go on without an answer (a manual resume, a budget
    pause in its place: n_question_unanswered told the planner so). None otherwise. Both bounds keep an old message (an
    earlier goal) or a note to the user after planning started from counting for ever. `scalar(sql, params)`: the first
    value of a query (Database.scalar). Swarm.asks_user (the nag guard, the snapshot's `clarify_wait`) and the requests
    use it, so they cannot drift apart."""
    if scalar(PLANS, (name, task_id)):
        return None
    asked = scalar(LAST_TO, (name, USER, since))
    if not asked or asked <= (scalar(LAST_TO, (USER, name, since)) or 0):
        return None
    return None if scalar(QUESTION_DROPPED, (asked,)) else int(asked)


def _new_value(change: Any) -> Any:
    """The new value of a task.updated event's change [old, new]."""
    return change[1] if isinstance(change, (list, tuple)) and len(change) == 2 else None


def _puts_in_users_review(task: Any, changes: Any) -> bool:
    """A task.updated event's task and changes put the task in review for the user: the task is in review with the user
    as its reviewer, and the event changed its status to review or its reviewer to the user (a new round, a task the
    reviewing member handed over). _REVIEW_ROUNDS is the same rule in SQL."""
    return (isinstance(task, dict) and isinstance(changes, dict) and isinstance(task.get("id"), int)
            and task.get("status") == Status.REVIEW and task.get("reviewer") == USER
            and (_new_value(changes.get("status")) == Status.REVIEW or _new_value(changes.get("reviewer")) == USER))


def _json(value: Any, default: Any) -> Any:
    """db.loads, with `default` for a value that is no JSON text (a damaged or hand-edited row): one bad row leaves
    out what it holds, never the other requests."""
    try:
        return loads(value, default)
    except (TypeError, ValueError):
        return default


def _message_request(kind: str, mid: int, sender: str, row: Any, **extra: Any) -> dict[str, Any]:
    return {"id": f"{kind[0]}{mid}", "kind": kind, "ts": row["ts"] if row is not None else None, "from": sender,
            "text": (row["text"] or "") if row is not None else "", **extra}


def requests_from(conn: sqlite3.Connection, *, clarify: bool) -> list[dict[str, Any]]:
    """What only the user can give a session now (docs/SPEC.md §21.5), read from its database through `conn` (a loaded
    swarm's, under its Database's lock, or a file's: nothing is written), oldest first:
    - `question` q<message id>: the question that holds a question pause (`pauses_swarm` true), and, with `clarify`
      (the session's [swarm] clarify), the last message to the user of a clarify planner that waits for the user's
      answers (`pauses_swarm` false; planner_question, the snapshot's `clarify_wait`); the full text;
    - `message` m<message id>: the last message of each active member to the user that no message of the user to that
      member followed (one per member), unless it is listed as a question; retired members' never;
    - `review` r<task id>-<event id>: a task in review for the user; the event is the newest task.updated that put it
      there, so a new submission is a new id (0 when the journal has none); `task` {id, title, result};
    - `mcp` s-<name>-<digest[:12]>: an MCP server that waits for the user's approval; `server` {name, description,
      tools}.
    Each has `ts` (when it was asked) and `from` (the member who asks). sqlite3.Error for a database it cannot read; a
    JSON value it cannot read (a damaged row) counts as empty. No query scans a whole table per request: a review's
    round is read back from the newest event, and a member's last message is checked against the messages after it."""
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row   # (never the connection's own: it may be a loaded swarm's)
    try:
        def scalar(sql: str, params: tuple = ()) -> Any:
            row = cur.execute(sql, params).fetchone()
            return None if row is None else row[0]

        def message(mid: int) -> Any:
            return cur.execute("SELECT id, ts, sender, text FROM messages WHERE id = ?", (mid,)).fetchone()

        found: list[dict[str, Any]] = []
        questions: set[int] = set()
        row = cur.execute("SELECT value FROM kv WHERE key = 'paused'").fetchone()
        paused = _json(row["value"], {}) if row is not None else {}
        if (isinstance(paused, dict) and paused.get("paused") and paused.get("reason") == _QUESTION
                and paused.get("asker") and isinstance(paused.get("message_id"), int)):   # Swarm.pending_question
            mid = paused["message_id"]
            questions.add(mid)
            found.append(_message_request("question", mid, paused["asker"], message(mid), pauses_swarm=True))
        if clarify:
            open_marks = ", ".join("?" * len(Status.OPEN))
            cards = cur.execute(f"SELECT id, assignee, tags, created_at FROM tasks WHERE assignee IS NOT NULL "
                                f"AND status IN ({open_marks}) ORDER BY id", tuple(sorted(Status.OPEN))).fetchall()
            for card in cards:
                tags = _json(card["tags"], [])
                if not isinstance(tags, list) or "planning" not in tags or CLARIFY_TAG not in tags:
                    continue
                asked = planner_question(scalar, card["assignee"], card["id"], card["created_at"])
                if asked is not None and asked not in questions:
                    questions.add(asked)
                    found.append(_message_request("question", asked, card["assignee"], message(asked),
                                                  pauses_swarm=False))
        reviews = cur.execute("SELECT id, title, result, assignee, updated_at FROM tasks WHERE status = ? "
                              "AND reviewer = ? ORDER BY id", (Status.REVIEW, USER)).fetchall()
        for task in reviews:
            event = cur.execute(_REVIEW_ROUND, (task["id"],)).fetchone()
            found.append({"id": f"r{task['id']}-{event['id'] if event is not None else 0}", "kind": "review",
                          "ts": event["ts"] if event is not None else task["updated_at"], "from": task["assignee"] or "",
                          "task": {"id": task["id"], "title": task["title"], "result": task["result"]}})
        for server in cur.execute("SELECT * FROM mcp_servers WHERE status = 'pending' ORDER BY name").fetchall():
            digest = (server["digest"] if "digest" in server.keys() else None) or ""   # (none: an older record)
            tools = _json(server["tools"], [])
            # (the tool names, as mcp_forge's public record gives them)
            names = [t.get("name", "") if isinstance(t, dict) else str(t) for t in tools] \
                if isinstance(tools, list) else []
            found.append({"id": f"s-{server['name']}-{str(digest)[:12]}", "kind": "mcp", "ts": server["updated_at"],
                          "from": server["owner"] or "",
                          "server": {"name": server["name"], "description": server["description"] or "",
                                     "tools": names}})
        members = [r["name"] for r in cur.execute("SELECT name FROM agents WHERE status != ? ORDER BY created_at, rowid",
                                                  (AgentStatus.RETIRED,)).fetchall()]
        if members:   # LAST_TO of every member to the user at once (since 0), then per member: did the user answer?
            to_user = {r[0]: r[1] for r in cur.execute(_LAST_TO_USER, (USER,)).fetchall()}
            for name in members:
                last = to_user.get(name)
                if last is None or last in questions or scalar(_ANSWERED_SINCE, (last, USER, name)):
                    continue
                found.append(_message_request("message", int(last), name, message(last)))
    finally:
        cur.close()
    order = {kind: i for i, kind in enumerate(REQUEST_KINDS)}
    found.sort(key=lambda r: (r["ts"] if isinstance(r["ts"], (int, float)) else 0.0, order[r["kind"]], r["id"]))
    return found


def requests_of(swarm: Any) -> list[dict[str, Any]]:
    """The requests to the user of a loaded swarm (requests_from), read under its Database's lock in one go."""
    db = swarm.db
    with db._lock:
        return requests_from(db.conn, clarify=bool(swarm.config.swarm.clarify))


def read_requests(db_path: str | Path, *, clarify: bool = False) -> list[dict[str, Any]]:
    """The requests to the user of a session that is not loaded, read from its database file (db.reading_file: nothing
    is written next to it); [] without a database or when it cannot be read (logged). `clarify`: the session's
    [swarm] clarify."""
    path = Path(db_path)
    if not path.exists():
        return []
    try:
        with reading_file(path, timeout=2.0) as (conn, _immutable):
            return requests_from(conn, clarify=clarify)
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, IndexError) as exc:   # (a damaged file)
        log.debug("cannot read the requests of %s: %s", path, exc)
        return []


def blocking_requests(requests: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The requests that hold the work (BLOCKING_KINDS: a question, a review, an MCP server to approve): while one
    waits, a running session is `waiting` (docs/SPEC.md §21.7)."""
    return [r for r in requests if r.get("kind") in BLOCKING_KINDS]


def request_key(rid: Any) -> tuple[Any, ...] | None:
    """What a request id names: ("question", message id), ("message", message id), ("review", task id, event id) or
    ("mcp", server name, digest[:12] or ""); None for anything else, a number above ID_MAX among them (no request
    has one)."""
    match = _REQUEST_ID.match(rid) if isinstance(rid, str) else None
    if match is None:
        return None
    if match["kind"]:
        key: tuple[Any, ...] = ("question" if match["kind"] == "q" else "message"), int(match["mid"])
    elif match["task"]:
        key = "review", int(match["task"]), int(match["event"])
    else:
        return "mcp", match["name"], match["digest"] or ""
    return key if all(number <= ID_MAX for number in key[1:]) else None


# ---- a session's journal, followed by id ----------------------------------------------------------------------------
JOURNAL_BATCH = 500


def journal_head(swarm: Any) -> int:
    """The id of the newest journaled event of the swarm's session (0 for none)."""
    return int(swarm.db.scalar("SELECT COALESCE(MAX(id), 0) FROM events") or 0)


async def follow_journal(swarm: Any, after: int, types: tuple[str, ...], *,
                         batch: int = JOURNAL_BATCH) -> AsyncIterator[dict[str, Any]]:
    """The swarm's journaled events of `types` with an id above `after`, in id order, for as long as it is iterated:
    read from the journal by id, woken by the bus. The bus queue is a wake-up only and holds one: a full queue drops a
    wake-up while one is pending, never an event (a queue read for its events drops events when it is full). Each pass
    reads the newest id first, then the rows up to it. Close it with contextlib.aclosing; a cancel closes it too."""
    if not types:
        raise ValueError("follow_journal needs at least one event type")
    queue = swarm.bus.subscribe(maxsize=1)   # before the first read: nothing written after it goes unseen
    marks = ", ".join("?" * len(types))
    sql = (f"SELECT id, ts, type, actor, data FROM events WHERE id > ? AND id <= ? AND type IN ({marks}) "
           "ORDER BY id LIMIT ?")
    try:
        cursor = after
        while True:
            head = journal_head(swarm)
            rows = swarm.db.all(sql, (cursor, head, *types, batch))
            for row in rows:
                yield {"id": row["id"], "ts": row["ts"], "type": row["type"], "actor": row["actor"],
                       "data": loads(row["data"], {})}
            if len(rows) >= batch:
                cursor = rows[-1]["id"]
                continue
            cursor = max(cursor, head)
            await queue.get()
    finally:
        swarm.bus.unsubscribe(queue)


# ---- the hub's automatic answers (answers: "auto") --------------------------------------------------------------------
_FOLLOWED = ("message.sent", "task.updated", "swarm.paused")


class SessionAutoReply:
    """The hub answers in the user's place for one run of a v1 session with `answers: "auto"` (docs/SPEC.md §21.5), as
    `dreamteam -p` with automatic replies does as a client:
    - every message of a member to the user gets AUTO_REPLY, at most `limit` (AUTO_REPLY_MAX) per run; past it the
      messages stay requests of kind `message`, and the user is told once (notification `u_auto_reply_limit`);
    - a task waiting for the user's review is approved with AUTO_REVIEW once per round (keyed on the task.updated event
      that put it there), past the limit too;
    - a question that holds a question pause is answered, past the limit too;
    - an MCP server that waits for approval is never answered: it stays a request.
    start() sweeps what already waits (requests_from: the same definitions as the requests), then a task follows the
    journal by id from where the run began (follow_journal: `message.sent`, `task.updated`, `swarm.paused` {question};
    a message the sweep answered is not answered again, one it left out as not its member's last is); it never acts
    inside a bus listener, which would re-enter `emit`. The actions run in process as the user (Mailbox.send,
    Board.review) in the swarm's language; a ToolError is logged and skipped. It is restart-safe: a member's message
    that a later message of the user to that member followed (another than this run's own answers: the board's user, a
    `-p` client, the previous run) is skipped, since the sets of a run are lost at a restart. SessionManager starts it
    after the swarm started (start(after=the head before `session.started`)) and stops it before the swarm stops."""

    def __init__(self, swarm: Any, *, limit: int = AUTO_REPLY_MAX) -> None:
        self.swarm = swarm
        self.limit = limit
        self.replies = 0                                # the messages this run answered (against `limit`)
        self._answered: set[int] = set()                # the members' message ids this run answered or gave up on
        self._own: set[int] = set()                     # the ids of this run's own answers
        self._approved: set[tuple[int, int]] = set()    # the review rounds (task id, event id) handled
        self._limit_told = False
        self._task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, after: int | None = None) -> None:
        """Answer what already waits, then follow the journal in a task (on the running event loop) from `after` (the
        head before the run started: SessionManager reads it before `session.started`, so each message a member sent
        while `swarm.start()` ran is answered, not only its last one, which the sweep answers; the dedupe makes the
        replay safe), else from the head now."""
        if self._task is not None:
            return
        if after is None:
            after = journal_head(self.swarm)   # the sweep answers what waits up to here, the task what comes after
        self.sweep()
        self._task = asyncio.create_task(self._follow(after), name=f"auto-reply-{self.swarm.session_id or 'swarm'}")

    async def stop(self) -> None:
        """Cancel the task and wait for it to end (it ends at its next wait: no action is cut in two)."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        await asyncio.wait({task})
        if not task.cancelled() and task.exception() is not None:
            log.error("the automatic answers of session %s ended with an error", self.swarm.session_id,
                      exc_info=task.exception())

    def sweep(self) -> None:
        """Answer the requests that wait now (requests_of), except MCP servers."""
        try:
            requests = requests_of(self.swarm)
        except Exception:
            log.exception("could not read the requests of session %s", self.swarm.session_id)
            return
        for request in requests:
            try:
                key = request_key(request["id"])
                if key is None:
                    continue
                if key[0] == "question" and request.get("pauses_swarm"):
                    self._question(request["from"], key[1])
                elif key[0] in ("question", "message"):
                    self._message(request["from"], key[1])
                elif key[0] == "review":
                    self._review(key[1], key[2])
            except Exception:
                log.exception("could not answer request %s of session %s", request.get("id"), self.swarm.session_id)

    async def _follow(self, after: int) -> None:
        async with contextlib.aclosing(follow_journal(self.swarm, after, _FOLLOWED)) as events:
            async for event in events:
                try:
                    self.on_event(event)
                except Exception:
                    log.exception("could not answer event %s of session %s", event.get("id"), self.swarm.session_id)

    def on_event(self, event: dict[str, Any]) -> None:
        """Act on one journaled event (follow_journal's shape)."""
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        etype = event.get("type")
        if etype == "message.sent":
            msg = data.get("message")
            if isinstance(msg, dict) and wants_reply(msg):
                self._message(msg["sender"], msg["id"])
        elif etype == "task.updated":
            task = data.get("task")
            if _puts_in_users_review(task, data.get("changes")) and isinstance(event.get("id"), int):
                self._review(task["id"], event["id"])
        elif etype == "swarm.paused" and data.get("reason") == _QUESTION:
            self._question(data.get("asker"), data.get("message_id"))

    # -- the actions ------------------------------------------------------------------------------------------------
    def _message(self, sender: Any, mid: int) -> None:
        if not isinstance(sender, str) or mid in self._answered:
            return
        if self._answered_later(sender, mid):   # (restart-safe: the user, or an earlier run, answered it already)
            self._answered.add(mid)
            return
        if self.replies >= self.limit:
            self._tell_limit()
            return
        if self._reply(sender, mid):
            self.replies += 1

    def _question(self, asker: Any, mid: Any) -> None:
        """The question pause of `asker`'s message `mid`, answered while it still holds (past the limit too): its answer
        may have been this run's reply to the message itself, which ends the pause (Mailbox.send)."""
        if not isinstance(asker, str) or not isinstance(mid, int):
            return
        pending = self.swarm.pending_question()
        if pending is None or pending.get("message_id") != mid or pending.get("asker") != asker:
            return
        self._reply(asker, mid)

    def _review(self, task_id: int, event_id: int) -> None:
        key = (task_id, event_id)
        if key in self._approved:
            return
        self._approved.add(key)
        try:
            task = self.swarm.board.get(task_id)
            if task.status != Status.REVIEW or task.reviewer != USER:   # decided meanwhile, or handed to a member
                return
            self.swarm.board.review(USER, task_id, True, review_text(self.swarm.language))
        except ToolError as exc:
            log.warning("session %s: automatic approval of task #%s failed: %s", self.swarm.session_id, task_id, exc)

    def _reply(self, to: str, mid: int) -> bool:
        """AUTO_REPLY to `to` in reply to message `mid`; False when the mailbox refused it (a member who left), which is
        not tried again."""
        self._answered.add(mid)
        try:
            sent = self.swarm.mail.send(USER, [to], reply_text(self.swarm.language), reply_to=mid)
        except ToolError as exc:
            log.warning("session %s: automatic answer to message #%s of %s failed: %s", self.swarm.session_id, mid, to,
                        exc)
            return False
        self._own.add(sent["id"])
        return True

    def _answered_later(self, sender: str, mid: int) -> bool:
        """A message of the user reached `sender` after message `mid` (named or through "all"), other than this run's
        own answers."""
        rows = self.swarm.db.all("SELECT m.id FROM messages m JOIN inbox i ON i.message_id = m.id "
                                 "WHERE m.sender = ? AND i.recipient = ? AND m.id > ?", (USER, sender, mid))
        return any(row["id"] not in self._own for row in rows)

    def _tell_limit(self) -> None:
        if self._limit_told:
            return
        self._limit_told = True
        self.swarm.notify(USER, "u_auto_reply_limit", wake=False, max=self.limit)


__all__ = ["AUTO_REPLY", "AUTO_REVIEW", "AUTO_REPLY_MAX", "BLOCKING_KINDS", "ID_MAX", "JOURNAL_BATCH", "LAST_TO",
           "PLANS", "QUESTION_DROPPED", "REQUEST_KINDS", "SessionAutoReply", "blocking_requests", "follow_journal",
           "hub_answers", "journal_head", "planner_question", "read_requests", "reply_text", "request_key",
           "requests_from", "requests_of", "review_key", "review_text", "wants_reply"]
