"""The thread mode's posts, comments, votes and subscriptions (docs/SPEC.md §22.2): swarm.forum.

Every write runs in one db transaction and emits its event after it; who is told of it (§22.3) is
`Forum._deliver`, and of a file change `Forum._on_event`."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import sqlite3
import time
import types
from typing import Any, Iterable

from . import prompts
from .board import (MAX_POST, MAX_TITLE, NOTIFY_TEXT_LIMIT, SIMILAR_MAX, _as_int, _clip, find_mentions,
                    similar_items)
from .db import Database, dumps, loads, utf8_safe
from .mailbox import seen_as
from .models import USER, AgentKind, AgentStatus, ReadRefusal, ToolError
from .workspace import REFUSAL_RESERVE, _covers, in_scratch, path_key, shown_list

log = logging.getLogger(__name__)

GOAL_POST = 1                 # the system posts' reserved ids (docs/SPEC.md §22.1)
PLAN_POST = 2
SYSTEM_KINDS = ("goal", "plan")
SUB_KINDS = ("feed", "post", "file", "reactions")
SUB_MODES = ("wake", "digest")
RULES = ("democracy", "veto")
RESET_REASONS = ("new_goal", "reopened", "wrap_up_end")
VOTE_VALUES = {"like": 1, "dislike": -1, "none": 0}
FEED_LIMIT = 30               # feed: the default and the largest number of posts shown
FEED_MAX = 100
COMMENTS_LIMIT = 200
SIMILAR_CANDIDATES = 200      # similar_open: the most recently active posts compared
SIMILAR_GOAL_CHARS = 300      # ... a post's "goal": its first paragraph, cut to this
GOAL_TARGET = f"p{GOAL_POST}"
# the delivery (docs/SPEC.md §22.3): a digest item's {text} cut; f_file's named paths and its paths cap
DIGEST_TEXT_LIMIT = 200
FILE_NAMED = 5
FILE_PATHS_MAX = 200
FILE_EVENTS = ("file.changed", "files.committed")
# notifications.data of a waking f_file waiting for its run of changes to settle: {first, last}
SETTLE_KEY = "_settle"

_TARGET = re.compile(r"(?:#|p)(\d+)|c(\d+)", re.IGNORECASE)
_POST_ID = re.compile(r"[#p]?(\d+)", re.IGNORECASE)
_PARAGRAPH = re.compile(r"\n[ \t]*\n")
_POST_SQL = """SELECT p.*,
    (SELECT COUNT(*) FROM votes WHERE target = 'p' || p.id AND value > 0) AS likes,
    (SELECT COUNT(*) FROM votes WHERE target = 'p' || p.id AND value < 0) AS dislikes,
    (SELECT COUNT(*) FROM post_comments WHERE post_id = p.id) AS comments
    FROM posts p"""
_COMMENT_SQL = """SELECT c.*,
    (SELECT COUNT(*) FROM votes WHERE target = 'c' || c.id AND value > 0) AS likes,
    (SELECT COUNT(*) FROM votes WHERE target = 'c' || c.id AND value < 0) AS dislikes
    FROM post_comments c"""
_SYSTEM_FIRST = "CASE p.kind WHEN 'goal' THEN 0 WHEN 'plan' THEN 1 ELSE 2 END, p.active_at DESC, p.id DESC"

# the unsubscribe refusal of the automatic feed row (docs/SPEC.md §22.3), in the caller's language
_FEED_STAYS = {
    "en": "The feed cannot be unsubscribed: change its mode (subscribe what=feed mode=digest|wake), or add narrower "
          "subscriptions.",
    "ru": "Подписку на ленту нельзя отменить: смени ее режим (subscribe what=feed mode=digest|wake) или добавь более "
          "узкие подписки.",
}


# the read-before-write refusals' first lines (docs/SPEC.md §22.11), in the member's language; what follows them is
# English data in post_read's format
UNREAD_TEXTS = {
    "en": {
        "comment_unread": "Not sent: #{post} \"{title}\" has what you have not read ({news}). You may write to a post "
                          "only after reading what is new in it: it is below and now counts as read. If your comment "
                          "is still needed, send it again (another member may already have said it or taken it on); "
                          "if it claims work, start that work only once it went through.",
        "update_unread": "Not changed: #{post} \"{title}\" has what you have not read ({news}). You may change your "
                         "post only after reading what is new in it, objections included: it is below and now counts "
                         "as read. If your edit is still right, send it again.",
        "create_unread": "Not posted: the feed has posts by others that you have not seen ({n}). You may post only "
                         "after seeing what is new in the feed: they are listed below and now count as seen. If one "
                         "of them is about your topic, comment there instead (`post_read` shows it); otherwise send "
                         "your post again.",
        "news_post": "you have not read the post itself",
        "news_edited": "the post was edited",
        "news_comments": "unread comments by others: {n}",
        "news_thread": "unread comments in the thread of c{id}: {n}",
    },
    "ru": {
        "comment_unread": "Не отправлено: в #{post} «{title}» есть то, чего ты не читал(а) ({news}). Писать в "
                          "объявление можно, только прочитав новое в нем: оно ниже и теперь считается прочитанным. "
                          "Если твой комментарий еще нужен, отправь его снова (возможно, другой участник уже сказал "
                          "это или взялся за это); если он заявляет работу, начинай ее, только когда он прошел.",
        "update_unread": "Не изменено: в #{post} «{title}» есть то, чего ты не читал(а) ({news}). Менять свое "
                         "объявление можно, только прочитав новое в нем, возражения тоже: оно ниже и теперь "
                         "считается прочитанным. Если правка еще верна, отправь ее снова.",
        "create_unread": "Не опубликовано: в ленте есть объявления других, которых ты не видел(а) ({n}). Публиковать "
                         "можно, только увидев новое в ленте: они перечислены ниже и теперь считаются "
                         "просмотренными. Если одно из них о твоей теме, напиши комментарий там (его покажет "
                         "`post_read`); иначе отправь объявление снова.",
        "news_post": "ты не читал(а) само объявление",
        "news_edited": "объявление изменено",
        "news_comments": "непрочитанные комментарии других: {n}",
        "news_thread": "непрочитанные комментарии в ветке c{id}: {n}",
    },
}
REFUSAL_ITEMS_MIN = 200   # what a refusal's items always get, whatever is left of its budget (§22.11 "Size")


def _lang(language: str | None) -> str:
    return language if language in _FEED_STAYS else "en"


def post_version(post: dict) -> str:
    """A post's content version (docs/SPEC.md §22.11): the first 16 hex digits of sha256 over the UTF-8 of its title,
    "\\0" and its text. Votes, edited_at and #2's data do not change it."""
    raw = f"{post['title'] or ''}\0{post['text'] or ''}".encode("utf-8", "surrogatepass")
    return hashlib.sha256(raw).hexdigest()[:16]


def _read_key() -> str:
    from .swarm import READ_KEY  # the swarm imports this module

    return READ_KEY


def read_budget(swarm: Any) -> int:
    """What a forum read (`feed`, `post_read`) fits into: max(200, [agents] tool_output_chars), read at each use."""
    return max(200, int(swarm.config.agents.tool_output_chars))


def t(language: str | None, key: str, **kw: Any) -> str:
    """A §22.4 text (prompts.TEXTS, THREAD_KEYS of §22.6) in `language`."""
    return prompts.t(language, key, **kw)


def rule_name(language: str | None, rule: str) -> str:
    """"democracy" | "veto" | "user" in words: rule_democracy, rule_veto, rule_user."""
    return t(language, f"rule_{rule}")


def vote_line(language: str | None, tally: dict) -> str:
    """The goal vote line of a goal_tally (docs/SPEC.md §22.4): goal_vote_line, or goal_vote_line_veto."""
    key = "goal_vote_line_veto" if tally["rule"] == "veto" else "goal_vote_line"
    return t(language, key, rule=rule_name(language, tally["rule"]), counted=tally["counted"],
             against=len(tally["against"]), needed=tally["needed"], **{"for": len(tally["for"])})


def names_or_dash(names: Iterable[str]) -> str:
    return ", ".join(names) or "—"


def decision_of(tally: dict, wrap_up: bool, rule: str | None = None) -> dict:
    """kv goal's `decision` (docs/SPEC.md §22.1) from a goal_tally; `rule` "user" for the user's completion."""
    return {"rule": rule or tally["rule"], "for": list(tally["for"]), "against": list(tally["against"]),
            "counted": tally["counted"], "wrap_up": bool(wrap_up)}


def decision_summary(language: str | None, decision: dict) -> str:
    """The generated summary of a goal decided by the vote: goal_decided, or goal_decided_wrap_up."""
    key = "goal_decided_wrap_up" if decision.get("wrap_up") else "goal_decided"
    return t(language, key, rule=rule_name(language, decision["rule"]), n=len(decision["for"]),
             counted=decision["counted"], names=names_or_dash(decision["for"]),
             against=names_or_dash(decision["against"]))


def user_summary(language: str | None, tally: dict) -> str:
    """The summary of the user's own completion without words of the user's: goal_completed_by_user."""
    return t(language, "goal_completed_by_user", vote=vote_line(language, tally))


def _rows(source: Any, sql: str, params: tuple = ()) -> list:
    if isinstance(source, Database):
        return source.all(sql, params)
    return source.execute(sql, params).fetchall()


def goal_tally(conn: sqlite3.Connection | Database, rule: str) -> dict:
    """The one vote rule (docs/SPEC.md §22.4), pure: a sqlite3 connection or a Database. The counted voters are the
    active AI agents in members order; `for` / `against` their likes / dislikes on #1. `met` needs at least one
    counted voter and one counted like (no vacuous veto)."""
    if rule not in RULES:
        raise ValueError(f"goal rule must be 'democracy' or 'veto', got {rule!r}")
    voters = [row[0] for row in _rows(
        conn, "SELECT name FROM agents WHERE kind = ? AND status != ? ORDER BY created_at, rowid",
        (AgentKind.LLM, AgentStatus.RETIRED))]
    votes = {row[0]: row[1] for row in _rows(conn, "SELECT voter, value FROM votes WHERE target = ?", (GOAL_TARGET,))}
    yes = [name for name in voters if (votes.get(name) or 0) > 0]
    no = [name for name in voters if (votes.get(name) or 0) < 0]
    counted = len(voters)
    needed = counted if rule == "veto" else counted // 2 + 1
    return {"rule": rule, "counted": counted, "for": yes, "against": no, "needed": needed,
            "met": counted >= 1 and len(yes) >= max(1, needed)}


def parse_target(target: Any) -> tuple[str, int]:
    """A vote target: ("p", id) for "#5" / "p5", ("c", id) for "c31"; a bare number is ambiguous."""
    match = _TARGET.fullmatch(str(target if target is not None else "").strip())
    if not match:
        raise ToolError("target must be '#5' for a post or 'c31' for a comment")
    return ("p", int(match.group(1))) if match.group(1) else ("c", int(match.group(2)))


def post_goal(body: str) -> str:
    """A post's "goal" for the similarity of §12: its first paragraph, cut to SIMILAR_GOAL_CHARS."""
    return _PARAGRAPH.split((body or "").strip(), maxsplit=1)[0].strip()[:SIMILAR_GOAL_CHARS]


def _post_dict(row: Any) -> dict[str, Any]:
    return {"id": row["id"], "kind": row["kind"], "title": row["title"], "text": row["text"],
            "author": row["author"], "created_at": row["created_at"], "edited_at": row["edited_at"],
            "active_at": row["active_at"], "likes": row["likes"], "dislikes": row["dislikes"],
            "comments": row["comments"]}


def _comment_dict(row: Any) -> dict[str, Any]:
    return {"id": row["id"], "post_id": row["post_id"], "author": row["author"], "text": row["text"],
            "reply_to": row["reply_to"], "ts": row["ts"], "likes": row["likes"], "dislikes": row["dislikes"]}


def _check_title(title: Any) -> str:
    value = str(title if title is not None else "").strip()
    if not value:
        raise ToolError("Post title must not be empty.")
    if "\n" in value or "\r" in value:
        raise ToolError("The title is one line; put the rest into the text.")
    if len(value) > MAX_TITLE:
        raise ToolError(f"Post title is too long ({len(value)} chars, max {MAX_TITLE}). Keep it short and put "
                        "details into the text.")
    return value


def _check_text(value: Any) -> str:
    body = str(value if value is not None else "").strip()
    if len(body) > MAX_POST:
        raise ToolError(f"Post is too long ({len(body)} chars, max {MAX_POST}). Put long content into a workspace "
                        "file and post its path.")
    return body


def _check_comment(value: Any) -> str:
    body = str(value if value is not None else "").strip()
    if not body:
        raise ToolError("Comment text must not be empty.")
    if len(body) > MAX_POST:
        raise ToolError(f"Comment is too long ({len(body)} chars, max {MAX_POST}). Put long content into a "
                        "workspace file and comment its path.")
    return body


class Forum:
    """swarm.forum in a thread session (None in a board one)."""

    def __init__(self, swarm: Any) -> None:
        self.swarm = swarm

    @property
    def db(self) -> Database:
        return self.swarm.db

    # --- reads -------------------------------------------------------------------------------------

    def feed(self, *, by: str | None = None, since: float | None = None, query: str | None = None,
             limit: int = FEED_LIMIT) -> list[dict]:
        """The system posts first (#1, then #2), then the others by active_at descending (then id descending).
        `limit` is clamped to 1..FEED_MAX and counts the system posts; `by` keeps one author's posts (so the system
        posts drop out), `since` the posts active since then, `query` those whose title or text contains it
        (str.casefold: SQLite's lower knows only ASCII)."""
        limit = max(1, min(FEED_MAX, _as_int(FEED_LIMIT if limit is None else limit, "limit")))
        where, params = self._feed_filter(by, since)
        sql = f"{_POST_SQL}{where} ORDER BY {_SYSTEM_FIRST}"
        needle = (query or "").strip().casefold()
        if not needle:
            return [_post_dict(r) for r in self.db.all(sql + " LIMIT ?", (*params, limit))]
        out: list[dict] = []
        for row in self.db.all(sql, params):
            if needle in row["title"].casefold() or needle in row["text"].casefold():
                out.append(_post_dict(row))
                if len(out) >= limit:
                    break
        return out

    def feed_total(self, *, by: str | None = None, since: float | None = None, query: str | None = None) -> int:
        """How many posts `feed` with these filters has in all (its `… and N more`)."""
        where, params = self._feed_filter(by, since)
        needle = (query or "").strip().casefold()
        if not needle:
            return int(self.db.scalar(f"SELECT COUNT(*) FROM posts p{where}", params) or 0)
        return sum(1 for row in self.db.all(f"SELECT title, text FROM posts p{where}", params)
                   if needle in row["title"].casefold() or needle in row["text"].casefold())

    def _feed_filter(self, by: str | None, since: float | None) -> tuple[str, tuple]:
        where: list[str] = []
        params: list[Any] = []
        if by is not None and str(by).strip():
            where.append("p.author = ?")
            params.append(self.swarm.resolve_member(by, allow_user=True, allow_retired=True))
        if since is not None:
            where.append("p.active_at >= ?")
            params.append(float(since))
        return (" WHERE " + " AND ".join(where) if where else ""), tuple(params)

    def counts(self) -> dict[str, int]:
        """{"posts": the ordinary posts (kind post), "comments": every comment}."""
        return {"posts": int(self.db.scalar("SELECT COUNT(*) FROM posts WHERE kind = 'post'") or 0),
                "comments": int(self.db.scalar("SELECT COUNT(*) FROM post_comments") or 0)}

    def get(self, post_id: int) -> dict:
        """A post dict; ToolError "No post #N"."""
        pid = _as_int(post_id, "post_id")
        row = self.db.one(f"{_POST_SQL} WHERE p.id = ?", (pid,))
        if row is None:
            raise ToolError(f"No post #{pid}")
        return _post_dict(row)

    def has_post(self, post_id: int) -> bool:
        return self.db.one("SELECT 1 FROM posts WHERE id = ?", (post_id,)) is not None

    def comments(self, post_id: int, since_id: int = 0, limit: int = COMMENTS_LIMIT) -> list[dict]:
        """The post's comments with id > since_id, ascending ids, at most `limit`; since_id must be a comment of
        the post (else `No comment c31 on #9`)."""
        post = self.get(post_id)
        since = _as_int(since_id or 0, "since_id")
        if since:
            self._comment_of(post["id"], since)
        limit = max(1, _as_int(COMMENTS_LIMIT if limit is None else limit, "limit"))
        return [_comment_dict(r) for r in self.db.all(
            f"{_COMMENT_SQL} WHERE c.post_id = ? AND c.id > ? ORDER BY c.id ASC LIMIT ?", (post["id"], since, limit))]

    def comment_get(self, comment_id: int) -> dict:
        """A comment dict; ToolError "No comment cN"."""
        cid = _as_int(comment_id, "comment_id")
        row = self.db.one(f"{_COMMENT_SQL} WHERE c.id = ?", (cid,))
        if row is None:
            raise ToolError(f"No comment c{cid}")
        return _comment_dict(row)

    def _comment_of(self, post_id: int, comment_id: int) -> Any:
        row = self.db.one("SELECT * FROM post_comments WHERE id = ? AND post_id = ?", (comment_id, post_id))
        if row is None:
            raise ToolError(f"No comment c{comment_id} on #{post_id}")
        return row

    def votes(self, target: str) -> list[dict]:
        """The voters of a target ("p9" / "c31") in the order they voted: {voter, value, counted}; `counted` only on
        #1, for an active AI agent."""
        kind, item = parse_target(target)
        key = f"{kind}{item}"
        counted = self._counted_voters() if key == GOAL_TARGET else set()
        return [{"voter": r["voter"], "value": "like" if r["value"] > 0 else "dislike",
                 "counted": r["voter"] in counted}
                for r in self.db.all("SELECT voter, value FROM votes WHERE target = ? ORDER BY ts, rowid", (key,))]

    def my_vote(self, actor: str, target: str) -> str | None:
        """The actor's vote on a target: "like", "dislike" or None."""
        kind, item = parse_target(target)
        value = self.db.scalar("SELECT value FROM votes WHERE target = ? AND voter = ?", (f"{kind}{item}", actor))
        return None if not value else ("like" if value > 0 else "dislike")

    def listing(self, viewer: str = USER) -> list[dict]:
        """Every post without its text in the feed's order, with `viewer`'s vote (`my_vote`: like | dislike | null):
        the snapshot's `posts` (docs/SPEC.md §22.8), one query whatever the feed's size."""
        rows = self.db.all(
            "SELECT p.id, p.kind, p.title, p.author, p.created_at, p.edited_at, p.active_at, "
            "(SELECT COUNT(*) FROM votes WHERE target = 'p' || p.id AND value > 0) AS likes, "
            "(SELECT COUNT(*) FROM votes WHERE target = 'p' || p.id AND value < 0) AS dislikes, "
            "(SELECT COUNT(*) FROM post_comments WHERE post_id = p.id) AS comments, "
            "(SELECT value FROM votes WHERE target = 'p' || p.id AND voter = ?) AS mine "
            f"FROM posts p ORDER BY {_SYSTEM_FIRST}", (viewer,))
        return [{"id": r["id"], "kind": r["kind"], "title": r["title"], "author": r["author"],
                 "created_at": r["created_at"], "edited_at": r["edited_at"], "active_at": r["active_at"],
                 "likes": r["likes"], "dislikes": r["dislikes"], "comments": r["comments"],
                 "my_vote": None if not r["mine"] else ("like" if r["mine"] > 0 else "dislike")} for r in rows]

    def subscriptions_by_member(self) -> dict[str, list[dict]]:
        """Every member's subscriptions by id, {member: [{id, kind, target, by, mode, auto}]}: the snapshot's
        `agents[].subscriptions` (docs/SPEC.md §22.8), one query."""
        out: dict[str, list[dict]] = {}
        for r in self.db.all("SELECT id, member, kind, target, by_member, mode, auto FROM subscriptions ORDER BY id"):
            out.setdefault(r["member"], []).append({"id": r["id"], "kind": r["kind"], "target": r["target"],
                                                    "by": r["by_member"], "mode": r["mode"], "auto": bool(r["auto"])})
        return out

    def plan_data(self) -> dict | None:
        """#2's data {"clarify", "asker"}, None while there is no #2."""
        row = self.db.one("SELECT data FROM posts WHERE id = ?", (PLAN_POST,))
        return None if row is None else (loads(row["data"]) or {"clarify": False, "asker": None})

    def plan_since(self) -> float | None:
        """When the swarm last wrote #2 (its edited_at, else its created_at): the clarify bounds count from it."""
        row = self.db.one("SELECT created_at, edited_at FROM posts WHERE id = ?", (PLAN_POST,))
        return None if row is None else (row["edited_at"] or row["created_at"])

    def set_plan_asker(self, name: str | None) -> None:
        """Writes the kickoff asker into #2's data (its edited_at stays as it is)."""
        data = self.plan_data()
        if data is not None:
            self.db.execute("UPDATE posts SET data = ? WHERE id = ?", (dumps({**data, "asker": name}), PLAN_POST))

    # --- posts and comments ------------------------------------------------------------------------

    def create(self, actor: str, title: str, text: str) -> dict:
        """A post of kind `post` by a member or USER; the text may be empty (a title-only post)."""
        self._check_actor(actor)
        title, body = _check_title(title), _check_text(text)
        now = time.time()
        refusal = None
        with self.db.transaction() as db:
            # read before write (docs/SPEC.md §22.11): checked right before the insert, in its transaction
            refusal = self._create_refusal(actor) if actor != USER else None
            if refusal is None:
                pid = db.insert("INSERT INTO posts(author, kind, title, text, created_at, active_at) "
                                "VALUES (?, 'post', ?, ?, ?, ?)", (actor, title, body, now, now))
                self._put_marks(self._movers(actor), {f"p{pid}": post_version({"title": title, "text": body})})
        if refusal is not None:  # raised after the block: the refusal's marks are committed
            raise ReadRefusal(refusal)
        post = self.get(pid)
        event = self.swarm.bus.emit("post.created", actor, post=post)
        self._deliver(event, self._mentioned(actor, f"{title}\n{body}"))
        return post

    def update(self, actor: str, post_id: int, *, title: str | None = None, text: str | None = None) -> dict:
        """An edit by the post's author or USER; the system posts are the swarm's. The result is the post dict with
        "changes", the fields that changed ([] when the values equal the current ones: no event)."""
        self._check_actor(actor)
        post = self.get(post_id)
        if post["kind"] in SYSTEM_KINDS:
            raise ToolError(f"#{post['id']} is a system post: the swarm writes it from the goal; comment on it "
                            "instead.")
        if actor != USER and post["author"] != actor:
            raise ToolError(f"#{post['id']} is {post['author']}'s post: only its author edits it; comment on it "
                            "instead.")
        if title is None and text is None:
            raise ToolError("Pass title or text.")
        values: dict[str, str] = {}
        if title is not None:
            values["title"] = _check_title(title)
        if text is not None:
            values["text"] = _check_text(text)
        changes = [key for key in ("title", "text") if key in values and values[key] != post[key]]
        if not changes:
            return {**post, "changes": []}
        now = time.time()
        sets = ", ".join(f"{key} = ?" for key in changes)
        refusal = None
        with self.db.transaction() as db:
            refusal = self._post_refusal(actor, post["id"], "update_unread") if actor != USER else None
            if refusal is None:
                db.execute(f"UPDATE posts SET {sets}, edited_at = ?, active_at = ? WHERE id = ?",
                           (*(values[key] for key in changes), now, now, post["id"]))
                self._put_marks(self._movers(actor), {f"p{post['id']}": self._version_of(post["id"])})
        if refusal is not None:
            raise ReadRefusal(refusal)
        new = self.get(post["id"])
        event = self.swarm.bus.emit("post.updated", actor, post=new, changes=changes)
        before = f"{post['title']}\n{post['text']}"
        self._deliver(event, self._mentioned(actor, f"{new['title']}\n{new['text']}", before))
        return {**new, "changes": changes}

    def comment(self, actor: str, post_id: int, text: str, reply_to: int | None = None) -> dict:
        """A comment on any existing post (the system posts too, also while the goal is done); `reply_to` a comment
        of the same post. Sets the post's active_at."""
        self._check_actor(actor)
        post = self.get(post_id)
        body = _check_comment(text)
        reply = None
        if reply_to is not None and str(reply_to).strip():
            reply = _as_int(reply_to, "reply_to")
            self._comment_of(post["id"], reply)
        event, comment = self._insert_comment(actor, post["id"], body, reply, check=actor != USER)
        self._deliver(event, self._mentioned(actor, body))
        return comment

    def _insert_comment(self, actor: str, post_id: int, body: str, reply: int | None,
                        check: bool = False) -> tuple[dict, dict]:
        """`check`: the read-before-write check of a member's comment (docs/SPEC.md §22.11), in the insert's
        transaction; a refusal writes nothing but its marks and raises ReadRefusal once they are committed."""
        now = time.time()
        refusal = None
        with self.db.transaction() as db:
            if check:
                refusal = (self._reply_refusal(actor, post_id, reply) if reply is not None
                           else self._post_refusal(actor, post_id, "comment_unread"))
            if refusal is None:
                cid = db.insert("INSERT INTO post_comments(post_id, author, text, reply_to, ts) "
                                "VALUES (?, ?, ?, ?, ?)", (post_id, actor, body, reply, now))
                db.execute("UPDATE posts SET active_at = ? WHERE id = ?", (now, post_id))
                self._put_marks(self._movers(actor), {f"c{cid}": None})
        if refusal is not None:
            raise ReadRefusal(refusal)
        comment = self.comment_get(cid)
        return self.swarm.bus.emit("post.comment", actor, post_id=post_id, comment=comment), comment

    def vote(self, actor: str, target: str, value: str) -> dict:
        """value "like" | "dislike" | "none" (withdraw). The result {target, value, likes, dislikes, changed}: `value`
        the actor's vote after the call, `changed` false when it changed nothing (no event); on #1 also "goal_vote"
        (after the vote) and "decided" (this vote completed the goal)."""
        self._check_actor(actor)
        kind, item = parse_target(target)
        choice = str(value if value is not None else "").strip().lower()
        if choice not in VOTE_VALUES:
            raise ToolError("value must be like, dislike or none")
        if kind == "p":
            author = self.get(item)["author"]
        else:
            author = self.comment_get(item)["author"]
        key = f"{kind}{item}"
        if author is not None and author == actor:
            raise ToolError(f"You cannot vote on your own {'post' if kind == 'p' else 'comment'}.")
        if key == GOAL_TARGET and (self.swarm.goal() or {}).get("status") != "active":
            raise ToolError("The goal is already reached: #1 is closed for votes until the goal is reopened.")
        wanted = VOTE_VALUES[choice]
        with self.db.transaction() as db:
            old = db.scalar("SELECT value FROM votes WHERE target = ? AND voter = ?", (key, actor)) or 0
            changed = old != wanted
            if changed and wanted:
                db.execute("INSERT INTO votes(target, voter, value, ts) VALUES (?, ?, ?, ?) ON CONFLICT(target, voter) "
                           "DO UPDATE SET value = excluded.value, ts = excluded.ts", (key, actor, wanted, time.time()))
            elif changed:
                db.execute("DELETE FROM votes WHERE target = ? AND voter = ?", (key, actor))
        likes, dislikes = self._counts(key)
        result: dict[str, Any] = {"target": key, "value": choice, "likes": likes, "dislikes": dislikes,
                                  "changed": changed}
        if changed:
            event = self.swarm.bus.emit("post.vote", actor, target=key, voter=actor, value=choice, likes=likes,
                                        dislikes=dislikes)
            self._deliver(event)
        if key == GOAL_TARGET:
            result["goal_vote"] = self.goal_vote()
            result["decided"] = self.recheck_goal(actor if actor in self._counted_voters() else None)
        return result

    def _counts(self, key: str) -> tuple[int, int]:
        row = self.db.one("SELECT COALESCE(SUM(value > 0), 0) AS likes, COALESCE(SUM(value < 0), 0) AS dislikes "
                          "FROM votes WHERE target = ?", (key,))
        return int(row["likes"]), int(row["dislikes"])

    def similar_open(self, post: dict) -> list[dict]:
        """Posts that look like `post` (board.similar_items over the SIMILAR_CANDIDATES most recently active posts
        of kind post, without the post itself and its author's own), best first, then the lower id; at most
        SIMILAR_MAX. A post's goal is its first paragraph (post_goal)."""
        rows = self.db.all(
            "SELECT id, title, text FROM posts WHERE kind = 'post' AND id != ? AND (author IS NULL OR author != ?) "
            "ORDER BY active_at DESC, id DESC LIMIT ?", (post["id"], post.get("author") or "", SIMILAR_CANDIDATES))
        items = [types.SimpleNamespace(id=r["id"], title=r["title"], goal=post_goal(r["text"])) for r in rows]
        hits = similar_items(post["title"], post_goal(post.get("text") or ""), items, limit=SIMILAR_MAX)
        return [self.get(hit.id) for hit in hits]

    # --- subscriptions (the data; who gets what is §22.3) ---------------------------------------------------

    def subscriptions(self, member: str) -> list[dict]:
        """The member's rows by id; `by_former` marks a `by` that names a former (retired) member."""
        return [self._sub_dict(r) for r in self.db.all(
            "SELECT * FROM subscriptions WHERE member = ? ORDER BY id", (member,))]

    def subscribe(self, actor: str, kind: str, target: str | None = None, by: str | None = None,
                  mode: str = "wake") -> dict:
        """Creates the caller's row, or changes the mode of its row with the same (kind, target, by); the result is
        the subscription with "op": created | changed | unchanged."""
        member = self._check_subscriber(actor)
        kind = str(kind or "").strip().lower()
        if kind not in SUB_KINDS:
            raise ToolError("what must be feed, post, file or reactions")
        mode = str(mode or "wake").strip().lower()
        if mode not in SUB_MODES:
            raise ToolError("mode must be wake or digest")
        key = self._sub_target(kind, target)
        by_member = ""
        if by is not None and str(by).strip():
            by_member = self.swarm.resolve_member(by, allow_user=True, allow_retired=False)
            if by_member == member:
                raise ToolError("You never get your own events: by cannot be yourself.")
        with self.db.transaction() as db:
            row = db.one("SELECT * FROM subscriptions WHERE member = ? AND kind = ? AND target = ? AND by_member = ?",
                         (member, kind, key, by_member))
            if row is None:
                op = "created"
                db.execute("INSERT INTO subscriptions(member, kind, target, by_member, mode, auto, created_at) "
                           "VALUES (?, ?, ?, ?, ?, 0, ?) ON CONFLICT(member, kind, target, by_member) "
                           "DO UPDATE SET mode = excluded.mode", (member, kind, key, by_member, mode, time.time()))
            elif row["mode"] != mode:
                op = "changed"
                db.execute("UPDATE subscriptions SET mode = ? WHERE id = ?", (mode, row["id"]))
            else:
                op = "unchanged"
            sub_id = db.scalar("SELECT id FROM subscriptions WHERE member = ? AND kind = ? AND target = ? AND "
                               "by_member = ?", (member, kind, key, by_member))
        sub = self._sub(sub_id)
        if op != "unchanged":
            self.swarm.bus.emit("subscription.changed", member, member=member, subscription=sub, op=op)
        return {**sub, "op": op}

    def unsubscribe(self, actor: str, subscription_id: int) -> None:
        """Removes one of the caller's rows; its feed row (no by) stays: only its mode changes."""
        member = self._check_subscriber(actor)
        sid = _as_int(subscription_id, "subscription_id")
        row = self.db.one("SELECT * FROM subscriptions WHERE id = ? AND member = ?", (sid, member))
        if row is None:
            raise ToolError(f"No subscription s{sid} of yours")
        if row["kind"] == "feed" and row["target"] == "" and row["by_member"] == "":
            raise ToolError(_FEED_STAYS[_lang(self.swarm.language_of(member))])
        sub = self._sub_dict(row)
        with self.db.transaction() as db:
            db.execute("DELETE FROM subscriptions WHERE id = ?", (sid,))
        self.swarm.bus.emit("subscription.changed", member, member=member, subscription=sub, op="removed")

    def auto_subscribe(self, member: str) -> None:
        """The automatic rows of a new member (docs/SPEC.md §22.3): feed, reactions, #1 and #2, all digest; the rows
        it has already stay as they are."""
        now = time.time()
        created: list[int] = []
        with self.db.transaction() as db:
            for kind, target in (("feed", ""), ("reactions", ""), ("post", str(GOAL_POST)), ("post", str(PLAN_POST))):
                cursor = db.execute(
                    "INSERT INTO subscriptions(member, kind, target, by_member, mode, auto, created_at) "
                    "VALUES (?, ?, ?, '', 'digest', 1, ?) ON CONFLICT DO NOTHING", (member, kind, target, now))
                if cursor.rowcount == 1:
                    created.append(int(cursor.lastrowid))
        for sid in created:
            self.swarm.bus.emit("subscription.changed", None, member=member, subscription=self._sub(sid), op="created")

    def drop_subscriptions(self, member: str) -> int:
        """At retire: every row of the member, the feed one too; one subscription.changed with the count."""
        with self.db.transaction() as db:
            count = db.execute("DELETE FROM subscriptions WHERE member = ?", (member,)).rowcount
        self.swarm.bus.emit("subscription.changed", None, member=member, op="removed", count=count)
        return count

    def _sub(self, sid: int) -> dict:
        return self._sub_dict(self.db.one("SELECT * FROM subscriptions WHERE id = ?", (sid,)))

    def _sub_dict(self, row: Any) -> dict:
        by = row["by_member"]
        return {"id": row["id"], "member": row["member"], "kind": row["kind"], "target": row["target"], "by": by,
                "mode": row["mode"], "auto": bool(row["auto"]), "created_at": row["created_at"],
                "by_former": bool(by) and by != USER and not self.swarm.is_member(by)}

    def _sub_target(self, kind: str, target: Any) -> str:
        raw = str(target if target is not None else "").strip()
        if kind in ("feed", "reactions"):
            if raw:
                raise ToolError("target is only for what=post or what=file")
            return ""
        if kind == "post":
            match = _POST_ID.fullmatch(raw)
            if not match:
                raise ToolError("what=post needs target: a post number such as #9")
            pid = int(match.group(1))
            if not self.has_post(pid):
                raise ToolError(f"No post #{pid}")
            return str(pid)
        if raw in ("", ".", "/"):
            return ""
        workspace = self.swarm.workspace
        rel = workspace.rel(workspace.resolve(raw))
        return "" if rel == "." else rel.rstrip("/")

    # --- the goal and its vote (docs/SPEC.md §22.4) --------------------------------------------------------

    def goal_vote(self) -> dict | None:
        """goal_tally of the session's rule; None while there is no #1."""
        if not self.has_post(GOAL_POST):
            return None
        return goal_tally(self.db, self.swarm.goal_rule)

    def recheck_goal(self, actor: str | None) -> bool:
        """Completes the active goal when the vote meets the rule: decision and the generated summary go to
        swarm.complete_goal; True when it did."""
        tally = self.goal_vote()
        goal = self.swarm.goal()
        if not tally or not tally["met"] or not goal or goal.get("status") != "active":
            return False
        decision = decision_of(tally, self.swarm.wrapping_up)
        self.swarm.complete_goal(actor, decision_summary(self.swarm.language, decision), decision=decision)
        return True

    def ensure_system_posts(self, goal: dict) -> bool:
        """Creates #1 and #2 for the goal when the forum has none (True), else rewrites their texts for it (False):
        one post.updated each, then the votes on #1 reset. #2's data is decided again each time."""
        lang = self.swarm.language
        goal_text = self._goal_text(goal)
        plan_text = self._plan_text(goal)
        clarify = bool(self.swarm.clarifying)
        data = dumps({"clarify": clarify, "asker": self._first_llm() if clarify else None})
        now = time.time()
        rows = [(GOAL_POST, "goal", t(lang, "post_goal_title"), goal_text, None),
                (PLAN_POST, "plan", t(lang, "post_plan_title"), plan_text, data)]
        created: list[int] = []
        rewritten: list[int] = []
        with self.db.transaction() as db:
            for pid, kind, title, body, extra in rows:
                if db.one("SELECT 1 FROM posts WHERE id = ?", (pid,)) is None:
                    db.execute("INSERT INTO posts(id, author, kind, title, text, data, created_at, active_at) "
                               "VALUES (?, NULL, ?, ?, ?, ?, ?, ?)", (pid, kind, title, body, extra, now, now))
                    created.append(pid)
                else:
                    db.execute("UPDATE posts SET text = ?, data = ?, edited_at = ?, active_at = ? WHERE id = ?",
                               (body, extra, now, now, pid))
                    rewritten.append(pid)
        for pid in created:
            self.swarm.bus.emit("post.created", None, post=self.get(pid))
        for pid in rewritten:
            self.swarm.bus.emit("post.updated", None, post=self.get(pid), changes=["text"])
        if rewritten:
            self.reset_goal_votes(None, "new_goal")
        return GOAL_POST in created

    def rewrite_goal_rule(self) -> bool:
        """#1's text for the current Swarm.goal_rule; True when it changed (votes, #2 and comments stay)."""
        goal = self.swarm.goal()
        row = self.db.one("SELECT text FROM posts WHERE id = ?", (GOAL_POST,))
        if row is None or not goal:
            return False
        body = self._goal_text(goal)
        if body == row["text"]:
            return False
        now = time.time()
        with self.db.transaction() as db:
            db.execute("UPDATE posts SET text = ?, edited_at = ?, active_at = ? WHERE id = ?",
                       (body, now, now, GOAL_POST))
        self.swarm.bus.emit("post.updated", None, post=self.get(GOAL_POST), changes=["text"])
        return True

    def reset_goal_votes(self, actor: str | None, reason: str) -> None:
        """Deletes every vote on #1; post.updated of #1 with ["votes"], for "new_goal" and "wrap_up_end" only when
        it deleted a row, for "reopened" always."""
        if reason not in RESET_REASONS:
            raise ValueError(f"unknown reason {reason!r}")
        if not self.has_post(GOAL_POST):
            return
        with self.db.transaction() as db:
            deleted = db.execute("DELETE FROM votes WHERE target = ?", (GOAL_TARGET,)).rowcount
        if deleted or reason == "reopened":
            self.swarm.bus.emit("post.updated", actor, post=self.get(GOAL_POST), changes=["votes"])

    def reopen(self, actor: str, comment: str) -> int | None:
        """The thread part of reopen_goal: the votes on #1 reset, and the comment (stripped, unless "" or "-")
        posted on #1 by USER, cut to MAX_POST, never refused. Returns the comment's id, None when it posted none
        (n_goal_reopened_thread carries it as read, docs/SPEC.md §22.11)."""
        self.reset_goal_votes(actor, "reopened")
        body = (comment or "").strip()
        if not body or body == "-" or not self.has_post(GOAL_POST):
            return None
        event, made = self._insert_comment(USER, GOAL_POST, _clip(body, MAX_POST), None)
        # the LLM members get the reopen's own waking notice in place of an f_* item (docs/SPEC.md §22.4)
        self._deliver(event, self._mentioned(USER, body), llm_quiet=True)
        return made["id"]

    def _goal_text(self, goal: dict) -> str:
        lang = self.swarm.language
        return self._with_description(lang, "post_goal_text", goal, rule=rule_name(lang, self.swarm.goal_rule))

    def _plan_text(self, goal: dict) -> str:
        lang = self.swarm.language
        body = self._with_description(lang, "post_plan_text", goal)
        if self.swarm.clarifying:
            body += "\n\n" + t(lang, "post_plan_clarify")
        return body

    @staticmethod
    def _with_description(lang: str, key: str, goal: dict, **kw: Any) -> str:
        """A system post's text with the goal's whole description; without one its blank paragraph goes."""
        description = (goal.get("description") or "").strip()
        marker = "\x00"
        body = t(lang, key, title=goal.get("title", ""), description=description or marker, **kw)
        return body if description else re.sub(r"\n*\x00\n*", "\n\n", body).strip()

    def _first_llm(self) -> str | None:
        for m in self.swarm.members():
            if m["kind"] == AgentKind.LLM:
                return m["name"]
        return None

    def _counted_voters(self) -> set[str]:
        return {r["name"] for r in self.db.all("SELECT name FROM agents WHERE kind = ? AND status != ?",
                                               (AgentKind.LLM, AgentStatus.RETIRED))}

    # --- actors, mentions, delivery --------------------------------------------------------------------------

    def _check_actor(self, actor: str) -> None:
        if actor != USER and not self.swarm.is_member(actor):
            raise ToolError(f"'{actor}' is not an active member of the swarm.")

    def _check_subscriber(self, actor: str) -> str:
        if actor == USER:
            raise ToolError("The user has no subscriptions.")
        self._check_actor(actor)
        return actor

    def _mentioned(self, actor: str, body: str, before: str = "") -> list[str]:
        """The @mentions of `body` (board.find_mentions, retired names in the matching), without the ones `before`
        had, the actor, non-members and retired members; USER for @user / @пользователь."""
        names = [m["name"] for m in self.swarm.members(include_retired=True)]
        found = find_mentions(body, names)
        if before:
            old = set(find_mentions(before, names))
            found = [name for name in found if name not in old]
        return [name for name in found if name != actor and (name == USER or self.swarm.is_member(name))]

    def _deliver(self, event: dict, mentioned: list[str] | tuple[str, ...] = (), *, llm_quiet: bool = False) -> None:
        """The fan-out of a forum event to the members and USER (docs/SPEC.md §22.3): a mentioned recipient's
        f_mention first, then the subscriptions' f_* items, at most one row per recipient. `llm_quiet`: the LLM
        members get nothing (reopen's comment)."""
        actor, kind, data = event.get("actor"), event.get("type"), event.get("data") or {}
        if actor is None:  # the system's own changes: the kickoff and the goal notices carry them
            return
        if kind == "post.updated" and not {"title", "text"} & set(data.get("changes") or ()):
            return  # a reset of the votes on #1
        if kind == "post.vote" and data.get("value") not in ("like", "dislike"):
            return  # a withdrawn vote tells nobody
        item = self._item_of(kind, data)
        if item is None:
            return
        seen = seen_as(actor)  # the members the same MCP client acts as know it already
        members = {m["name"]: m for m in self.swarm.members()}
        told: set[str] = set()
        for name in dict.fromkeys(mentioned):
            if name == actor or name in seen or (name != USER and name not in members):
                continue
            told.add(name)  # a mention is the recipient's one item for the event, never turned off
            if name == USER or not (llm_quiet and members[name]["kind"] == AgentKind.LLM):
                self._send_item(name, "f_mention", actor, item, wake=True)
        user_wakes = actor == USER and kind in ("post.created", "post.comment")
        subs = self._subscriptions_of(("feed", "post", "reactions"))
        for name, member in members.items():
            if name == actor or name in seen or name in told:
                continue
            if llm_quiet and member["kind"] == AgentKind.LLM:
                continue
            key = self._key_for(kind, item, name)
            if user_wakes:  # the user's posts and comments wake every member, whatever it follows
                self._send_item(name, key, actor, item, wake=True)
                continue
            rows = [r for r in subs.get(name, ()) if r["by_member"] in ("", actor)
                    and self._matches(r, kind, item, name)]
            if not rows:
                continue
            if kind == "post.vote":  # votes never wake anyone, whatever the reactions row's mode
                self._send_item(name, key, actor, item, wake=False)
                continue
            wake = any(r["mode"] == "wake" for r in rows)
            # a member's new post reaching X only through its feed in wake waits for X's own job (§5)
            defer = wake and kind == "post.created" and self.swarm.waits_for_job(name)
            self._send_item(name, key, actor, item, wake=wake, defer=defer)
        if actor != USER and USER not in told and USER not in seen and kind == "post.comment":
            # USER has no subscriptions: comments on its posts and replies to its comments (docs/SPEC.md §22.3)
            if USER in (item["post"]["author"], item["reply_author"]):
                self._send_item(USER, self._key_for(kind, item, USER), actor, item, wake=True)

    def _item_of(self, kind: str, data: dict) -> dict | None:
        """What the f_* items of a forum event name: the post, the comment, the authors the reactions follow."""
        item: dict[str, Any] = {"comment": None, "reply_author": None, "target": "", "target_author": None}
        if kind in ("post.created", "post.updated"):
            item["post"] = data["post"]
        elif kind == "post.comment":
            comment = item["comment"] = data["comment"]
            item["post"] = self.get(data["post_id"])
            if comment.get("reply_to"):
                row = self.db.one("SELECT author FROM post_comments WHERE id = ?", (comment["reply_to"],))
                item["reply_author"] = row["author"] if row else None
        elif kind == "post.vote":
            target, number = parse_target(data["target"])
            item["value"] = data["value"]
            if target == "p":
                item["post"] = self.get(number)
                item["target"], item["target_author"] = f"#{number}", item["post"]["author"]
            else:
                comment = self.comment_get(number)
                item["post"] = self.get(comment["post_id"])
                item["target"] = f"c{number} (#{comment['post_id']})"
                item["target_author"] = comment["author"]
        else:
            return None
        return item

    @staticmethod
    def _key_for(kind: str, item: dict, name: str) -> str:
        if kind == "post.comment":
            return "f_reply" if item["reply_author"] == name else "f_comment"
        return {"post.created": "f_post", "post.updated": "f_edit", "post.vote": "f_vote"}[kind]

    @staticmethod
    def _matches(row: Any, kind: str, item: dict, name: str) -> bool:
        """A subscription row against a forum event (the table of docs/SPEC.md §22.3; `by` is checked apart)."""
        post = item["post"]
        if row["kind"] == "feed":
            return kind == "post.created"
        if row["kind"] == "post":
            return kind in ("post.comment", "post.updated") and row["target"] == str(post["id"])
        if row["kind"] == "reactions":
            if kind == "post.comment":
                return name in (post["author"], item["reply_author"])
            if kind == "post.updated":
                return post["author"] == name
            if kind == "post.vote":
                return item["target_author"] == name
        return False

    def _subscriptions_of(self, kinds: tuple[str, ...]) -> dict[str, list]:
        """member -> its subscription rows of these kinds, by id."""
        out: dict[str, list] = {}
        rows = self.db.all(f"SELECT * FROM subscriptions WHERE kind IN ({', '.join('?' * len(kinds))}) ORDER BY id",
                           kinds)
        for row in rows:
            out.setdefault(row["member"], []).append(row)
        return out

    def _send_item(self, name: str, key: str, actor: str, item: dict, *, wake: bool, defer: bool = False) -> None:
        """One f_* item: waking (its text cut to NOTIFY_TEXT_LIMIT), or a digest item (cut to DIGEST_TEXT_LIMIT)."""
        post, comment = item["post"], item["comment"]
        limit = NOTIFY_TEXT_LIMIT if wake else DIGEST_TEXT_LIMIT
        kw: dict[str, Any] = {"by": actor, "post": post["id"], "title": post["title"]}
        if key == "f_vote":
            kw = {"by": actor, "target": item["target"], "title": post["title"],
                  "value": t(self.swarm.language_of(name), f"vote_{item['value']}")}
        elif key == "f_mention":
            kw["comment"] = f" (c{comment['id']})" if comment else ""
            kw["text"] = _clip(comment["text"] if comment else post["text"], limit)
        elif key in ("f_comment", "f_reply"):
            kw.update(comment=f"c{comment['id']}", text=_clip(comment["text"], limit))
            if key == "f_reply":
                kw["reply_to"] = f"c{comment['reply_to']}"
        elif key == "f_post":
            kw["text"] = _clip(post["text"], limit)
        kw[_read_key()] = self._item_reads(key, post, comment, limit)
        if name == USER:
            self.swarm.notify(USER, key, **kw)
        elif wake:
            self.swarm.notify(name, key, defer=defer, **kw)
        else:
            self.swarm.notify(name, key, wake=False, batch=True, **kw)

    @staticmethod
    def _item_reads(key: str, post: dict, comment: dict | None, limit: int) -> dict[str, str | None]:
        """An f_* item's `_read` (docs/SPEC.md §22.11): what its text carries whole. A post's text not cut: `p<id>`
        at its version, else by title; a comment's text not cut: `c<id>`, with its post by title."""
        target = f"p{post['id']}"
        if key == "f_post" or (key == "f_mention" and not comment):
            return {target: post_version(post) if len(post["text"] or "") <= limit else None}
        reads: dict[str, str | None] = {}
        if key in ("f_comment", "f_reply", "f_mention") and comment and len(comment["text"] or "") <= limit:
            reads[f"c{comment['id']}"] = None
        reads[target] = None
        return reads

    # --- read marks (docs/SPEC.md §22.11) ----------------------------------------------------------------------

    def _movers(self, actor: str | None) -> list[str]:
        """Whose marks a call of `actor` moves: the actor when it is a member, and the members of seen_as(actor),
        the MCP client acting as them saw it too; never USER."""
        if actor is None:
            return []
        names = [actor] if actor != USER and self.swarm.is_member(actor) else []
        names += sorted(n for n in seen_as(actor) if n != USER and n not in names and self.swarm.is_member(n))
        return names

    def _put_marks(self, names: Iterable[str], marks: dict[str, str | None]) -> None:
        """Upserts: a version replaces the row's; a post by title (None) and a comment never erase a version."""
        now = time.time()
        for name in names:
            for target, version in marks.items():
                if version is None:
                    self.db.execute("INSERT INTO read_marks(member, target, version, ts) VALUES (?, ?, NULL, ?) "
                                    "ON CONFLICT (member, target) DO NOTHING", (name, target, now))
                else:
                    self.db.execute("INSERT INTO read_marks(member, target, version, ts) VALUES (?, ?, ?, ?) "
                                    "ON CONFLICT (member, target) DO UPDATE SET version = excluded.version, "
                                    "ts = excluded.ts", (name, target, version, now))

    def mark_read(self, member: str, marks: dict[str, str | None]) -> None:
        """Read marks `p<id>` (a version, or None: by title) and `c<id>` (None) of `member` and of the members of
        seen_as(member), in one transaction; nothing for USER itself or a non-member."""
        names = self._movers(member)
        if not names or not marks:
            return
        with self.db.transaction():
            self._put_marks(names, marks)

    def note_shown(self, member: str, rows: Iterable[Any]) -> None:
        """render_wake: the `_read` of the notification rows it printed. A version is applied only while it is
        still the post's version, else that post by title. Every version shown is kept per target, so the order of
        the rows does not matter: a post shown whole at its current version by any row is marked at it."""
        key = _read_key()
        wanted: dict[str, set[str]] = {}
        for row in rows:
            raw = row["data"]
            data = loads(raw) if isinstance(raw, str) else raw
            reads = data.get(key) if isinstance(data, dict) else None
            if not isinstance(reads, dict):
                continue
            for target, version in reads.items():
                shown = wanted.setdefault(str(target), set())
                if version:
                    shown.add(str(version))
        names = self._movers(member)
        if not names or not wanted:
            return
        with self.db.transaction():
            marks: dict[str, str | None] = {}
            for target, versions in wanted.items():
                version = None  # a comment; or a post by title, edited later, or shown at an older version only
                if target.startswith("p") and versions:
                    current = self._version_of(int(target[1:]))
                    version = current if current in versions else None
                marks[target] = version
            self._put_marks(names, marks)

    def drop_reads(self, member: str) -> int:
        """At retire: every read mark of the member."""
        with self.db.transaction() as db:
            return db.execute("DELETE FROM read_marks WHERE member = ?", (member,)).rowcount

    def mark_of(self, member: str, target: str) -> str | None:
        """The version of `member`'s mark of `target` (None: no row, or by title only)."""
        return self.db.scalar("SELECT version FROM read_marks WHERE member = ? AND target = ?", (member, target))

    def system_reads(self) -> dict[str, str]:
        """`p1` and `p2` at their current versions: the kickoff notices carry them (§22.11)."""
        return {f"p{r['id']}": post_version(r) for r in self.db.all(
            "SELECT id, title, text FROM posts WHERE id IN (?, ?)", (GOAL_POST, PLAN_POST))}

    def _version_of(self, post_id: int) -> str | None:
        row = self.db.one("SELECT title, text FROM posts WHERE id = ?", (post_id,))
        return None if row is None else post_version(row)

    # --- the checks (§22.11 "The checks"); each runs inside its write's transaction ------------------------------

    def _post_state(self, member: str, post_id: int) -> str | None:
        """"unread" (no p row, or by title only), "edited" (another version) or None (read as it is)."""
        row = self.db.one("SELECT version FROM read_marks WHERE member = ? AND target = ?", (member, f"p{post_id}"))
        if row is None or row["version"] is None:
            return "unread"
        return "edited" if row["version"] != self._version_of(post_id) else None

    def _unread_comments(self, member: str, post_id: int, thread_of: int | None = None) -> list[dict]:
        """The post's comments by others that `member` has not read, ascending ids; `thread_of` cN: only cN and the
        replies under it at any depth."""
        unread = ("c.author != ? AND NOT EXISTS (SELECT 1 FROM read_marks r WHERE r.member = ? AND "
                  "r.target = 'c' || c.id)")
        if thread_of is None:
            rows = self.db.all(f"{_COMMENT_SQL} WHERE c.post_id = ? AND {unread} ORDER BY c.id",
                               (post_id, member, member))
        else:
            rows = self.db.all(
                "WITH RECURSIVE thread(id) AS (SELECT ? UNION SELECT k.id FROM post_comments k JOIN thread t "
                f"ON k.reply_to = t.id WHERE k.post_id = ?) {_COMMENT_SQL} WHERE c.id IN (SELECT id FROM thread) "
                f"AND c.post_id = ? AND {unread} ORDER BY c.id", (thread_of, post_id, post_id, member, member))
        return [_comment_dict(r) for r in rows]

    def _post_refusal(self, member: str, post_id: int, key: str) -> str | None:
        """A top-level comment's and post_update's check: the post's own news and its unread comments."""
        state = self._post_state(member, post_id)
        comments = self._unread_comments(member, post_id)
        if state is None and not comments:
            return None
        return self._refuse(member, key, post_id, state, comments)

    def _reply_refusal(self, member: str, post_id: int, reply_to: int) -> str | None:
        """A reply's check: the unread comments of the thread of cN, and an edit of a post read in an earlier
        version (a post never read does not block it)."""
        state = self._post_state(member, post_id)
        state = state if state == "edited" else None
        comments = self._unread_comments(member, post_id, thread_of=reply_to)
        if state is None and not comments:
            return None
        return self._refuse(member, "comment_unread", post_id, state, comments, thread_of=reply_to)

    def _refusal_room(self, first: str) -> int:
        """What a refusal's items get after its first lines: max(200, tool_output_chars) less REFUSAL_RESERVE and
        the first lines, at least REFUSAL_ITEMS_MIN."""
        budget = max(200, int(self.swarm.config.agents.tool_output_chars)) - REFUSAL_RESERVE
        return max(REFUSAL_ITEMS_MIN, budget - len(first) - 1)

    def _refuse(self, member: str, key: str, post_id: int, state: str | None, comments: list[dict],
                thread_of: int | None = None) -> str:
        """The refusal text of a comment or an edit; the member's marks (and its seen_as members') move to what it
        shows: the post block's version, the comments listed."""
        from .tools import forum_tools as ft  # the tools import this module

        lang = self.swarm.language_of(member)
        texts = UNREAD_TEXTS.get(lang, UNREAD_TEXTS["en"])
        post = self.get(post_id)
        news = []
        if state == "unread":
            news.append(texts["news_post"])
        elif state == "edited":
            news.append(texts["news_edited"])
        if comments:
            news.append(texts["news_thread"].format(id=thread_of, n=len(comments)) if thread_of is not None
                        else texts["news_comments"].format(n=len(comments)))
        first = texts[key].format(post=post_id, title=post["title"], news="; ".join(news))
        room = self._refusal_room(first)
        lines = [first]
        marks: dict[str, str | None] = {}
        header = f"Unread comments ({len(comments)}):"
        # the continuation line: never needed for one comment, which is always shown (cut when it must be)
        more = len(f"… and {len(comments)} more unread comments: `post_read` post_id={post_id} "
                   f"since_id=c{comments[-1]['id']}, or send again to see them.") + 1 if len(comments) > 1 else 0
        # the lines of every comment, each with its newline: what showing them all takes
        whole = sum(len(line) + 1 for line in ft.comment_prefix(self.swarm, member, comments, 1 << 62)[1])
        if state is not None:
            now = time.time()
            block = [ft.post_head(self.swarm, post, member, now)]
            vote_part = ft.goal_vote_part(self.swarm, lang) if post_id == GOAL_POST else []
            fixed = sum(len(line) + 1 for line in (*block, *vote_part))
            text = post["text"] or ""
            if text.strip():
                # with comments to show too, the comment block keeps its header and the lines of all of them, or
                # a third of the room (at least REFUSAL_ITEMS_MIN) and the continuation line (as in post_read)
                share = max(REFUSAL_ITEMS_MIN, room // 3)
                kept = (len(header) + 1 + (whole if whole <= share else share + more)) if comments else 0
                left = room - fixed - kept
                block.append(text if len(text) + 1 <= left else ft.cut_to(text, left - 1, "text"))
            block += vote_part
            lines += block
            room -= sum(len(line) + 1 for line in block)
            marks[f"p{post_id}"] = post_version(post)
        if comments:
            room -= len(header) + 1
            # the continuation line is reserved only when not every comment fits
            shown, comment_lines = ft.comment_prefix(self.swarm, member, comments,
                                                     room if whole <= room else room - more)
            lines += [header, *comment_lines]
            if len(shown) < len(comments):
                lines.append(f"… and {len(comments) - len(shown)} more unread comments: `post_read` "
                             f"post_id={post_id} since_id=c{shown[-1]['id']}, or send again to see them.")
            marks.update({f"c{c['id']}": None for c in shown})
        self._put_marks(self._movers(member), marks)
        return "\n".join(lines)

    def _create_refusal(self, member: str) -> str | None:
        """post_create's check: the posts of kind post by others (the user's too) of which `member` has no mark. A
        refusal lists the newest that fit and marks them all by title."""
        from .tools.fmt import item_time

        rows = self.db.all("SELECT id, title, author, created_at FROM posts p WHERE kind = 'post' AND author IS NOT "
                           "NULL AND author != ? AND NOT EXISTS (SELECT 1 FROM read_marks r WHERE r.member = ? AND "
                           "r.target = 'p' || p.id) ORDER BY created_at DESC, id DESC", (member, member))
        if not rows:
            return None
        lang = self.swarm.language_of(member)
        first = UNREAD_TEXTS.get(lang, UNREAD_TEXTS["en"])["create_unread"].format(n=len(rows))
        header = f"Posts you have not seen ({len(rows)}), newest first:"
        more = len(f"… and {len(rows)} older posts you have not seen: `feed` lists them.") + 1
        room = self._refusal_room(first) - len(header) - 1 - more
        now = time.time()
        listed: list[str] = []
        used = 0
        for row in rows:
            line = f"#{row['id']} \"{row['title']}\" — {row['author']}, {item_time(row['created_at'] or now, now)}"
            if listed and used + len(line) + 1 > room:
                break
            if not listed and len(line) + 1 > room:
                line = line[:max(1, room - 1) - 1] + "…"
            listed.append(line)
            used += len(line) + 1
        lines = [first, header, *listed]
        if len(listed) < len(rows):
            lines.append(f"… and {len(rows) - len(listed)} older posts you have not seen: `feed` lists them.")
        # the whole feed counts as seen: a member hired late must not page a long feed through refusals
        self._put_marks(self._movers(member), {f"p{row['id']}": None for row in rows})
        return "\n".join(lines)

    # --- file subscriptions (docs/SPEC.md §22.3 "File subscriptions") -------------------------------------------

    def _on_event(self, event: dict) -> None:
        """The bus listener (Swarm._init: swarm.bus.listen) for file.changed and files.committed: the fan-out is
        scheduled with call_soon on the running loop (no notification inside another event's dispatch; the context,
        so seen_as, is copied), inline without a loop."""
        if event.get("type") not in FILE_EVENTS:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._deliver_files(event)
            return
        loop.call_soon(self._deliver_files, event)

    def _deliver_files(self, event: dict) -> None:
        """The f_file items of a file event; never raises (a late write's event may come after the session closed)."""
        try:
            self._fan_out_files(event)
        except sqlite3.ProgrammingError:
            log.debug("the f_file items of %s were not delivered: the database is closed", event.get("type"))
        except Exception:  # noqa: BLE001 - a file change must never fail for its notifications
            log.exception("the f_file items of %s failed", event.get("type"))

    def _fan_out_files(self, event: dict) -> None:
        actor, data = event.get("actor"), event.get("data") or {}
        if actor is None:
            return
        if event["type"] == "file.changed":
            paths, editors, extra = [str(data.get("path") or "")], [actor], 0
        else:
            paths = [str(f.get("path") or "") for f in data.get("files") or ()]
            editors = list(dict.fromkeys([actor, *(data.get("suspects") or ())]))
            extra = max(0, int(data.get("more") or 0))
        paths = [p for p in dict.fromkeys(paths) if p]
        if not paths:
            return
        seen = seen_as(actor)
        subs = self._subscriptions_of(("file",))
        when = float(event.get("ts") or time.time())
        for member in self.swarm.members():
            name = member["name"]
            if name in seen or editors == [name]:
                # own changes: a commit only when the member is its sole suspect; a file.changed always has its
                # actor as sole editor
                continue
            rows = [r for r in subs.get(name, ()) if r["by_member"] in ("", *editors)]
            hits = [r for r in rows if any(_path_matches(r["target"], p) for p in paths)]
            if not hits:
                continue
            matched = [p for p in paths if any(_path_matches(r["target"], p) for r in hits)]
            wake = any(r["mode"] == "wake" for r in hits)
            # the commit's unlisted paths are known to match only a whole-workspace subscription
            member_extra = extra if any(r["target"] == "" for r in rows) else 0
            self._file_item(name, matched, editors, member_extra, wake, when)

    def _file_item(self, name: str, paths: list[str], editors: list[str], extra: int, wake: bool,
                   when: float) -> None:
        """An f_file item, merged into the member's undelivered f_file row of the same delivery when there is one
        (coalescing: its ts and _settle.first kept); a waking one is stored quiet with _settle until it settles."""
        from .swarm import DIGEST_KEY  # the swarm imports this module

        same = "IS NULL" if wake else "IS NOT NULL"
        row = self.db.one(f"SELECT id, data FROM notifications WHERE recipient = ? AND kind = 'f_file' AND "
                          f"delivered_at IS NULL AND json_extract(data, '$.{DIGEST_KEY}') {same} ORDER BY id DESC "
                          "LIMIT 1", (name,))
        if row is None:
            kw = _file_kw(paths, editors, extra)
            if not wake:
                self.swarm.notify(name, "f_file", wake=False, batch=True, **kw)
            else:
                self.swarm.notify(name, "f_file", wake=False, **kw, **{SETTLE_KEY: {"first": when, "last": when}})
            return
        old = loads(row["data"]) or {}
        old_paths = list(old.get("paths") or ())
        overflow = max(0, int(old.get("more") or 0) - max(0, len(old_paths) - FILE_NAMED))
        known = {path_key(p) for p in old_paths}
        added = [p for p in paths if path_key(p) not in known]
        kw = _file_kw(old_paths + added, list(dict.fromkeys([*(old.get("editors") or ()), *editors])),
                      overflow + extra)
        data = {**old, **kw}
        if isinstance(old.get(SETTLE_KEY), dict):
            data[SETTLE_KEY] = {**old[SETTLE_KEY], "last": when}
        text = utf8_safe(prompts.t(self.swarm.language_of(name), "f_file", **kw))
        self.db.execute("UPDATE notifications SET text = ?, data = ? WHERE id = ?", (text, dumps(data), row["id"]))


def _path_matches(target: str, rel: str) -> bool:
    """A file subscription's target against a changed path: '' is the whole workspace (.scratch/ aside), else the
    path itself or one under it, compared by workspace.path_key; .scratch/ paths only for a target inside it."""
    if in_scratch(rel) and not (target and in_scratch(target)):
        return False
    if not target:
        return True
    return _covers(target, rel)


def _file_kw(paths: list[str], editors: list[str], extra: int) -> dict[str, Any]:
    """f_file's placeholders and data: paths capped at FILE_PATHS_MAX (the rest counted in `more`, with the ones
    past FILE_NAMED), editors, and the shown lists."""
    kept = paths[:FILE_PATHS_MAX]
    more = max(0, len(kept) - FILE_NAMED) + len(paths) - len(kept) + extra
    files = shown_list(kept, FILE_NAMED)
    return {"by": shown_list(editors, FILE_NAMED), "files": files, "more": more, "paths": kept,
            "editors": editors}
