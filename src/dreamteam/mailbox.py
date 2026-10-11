"""Direct messages between members and the user (SPEC §5)."""

from __future__ import annotations

import contextlib
import contextvars
import logging
import time
from typing import Any, Iterable, Iterator

from .db import Database, dumps, loads
from .models import ALL, USER, ToolError

log = logging.getLogger(__name__)

MAX_TEXT = 50000
# (sender, members): a message `sender` sends in the current context is no news for `members`: the MCP client that
# sends it acts as them too (it may send as the user and listen as its member; hub_mcp.hub_call sets this). They get
# it as already read. Tied to the sender: a task started inside the block inherits the context (e.g. the runtime of
# an agent the client hires), and the messages of others must still reach those members as news.
_SEEN_BY: contextvars.ContextVar[tuple[str, frozenset[str]] | None] = contextvars.ContextVar("dreamteam_seen_by",
                                                                                            default=None)


@contextlib.contextmanager
def sent_as_seen_by(members: Iterable[str], sender: str) -> Iterator[None]:
    """Messages `sender` sends inside this block reach `members` as already read: they are delivered (their history
    shows them) but neither wake those members nor come back to them as news."""
    token = _SEEN_BY.set((sender, frozenset(members)))
    try:
        yield
    finally:
        _SEEN_BY.reset(token)


def _message_dict(row: Any) -> dict[str, Any]:
    return {"id": row["id"], "ts": row["ts"], "sender": row["sender"],
            "recipients": loads(row["recipients"], []), "text": row["text"], "reply_to": row["reply_to"]}


def _as_id(value: Any, what: str) -> int:
    if isinstance(value, bool):
        raise ToolError(f"{what} must be a message id (integer), got {value!r}.")
    try:
        return int(str(value).strip().lstrip("#")) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        raise ToolError(f"{what} must be a message id (integer), got {value!r}.") from None


def _as_limit(value: Any) -> int:
    if not isinstance(value, bool):
        try:
            return max(1, int(str(value).strip()) if isinstance(value, str) else int(value))
        except (TypeError, ValueError):
            pass
    raise ToolError(f"limit must be a positive integer, got {value!r}.")


_TO_ME = "EXISTS (SELECT 1 FROM inbox i WHERE i.message_id = m.id AND i.recipient = ?)"


class Mailbox:
    def __init__(self, swarm: Any) -> None:
        self.swarm = swarm

    @property
    def db(self) -> Database:
        return self.swarm.db

    # --- sending ---------------------------------------------------------------------------------------

    def send(self, sender: str, to: str | list[str], text: str, reply_to: int | None = None) -> dict:
        body = str(text if text is not None else "").strip()
        if not body:
            raise ToolError("Message text must not be empty.")
        if len(body) > MAX_TEXT:
            raise ToolError(f"Message is too long ({len(body)} chars, max {MAX_TEXT}). Put long content into a "
                            "workspace file and send its path.")
        recipients, broadcast = self._recipients(sender, to)
        reply = self._check_reply(reply_to)
        context = _SEEN_BY.get()  # the sender's own other identities (sent_as_seen_by)
        seen = context[1] if context is not None and context[0] == sender else frozenset()
        with self.db.transaction():
            ts = time.time()
            message_id = self.db.insert(
                "INSERT INTO messages(ts, sender, recipients, text, reply_to) VALUES (?, ?, ?, ?, ?)",
                (ts, sender, dumps(recipients), body, reply))
            for name in recipients:
                self.db.execute("INSERT INTO inbox(message_id, recipient, read_at) VALUES (?, ?, ?)",
                                (message_id, name, ts if name in seen else None))
        message = {"id": message_id, "ts": ts, "sender": sender, "recipients": recipients, "text": body,
                   "reply_to": reply}
        log.info("message #%s from %s to %s", message_id, sender, ", ".join(recipients))
        self.swarm.bus.emit("message.sent", sender, message=message)
        # A member's broadcast does not wake an agent asleep while it waits for its own job: that job's end wakes it
        # and shows the message then (a wake-up only to read "all" news would re-read its whole context). Personal
        # messages and the user's messages wake as always.
        waits_for_job = getattr(self.swarm, "waits_for_job", None) if sender != USER else None
        for name in recipients:
            if name in seen:
                continue
            if name in broadcast and waits_for_job is not None and waits_for_job(name):
                self.swarm.defer_wake(name)
                continue
            self.swarm.signal(name)
        return message

    def _recipients(self, sender: str, to: str | list[str] | None) -> tuple[list[str], set[str]]:
        """The recipients in order, and those of them reached only through "all" (not named)."""
        items = self._split(to)
        if not items:
            raise ToolError('No recipients. Use a member name, a list of names, "all" or "user".')
        resolved: list[str] = []
        named: set[str] = set()
        named_self = False
        for item in items:
            if item.lower() == ALL:
                resolved += [m["name"] for m in self.swarm.members()]
            else:
                name = self.swarm.resolve_member(item, allow_user=True)
                named_self = named_self or name == sender
                resolved.append(name)
                named.add(name)
        recipients: list[str] = []
        for name in resolved:
            if name != sender and name not in recipients:
                recipients.append(name)
        if not recipients:
            if named_self:
                raise ToolError("You cannot send a message only to yourself. Use `update_notes` to keep notes.")
            raise ToolError('No recipients: there are no other active members. Message "user" or hire someone.')
        return recipients, set(recipients) - named

    @staticmethod
    def _split(to: str | list[str] | None) -> list[str]:
        if to is None:
            return []
        if isinstance(to, str):
            raw = to.split(",")
        elif isinstance(to, (list, tuple, set, frozenset)):
            raw = [str(x) for x in to if x is not None]
        else:
            raise ToolError(f'Invalid recipients {to!r}: use a member name, a list of names, "all" or "user".')
        return [" ".join(x.split()) for x in raw if x and x.strip()]

    def _check_reply(self, reply_to: Any) -> int | None:
        if reply_to is None or (isinstance(reply_to, str) and not reply_to.strip()):
            return None
        rid = _as_id(reply_to, "reply_to")
        if self.db.one("SELECT id FROM messages WHERE id = ?", (rid,)) is None:
            raise ToolError(f"Message #{rid} does not exist; reply_to must be the id of an existing message.")
        return rid

    # --- reading ---------------------------------------------------------------------------------------

    def get(self, message_id: int) -> dict:
        mid = _as_id(message_id, "message_id")
        row = self.db.one("SELECT * FROM messages WHERE id = ?", (mid,))
        if row is None:
            raise ToolError(f"Message #{mid} not found.")
        return _message_dict(row)

    def unread(self, name: str, mark_read: bool = True, limit: int = 100) -> list[dict]:
        """Oldest unread messages first; the returned ones are marked read unless mark_read=False."""
        with self.db.transaction():
            rows = self.db.all(
                "SELECT m.* FROM messages m JOIN inbox i ON i.message_id = m.id"
                " WHERE i.recipient = ? AND i.read_at IS NULL ORDER BY m.id ASC LIMIT ?",
                (name, _as_limit(limit)))
            if mark_read and rows:
                ids = [r["id"] for r in rows]
                self.db.execute(
                    f"UPDATE inbox SET read_at = ? WHERE recipient = ? AND message_id IN ({', '.join('?' * len(ids))})",
                    (time.time(), name, *ids))
        return [_message_dict(r) for r in rows]

    def mark_read(self, name: str, message_ids: list[int]) -> int:
        """Mark these messages read for recipient `name` (e.g. shown by read_messages with=...), so they are
        not delivered again at the next wake-up. Returns how many were unread."""
        ids = [int(i) for i in message_ids]
        if not ids:
            return 0
        cur = self.db.execute(
            f"UPDATE inbox SET read_at = ? WHERE recipient = ? AND read_at IS NULL AND message_id IN "
            f"({', '.join('?' * len(ids))})", (time.time(), name, *ids))
        return max(int(cur.rowcount), 0)

    def unread_count(self, name: str) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM inbox WHERE recipient = ? AND read_at IS NULL", (name,)))

    def history(self, name: str, with_name: str | None = None, limit: int = 50,
                before_id: int | None = None) -> list[dict]:
        """The latest `limit` messages sent or received by `name` (optionally only with `with_name`),
        older than `before_id`, in ascending order."""
        if with_name:
            where, params = self._between(name, self._lenient(with_name))
        else:
            where, params = f"(m.sender = ? OR {_TO_ME})", [name, name]
        if before_id:
            where += " AND m.id < ?"
            params.append(_as_id(before_id, "before_id"))
        return self._latest(where, params, limit)

    def conversation(self, a: str, b: str, limit: int = 200, since_id: int = 0) -> list[dict]:
        where, params = self._between(a, b)
        return self._page(where, params, limit, since_id)

    def recent(self, limit: int = 100, since_id: int = 0) -> list[dict]:
        return self._page("1 = 1", [], limit, since_id)

    # --- helpers ---------------------------------------------------------------------------------------

    @staticmethod
    def _between(a: str, b: str) -> tuple[str, list[Any]]:
        where = f"((m.sender = ? AND {_TO_ME}) OR (m.sender = ? AND {_TO_ME}))"
        return where, [a, b, b, a]

    def _lenient(self, name: str) -> str:
        try:
            return self.swarm.resolve_member(name, allow_user=True, allow_retired=True)
        except ToolError:
            return name

    def _page(self, where: str, params: list[Any], limit: int, since_id: int) -> list[dict]:
        """Messages with id > since_id (first `limit` of them); since_id == 0: the latest `limit`."""
        if since_id:
            rows = self.db.all(f"SELECT m.* FROM messages m WHERE {where} AND m.id > ? ORDER BY m.id ASC LIMIT ?",
                               [*params, _as_id(since_id, "since_id"), _as_limit(limit)])
            return [_message_dict(r) for r in rows]
        return self._latest(where, params, limit)

    def _latest(self, where: str, params: list[Any], limit: int) -> list[dict]:
        rows = self.db.all(f"SELECT m.* FROM messages m WHERE {where} ORDER BY m.id DESC LIMIT ?",
                           [*params, _as_limit(limit)])
        return [_message_dict(r) for r in reversed(rows)]
