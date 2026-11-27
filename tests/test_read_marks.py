"""Read before write in the forum (docs/SPEC.md §22.11): the read marks of posts and comments, what moves them (reads,
writes, refusals, rendered notifications, the kickoff), the checks of `comment`, `post_update` and `post_create`, the
refusals and their size, and the budgets of `post_read` and `feed`. A real thread Swarm; ScriptedLLM, no network."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from dreamteam import prompts
from dreamteam import swarm as swarm_module
from dreamteam.config import Config
from dreamteam.db import Database
from dreamteam.forum import GOAL_POST, PLAN_POST, REFUSAL_ITEMS_MIN, UNREAD_TEXTS, Forum, post_version
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.mailbox import sent_as_seen_by
from dreamteam.models import USER, AgentKind, ReadRefusal, ToolError
from dreamteam.swarm import READ_KEY, Swarm
from dreamteam.tools import ToolContext, call_tool
from dreamteam.workspace import REFUSAL_RESERVE
from helpers.forum_reads import read_all

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

ANNA, BOB, CARL, DORA, EVE = "Anna Smith", "Bob Stone", "Carl Brown", "Dora White", "Eve Black"


def make_config(tmp_path: Path, mode: str = "thread", language: str = "en", start_agents: int = 1) -> Config:
    raw: dict[str, Any] = {
        "swarm": {"name": "Read swarm", "language": language, "start_agents": start_agents, "max_agents": 6,
                  "max_cost_usd": 0, "mode": mode, "clarify": False},
        "agents": {"stagger_seconds": 0, "idle_nudge_seconds": 3600, "max_steps_per_turn": 12},
        "workspace": {"git": False},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
    }
    return Config.from_dict(tmp_path / "project", raw)


@pytest.fixture
def make_swarm(tmp_path):
    made: list[Swarm] = []

    def factory(**kw: Any) -> Swarm:
        made.append(Swarm(config=make_config(tmp_path / f"s{len(made)}", **kw)))
        return made[-1]

    yield factory
    for sw in made:
        sw.close()


@pytest.fixture
def sw(make_swarm):
    return make_swarm()


def add(sw: Swarm, name: str, kind: str = AgentKind.LLM, language: str | None = None) -> str:
    return sw._create_member(USER, "Generalist", name=name, kind=kind, language=language)


def set_goal(sw: Swarm, title: str = "Build the parser", description: str = "A fast one.") -> dict:
    goal = {"title": title, "description": description, "status": "active", "summary": "", "set_by": USER,
            "updated_at": time.time()}
    sw.db.set("goal", goal)
    sw.forum.ensure_system_posts(goal)
    return goal


def with_budget(sw: Swarm, chars: int) -> None:
    sw.config = dataclasses.replace(sw.config, agents=dataclasses.replace(sw.config.agents, tool_output_chars=chars))


def marks(sw: Swarm, member: str) -> dict[str, str | None]:
    return {r["target"]: r["version"] for r in sw.db.all("SELECT target, version FROM read_marks WHERE member = ?",
                                                          (member,))}


def version(sw: Swarm, post_id: int) -> str:
    return post_version(sw.forum.get(post_id))


async def tool(sw: Swarm, actor: str, name: str, args: dict | None = None) -> tuple[str, bool]:
    return await call_tool(ToolContext(sw, actor), name, args or {})


async def ok(sw: Swarm, actor: str, name: str, args: dict | None = None) -> str:
    text, is_error = await tool(sw, actor, name, args)
    assert not is_error, text
    return text


async def refused(sw: Swarm, actor: str, name: str, args: dict | None = None) -> str:
    text, is_error = await tool(sw, actor, name, args)
    assert is_error and text.startswith("Error: Not "), text
    return text.removeprefix("Error: ")


def counts(sw: Swarm) -> tuple[int, int, int, int]:
    """Comments, posts, forum events and notifications: what a refused write must not add to."""
    forum_events = [e for e in sw.bus.recent(limit=10000) if e["type"].startswith("post.")]
    return (int(sw.db.scalar("SELECT COUNT(*) FROM post_comments")), int(sw.db.scalar("SELECT COUNT(*) FROM posts")),
            len(forum_events), int(sw.db.scalar("SELECT COUNT(*) FROM notifications")))


# ------------------------------------------------------------------------------------------------ the schema
def test_read_marks_are_a_table_of_thread_databases_only(make_swarm, tmp_path):
    def tables(db: Database) -> set[str]:
        return {r[0] for r in db.all("SELECT name FROM sqlite_master WHERE type = 'table'")}

    assert "read_marks" in tables(make_swarm().db)
    assert "read_marks" not in tables(make_swarm(mode="board").db)
    path = tmp_path / "old.db"
    Database(path, forum=True).close()
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE read_marks")   # a thread database made before the table existed
    conn.commit()
    conn.close()
    db = Database(path)
    try:
        assert "read_marks" not in tables(db)
        db.ensure_forum()   # a thread swarm runs it at every open
        assert "read_marks" in tables(db)
    finally:
        db.close()


def test_post_version_is_the_content(sw):
    add(sw, ANNA), add(sw, BOB)
    base = post_version({"title": "Plan", "text": "v1"})
    assert re.fullmatch(r"[0-9a-f]{16}", base)
    assert base != post_version({"title": "Plan2", "text": "v1"}) != post_version({"title": "Plan", "text": "v2"})
    set_goal(sw)
    one, two = version(sw, GOAL_POST), version(sw, PLAN_POST)
    sw.forum.vote(BOB, "p1", "dislike")
    sw.forum.reset_goal_votes(None, "new_goal")
    sw.forum.set_plan_asker(ANNA)
    assert (version(sw, GOAL_POST), version(sw, PLAN_POST)) == (one, two)   # votes and #2's data: no change
    set_goal(sw, "Build the lexer")
    assert version(sw, GOAL_POST) != one and version(sw, PLAN_POST) != two   # a new goal's rewrite


# ------------------------------------------------------------------------------------------------ comment
async def test_a_comment_is_refused_until_the_news_is_read(sw):
    add(sw, ANNA), add(sw, BOB)
    set_goal(sw)
    await ok(sw, ANNA, "post_read", {"post_id": 2})
    await ok(sw, BOB, "post_read", {"post_id": 2})
    assert await ok(sw, ANNA, "comment", {"post_id": 2, "text": "I take the lexer."}) == \
        'Commented c1 on #2 "Let\'s plan the work".'
    before = counts(sw)
    text = await refused(sw, BOB, "comment", {"post_id": 2, "text": "I take the lexer."})
    first = text.splitlines()[0]
    assert first == UNREAD_TEXTS["en"]["comment_unread"].format(post=2, title="Let's plan the work",
                                                                 news="unread comments by others: 1")
    assert text.splitlines()[1] == "Unread comments (1):"
    assert text.splitlines()[2].startswith(f"c1 {ANNA}, ") and text.endswith(": I take the lexer.")
    assert counts(sw) == before   # nothing written but the marks
    assert marks(sw, BOB)["c1"] is None and "c1" in marks(sw, BOB)
    assert await ok(sw, BOB, "comment", {"post_id": 2, "text": "Then I take the parser."}) == \
        'Commented c2 on #2 "Let\'s plan the work".'
    # the author's edit of its post blocks a comment, with the new text shown
    sw.forum.create(ANNA, "Lexer plan", "Tokens first.")
    await ok(sw, BOB, "post_read", {"post_id": 3})
    await ok(sw, ANNA, "post_update", {"post_id": 3, "text": "Regexes first."})
    text = await refused(sw, BOB, "comment", {"post_id": 3, "text": "Fine."})
    assert "(the post was edited)" in text.splitlines()[0]
    assert text.splitlines()[1].startswith(f'#3 "Lexer plan" by {ANNA}, ') and text.splitlines()[2] == "Regexes first."
    assert marks(sw, BOB)["p3"] == version(sw, 3)
    await ok(sw, BOB, "comment", {"post_id": 3, "text": "Fine."})


async def test_the_user_blocks_a_member_but_is_never_checked_and_own_comments_never_block(sw):
    add(sw, ANNA), add(sw, BOB)
    set_goal(sw)
    await ok(sw, ANNA, "post_read", {"post_id": 2})
    sw.forum.comment(USER, 2, "Use Python 3.12.")          # USER: never refused, though it read nothing
    text = await refused(sw, ANNA, "comment", {"post_id": 2, "text": "I take the lexer."})
    assert "Use Python 3.12." in text and "c1 user, " in text
    await ok(sw, ANNA, "comment", {"post_id": 2, "text": "I take the lexer."})
    await ok(sw, ANNA, "comment", {"post_id": 2, "text": "And the tests."})   # its own comment never blocks it
    assert await ok(sw, USER, "comment", {"post_id": 2, "text": "Thanks."})
    assert await ok(sw, USER, "post_create", {"title": "Style", "text": "Type hints."})
    assert not marks(sw, USER)
    # votes and subscriptions are never checked
    assert await ok(sw, BOB, "vote", {"target": "c1", "value": "like"})
    created = await ok(sw, BOB, "subscribe", {"what": "post", "target": "#3"})
    assert await ok(sw, BOB, "unsubscribe", {"subscription_id": created.split(":")[0].removeprefix("Subscribed ")})


async def test_seen_as_marks_what_the_client_wrote_or_read_for_its_members(sw):
    add(sw, ANNA, kind=AgentKind.EXTERNAL), add(sw, BOB, kind=AgentKind.EXTERNAL), add(sw, CARL)
    set_goal(sw)
    for name in (ANNA, BOB):
        await ok(sw, name, "post_read", {"post_id": 2})
    with sent_as_seen_by({ANNA}, BOB):   # a client acting as Anna and Bob writes as Bob
        await ok(sw, BOB, "comment", {"post_id": 2, "text": "I take the lexer."})
        await ok(sw, BOB, "post_create", {"title": "Lexer", "text": "Tokens."})
        await ok(sw, BOB, "post_update", {"post_id": 3, "text": "Tokens first."})
    assert marks(sw, ANNA)["c1"] is None and marks(sw, ANNA)["p3"] == version(sw, 3)
    with sent_as_seen_by({ANNA}, USER):   # ... and as the user
        sw.forum.comment(USER, 3, "Looks good.")
        sw.forum.create(USER, "Docs", "Later.")
    assert "c2" in marks(sw, ANNA) and marks(sw, ANNA)["p4"] == version(sw, 4)
    # the association is gone: the marks it wrote stay, and Anna passes at once
    await ok(sw, ANNA, "comment", {"post_id": 3, "text": "Agreed."})
    await ok(sw, ANNA, "comment", {"post_id": 2, "text": "And the docs."})
    await ok(sw, ANNA, "post_create", {"title": "Tests", "text": ""})
    # a post_read by the client as Bob marks for Anna too
    await ok(sw, CARL, "post_read", {"post_id": 2})
    await ok(sw, CARL, "comment", {"post_id": 2, "text": "I take the parser."})
    with sent_as_seen_by({ANNA}, BOB):
        await ok(sw, BOB, "post_read", {"post_id": 2})
    assert {f"c{c['id']}" for c in sw.forum.comments(2)} <= set(marks(sw, ANNA))
    await ok(sw, ANNA, "comment", {"post_id": 2, "text": "Good."})


# ------------------------------------------------------------------------------------------------ reply_to
async def test_a_reply_is_checked_against_its_thread_and_an_edit(sw):
    for name in (ANNA, BOB, CARL, DORA):
        add(sw, name)
    sw.forum.create(ANNA, "Parser plan", "Recursive descent.")
    for name in (BOB, CARL):
        await ok(sw, name, "post_read", {"post_id": 3})
    sw.forum.comment(CARL, 3, "Which grammar?")                               # c1
    await ok(sw, BOB, "post_read", {"post_id": 3})
    sw.forum.comment(CARL, 3, "The JSON one, I guess.", reply_to=1)            # c2, under c1
    text = await refused(sw, BOB, "comment", {"post_id": 3, "text": "Yes, JSON.", "reply_to": "c1"})
    assert "(unread comments in the thread of c1: 1)" in text.splitlines()[0]
    assert "The JSON one" in text and "Which grammar?" not in text
    await ok(sw, BOB, "comment", {"post_id": 3, "text": "Yes, JSON.", "reply_to": "c1"})   # c3
    read_all(sw, CARL)
    sw.forum.comment(CARL, 3, "I take the lexer.")                             # c4: a new top-level comment
    sw.forum.comment(CARL, 3, "Starting now.", reply_to=4)                     # c5: a reply elsewhere
    # neither blocks a reply in c1's thread: a claim sent as a reply passes an unread top-level claim
    await ok(sw, BOB, "comment", {"post_id": 3, "text": "And the AST?", "reply_to": "c1"})
    text = await refused(sw, BOB, "comment", {"post_id": 3, "text": "Me too.", "reply_to": "c4"})   # cN unread
    assert "(unread comments in the thread of c4: 2)" in text.splitlines()[0]
    # an edit of a post read in an earlier version blocks a reply; a post never read does not
    sw.forum.mark_read(ANNA, {f"c{c['id']}": None for c in sw.forum.comments(3)})
    sw.forum.update(ANNA, 3, text="Recursive descent, with a Pratt parser for expressions.")
    text = await refused(sw, BOB, "comment", {"post_id": 3, "text": "Nice.", "reply_to": "c1"})
    assert text.splitlines()[0].count("(the post was edited)") == 1 and "Pratt parser" in text
    await ok(sw, BOB, "comment", {"post_id": 3, "text": "Nice.", "reply_to": "c1"})
    sw.forum.mark_read(DORA, {f"c{c['id']}": None for c in sw.forum.comments(3)})
    assert "p3" not in marks(sw, DORA)
    await ok(sw, DORA, "comment", {"post_id": 3, "text": "Agreed.", "reply_to": "c1"})


# ------------------------------------------------------------------------------------------------ post_update
async def test_post_update_is_refused_after_unread_comments_and_the_users_edit(sw):
    add(sw, ANNA), add(sw, BOB)
    sw.forum.create(ANNA, "Plan", "v1")
    await ok(sw, BOB, "post_read", {"post_id": 3})
    sw.forum.comment(BOB, 3, "This will not scale.")
    text = await refused(sw, ANNA, "post_update", {"post_id": 3, "text": "v2"})
    assert text.splitlines()[0] == UNREAD_TEXTS["en"]["update_unread"].format(
        post=3, title="Plan", news="unread comments by others: 1")
    assert "This will not scale." in text and sw.forum.get(3)["text"] == "v1"
    assert await ok(sw, ANNA, "post_update", {"post_id": 3, "text": "v2"}) == 'Updated #3 "Plan" (text).'
    sw.forum.update(USER, 3, text="v3 by the user")
    assert await ok(sw, ANNA, "post_update", {"post_id": 3, "text": "v3 by the user"}) == "#3 is unchanged."
    text = await refused(sw, ANNA, "post_update", {"post_id": 3, "text": "v4"})
    assert "(the post was edited)" in text.splitlines()[0] and "v3 by the user" in text
    await ok(sw, ANNA, "post_update", {"post_id": 3, "text": "v4"})


# ------------------------------------------------------------------------------------------------ post_create
async def test_post_create_is_refused_while_the_feed_has_unseen_posts(sw):
    add(sw, ANNA), add(sw, BOB)
    set_goal(sw)
    await ok(sw, ANNA, "post_create", {"title": "Lexer", "text": ""})   # #1 and #2 never count
    read_all(sw, BOB)
    sw.forum.create(BOB, "Parser", "AST.")
    text = await refused(sw, ANNA, "post_create", {"title": "Tests", "text": ""})
    lines = text.splitlines()
    assert lines[0] == UNREAD_TEXTS["en"]["create_unread"].format(n=1)
    assert lines[1] == "Posts you have not seen (1), newest first:"
    assert re.fullmatch(rf'#4 "Parser" — {BOB}, \d\d:\d\d', lines[2])
    assert marks(sw, ANNA)["p4"] is None
    await ok(sw, ANNA, "post_create", {"title": "Tests", "text": ""})
    sw.forum.create(USER, "Docs", "")                                  # the user's posts count too
    await refused(sw, ANNA, "post_create", {"title": "More", "text": ""})
    # a feed listing, an f_post shown and post_read each count as seeing a post
    read_all(sw, BOB)
    sw.forum.create(BOB, "One", "")
    sw.render_wake(ANNA)   # what it held before
    await ok(sw, ANNA, "feed")
    await ok(sw, ANNA, "post_create", {"title": "After the feed", "text": ""})
    read_all(sw, BOB)
    sw.forum.create(BOB, "Two", "")
    assert "posted #" in sw.render_wake(ANNA)                          # its digest f_post
    await ok(sw, ANNA, "post_create", {"title": "After the wake-up", "text": ""})
    read_all(sw, BOB)
    post = sw.forum.create(BOB, "Three", "")
    await ok(sw, ANNA, "post_read", {"post_id": post["id"]})
    await ok(sw, ANNA, "post_create", {"title": "After the read", "text": ""})


async def test_a_create_refusal_lists_the_newest_and_marks_the_whole_feed(sw):
    add(sw, ANNA), add(sw, BOB)
    with_budget(sw, 1500)
    made = [sw.forum.create(BOB, f"Post {i} " + "t" * 150, "") for i in range(30)]
    text = await refused(sw, ANNA, "post_create", {"title": "Mine", "text": ""})
    lines = text.splitlines()
    assert len("Error: " + text) <= 1500 - REFUSAL_RESERVE + 7
    listed = [line for line in lines if line.startswith("#")]
    assert 1 <= len(listed) < 30 and listed[0].startswith(f"#{made[-1]['id']} ")   # newest first
    assert lines[-1] == f"… and {30 - len(listed)} older posts you have not seen: `feed` lists them."
    assert all(f"p{p['id']}" in marks(sw, ANNA) for p in made)          # all of them count as seen
    await ok(sw, ANNA, "post_create", {"title": "Mine", "text": ""})


# ------------------------------------------------------------------------------------------------ atomicity
async def test_two_claims_on_one_state_give_one_comment_and_the_check_runs_in_the_write_transaction(sw, monkeypatch):
    add(sw, ANNA), add(sw, BOB)
    set_goal(sw)
    for name in (ANNA, BOB):
        await ok(sw, name, "post_read", {"post_id": 2})
    inside: list[bool] = []
    original = Forum._post_refusal

    def spy(self, member, post_id, key):
        inside.append(self.db.conn.in_transaction)
        return original(self, member, post_id, key)

    monkeypatch.setattr(Forum, "_post_refusal", spy)
    first, second = await asyncio.gather(tool(sw, ANNA, "comment", {"post_id": 2, "text": "I take the lexer."}),
                                         tool(sw, BOB, "comment", {"post_id": 2, "text": "I take the lexer."}))
    assert sorted([first[1], second[1]]) == [False, True] and inside == [True, True]
    assert [c["author"] for c in sw.forum.comments(2)] in ([ANNA], [BOB])
    # a refused write raises after its transaction: the marks of the refusal are committed
    loser = BOB if second[1] else ANNA
    assert "c1" in marks(sw, loser)
    sw.forum.comment(USER, 2, "news")
    with pytest.raises(ReadRefusal):
        sw.forum.comment(loser, 2, "x")
    assert "c2" in marks(sw, loser) and len(sw.forum.comments(2)) == 2


# ----------------------------------------------------------------------------------------- a wait after a refusal
WAIT = {"name": "wait", "arguments": {}}


class Script:
    """Each LLM call answers with the next queued response (none: just `wait`)."""

    def __init__(self) -> None:
        self.todo: dict[str, list[list[dict]]] = {}
        self.calls: dict[str, int] = {}

    def factory(self, profile: Any, agent: str) -> ScriptedLLM:
        def handler(system: str, messages: list[dict], tools: list[dict]) -> dict:
            self.calls[agent] = self.calls.get(agent, 0) + 1
            queue = self.todo.get(agent) or []
            return {"tool_calls": queue.pop(0) if queue else [WAIT]}

        return ScriptedLLM(handler, profile)


async def run_until(predicate: Callable[[], bool], timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached in time")


def results(sw: Swarm, name: str, tool_name: str) -> list[dict]:
    rows = sw.db.all("SELECT msg FROM history WHERE agent = ? ORDER BY seq", (name,))
    return [m for m in (json.loads(r["msg"]) for r in rows) if m.get("role") == "tool" and m.get("name") == tool_name]


@pytest.fixture
def fast_scheduler(monkeypatch):
    monkeypatch.setattr(swarm_module, "SCHEDULER_SECONDS", 0.05)


@pytest.mark.parametrize("language", ["en", "ru"])
async def test_a_wait_after_a_refused_comment_keeps_the_activation_running(tmp_path, fast_scheduler, language):
    script = Script()
    sw = Swarm(config=make_config(tmp_path, language=language), llm_factory=script.factory)
    try:
        await sw.start()
        a = sw.members()[0]["name"]
        sw.set_goal(USER, "Build the parser")
        await run_until(lambda: "p2" in marks(sw, a))
        await asyncio.sleep(0.3)
        assert marks(sw, a).get("p2") == version(sw, PLAN_POST)          # the kickoff carried #1 and #2
        add(sw, EVE, kind=AgentKind.EXTERNAL)
        long = "I have a plan: " + "x" * 300                              # cut in a digest item: not read
        sw.forum.mark_read(EVE, {"p2": version(sw, 2)})
        sw.forum.comment(EVE, PLAN_POST, long)
        claim = {"name": "comment", "arguments": {"post_id": 2, "text": "I take the lexer."}}
        script.todo[a] = [[claim, WAIT], [claim]]
        calls = script.calls[a]
        await ok(sw, USER, "send_message", {"to": [a], "text": "Go on."})
        await run_until(lambda: script.calls[a] >= calls + 3)
        await asyncio.sleep(0.3)
        assert script.calls[a] == calls + 3
        waits = results(sw, a, "wait")
        refusal = [m for m in results(sw, a, "comment") if m["is_error"]]
        assert len(refusal) == 1 and long in refusal[0]["content"]
        held_waits = [m for m in waits if m["is_error"]]
        assert [m["content"] for m in held_waits] == ["Error: " + prompts.t(language, "wait_refused", tool="comment")]
        assert [c["author"] for c in sw.forum.comments(2)] == [EVE, a]   # the retry passed
        assert not waits[-1]["is_error"]                                 # a `wait` alone ends the activation
        kept = marks(sw, a)
        sw.runtimes[a]._compact_without_llm()                            # a compaction keeps the marks
        assert marks(sw, a) == kept and "c1" in kept
    finally:
        await sw.stop()
        sw.close()


# ------------------------------------------------------------------------------------------------ delivery
def test_a_waking_comment_marks_once_shown_only_when_its_text_is_whole(sw):
    add(sw, ANNA), add(sw, BOB)
    sw.forum.create(ANNA, "Plan", "")
    sw.forum.subscribe(ANNA, "post", "3", mode="wake")
    sw.forum.mark_read(BOB, {"p3": version(sw, 3)})
    short = sw.forum.comment(BOB, 3, "x" * 1500)
    assert f"c{short['id']}" not in marks(sw, ANNA)                      # not before it is shown
    sw.render_wake(ANNA)
    assert f"c{short['id']}" in marks(sw, ANNA)
    longer = sw.forum.comment(BOB, 3, "y" * 1501)
    sw.render_wake(ANNA)
    assert f"c{longer['id']}" not in marks(sw, ANNA)


def test_a_digest_item_marks_when_whole_and_among_the_rows_shown(sw):
    add(sw, ANNA), add(sw, BOB)
    set_goal(sw)
    sw.forum.mark_read(BOB, {"p2": version(sw, 2)})
    whole = sw.forum.comment(BOB, 2, "z" * 200)
    cut = sw.forum.comment(BOB, 2, "z" * 201)
    sw.render_wake(ANNA)
    assert f"c{whole['id']}" in marks(sw, ANNA) and f"c{cut['id']}" not in marks(sw, ANNA)
    posts = [sw.forum.create(BOB, f"Note {i}", "short") for i in range(35)]
    sw.render_wake(ANNA)
    shown = [p for p in posts if f"p{p['id']}" in marks(sw, ANNA)]
    assert shown == posts[:swarm_module.DIGEST_SHOWN]                    # the rows left out move nothing
    assert all(marks(sw, ANNA)[f"p{p['id']}"] == version(sw, p["id"]) for p in shown)


def test_an_edit_item_never_moves_a_version_and_a_stale_version_is_applied_by_title(sw):
    add(sw, ANNA), add(sw, BOB)
    post = sw.forum.create(BOB, "Plan", "v1")
    sw.forum.subscribe(ANNA, "post", str(post["id"]), mode="digest")
    sw.forum.update(BOB, post["id"], text="v2")                          # Anna's f_post (v1) is stale now
    sw.render_wake(ANNA)
    assert marks(sw, ANNA) == {f"p{post['id']}": None}
    sw.forum.mark_read(ANNA, {f"p{post['id']}": version(sw, post["id"])})
    sw.forum.update(BOB, post["id"], text="v3")
    row = sw.db.one("SELECT data FROM notifications WHERE recipient = ? AND kind = 'f_edit' ORDER BY id DESC",
                    (ANNA,))
    assert json.loads(row["data"])[READ_KEY] == {f"p{post['id']}": None}
    sw.render_wake(ANNA)
    assert marks(sw, ANNA)[f"p{post['id']}"] == post_version({"title": "Plan", "text": "v2"})


def test_a_current_version_shown_by_any_row_wins_over_an_older_one_whatever_the_order(sw):
    add(sw, ANNA), add(sw, BOB)
    post = sw.forum.create(BOB, "Plan", "v1")
    sw.forum.subscribe(ANNA, "post", str(post["id"]), mode="digest")
    sw.forum.update(BOB, post["id"], text=f"v2 @{ANNA}")                 # a waking mention at the current version
    reads = [json.loads(r["data"])[READ_KEY] for r in sw.db.all(
        "SELECT data FROM notifications WHERE recipient = ? AND delivered_at IS NULL ORDER BY id", (ANNA,))]
    target = f"p{post['id']}"
    shown = {r[target] for r in reads if r.get(target)}
    assert version(sw, post["id"]) in shown and len(shown) == 2           # the digest row still carries v1
    sw.render_wake(ANNA)                                                   # waking rows first, the digest after
    assert marks(sw, ANNA)[target] == version(sw, post["id"])


async def test_wait_for_events_of_an_external_member_and_the_users_renders(sw):
    add(sw, ANNA), add(sw, EVE, kind=AgentKind.EXTERNAL)
    set_goal(sw)
    sw.forum.mark_read(ANNA, {"p2": version(sw, 2)})
    made = sw.forum.comment(ANNA, 2, "Short.")
    text = await ok(sw, EVE, "wait_for_events", {"timeout": 0})
    assert "Short." in text and f"c{made['id']}" in marks(sw, EVE)
    post = sw.forum.create(USER, "Mine", "")
    sw.forum.mark_read(ANNA, {f"p{post['id']}": version(sw, post["id"])})
    answer = sw.forum.comment(ANNA, post["id"], "On it.")                # USER's f_comment
    with sent_as_seen_by({EVE}, USER):
        sw.render_wake(USER)
    assert f"c{answer['id']}" in marks(sw, EVE) and not marks(sw, USER)
    other = sw.forum.comment(ANNA, post["id"], "Done.")
    sw.render_wake(USER)                                                  # no seen_as: nobody's marks
    assert f"c{other['id']}" not in marks(sw, EVE) and not marks(sw, USER)


async def test_the_kickoff_and_the_reopen_notices_mark_what_they_carry(tmp_path):
    sw = Swarm(config=make_config(tmp_path, start_agents=2))
    try:
        await sw.start(run_agents=False)
        names = [m["name"] for m in sw.members()]
        sw.set_goal(USER, "Build the parser", "Fast.")
        for name in names:
            sw.render_wake(name)
            assert marks(sw, name) == {"p1": version(sw, 1), "p2": version(sw, 2)}
        sw.set_goal(USER, "Build the lexer")                               # n_goal_updated_thread
        sw.render_wake(names[0])
        assert marks(sw, names[0]) == {"p1": version(sw, 1), "p2": version(sw, 2)}
        sw.complete_goal_by_user("Done.")
        sw.reopen_goal(USER, "More tests, please.")
        await asyncio.sleep(0.05)
        sw.render_wake(names[0])
        (comment,) = sw.forum.comments(1)
        assert f"c{comment['id']}" in marks(sw, names[0])
    finally:
        await sw.stop()
        sw.close()


# ------------------------------------------------------------------------------------------------ post_read
async def test_post_read_fits_the_budget_and_marks_exactly_what_it_shows(sw):
    add(sw, ANNA), add(sw, BOB)
    with_budget(sw, 2000)
    sw.forum.create(ANNA, "Plan", "Short text.")
    sw.forum.mark_read(BOB, {"p3": version(sw, 3)})
    made = [sw.forum.comment(BOB, 3, f"comment {i} " + "w" * 190) for i in range(30)]
    text = await ok(sw, ANNA, "post_read", {"post_id": 3})
    assert len(text) <= 2000
    shown = [c for c in made if re.search(rf"^c{c['id']} ", text, re.M)]
    assert shown == made[:len(shown)] and 1 <= len(shown) < 30         # an id-ordered prefix
    assert text.splitlines()[-1] == f"… and {30 - len(shown)} more: `post_read` post_id=3 since_id=c{shown[-1]['id']}."
    assert {t for t in marks(sw, ANNA) if t.startswith("c")} == {f"c{c['id']}" for c in shown}
    # a text longer than the budget is cut with its note and counts as read
    sw.forum.create(BOB, "Long", "L" * 5000)
    text = await ok(sw, ANNA, "post_read", {"post_id": 4})
    assert len(text) <= 2000 and re.search(r"^\[text cut at \d+ of 5000 characters\]$", text, re.M)
    assert marks(sw, ANNA)["p4"] == version(sw, 4)


async def page_through(sw: Swarm, member: str, post_id: int, budget: int) -> list[str]:
    pages = [await ok(sw, member, "post_read", {"post_id": post_id})]
    while True:
        last = pages[-1].splitlines()[-1]
        match = re.fullmatch(rf"… and \d+ more: `post_read` post_id={post_id} since_id=(c\d+)\.", last)
        if not match:
            return pages
        assert len(pages) < 50
        pages.append(await ok(sw, member, "post_read", {"post_id": post_id, "since_id": match.group(1)}))


async def test_long_posts_are_paged_to_their_end(sw):
    add(sw, ANNA), add(sw, BOB)
    with_budget(sw, 2000)
    sw.forum.create(ANNA, "Plan", "T" * 1950)                          # a text about the budget's length
    sw.forum.mark_read(BOB, {"p3": version(sw, 3)})
    made = [sw.forum.comment(BOB, 3, f"c-{i} " + "v" * 300) for i in range(8)]
    pages = await page_through(sw, ANNA, 3, 2000)
    assert len(pages) > 1 and all(re.search(r"^c\d+ ", page, re.M) for page in pages)   # each shows a comment
    assert {f"c{c['id']}" for c in made} <= set(marks(sw, ANNA))
    assert "[text unchanged since you read it: 1950 characters]" in pages[1]   # a since_id page: the head only
    sw.forum.create(ANNA, "Huge", "")
    sw.forum.mark_read(BOB, {"p4": version(sw, 4)})
    huge = sw.forum.comment(BOB, 4, "H" * 15000)
    rest = [sw.forum.comment(BOB, 4, f"after {i}") for i in range(3)]
    pages = await page_through(sw, ANNA, 4, 2000)
    assert re.search(r"\[comment cut at \d+ of 15000 characters\]", pages[0]) and len(pages) == 2
    assert {f"c{c['id']}" for c in [huge, *rest]} <= set(marks(sw, ANNA))


async def test_a_refusal_stays_within_the_budget_and_always_shows_one_item(sw):
    add(sw, ANNA), add(sw, BOB)
    sw.forum.create(ANNA, "Plan", "P" * 3000)
    sw.forum.mark_read(BOB, {"p3": version(sw, 3)})
    for i in range(60):
        sw.forum.comment(BOB, 3, f"objection {i} " + "o" * 400)
    text, is_error = await tool(sw, ANNA, "comment", {"post_id": 3, "text": "Hm."})
    assert is_error and len(text) <= sw.config.agents.tool_output_chars - REFUSAL_RESERVE + len("Error: ")
    assert "… and " in text.splitlines()[-1] and "more unread comments" in text.splitlines()[-1]
    with_budget(sw, 1)
    sw.forum.comment(BOB, 3, "one more " + "q" * 3000)
    before = len(marks(sw, ANNA))
    text, is_error = await tool(sw, ANNA, "comment", {"post_id": 3, "text": "Hm."})
    assert is_error and re.search(r"^c\d+ ", text, re.M) and len(marks(sw, ANNA)) > before
    for _ in range(80):                                                 # every refusal moves a mark: retries end
        text, is_error = await tool(sw, ANNA, "comment", {"post_id": 3, "text": "Hm."})
        if not is_error:
            break
    assert not is_error


async def test_a_refusal_with_a_post_block_gives_its_comments_their_share(sw):
    """The comments keep their header and REFUSAL_ITEMS_MIN (a third of the room) while the post text is cut; the
    continuation line is reserved only when not every comment fits."""
    add(sw, ANNA), add(sw, BOB)
    sw.forum.create(BOB, "Plan", "P" * 3000)
    sw.forum.mark_read(ANNA, {"p3": version(sw, 3)})
    sw.forum.update(BOB, 3, text="Q" * 3000)                             # Anna's mark is of an earlier version
    claim = sw.forum.comment(BOB, 3, "I take this task: " + "t" * 400)
    for chars in (1000, 1500, 2000):
        with_budget(sw, chars)
        sw.db.execute("DELETE FROM read_marks WHERE member = ? AND target LIKE 'c%'", (ANNA,))
        text = await refused(sw, ANNA, "comment", {"post_id": 3, "text": "Hm."})
        item = text[text.index(f"\nc{claim['id']} ") + 1:]                 # the last item: its line and cut note
        assert len(item) + 1 >= REFUSAL_ITEMS_MIN, (chars, item)
        cut = re.search(r"\[comment cut at (\d+) of", item)
        assert cut is None or int(cut.group(1)) >= 100, (chars, item)
        assert "more unread comments" not in text                        # one comment: no continuation
        assert len(text) <= chars - REFUSAL_RESERVE                     # `Error: ` is in the reserve
        assert marks(sw, ANNA)[f"c{claim['id']}"] is None and marks(sw, ANNA)["p3"] == version(sw, 3)
        sw.forum.mark_read(ANNA, {"p3": post_version({"title": "Plan", "text": "P" * 3000})})
    with_budget(sw, 4000)                                                # the comment fits whole: the text gets the
    sw.db.execute("DELETE FROM read_marks WHERE member = ? AND target LIKE 'c%'", (ANNA,))   # rest of the room
    text = await refused(sw, ANNA, "comment", {"post_id": 3, "text": "Hm."})
    assert claim["text"] in text and "[comment cut" not in text
    assert 4000 - REFUSAL_RESERVE - 20 <= len(text) <= 4000 - REFUSAL_RESERVE


def test_a_cut_comment_with_line_breaks_stays_within_its_room(sw):
    from dreamteam.tools.forum_tools import comment_prefix

    add(sw, ANNA), add(sw, BOB)
    sw.forum.create(BOB, "Plan", "")
    long = sw.forum.comment(BOB, 3, "a\n" * 300)                         # every line break is indented in the item
    comment = sw.forum.comments(3)[0]
    assert comment["id"] == long["id"]
    for room in (120, 200, 333):
        shown, lines = comment_prefix(sw, ANNA, [comment], room)
        assert shown == [comment] and len(lines) == 1
        assert len(lines[0]) + 1 <= room and "[comment cut at " in lines[0], room


# ------------------------------------------------------------------------------------------------ feed
async def test_a_long_feed_fits_the_budget_and_marks_only_the_posts_shown(sw):
    add(sw, ANNA), add(sw, BOB)
    made = [sw.forum.create(BOB, f"Post {i:03} " + "t" * 180, "") for i in range(100)]
    text = await ok(sw, ANNA, "feed", {"limit": 100})
    assert len(text) <= sw.config.agents.tool_output_chars
    listed = [int(m) for m in re.findall(r"^#(\d+) ", text, re.M)]
    assert 1 <= len(listed) < 100
    assert text.splitlines()[-1] == f"… and {100 - len(listed)} more: `feed` with limit, by, since or query."
    assert {t for t in marks(sw, ANNA)} == {f"p{pid}" for pid in listed}
    left_out = next(p for p in made if p["id"] not in listed)
    text = await refused(sw, ANNA, "post_create", {"title": "Mine", "text": ""})
    assert f"#{left_out['id']} " in text or "older posts you have not seen" in text


# ------------------------------------------------------------------------------------------------ files, retire
async def test_file_marks_never_go_to_read_marks(sw):
    add(sw, ANNA)
    await ok(sw, ANNA, "write_file", {"path": "notes.md", "content": "one\n"})
    await ok(sw, ANNA, "read_file", {"path": "notes.md"})
    assert all(re.fullmatch(r"[pc]\d+", r["target"]) for r in sw.db.all("SELECT target FROM read_marks"))


async def test_retire_drops_the_marks_and_a_welcome_back_keeps_them(sw):
    add(sw, ANNA), add(sw, BOB)
    set_goal(sw)
    await ok(sw, ANNA, "post_read", {"post_id": 2})
    await ok(sw, BOB, "post_read", {"post_id": 2})
    await ok(sw, USER, "join_swarm", {"role": "Reviewer", "name": "Claude Code"})
    await ok(sw, "Claude Code", "post_read", {"post_id": 1})
    kept = marks(sw, "Claude Code")
    await ok(sw, USER, "join_swarm", {"role": "Reviewer", "name": "Claude Code"})   # the same member again
    assert marks(sw, "Claude Code") == kept
    await sw.retire(USER, BOB)
    assert not marks(sw, BOB) and marks(sw, ANNA)
    with pytest.raises(ToolError):
        sw.forum.comment(BOB, 2, "gone")
