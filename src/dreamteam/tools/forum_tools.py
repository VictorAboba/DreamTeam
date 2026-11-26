"""The thread mode's forum tools (docs/SPEC.md §22.5): the feed, posts, comments, votes and subscriptions."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from .. import prompts
from ..forum import vote_line
from ..jobs import fmt_duration
from ..models import USER, AgentKind, ToolError
from . import ToolContext, tool
from .fmt import item_time

THREAD_ONLY = ("thread",)
FEED_DEFAULT = 30
FEED_MAX = 100
COMMENTS_MAX = 200
VOTERS_SHOWN = 10

# --- ids -----------------------------------------------------------------------------------------------------------

def _number(value: Any, pattern: str) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value if value > 0 else None
    match = re.fullmatch(pattern, str(value).strip())
    number = int(match.group(1)) if match else 0
    return number if number > 0 else None


def _comment_id(value: Any, arg: str = "reply_to") -> int:
    """A comment id as members write it: 31, "31", "c31", "C31"."""
    number = _number(value, r"[cC]?\s*(\d+)")
    if number is None:
        raise ToolError(f"{arg} must be a comment id such as c31")
    return number


def _vote_target(value: Any) -> str:
    """A vote target as the votes table stores it: "#5", "p5", "P5" -> "p5" (a post), "c31" -> "c31" (a comment); a
    bare number is ambiguous."""
    match = re.fullmatch(r"(#|[pP]|[cC])\s*(\d+)", str(value).strip())
    if not match or int(match.group(2)) <= 0:
        raise ToolError("target must be '#5' for a post or 'c31' for a comment")
    return ("c" if match.group(1) in "cC" else "p") + str(int(match.group(2)))


def _sub_id(value: Any) -> int:
    """A subscription id: 12, "12", "s12"."""
    number = _number(value, r"[sS]?\s*(\d+)")
    if number is None:
        raise ToolError("subscription_id must be an id such as s12")
    return number


def _post_target(value: Any) -> str:
    """subscribe's post target as the forum stores it ("#9", "p9" -> "9"); anything else goes to the forum's check."""
    match = re.fullmatch(r"(?:#|[pP])?\s*(\d+)", str(value).strip())
    return str(int(match.group(1))) if match else str(value).strip()


# --- shown values ----------------------------------------------------------------------------------------------------

def _author(name: str | None) -> str:
    return "[system]" if not name else name


def _counts(item: dict) -> str:
    return f"likes {item.get('likes', 0)}, dislikes {item.get('dislikes', 0)}"


def _value_word(value: int) -> str:
    return "like" if value > 0 else "dislike"


def _my_votes(swarm, actor: str, targets: list[str]) -> dict[str, int]:
    """The caller's votes on `targets` ({"p9": 1, "c31": -1})."""
    if not targets:
        return {}
    rows = swarm.db.all(f"SELECT target, value FROM votes WHERE voter = ? AND target IN "
                        f"({', '.join('?' * len(targets))})", (actor, *targets))
    return {r["target"]: int(r["value"]) for r in rows}


def _voters(swarm, target: str) -> list[tuple[str, int]]:
    return [(r["voter"], int(r["value"])) for r in
            swarm.db.all("SELECT voter, value FROM votes WHERE target = ? ORDER BY ts, voter", (target,))]


def _names(names: list[str]) -> str:
    shown = ", ".join(names[:VOTERS_SHOWN])
    return shown + (", …" if len(names) > VOTERS_SHOWN else "")


def _clip_line(value: Any, limit: int) -> str:
    line = " ".join(str(value or "").split())
    return line if len(line) <= limit else line[:limit - 1] + "…"


def _post_of_comment(swarm, comment_id: int) -> int | None:
    return swarm.db.scalar("SELECT post_id FROM post_comments WHERE id = ?", (comment_id,))


# --- the feed and posts ----------------------------------------------------------------------------------------------

@tool(
    "feed",
    en="Show the feed: the system posts #1 (\"Goal reached?\", with the goal vote) and #2 (\"Let's plan the work\") "
       "first, then the posts by their latest activity (a new post, an edit or a comment), each with its author, "
       "likes, dislikes and comments. by, since and query narrow the list.",
    ru="Показать ленту: сначала системные объявления #1 («Цель достигнута?», с голосованием о цели) и #2 («Спланируем "
       "работу»), затем объявления по последней активности (новое объявление, правка или комментарий), у каждого — "
       "автор, «нравится», «не нравится» и комментарии. by, since и query сужают список.",
    properties={
        "by": {"type": "string", "description": "Only posts by this member (\"user\": the user's)"},
        "since": {"type": "number", "description": "Only posts active in the last N minutes"},
        "query": {"type": "string", "description": "Text to find in titles and texts"},
        "limit": {"type": "integer", "description": "Posts to show (default 30, max 100)"},
    },
    modes=THREAD_ONLY,
)
async def feed(ctx: ToolContext, args: dict[str, Any]) -> str:
    swarm = ctx.swarm
    minutes = args.get("since")
    if minutes is not None and not minutes > 0:
        raise ToolError("since must be a number of minutes > 0")
    by = swarm.resolve_member(args["by"], allow_user=True, allow_retired=True) if args.get("by") else None
    query = (args.get("query") or "").strip() or None
    limit = max(1, min(args.get("limit") or FEED_DEFAULT, FEED_MAX))
    since = time.time() - minutes * 60 if minutes is not None else None
    posts = swarm.forum.feed(by=by, since=since, query=query, limit=limit)
    total = max(swarm.forum.feed_total(by=by, since=since, query=query), len(posts))
    filters = ([f"by {by}"] if by else []) + ([f"active in the last {fmt_duration(minutes * 60)}"] if since else [])
    filters += [f"matching \"{query}\""] if query else []
    narrowed = f"; {', '.join(filters)}" if filters else ""
    if not posts:
        return f"Feed: no posts{narrowed}."
    lines = [f"Feed: {total} post{'s' if total != 1 else ''}, newest activity first{narrowed}."]
    mine = _my_votes(swarm, ctx.actor, [f"p{p['id']}" for p in posts])
    now = time.time()
    for post in posts:
        edited = " (edited)" if post.get("edited_at") else ""
        active = item_time(post.get("active_at") or post.get("created_at") or now, now)
        line = (f"#{post['id']} \"{post['title']}\" — {_author(post.get('author'))}, {_counts(post)}, "
                f"comments {post.get('comments', 0)}, active {active}{edited}")
        vote = mine.get(f"p{post['id']}")
        lines.append(line + (f"; you: {_value_word(vote)}" if vote else ""))
        if post["id"] == 1:
            tally = swarm.forum.goal_vote()
            if tally is not None:
                lines.append("  " + vote_line(ctx.lang, tally))
    if total > len(posts):
        lines.append(f"… and {total - len(posts)} more: `feed` with limit, by, since or query.")
    return "\n".join(lines)


def _goal_vote_part(ctx: ToolContext) -> list[str]:
    """#1's vote in post_read: the vote line with the counted voters' names, then the votes that do not count."""
    swarm = ctx.swarm
    tally = swarm.forum.goal_vote()
    if tally is None:
        return []
    counted = set(tally.get("for") or []) | set(tally.get("against") or [])
    voters = (", ".join(tally.get("for") or []) or "—", ", ".join(tally.get("against") or []) or "—")
    names = f" For: {voters[0]}; against: {voters[1]}."
    lines = [vote_line(ctx.lang, tally) + names]
    others = [f"{voter} ({_value_word(value)})" for voter, value in _voters(swarm, "p1") if voter not in counted]
    if others:
        lines.append("Not counted: " + ", ".join(others) + ".")
    return lines


def _comment_lines(comments: list[dict], mine: dict[str, int]) -> list[str]:
    """The comments as a tree: replies under the comment they answer, two spaces per level; a reply to a comment
    not shown is at the top level with its `reply to`."""
    now = time.time()
    shown = {c["id"] for c in comments}
    children: dict[int, list[dict]] = {}
    roots = []
    for c in comments:
        if c.get("reply_to") in shown:
            children.setdefault(c["reply_to"], []).append(c)
        else:
            roots.append(c)
    lines: list[str] = []

    def add(c: dict, depth: int) -> None:
        indent = "  " * depth
        reply = f", reply to c{c['reply_to']}" if c.get("reply_to") and c["reply_to"] not in shown else ""
        vote = mine.get(f"c{c['id']}")
        you = f", you: {_value_word(vote)}" if vote else ""
        body = str(c.get("text") or "").replace("\n", "\n" + indent + "  ")
        lines.append(f"{indent}c{c['id']} {_author(c.get('author'))}, {item_time(c.get('ts') or now, now)}, "
                     f"{_counts(c)}{you}{reply}: {body}")
        for child in children.get(c["id"], []):
            add(child, depth + 1)

    for c in roots:
        add(c, 0)
    return lines


@tool(
    "post_read",
    en="Read a post: its text, likes and dislikes (for #1 the goal vote), then its comments as a tree (replies under "
       "the comment they answer; ids like c31, each with its likes and dislikes). since_id: only the comments after "
       "that one, to read what is new.",
    ru="Прочитать объявление: текст, «нравится» и «не нравится» (для #1 — голосование о цели), затем комментарии "
       "деревом (ответы под комментарием, на который они отвечают; id вида c31, у каждого — «нравится» и «не "
       "нравится»). since_id — только комментарии после указанного, чтобы прочитать новое.",
    properties={
        "post_id": {"type": "integer", "description": "Post number, e.g. 9"},
        "since_id": {"type": "string", "description": "Only the comments after this one: c31 or 31"},
    },
    required=["post_id"],
    modes=THREAD_ONLY,
)
async def post_read(ctx: ToolContext, args: dict[str, Any]) -> str:
    swarm = ctx.swarm
    post_id = args["post_id"]
    post = swarm.forum.get(post_id)
    since_id = _comment_id(args["since_id"], "since_id") if args.get("since_id") not in (None, "") else 0
    if since_id and _post_of_comment(swarm, since_id) != post_id:
        raise ToolError(f"No comment c{since_id} on #{post_id}")
    now = time.time()
    edited = f" (edited {item_time(post['edited_at'], now)})" if post.get("edited_at") else ""
    voters = _voters(swarm, f"p{post_id}")
    likes = [v for v, value in voters if value > 0]
    dislikes = [v for v, value in voters if value < 0]
    if post_id == 1:
        counts = _counts(post)
    else:
        counts = (f"likes {post.get('likes', 0)}" + (f" ({_names(likes)})" if likes else "")
                  + f", dislikes {post.get('dislikes', 0)}" + (f" ({_names(dislikes)})" if dislikes else ""))
    vote = _my_votes(swarm, ctx.actor, [f"p{post_id}"]).get(f"p{post_id}")
    head = (f"#{post_id} \"{post['title']}\" by {_author(post.get('author'))}, "
            f"{item_time(post.get('created_at') or now, now)}{edited}; {counts}"
            + (f"; you: {_value_word(vote)}" if vote else ""))
    lines = [head]
    if (post.get("text") or "").strip():
        lines.append(post["text"])
    if post_id == 1:
        lines.extend(_goal_vote_part(ctx))
    total = int(post.get("comments") or 0)
    after = total if not since_id else int(swarm.db.scalar(
        "SELECT COUNT(*) FROM post_comments WHERE post_id = ? AND id > ?", (post_id, since_id)) or 0)
    comments = swarm.forum.comments(post_id, since_id=since_id, limit=COMMENTS_MAX) if after else []
    if since_id:
        lines.append(f"Comments after c{since_id} ({after} of {total})" + (":" if comments else "."))
    elif total:
        lines.append(f"Comments ({total}):")
    else:
        lines.append("No comments yet.")
    if comments:
        mine = _my_votes(swarm, ctx.actor, [f"c{c['id']}" for c in comments])
        lines.extend(_comment_lines(comments, mine))
        if after > len(comments):
            lines.append(f"… and {after - len(comments)} more: `post_read` post_id={post_id} "
                         f"since_id=c{comments[-1]['id']}.")
    return "\n".join(lines)


def _similar_entry(post: dict, language: str) -> str:
    title = f"«{post['title']}»" if language == "ru" else f"\"{post['title']}\""
    return f"#{post['id']} {title}" + (f" ({post['author']})" if post.get("author") else "")


@tool(
    "post_create",
    en="Publish a post to the feed, one topic per post: a finding, a result, an intention, a question or a proposal. "
       "Name a member as @First Last to wake them. Look at the feed first: if a post on the same topic exists, "
       "comment there instead.",
    ru="Опубликовать объявление в ленте, одна тема — одно объявление: находка, результат, намерение, вопрос или "
       "предложение. Чтобы разбудить участника, упомяни его как @Имя Фамилия. Сначала посмотри ленту: если "
       "объявление на ту же тему уже есть, напиши комментарий там.",
    properties={
        "title": {"type": "string", "description": "One line, at most 200 characters"},
        "text": {"type": "string", "description": "Markdown, at most 20000 characters"},
    },
    required=["title", "text"],
    allow_empty=("text",),
    modes=THREAD_ONLY,
)
async def post_create(ctx: ToolContext, args: dict[str, Any]) -> str:
    forum = ctx.swarm.forum
    post = forum.create(ctx.actor, args["title"], args.get("text") or "")
    result = f"Posted #{post['id']} \"{post['title']}\"."
    if not ctx.is_user:  # as with task_create, the user is not told about look-alikes
        similar = forum.similar_open(post)
        if similar:
            listed = ", ".join(_similar_entry(p, ctx.lang) for p in similar)
            result += " " + prompts.t(ctx.lang, "similar_posts", posts=listed)
    return result


@tool(
    "post_update",
    en="Edit your post: a new title and/or the whole new text; those who follow the post are told. A post cannot be "
       "deleted: if it is obsolete, say so in its text.",
    ru="Изменить свое объявление: новый title и/или весь новый text; те, кто следит за объявлением, получат "
       "уведомление. Объявление нельзя удалить: если оно устарело, напиши об этом в его тексте.",
    properties={
        "post_id": {"type": "integer", "description": "Post number, e.g. 9"},
        "title": {"type": "string", "description": "One line, at most 200 characters"},
        "text": {"type": "string", "description": "Markdown, at most 20000 characters"},
    },
    required=["post_id"],
    modes=THREAD_ONLY,
)
async def post_update(ctx: ToolContext, args: dict[str, Any]) -> str:
    forum = ctx.swarm.forum
    post_id = args["post_id"]
    before = forum.get(post_id)
    # "" is no value: models send it for an argument they leave out, and it must not wipe a text
    after = forum.update(ctx.actor, post_id, title=args.get("title") or None, text=args.get("text") or None)
    changed = [field for field in ("title", "text") if before.get(field) != after.get(field)]
    if not changed:
        return f"#{post_id} is unchanged."
    return f"Updated #{post_id} \"{after['title']}\" ({', '.join(changed)})."


@tool(
    "comment",
    en="Comment on a post: discuss it, answer a question, report what you did about it. reply_to: the comment you "
       "answer, whose author is then told. To only agree or disagree, vote instead: a vote wakes no one. Name a "
       "member as @First Last to wake them.",
    ru="Прокомментировать объявление: обсудить его, ответить на вопрос, сообщить, что ты сделал по нему. reply_to — "
       "комментарий, на который ты отвечаешь; его автор получит уведомление. Чтобы просто согласиться или не "
       "согласиться, проголосуй: голос никого не будит. Чтобы разбудить участника, упомяни его как @Имя Фамилия.",
    properties={
        "post_id": {"type": "integer", "description": "Post number, e.g. 9"},
        "text": {"type": "string", "description": "Markdown, at most 20000 characters"},
        "reply_to": {"type": "string", "description": "The comment you answer: c31 or 31"},
    },
    required=["post_id", "text"],
    modes=THREAD_ONLY,
)
async def comment(ctx: ToolContext, args: dict[str, Any]) -> str:
    swarm = ctx.swarm
    post_id = args["post_id"]
    raw = args.get("reply_to")
    reply_to = _comment_id(raw) if raw not in (None, "") and str(raw).strip() not in ("0", "") else None
    made = swarm.forum.comment(ctx.actor, post_id, args["text"], reply_to)
    title = swarm.forum.get(post_id)["title"]
    if reply_to is None:
        return f"Commented c{made['id']} on #{post_id} \"{title}\"."
    author = swarm.db.scalar("SELECT author FROM post_comments WHERE id = ?", (reply_to,))
    return f"Replied c{made['id']} to c{reply_to} ({_author(author)}) on #{post_id} \"{title}\"."


@tool(
    "vote",
    en="Like or dislike a post or a comment (none withdraws your vote). A vote wakes no one and costs no one a turn: "
       "use it instead of a comment that only agrees or disagrees. On #1 (\"Goal reached?\") the AI agents' votes "
       "decide whether the goal is reached, under the session's rule (#1 states it): like #1 only after checking the "
       "result yourself against the goal, otherwise dislike it and say in a comment what is missing; change your vote "
       "when the situation changes. You cannot vote on your own posts and comments.",
    ru="Отметить объявление или комментарий: like («нравится») или dislike («не нравится»); none снимает твой голос. "
       "Голос никого не будит и никому не стоит хода: используй его вместо комментария, который только соглашается "
       "или не соглашается. На #1 («Цель достигнута?») голоса ИИ-агентов роя решают, достигнута ли цель, по правилу "
       "сессии (оно указано в #1): ставь like на #1 только после того, как сам проверил результат по цели, иначе "
       "ставь dislike и напиши в комментарии, чего не хватает; меняй голос, когда положение меняется. За свои "
       "объявления и комментарии голосовать нельзя.",
    properties={
        "target": {"type": "string", "description": "#5 or p5 for a post, c31 for a comment"},
        "value": {"type": "string", "enum": ["like", "dislike", "none"],
                  "description": "like, dislike or none (withdraw your vote)"},
    },
    required=["target", "value"],
    modes=THREAD_ONLY,
)
async def vote(ctx: ToolContext, args: dict[str, Any]) -> str:
    swarm = ctx.swarm
    target, value = _vote_target(args["target"]), args["value"]
    before = swarm.db.scalar("SELECT value FROM votes WHERE target = ? AND voter = ?", (target, ctx.actor))
    result = swarm.forum.vote(ctx.actor, target, value)
    shown = f"#{target[1:]}" if target.startswith("p") else f"{target} on #{_post_of_comment(swarm, int(target[1:]))}"
    counts = f"({_counts(result)})"
    if value == "none":
        line = f"You have no vote on {shown}." if before is None else f"Withdrew your vote on {shown} {counts}."
    elif before is not None and (int(before) > 0) == (value == "like"):
        line = f"You already {value} {shown} {counts}."
    else:
        line = f"{'Liked' if value == 'like' else 'Disliked'} {shown} {counts}."
    if target != "p1":
        return line
    tally = result.get("goal_vote") or swarm.forum.goal_vote()
    extra = vote_line(ctx.lang, tally) if tally else ""
    if result.get("decided"):
        extra += " The goal is reached by the vote."
    if swarm.wrapping_up:
        extra += " (Wrap-up mode: a like on #1 means \"finish with what we have\".)"
    if ctx.kind != AgentKind.LLM:
        extra += " Your vote is shown, not counted: only the swarm's AI agents' votes count."
    return line + ("\n" + extra.strip() if extra.strip() else "")


# --- subscriptions ---------------------------------------------------------------------------------------------------

def _by_text(by: str, active: set[str] | None = None) -> str:
    if not by:
        return "anyone"
    if by == USER:
        return "the user"
    if active is not None and by not in active:
        return f"{by} (a former member)"
    return by


def _path_text(swarm, target: str) -> str:
    """A file subscription's path; a folder ends with "/"."""
    if not target:
        return "the whole workspace"
    root = getattr(getattr(swarm, "workspace", None), "root", None)
    try:
        folder = root is not None and (Path(root) / target).is_dir()
    except OSError:
        folder = False
    return target + "/" if folder else target


def _post_title(swarm, target: str) -> str:
    try:
        return f" \"{swarm.forum.get(int(target))['title']}\""
    except (ToolError, ValueError):
        return ""   # #1 and #2 before the first goal: their rows wait for the posts


def _short(swarm, sub: dict, with_by: bool = True) -> str:
    """What a subscription follows, in a few words ("comments and edits of #9")."""
    kind, target = sub["kind"], sub.get("target") or ""
    by = f" by {_by_text(sub.get('by') or '')}" if with_by and sub.get("by") else ""
    if kind == "feed":
        return f"new posts{by}"
    if kind == "post":
        return f"comments and edits of #{target}{by}"
    if kind == "file":
        where = "in the whole workspace" if not target else f"under {_path_text(swarm, target)}"
        return f"changes {where}{by}"
    return f"reactions to your posts and comments{by}"


def _long(swarm, sub: dict) -> str:
    """What a new subscription follows, from whom."""
    kind, by = sub["kind"], _by_text(sub.get("by") or "")
    if kind == "post":
        return f"comments and edits of #{sub['target']}{_post_title(swarm, sub['target'])} by {by}"
    if kind == "reactions":
        return "reactions to your posts and comments" + (f" by {by}" if sub.get("by") else "")
    return f"{_short(swarm, sub, with_by=False)} by {by}"


def _votes_note(sub: dict) -> str:
    return " (votes always come in your digest)" if sub["kind"] == "reactions" and sub.get("mode") == "wake" else ""


def _listed(swarm, sub: dict, active: set[str]) -> str:
    kind, target, by = sub["kind"], sub.get("target") or "", _by_text(sub.get("by") or "", active)
    if kind == "feed":
        head, what = "feed", f"new posts by {by}"
    elif kind == "post":
        head, what = f"post #{target}{_post_title(swarm, target)}", f"comments and edits by {by}"
    elif kind == "file":
        head, what = f"file {_path_text(swarm, target)}", f"changes by {by}"
    else:
        head, what = "reactions", f"comments, replies, edits and votes on your posts and comments, by {by}"
    note = ""
    if kind == "feed" and not sub.get("by"):
        note = " (automatic: it cannot be removed, only its mode changed)" if sub.get("auto") else \
            " (it cannot be removed, only its mode changed)"
    elif sub.get("auto"):
        note = " (automatic)"
    return f"s{sub['id']} {head}: {what}, {sub.get('mode')}{note}"


@tool(
    "subscriptions",
    en="List your subscriptions with their ids (s12): what wakes you at once (wake) and what is collected for one "
       "wake-up (digest).",
    ru="Показать свои подписки с их id (s12): что будит тебя сразу (wake) и что собирается к одному пробуждению "
       "(digest).",
    audience=("llm", "external"),
    modes=THREAD_ONLY,
)
async def subscriptions(ctx: ToolContext, args: dict[str, Any]) -> str:
    swarm = ctx.swarm
    rows = swarm.forum.subscriptions(ctx.actor)
    if not rows:
        return "You have no subscriptions."
    active = {m["name"] for m in swarm.members()}
    lines = [f"Your subscriptions ({len(rows)}):", *(_listed(swarm, sub, active) for sub in rows)]
    lines.append(f"Digest items come with your next wake-up, and while you are idle at the latest about "
                 f"{fmt_duration(swarm.config.agents.digest_seconds)} after the oldest one; votes never wake.")
    return "\n".join(lines)


@tool(
    "subscribe",
    en="Follow something, or change the mode of a subscription you have (the same what, target and by). You follow "
       "the whole feed and the reactions to your posts and comments from the start; follow a post to get its "
       "comments, and use by to follow one member. Votes never wake; @mentions of you always wake you, without a "
       "subscription. A file subscription sees write_file and edit_file edits and the changes that shell commands "
       "and background jobs commit; who made a shell or job change is only a guess (git cannot tell who wrote a "
       "file), and with git off in the workspace only write_file and edit_file edits are seen.",
    ru="Подписаться на что-то или сменить режим своей подписки (те же what, target и by). На всю ленту и на реакции к "
       "твоим объявлениям и комментариям ты подписан с самого начала; подпишись на объявление, чтобы получать его "
       "комментарии, а by позволяет следить за одним участником. Голоса никогда не будят; @упоминания тебя будят "
       "всегда, без подписки. Подписка на файлы видит правки через write_file и edit_file и изменения, которые "
       "фиксируют команды shell и фоновые процессы; кто сделал изменение из shell или фонового процесса, "
       "определяется лишь приблизительно (git не знает, кто написал файл), а при выключенном git в рабочей папке "
       "видны только правки через write_file и edit_file.",
    properties={
        "what": {"type": "string", "enum": ["feed", "post", "file", "reactions"],
                 "description": "feed (new posts), post (comments and edits of one post), file (changes under a "
                                "workspace path) or reactions (to your posts and comments)"},
        "target": {"type": "string", "description": "post: a post number (#9 or 9); file: a workspace path, a folder "
                                                    "covering what is under it (none: the whole workspace)"},
        "by": {"type": "string", "description": "Only what this member does (\"user\": the user)"},
        "mode": {"type": "string", "enum": ["wake", "digest"],
                 "description": "wake (default: wakes you at once) or digest (collected for one wake-up)"},
    },
    required=["what"],
    audience=("llm", "external"),
    modes=THREAD_ONLY,
)
async def subscribe(ctx: ToolContext, args: dict[str, Any]) -> str:
    swarm = ctx.swarm
    what = args["what"]
    target = args.get("target")
    if what == "post" and target not in (None, ""):
        target = _post_target(target)
    sub = swarm.forum.subscribe(ctx.actor, what, target or None, args.get("by") or None, args.get("mode") or "wake")
    op, note = sub.get("op"), _votes_note(sub)
    if op == "created":
        return f"Subscribed s{sub['id']}: {_long(swarm, sub)}, {sub['mode']}{note}."
    if op == "changed":
        return f"s{sub['id']} ({_short(swarm, sub)}): mode changed to {sub['mode']}{note}."
    return f"You already have s{sub['id']} ({_short(swarm, sub)}), {sub['mode']}{note}."


@tool(
    "unsubscribe",
    en="Remove one of your subscriptions by its id (subscriptions lists them). The subscription to the whole feed "
       "cannot be removed: change its mode instead.",
    ru="Удалить свою подписку по ее id (их показывает subscriptions). Подписку на всю ленту удалить нельзя: вместо "
       "этого смени ее режим.",
    properties={"subscription_id": {"type": "string", "description": "s12 or 12"}},
    required=["subscription_id"],
    audience=("llm", "external"),
    modes=THREAD_ONLY,
)
async def unsubscribe(ctx: ToolContext, args: dict[str, Any]) -> str:
    swarm = ctx.swarm
    sub_id = _sub_id(args["subscription_id"])
    sub = next((s for s in swarm.forum.subscriptions(ctx.actor) if s["id"] == sub_id), None)
    swarm.forum.unsubscribe(ctx.actor, sub_id)   # its refusals: another member's id, the feed row
    return f"Unsubscribed s{sub_id}" + (f" ({_short(swarm, sub)})." if sub is not None else ".")
