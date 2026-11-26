"""The thread mode's delivery (docs/SPEC.md §22.3): who gets which f_* item of a forum or file event, waking or as a
digest item, USER's items, the digest group of the wake-up (§22.6 item 5), digest_deadline and the release of due
digests and settled file items, the file subscriptions' coalescing, and wait_for_events' digest cap. A real thread
Swarm, never started unless the test needs a loop; ScriptedLLM only, no network."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dreamteam import prompts
from dreamteam.agent import AgentRuntime
from dreamteam.config import Config
from dreamteam.forum import GOAL_POST, PLAN_POST
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.mailbox import seen_as, sent_as_seen_by
from dreamteam.models import USER, AgentKind
from dreamteam.swarm import DIGEST_KEY, QUIET_KEY, Swarm
from dreamteam.tools import ToolContext, call_tool
from dreamteam.tools.fmt import STAMP_SPAN, item_time, stamp_range

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

ANNA, BOB, CARL, EVE = "Anna Smith", "Bob Stone", "Carl Brown", "Eve Black"


def make_config(tmp_path: Path, *, agents: dict | None = None, **swarm: Any) -> Config:
    raw: dict[str, Any] = {
        "swarm": {"name": "Delivery", "language": "en", "start_agents": 0, "max_agents": 8, "max_cost_usd": 0,
                  "mode": "thread", "clarify": False, **swarm},
        "agents": {"stagger_seconds": 0, **(agents or {})},
        "workspace": {"git": False},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
    }
    return Config.from_dict(tmp_path / "project", raw)


@pytest.fixture
def make_swarm(tmp_path):
    made: list[Swarm] = []

    def factory(**kw: Any) -> Swarm:
        sw = Swarm(config=make_config(tmp_path / f"s{len(made)}", **kw),
                   llm_factory=lambda profile, agent: ScriptedLLM(lambda *a: "ok", profile))
        made.append(sw)
        return sw

    yield factory
    for sw in made:
        try:
            sw.close()
        except Exception:  # noqa: BLE001 - closed already
            pass


@pytest.fixture
def sw(make_swarm):
    return make_swarm()


def add(sw: Swarm, name: str, kind: str = AgentKind.LLM, language: str | None = None) -> str:
    return sw._create_member(USER, "Generalist", name=name, kind=kind, language=language)


def team(sw: Swarm, *names: str) -> None:
    for name in names:
        add(sw, name)


def set_goal(sw: Swarm, status: str = "active") -> None:
    goal = {"title": "Build the parser", "description": "", "status": status, "summary": "", "set_by": USER,
            "updated_at": time.time()}
    sw.db.set("goal", goal)
    sw.forum.ensure_system_posts(goal)


def rows(sw: Swarm, recipient: str | None = None, kind: str | None = None, undelivered: bool = False) -> list[dict]:
    out = []
    for r in sw.db.all("SELECT * FROM notifications ORDER BY id"):
        item = dict(r)
        item["data"] = json.loads(r["data"] or "{}")
        if recipient is not None and r["recipient"] != recipient:
            continue
        if kind is not None and r["kind"] != kind:
            continue
        if undelivered and r["delivered_at"] is not None:
            continue
        out.append(item)
    return out


def f_rows(sw: Swarm, recipient: str) -> list[dict]:
    return [r for r in rows(sw, recipient) if r["kind"].startswith("f_")]


def digest(row: dict) -> bool:
    return bool(row["data"].get(DIGEST_KEY)) and bool(row["data"].get(QUIET_KEY))


def waking(row: dict) -> bool:
    return QUIET_KEY not in row["data"] and DIGEST_KEY not in row["data"]


def age(sw: Swarm, seconds: float, recipient: str | None = None) -> None:
    """Every undelivered notification (of `recipient`) `seconds` older, and the digest floor with it."""
    sql = "UPDATE notifications SET ts = ts - ? WHERE delivered_at IS NULL"
    params: tuple = (seconds,)
    if recipient is not None:
        sql += " AND recipient = ?"
        params += (recipient,)
    sw.db.execute(sql, params)
    sw._digest_floor -= seconds


def fake_runtime(sw: Swarm, name: str) -> list[int]:
    """An idle runtime stand-in: counts its wakes (the swarm is never started, so no LLM runs)."""
    woken: list[int] = []
    sw.runtimes[name] = SimpleNamespace(wake=lambda: woken.append(1), state="idle", interrupted=False,
                                        stop=lambda: None)
    return woken


async def tool(sw: Swarm, actor: str, name: str, args: dict | None = None) -> tuple[str, bool]:
    """A tool call as the member makes it: an LLM member's runtime serves its frozen tool set."""
    runtime = AgentRuntime(sw, actor) if sw.kind_of(actor) == AgentKind.LLM else None
    return await call_tool(ToolContext(sw, actor, runtime), name, args or {})


# ============================================================================== forum events: who gets what
def test_a_new_post_is_a_digest_item_for_the_others_and_nothing_for_its_author_or_the_user(sw):
    team(sw, ANNA, BOB, CARL)
    sw.forum.create(ANNA, "Parser plan", "x" * 900)
    assert f_rows(sw, ANNA) == [] and f_rows(sw, USER) == []
    for name in (BOB, CARL):
        (row,) = f_rows(sw, name)
        assert row["kind"] == "f_post" and digest(row)
        assert row["text"] == f'{ANNA} posted #3 "Parser plan": ' + "x" * 199 + "…"
    assert not sw.has_pending(BOB)


def test_a_feed_in_wake_wakes_with_the_long_cut_and_a_by_row_wakes_only_for_its_author(sw):
    team(sw, ANNA, BOB, CARL)
    sw.forum.subscribe(BOB, "feed", mode="wake")
    sw.forum.subscribe(CARL, "feed", by=ANNA, mode="wake")
    sw.forum.create(ANNA, "Parser plan", "y" * 2000)
    bob, carl = f_rows(sw, BOB)[0], f_rows(sw, CARL)[0]
    assert waking(bob) and waking(carl) and sw.has_pending(BOB)
    assert bob["text"].endswith("y" * 1499 + "…")
    sw.forum.create(BOB, "Lexer", "z")
    carl_rows = f_rows(sw, CARL)
    assert len(carl_rows) == 2 and digest(carl_rows[1])  # Bob's post: only Carl's feed row in digest matches


def test_a_mention_is_one_waking_item_whatever_the_subscriptions(sw):
    team(sw, ANNA, BOB, CARL)
    sw.forum.unsubscribe(BOB, next(s["id"] for s in sw.forum.subscriptions(BOB) if s["kind"] == "reactions"))
    sw.forum.create(ANNA, "Help", "@Bob Stone and @user, look")
    (bob,) = f_rows(sw, BOB)
    assert bob["kind"] == "f_mention" and waking(bob)
    assert bob["text"].startswith(f'{ANNA} mentioned you on #3 "Help": @Bob Stone and @user, look\nIf it needs')
    (user,) = f_rows(sw, USER)
    assert user["kind"] == "f_mention"
    assert [r["kind"] for r in f_rows(sw, CARL)] == ["f_post"]
    sw.forum.comment(CARL, 3, "@Anna Smith done")
    (anna,) = f_rows(sw, ANNA)  # her reactions row matches too: still one item, the mention
    assert anna["kind"] == "f_mention" and "(c1)" in anna["text"]


def test_comments_follow_post_and_reactions_rows_with_one_item_per_recipient(sw):
    team(sw, ANNA, BOB, CARL, EVE)
    sw.forum.create(ANNA, "Parser plan", "text")
    sw.forum.subscribe(CARL, "post", "#3", mode="wake")
    sw.forum.subscribe(ANNA, "post", "3", mode="wake")
    sw.db.execute("DELETE FROM notifications")
    first = sw.forum.comment(BOB, 3, "a question")
    anna, carl = f_rows(sw, ANNA), f_rows(sw, CARL)
    assert [r["kind"] for r in anna] == ["f_comment"] and waking(anna[0])  # post row (wake) + reactions (digest)
    assert anna[0]["text"] == f'{BOB} commented on #3 "Parser plan" (c{first["id"]}): a question'
    assert [r["kind"] for r in carl] == ["f_comment"] and waking(carl[0])
    assert f_rows(sw, EVE) == []  # a comment belongs to its post, not to the feed
    sw.forum.comment(CARL, 3, "an answer", reply_to=first["id"])
    bob = f_rows(sw, BOB)
    assert [r["kind"] for r in bob] == ["f_reply"] and digest(bob[0])
    assert f"replied to your comment c{first['id']} on #3" in bob[0]["text"]
    assert [r["kind"] for r in f_rows(sw, ANNA)] == ["f_comment", "f_comment"]


def test_comments_on_the_system_posts_reach_their_post_rows(sw):
    team(sw, ANNA, BOB)
    set_goal(sw)
    sw.forum.comment(ANNA, PLAN_POST, "I take the lexer")
    (bob,) = f_rows(sw, BOB)
    assert bob["kind"] == "f_comment" and digest(bob) and "#2" in bob["text"]
    assert f_rows(sw, USER) == []


def test_the_users_posts_and_comments_wake_every_member(sw):
    team(sw, ANNA, BOB)
    add(sw, EVE, AgentKind.EXTERNAL)
    sw.forum.create(USER, "From the user", "please")
    for name in (ANNA, BOB, EVE):
        (row,) = f_rows(sw, name)
        assert row["kind"] == "f_post" and waking(row) and row["text"].startswith("user posted #3")
    post = sw.forum.create(ANNA, "Mine", "")
    sw.forum.comment(USER, post["id"], "nice")
    assert [r["kind"] for r in f_rows(sw, BOB)] == ["f_post", "f_post", "f_comment"]
    assert waking(f_rows(sw, BOB)[-1])


def test_users_items_comments_on_its_posts_and_replies_to_its_comments(sw):
    team(sw, ANNA, BOB)
    set_goal(sw)
    mine = sw.forum.create(USER, "User's post", "")
    sw.forum.comment(ANNA, mine["id"], "on it")
    sw.forum.create(ANNA, "Anna's post", "")
    theirs = sw.forum.comment(USER, GOAL_POST, "check the tests")
    sw.forum.comment(BOB, GOAL_POST, "they pass", reply_to=theirs["id"])
    sw.forum.comment(BOB, GOAL_POST, "unrelated")
    sw.forum.vote(ANNA, f"#{mine['id']}", "like")
    assert [r["kind"] for r in f_rows(sw, USER)] == ["f_comment", "f_reply"]
    assert all(r["data"] == {k: v for k, v in r["data"].items() if not k.startswith("_")}
               for r in f_rows(sw, USER))  # never quiet


def test_votes_never_wake_and_a_withdrawn_vote_tells_nobody(sw):
    team(sw, ANNA, BOB)
    set_goal(sw)
    sw.forum.subscribe(ANNA, "reactions", mode="wake")
    post = sw.forum.create(ANNA, "Parser plan", "")
    sw.forum.vote(BOB, f"#{post['id']}", "like")
    (row,) = f_rows(sw, ANNA)
    assert row["kind"] == "f_vote" and digest(row)
    assert row["text"] == f'{BOB} rated your #{post["id"]} "Parser plan": like.'
    sw.forum.vote(BOB, f"#{post['id']}", "none")
    assert len(f_rows(sw, ANNA)) == 1
    sw.forum.vote(BOB, f"#{post['id']}", "dislike")
    assert [r["text"].rsplit(": ", 1)[1] for r in f_rows(sw, ANNA)] == ["like.", "dislike."]
    comment = sw.forum.comment(ANNA, PLAN_POST, "my part")
    sw.forum.vote(BOB, f"c{comment['id']}", "like")
    assert f_rows(sw, ANNA)[-1]["text"] == f'{BOB} rated your c{comment["id"]} (#2) "Let\'s plan the work": like.'
    before = len(rows(sw))
    sw.forum.vote(BOB, "#1", "like")  # #1 has no author: nobody's reactions
    assert [r for r in rows(sw)[before:] if r["kind"] == "f_vote"] == []


def test_a_vote_in_russian_names_the_value_in_the_recipients_language(sw):
    add(sw, ANNA, language="ru")
    add(sw, BOB)
    post = sw.forum.create(ANNA, "План", "")
    sw.forum.vote(BOB, f"#{post['id']}", "dislike")
    (row,) = f_rows(sw, ANNA)
    assert row["text"] == f"{BOB} оценил(а) твой #{post['id']} «План»: не нравится."


def test_edits_reach_post_rows_and_the_authors_reactions_and_only_new_mentions_fire(sw):
    team(sw, ANNA, BOB, CARL)
    post = sw.forum.create(ANNA, "Parser plan", "@Bob Stone first")
    sw.forum.subscribe(CARL, "post", f"#{post['id']}")
    sw.forum.update(ANNA, post["id"], text="@Bob Stone first, @Carl Brown too")
    assert [r["kind"] for r in f_rows(sw, BOB)] == ["f_mention"]  # the old mention does not fire again
    assert [r["kind"] for r in f_rows(sw, CARL)] == ["f_post", "f_mention"]
    sw.forum.update(USER, post["id"], title="Parser plan v2")
    anna = f_rows(sw, ANNA)
    assert [r["kind"] for r in anna] == ["f_edit"] and digest(anna[0])
    assert anna[0]["text"] == 'user edited #3 "Parser plan v2"; read it with `post_read`.'
    assert f_rows(sw, CARL)[-1]["kind"] == "f_edit" and waking(f_rows(sw, CARL)[-1])
    assert f_rows(sw, USER) == []


def test_the_system_and_the_mcp_clients_other_identities_get_nothing(sw):
    team(sw, ANNA, BOB)
    set_goal(sw)
    assert [r for r in rows(sw) if r["kind"].startswith("f_")] == []  # #1 and #2 written: no fan-out
    sw.forum.reset_goal_votes(None, "new_goal")
    with sent_as_seen_by({BOB}, USER):
        assert seen_as(USER) == {BOB} and seen_as(ANNA) == frozenset()
        sw.forum.create(USER, "As the user", "")
    assert f_rows(sw, BOB) == [] and [r["kind"] for r in f_rows(sw, ANNA)] == ["f_post"]


def test_a_reopen_comment_reaches_only_external_members(sw):
    team(sw, ANNA, BOB)
    add(sw, EVE, AgentKind.EXTERNAL)
    add(sw, CARL, AgentKind.EXTERNAL)
    set_goal(sw)
    sw.forum.reopen(USER, "not yet, @Carl Brown and @Anna Smith")
    assert f_rows(sw, ANNA) == [] and f_rows(sw, BOB) == []
    assert [r["kind"] for r in f_rows(sw, EVE)] == ["f_comment"] and waking(f_rows(sw, EVE)[0])
    assert [r["kind"] for r in f_rows(sw, CARL)] == ["f_mention"]


def test_retired_members_get_nothing_and_by_a_former_name_matches_nothing(sw):
    team(sw, ANNA, BOB, CARL)
    sw.forum.subscribe(CARL, "feed", by=ANNA, mode="wake")
    sw.db.execute("UPDATE agents SET status = 'retired' WHERE name = ?", (BOB,))
    sw.forum.create(ANNA, "Plan", "")
    assert f_rows(sw, BOB) == [] and waking(f_rows(sw, CARL)[0])


def test_a_new_post_waits_for_the_members_own_job(sw, monkeypatch):
    team(sw, ANNA, BOB)
    sw.forum.subscribe(BOB, "feed", mode="wake")
    woken = fake_runtime(sw, BOB)
    monkeypatch.setattr(sw, "waits_for_job", lambda name: name == BOB)
    sw.forum.create(ANNA, "Plan", "")
    (row,) = f_rows(sw, BOB)
    assert waking(row) and sw.has_pending(BOB) and BOB in sw._deferred and woken == []
    sw.forum.create(USER, "From the user", "")  # the user's posts are never deferred
    assert woken == [1]
    post = sw.forum.create(BOB, "Mine", "")
    sw.forum.comment(ANNA, post["id"], "mentioning nobody")  # a comment is no new post: it wakes (digest here)
    assert digest(f_rows(sw, BOB)[-1])
    sw.forum.subscribe(BOB, "post", str(post["id"]), mode="wake")
    sw._deferred.clear()
    n = len(woken)
    sw.forum.comment(ANNA, post["id"], "another one")  # a waking comment is signalled, not deferred
    assert waking(f_rows(sw, BOB)[-1]) and len(woken) == n + 1 and BOB not in sw._deferred
    sw.forum.comment(ANNA, post["id"], "@Bob Stone look")  # a mention too
    last = f_rows(sw, BOB)[-1]
    assert last["kind"] == "f_mention" and waking(last) and len(woken) == n + 2 and BOB not in sw._deferred


# ============================================================================== the digest: rendering and batching
def test_the_wake_up_shows_the_digest_group_votes_last_and_marks_every_row_delivered(sw):
    team(sw, ANNA, BOB)
    set_goal(sw)
    post = sw.forum.create(ANNA, "Plan", "")
    sw.forum.vote(BOB, f"#{post['id']}", "like")
    for i in range(32):
        sw.forum.create(BOB, f"Note {i}", "")
    sw.notify(ANNA, "n_network_on", wake=False)
    sw.db.execute("UPDATE notifications SET ts = ts - 1000 WHERE kind = 'f_vote'")  # oldest, still shown last
    text = sw.render_wake(ANNA)
    lines = text.splitlines()
    assert lines[1] == "What happened:" and lines[2].startswith("• ") and lines[3].startswith("From what")
    head = lines.index("From what you follow (oldest first, votes last):")
    shown = lines[head + 1:head + 31]
    assert shown[0].startswith('• Bob Stone posted #4 "Note 0"') and len(shown) == 30
    assert lines[head + 31] == "… and 3 more: see the feed (`feed`)."
    assert "rated your" not in text  # the vote is past the 30
    assert rows(sw, ANNA, undelivered=True) == []
    assert text.index("From what you follow") < text.index("Global goal:")


def test_a_digest_only_wake_up_has_no_reasons_line_and_an_incoming_render_shows_it(sw):
    team(sw, ANNA, BOB)
    sw.forum.create(BOB, "Plan", "")
    text = sw.render_wake(ANNA, incoming=True)
    assert "What happened:" not in text
    assert text.splitlines()[1:] == ["From what you follow (oldest first, votes last):",
                                     f'• {BOB} posted #3 "Plan": ']
    assert sw.render_wake(ANNA, incoming=True) is None


def test_stamps_mark_the_digest_bullets_and_their_ts_counts_for_the_range(sw):
    team(sw, ANNA, BOB)
    sw.forum.create(BOB, "Plan", "")
    sw.db.execute("UPDATE notifications SET ts = ts - ? WHERE delivered_at IS NULL", (STAMP_SPAN + 60,))
    (row,) = rows(sw, ANNA, undelivered=True)
    before = time.time()
    lines = sw.render_wake(ANNA, incoming=True, stamps=True).splitlines()
    headers = {prompts.t("en", "incoming_header", time=stamp_range(row["ts"], t)) for t in (before, time.time())}
    assert lines[0] in headers and "–" in lines[0]  # either minute when one turns over between the reads
    assert lines[1] == "From what you follow (oldest first, votes last):"
    assert lines[2] == f"• {item_time(row['ts'], before)} {row['text']}"
    sw.notify(ANNA, "n_network_on", wake=False)  # an aged row outside the digest, a fresh digest row
    sw.db.execute("UPDATE notifications SET ts = ts - ? WHERE delivered_at IS NULL", (STAMP_SPAN + 60,))
    sw.forum.create(BOB, "Lexer", "")
    fresh = rows(sw, ANNA, kind="f_post", undelivered=True)[0]
    before = time.time()
    lines = sw.render_wake(ANNA, incoming=True, stamps=True).splitlines()
    assert "–" in lines[0] and lines[-1] == f"• {item_time(fresh['ts'], before)} {fresh['text']}"


def test_digest_deadline_counts_from_the_oldest_non_vote_row_and_the_floor(sw):
    team(sw, ANNA, BOB)
    post = sw.forum.create(ANNA, "Plan", "")
    assert sw.digest_deadline(ANNA) is None  # no goal
    set_goal(sw)
    sw.forum.vote(BOB, f"#{post['id']}", "like")
    assert sw.digest_deadline(ANNA) is None  # votes alone never fall due
    sw.forum.create(BOB, "Lexer", "")
    first = rows(sw, ANNA, kind="f_post")[0]["ts"]
    assert sw.digest_deadline(ANNA) == pytest.approx(first + 60)
    sw._digest_floor = first + 5
    assert sw.digest_deadline(ANNA) == pytest.approx(first + 65)
    sw.config = dataclasses.replace(sw.config, agents=dataclasses.replace(sw.config.agents, digest_seconds=20))
    assert sw.digest_deadline(ANNA) == pytest.approx(first + 25)
    set_goal(sw, status="done")
    assert sw.digest_deadline(ANNA) is None


def test_due_digests_wake_an_idle_agent_once_never_for_votes_alone_or_without_an_active_goal(sw):
    team(sw, ANNA, BOB, CARL)
    set_goal(sw)
    woken = {name: fake_runtime(sw, name) for name in (ANNA, BOB, CARL)}
    post = sw.forum.create(ANNA, "Plan", "")
    sw.forum.vote(CARL, f"#{post['id']}", "like")
    sw.forum.create(CARL, "Lexer", "")
    sw._deliver_digests()
    assert not any(woken.values())  # not due yet
    age(sw, 61)
    sw._deliver_digests()
    assert woken == {ANNA: [1], BOB: [1], CARL: [1]}
    assert sw.has_pending(ANNA) and all(r["data"].get(DIGEST_KEY) and QUIET_KEY not in r["data"]
                                        for r in rows(sw, ANNA, undelivered=True))
    sw._deliver_digests()
    assert woken[ANNA] == [1]  # pending now: not idle, never woken twice
    for name in (ANNA, BOB, CARL):
        sw.render_wake(name)
    sw.forum.vote(BOB, f"#{post['id']}", "dislike")
    age(sw, 1000)
    sw._deliver_digests()
    assert woken[ANNA] == [1]  # a vote alone
    sw.forum.create(BOB, "Again", "")
    age(sw, 1000)
    sw._timers.add(object())
    sw._deliver_digests()
    assert woken[ANNA] == [1]  # a staggered notice is still to come
    sw._timers.clear()
    set_goal(sw, status="done")
    sw._deliver_digests()
    assert woken[ANNA] == [1]  # no batch without an active goal


def test_resume_moves_the_digest_floor(sw):
    team(sw, ANNA)
    set_goal(sw)
    sw.db.set("paused", {"paused": True, "reason": "paused by the user"})
    before = time.time()
    sw.resume(USER)
    assert sw._digest_floor >= before


# ============================================================================== file subscriptions
def file_event(sw: Swarm, actor: str, path: str) -> None:
    sw.bus.emit("file.changed", actor, path=path, op="write")


def commit_event(sw: Swarm, actor: str, paths: list[str], suspects: list[str], more: int = 0) -> None:
    sw.bus.emit("files.committed", actor, kind="shell", commit="abc", files=[{"path": p, "change": "M"} for p in paths],
                **({"more": more} if more else {}), suspects=suspects)


def test_file_changes_match_the_path_or_a_folder_and_skip_own_and_scratch_changes(sw):
    team(sw, ANNA, BOB, CARL)
    sw.forum.subscribe(BOB, "file", "src", mode="digest")
    sw.forum.subscribe(CARL, "file", "", mode="digest")
    file_event(sw, ANNA, "src/a.py")   # no loop: the fan-out runs inline
    file_event(sw, ANNA, "srcx/b.py")
    file_event(sw, ANNA, ".scratch/Anna Smith/n.txt")
    file_event(sw, CARL, "src/c.py")
    (bob,) = f_rows(sw, BOB)
    assert bob["kind"] == "f_file" and digest(bob)
    assert bob["data"]["paths"] == ["src/a.py", "src/c.py"] and bob["data"]["editors"] == [ANNA, CARL]
    assert bob["text"] == f"Files changed ({ANNA}, {CARL}): src/a.py, src/c.py."
    (carl,) = f_rows(sw, CARL)
    assert carl["data"]["paths"] == ["src/a.py", "srcx/b.py"]  # its own change and .scratch/ left out
    sw.forum.subscribe(ANNA, "file", ".scratch", mode="digest")
    file_event(sw, BOB, ".scratch/Bob Stone/x.txt")
    assert f_rows(sw, ANNA)[0]["data"]["paths"] == [".scratch/Bob Stone/x.txt"]


def test_file_rows_coalesce_per_delivery_and_a_waking_one_settles(make_swarm):
    sw = make_swarm(agents={"file_settle_seconds": 60, "file_max_wait_seconds": 300})
    team(sw, ANNA, BOB)
    sw.forum.subscribe(BOB, "file", "docs", mode="wake")
    sw.forum.subscribe(BOB, "file", "src", mode="digest")
    file_event(sw, ANNA, "docs/a.md")
    file_event(sw, ANNA, "src/a.py")
    woken = fake_runtime(sw, BOB)
    first = f_rows(sw, BOB)[0]
    settle = first["data"]["_settle"]
    assert first["data"][QUIET_KEY] and DIGEST_KEY not in first["data"] and settle["first"] == settle["last"]
    for i in range(7):
        file_event(sw, ANNA, f"docs/{i}.md")
    wake, dig = f_rows(sw, BOB)
    assert digest(dig) and dig["data"]["paths"] == ["src/a.py"]
    assert wake["ts"] == first["ts"] and wake["data"]["_settle"]["first"] == settle["first"]
    assert wake["data"]["paths"][:2] == ["docs/a.md", "docs/0.md"] and wake["data"]["more"] == 3
    assert wake["text"] == f"Files changed ({ANNA}): docs/a.md, docs/0.md, docs/1.md, docs/2.md, docs/3.md (+3)."
    assert not sw.has_pending(BOB)
    now = time.time()
    assert sw._release_settled(now) == [] and woken == []
    assert sw._release_settled(now + 61) == [BOB] and woken == [1]
    released = f_rows(sw, BOB)[0]["data"]
    assert QUIET_KEY not in released and "_settle" not in released and sw.has_pending(BOB)


def test_a_file_run_that_keeps_changing_is_released_at_the_cap(make_swarm):
    sw = make_swarm(agents={"file_settle_seconds": 60, "file_max_wait_seconds": 300})
    team(sw, ANNA, BOB)
    sw.forum.subscribe(BOB, "file", "", mode="wake")
    file_event(sw, ANNA, "a.md")
    row = f_rows(sw, BOB)[0]
    data = dict(row["data"], _settle={"first": row["data"]["_settle"]["first"] - 301, "last": time.time()})
    sw.db.execute("UPDATE notifications SET data = ? WHERE id = ?", (json.dumps(data), row["id"]))
    assert sw._release_settled(time.time()) == [BOB]
    sw2 = make_swarm(agents={"file_settle_seconds": 0, "file_max_wait_seconds": 0})
    team(sw2, ANNA, BOB)
    sw2.forum.subscribe(BOB, "file", "", mode="wake")
    file_event(sw2, ANNA, "a.md")
    assert sw2._release_settled(time.time()) == [BOB]  # 0: at the next pass


def test_t1_releases_settled_file_items_whatever_the_goal_and_never_batches_an_external_member(make_swarm):
    sw = make_swarm(agents={"file_settle_seconds": 0})
    team(sw, ANNA, BOB)
    add(sw, EVE, AgentKind.EXTERNAL)
    woken = fake_runtime(sw, BOB)
    for name in (BOB, EVE):
        sw.forum.subscribe(name, "file", "", mode="wake")

    def no_goal() -> None:
        assert not sw.goal()  # none set yet

    def done() -> None:
        set_goal(sw, status="done")

    def staggered() -> None:
        set_goal(sw)
        sw._timers.add(object())

    for i, case in enumerate((no_goal, done, staggered)):
        case()
        file_event(sw, ANNA, f"a{i}.md")  # no loop: the fan-out runs inline
        sw._signals.setdefault(EVE, asyncio.Event()).clear()
        n = len(woken)
        sw._deliver_digests()
        for name in (BOB, EVE):
            (row,) = rows(sw, name, kind="f_file", undelivered=True)
            assert QUIET_KEY not in row["data"] and "_settle" not in row["data"], case.__name__
        assert len(woken) == n + 1 and sw._signals[EVE].is_set(), case.__name__
        sw.render_wake(BOB)
        sw.render_wake(EVE)
    sw._timers.clear()
    set_goal(sw)
    sw.forum.create(ANNA, "Plan", "")  # Eve's feed row is in digest
    sw._signals[EVE].clear()
    age(sw, 61)
    sw._deliver_digests()
    (eve,) = rows(sw, EVE, kind="f_post", undelivered=True)
    assert digest(eve) and not sw._signals[EVE].is_set()


def test_a_commit_reaches_its_actor_unless_it_is_the_sole_suspect(sw):
    team(sw, ANNA, BOB, CARL)
    for name in (ANNA, BOB, CARL):
        sw.forum.subscribe(name, "file", "", mode="digest")
    sw.forum.subscribe(CARL, "file", "lib", by=BOB, mode="wake")
    commit_event(sw, ANNA, ["src/a.py"], [ANNA])
    assert f_rows(sw, ANNA) == [] and len(f_rows(sw, BOB)) == 1
    commit_event(sw, ANNA, ["lib/b.py"], [ANNA, BOB], more=4)
    (anna,) = f_rows(sw, ANNA)
    assert anna["data"]["editors"] == [ANNA, BOB] and anna["text"] == f"Files changed ({ANNA}, {BOB}): lib/b.py."
    assert anna["data"]["more"] == 4  # the event's unlisted files: data only
    assert [digest(r) for r in f_rows(sw, CARL)] == [True, False]  # by=Bob matches a suspect: its wake row
    paths = [f"p{i}.txt" for i in range(250)]
    commit_event(sw, CARL, paths, [CARL])
    big = f_rows(sw, BOB)[0]["data"]
    assert len(big["paths"]) == 200 and big["more"] == 195 + 52 + 4  # past the 5 named, past the cap, the event's
    assert big["paths"][:2] == ["src/a.py", "lib/b.py"]


def test_a_commits_unlisted_files_count_only_for_a_whole_workspace_subscription(sw):
    team(sw, ANNA, BOB, CARL)
    sw.forum.subscribe(BOB, "file", "docs", mode="digest")
    sw.forum.subscribe(CARL, "file", "", mode="digest")
    paths = ["docs/a.md"] + [f"src/{i}.py" for i in range(6)]
    commit_event(sw, ANNA, paths, [ANNA], more=50)
    (bob,) = f_rows(sw, BOB)
    assert bob["data"]["paths"] == ["docs/a.md"] and bob["data"]["more"] == 0
    assert bob["text"] == f"Files changed ({ANNA}): docs/a.md."
    (carl,) = f_rows(sw, CARL)
    assert carl["data"]["more"] == 2 + 50
    assert carl["text"] == f"Files changed ({ANNA}): docs/a.md, src/0.py, src/1.py, src/2.py, src/3.py (+2)."


async def test_with_a_loop_the_file_fan_out_runs_after_the_dispatch(sw):
    team(sw, ANNA, BOB)
    sw.forum.subscribe(BOB, "file", "", mode="digest")
    file_event(sw, ANNA, "a.md")
    assert f_rows(sw, BOB) == []
    await asyncio.sleep(0)
    assert len(f_rows(sw, BOB)) == 1


def test_a_file_change_skips_the_mcp_clients_other_identities(sw):
    team(sw, ANNA, BOB, CARL)
    for name in (BOB, CARL):
        sw.forum.subscribe(name, "file", "", mode="digest")
    with sent_as_seen_by({BOB}, ANNA):
        file_event(sw, ANNA, "a.md")  # no loop: inline
    assert f_rows(sw, BOB) == [] and len(f_rows(sw, CARL)) == 1


async def test_with_a_loop_the_file_fan_out_keeps_the_identities_of_its_context(sw):
    team(sw, ANNA, BOB, CARL)
    for name in (BOB, CARL):
        sw.forum.subscribe(name, "file", "", mode="digest")
    with sent_as_seen_by({BOB}, ANNA):
        file_event(sw, ANNA, "a.md")
    assert f_rows(sw, CARL) == []
    await asyncio.sleep(0)  # the fan-out runs after the block has exited: in the context call_soon copied
    assert f_rows(sw, BOB) == [] and len(f_rows(sw, CARL)) == 1


async def test_write_file_reaches_a_file_subscription_end_to_end(make_swarm):
    sw = make_swarm()
    await sw.start(run_agents=False)
    try:
        team(sw, ANNA, BOB)
        await tool(sw, BOB, "subscribe", {"what": "file", "target": "notes", "mode": "digest"})
        text, error = await tool(sw, ANNA, "write_file", {"path": "notes/plan.md", "content": "hi"})
        assert not error, text
        await asyncio.sleep(0)
        (row,) = f_rows(sw, BOB)
        assert row["data"]["paths"] == ["notes/plan.md"] and digest(row)
    finally:
        await sw.stop()


# ============================================================================== wait_for_events
async def test_wait_for_events_returns_the_digest_at_its_deadline_not_at_the_timeout(make_swarm, monkeypatch):
    sw = make_swarm()
    await sw.start(run_agents=False)
    try:
        team(sw, ANNA)
        add(sw, EVE, AgentKind.EXTERNAL)
        set_goal(sw)
        sw.render_wake(EVE)
        monkeypatch.setattr(sw, "_digest_seconds", lambda: 0.3)
        task = asyncio.ensure_future(tool(sw, EVE, "wait_for_events", {"timeout": 600}))
        await asyncio.sleep(0.05)
        sw.forum.create(ANNA, "Plan", "")  # a digest row while the wait sleeps: its deadline caps the sleep
        started = time.monotonic()
        for _ in range(5):
            await asyncio.sleep(0)
        assert not task.done()  # the row's signal only makes the wait compute its deadline again
        text, error = await asyncio.wait_for(task, 10)
        assert not error and 0.2 <= time.monotonic() - started < 5  # it waited for the deadline (0.3 s)
        assert "From what you follow" in text and f'{ANNA} posted #3 "Plan"' in text
        assert rows(sw, EVE, undelivered=True) == []
        post = sw.forum.create(EVE, "Mine", "")
        task = asyncio.ensure_future(tool(sw, EVE, "wait_for_events", {"timeout": 600}))
        await asyncio.sleep(0.05)
        sw.forum.vote(ANNA, f"#{post['id']}", "like")  # a vote alone never ends the wait early
        await asyncio.sleep(0.5)  # past the 0.3 s a digest row would give
        assert not task.done()
        sw.forum.comment(ANNA, post["id"], "@Eve Black see")
        text, _ = await asyncio.wait_for(task, 5)
        assert "rated your" in text and "mentioned you" in text
        sw.forum.vote(ANNA, f"#{post['id']}", "dislike")
        text, _ = await tool(sw, EVE, "wait_for_events", {"timeout": 1})
        assert "rated your" in text  # the timeout still brings what has gathered
    finally:
        await sw.stop()


def test_board_sessions_write_no_digest_rows(make_swarm):
    sw = make_swarm(mode="board")
    add(sw, ANNA)
    sw.notify(ANNA, "n_network_on", wake=False)
    assert sw.forum is None and rows(sw, ANNA)[0]["data"] == {QUIET_KEY: True}
    assert sw.digest_deadline(ANNA) is None
    assert "From what you follow" not in sw.render_wake(ANNA)
