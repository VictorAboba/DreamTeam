"""The thread mode's Forum (docs/SPEC.md §22.2) and the data side of the goal vote (§22.4), on a real thread Swarm
that is never started (no LLM call)."""

from __future__ import annotations

import dataclasses
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dreamteam import autoreply
from dreamteam import forum as forummod
from dreamteam.config import Config
from dreamteam.forum import GOAL_POST, PLAN_POST, Forum, goal_tally
from dreamteam.models import USER, AgentKind, ToolError
from dreamteam.swarm import Swarm

ANNA, BOB, CARL, DORA, EVE = "Anna Smith", "Bob Stone", "Carl Brown", "Dora White", "Eve Black"


def make_config(tmp_path: Path, **swarm: Any) -> Config:
    raw: dict[str, Any] = {
        "swarm": {"name": "Test swarm", "language": "en", "start_agents": 1, "max_cost_usd": 0, "mode": "thread",
                  **swarm},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
    }
    return Config.from_dict(tmp_path / "project", raw)


@pytest.fixture
def sw(tmp_path):
    swarm = Swarm(config=make_config(tmp_path))
    try:
        yield swarm
    finally:
        swarm.close()


@pytest.fixture
def make_swarm(tmp_path):
    made: list[Swarm] = []

    def factory(**swarm: Any) -> Swarm:
        made.append(Swarm(config=make_config(tmp_path / f"s{len(made)}", **swarm)))
        return made[-1]

    yield factory
    for swarm in made:
        swarm.close()


def add(sw: Swarm, name: str, kind: str = AgentKind.LLM, language: str | None = None) -> str:
    return sw._create_member(USER, "Generalist", name=name, kind=kind, language=language)


def retire(sw: Swarm, name: str) -> None:
    sw.db.execute("UPDATE agents SET status = 'retired' WHERE name = ?", (name,))


def set_goal(sw: Swarm, title: str = "Build the parser", description: str = "A fast one.") -> dict:
    goal = {"title": title, "description": description, "status": "active", "summary": "", "set_by": USER,
            "updated_at": time.time()}
    sw.db.set("goal", goal)
    sw.forum.ensure_system_posts(goal)
    return goal


def events(sw: Swarm, *types: str) -> list[dict]:
    return [e for e in sw.bus.recent(limit=10000) if e["type"] in types]


@pytest.fixture
def completions(sw, monkeypatch):
    """swarm.complete_goal recorded (its decision keyword belongs to the integration part): the goal is marked done
    with the decision, as §22.4 says it is stored."""
    calls: list[dict] = []

    def complete(actor, summary, *, decision=None):
        calls.append({"actor": actor, "summary": summary, "decision": decision})
        goal = sw.goal()
        goal.update(status="done", summary=summary, completed_by=actor, decision=decision)
        sw.db.set("goal", goal)
        return goal

    monkeypatch.setattr(sw, "complete_goal", complete)
    return calls


@pytest.fixture
def delivered(sw, monkeypatch):
    calls: list[tuple[str, list[str], bool]] = []

    def deliver(event, mentioned=(), *, llm_quiet=False):
        calls.append((event["type"], list(mentioned), llm_quiet))

    monkeypatch.setattr(sw.forum, "_deliver", deliver)
    return calls


# --- ids and the system posts -------------------------------------------------------------------------------

def test_a_post_before_any_goal_is_number_3_and_there_is_no_1(sw):
    add(sw, ANNA)
    post = sw.forum.create(ANNA, "First thoughts", "")
    assert post["id"] == 3 and post["kind"] == "post" and post["text"] == ""
    with pytest.raises(ToolError, match=r"^No post #1$"):
        sw.forum.get(1)
    assert sw.forum.goal_vote() is None
    assert [p["id"] for p in sw.forum.feed()] == [3]


def test_ensure_system_posts_creates_1_and_2_then_rewrites_them(sw):
    add(sw, ANNA)
    goal = {"title": "Build the parser", "description": "A fast one.", "status": "active"}
    sw.db.set("goal", goal)
    assert sw.forum.ensure_system_posts(goal) is True
    one, two = sw.forum.get(1), sw.forum.get(2)
    assert (one["kind"], one["author"], one["title"]) == ("goal", None, "Goal reached?")
    assert (two["kind"], two["author"], two["title"]) == ("plan", None, "Let's plan the work")
    assert one["text"].startswith("**Goal:** Build the parser\n\nA fast one.\n\nThe goal is decided by the votes")
    assert "rule: democracy." in one["text"]
    assert "Discuss here, in the comments" in two["text"] and "First one member asks" not in two["text"]
    created = events(sw, "post.created")
    assert [(e["actor"], e["data"]["post"]["id"]) for e in created] == [(None, 1), (None, 2)]
    assert sw.forum.create(ANNA, "Next", "x")["id"] == 3
    assert sw.forum.ensure_system_posts({"title": "Another goal", "description": ""}) is False
    assert sw.forum.get(1)["text"].startswith("**Goal:** Another goal\n\nThe goal is decided")
    assert sw.forum.get(2)["title"] == "Let's plan the work"


def test_a_long_description_is_never_cut(sw):
    description = "d" * (forummod.MAX_POST + 500)
    set_goal(sw, description=description)
    assert description in sw.forum.get(1)["text"] and description in sw.forum.get(2)["text"]


def test_the_system_posts_in_russian(make_swarm):
    ru = make_swarm(language="ru", goal_rule="veto")
    set_goal(ru, "Собрать парсер", "")
    one, two = ru.forum.get(1), ru.forum.get(2)
    assert one["title"] == "Цель достигнута?" and two["title"] == "Спланируем работу"
    assert one["text"].startswith("**Цель:** Собрать парсер\n\nЦель решается голосами")
    assert "правило: вето." in one["text"]
    assert chr(0x451) not in one["text"] + two["text"]


def test_a_rewrite_resets_the_votes_on_1_only(sw, delivered):
    add(sw, ANNA), add(sw, BOB)
    set_goal(sw)
    c = sw.forum.comment(ANNA, 2, "I take the lexer.")
    sw.forum.vote(BOB, "p1", "dislike")
    sw.forum.vote(BOB, "p2", "like")
    sw.forum.vote(BOB, f"c{c['id']}", "like")
    before = len(events(sw, "post.updated"))
    delivered.clear()
    assert sw.forum.ensure_system_posts({"title": "New goal", "description": ""}) is False
    updates = events(sw, "post.updated")[before:]
    assert [(e["actor"], e["data"]["post"]["id"], e["data"]["changes"]) for e in updates] == [
        (None, 1, ["text"]), (None, 2, ["text"]), (None, 1, ["votes"])]
    assert updates[-1]["data"]["post"]["dislikes"] == 0
    assert sw.forum.votes("p1") == [] and sw.forum.my_vote(BOB, "p2") == "like"
    assert sw.forum.my_vote(BOB, f"c{c['id']}") == "like" and sw.forum.comments(2)[0]["text"] == "I take the lexer."
    assert delivered == []  # the swarm's rewrites fan out to nobody


def test_a_rewrite_without_votes_emits_no_votes_reset(sw):
    set_goal(sw)
    sw.forum.ensure_system_posts({"title": "New goal", "description": ""})
    assert [e["data"]["changes"] for e in events(sw, "post.updated")] == [["text"], ["text"]]


def test_the_plan_data_follows_the_clarify_setting(make_swarm):
    off = make_swarm()
    add(off, ANNA)
    set_goal(off)
    assert off.forum.plan_data() == {"clarify": False, "asker": None}
    on = make_swarm(clarify=True)
    add(on, ANNA, kind=AgentKind.EXTERNAL), add(on, BOB), add(on, CARL)
    set_goal(on)
    assert on.forum.plan_data() == {"clarify": True, "asker": BOB}
    assert on.forum.get(2)["text"].endswith("comments the answers here; plan after them.")
    since = on.forum.plan_since()
    on.forum.set_plan_asker(CARL)
    assert on.forum.plan_data() == {"clarify": True, "asker": CARL} and on.forum.plan_since() == since
    empty = make_swarm(clarify=True)
    set_goal(empty)
    assert empty.forum.plan_data() == {"clarify": True, "asker": None}


def test_rewrite_goal_rule(sw, tmp_path):
    add(sw, ANNA), add(sw, BOB)
    assert sw.forum.rewrite_goal_rule() is False  # no #1
    set_goal(sw)
    sw.forum.vote(ANNA, "p1", "like")
    assert sw.forum.rewrite_goal_rule() is False and events(sw, "post.updated") == []
    sw.reconfigure(make_config(tmp_path, goal_rule="veto"))
    plan = sw.forum.get(2)
    assert sw.forum.rewrite_goal_rule() is True
    assert "rule: veto." in sw.forum.get(1)["text"] and sw.forum.my_vote(ANNA, "p1") == "like"
    assert sw.forum.get(2) == plan
    assert [(e["actor"], e["data"]["changes"]) for e in events(sw, "post.updated")] == [(None, ["text"])]


# --- posts -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("title, text, message", [
    ("  ", "", "Post title must not be empty."),
    ("one\ntwo", "", "The title is one line; put the rest into the text."),
    ("t" * 201, "", "Post title is too long (201 chars, max 200). Keep it short and put details into the text."),
    ("ok", "x" * 20001, "Post is too long (20001 chars, max 20000). Put long content into a workspace file and post "
                        "its path."),
])
def test_create_refusals(sw, title, text, message):
    add(sw, ANNA)
    with pytest.raises(ToolError) as err:
        sw.forum.create(ANNA, title, text)
    assert str(err.value) == message
    assert sw.db.scalar("SELECT COUNT(*) FROM posts") == 0


def test_create_a_post(sw, delivered):
    add(sw, ANNA)
    post = sw.forum.create(ANNA, "  Parser benchmark ", "  numbers  ")
    assert {k: post[k] for k in ("id", "kind", "title", "text", "author", "edited_at", "likes", "dislikes",
                                 "comments")} == {"id": 3, "kind": "post", "title": "Parser benchmark",
                                                  "text": "numbers", "author": ANNA, "edited_at": None,
                                                  "likes": 0, "dislikes": 0, "comments": 0}
    assert post["created_at"] == post["active_at"]
    assert set(post) == {"id", "kind", "title", "text", "author", "created_at", "edited_at", "active_at", "likes",
                         "dislikes", "comments"}
    [event] = events(sw, "post.created")
    assert event["actor"] == ANNA and event["data"] == {"post": post}
    assert delivered == [("post.created", [], False)]
    assert sw.forum.create(USER, "From the user", "")["author"] == USER


def test_only_active_members_and_the_user_write(sw):
    add(sw, ANNA)
    retire(sw, ANNA)
    for actor in (ANNA, "Nobody Here"):
        with pytest.raises(ToolError, match="is not an active member"):
            sw.forum.create(actor, "title", "")


def test_update(sw, delivered):
    add(sw, ANNA), add(sw, BOB)
    set_goal(sw)
    post = sw.forum.create(ANNA, "Plan", "v1")
    for pid in (1, 2):
        for actor in (ANNA, USER):
            with pytest.raises(ToolError) as err:
                sw.forum.update(actor, pid, text="x")
            assert str(err.value) == (f"#{pid} is a system post: the swarm writes it from the goal; comment on it "
                                      "instead.")
    with pytest.raises(ToolError) as err:
        sw.forum.update(BOB, post["id"], text="mine now")
    assert str(err.value) == "#3 is Anna Smith's post: only its author edits it; comment on it instead."
    with pytest.raises(ToolError, match=r"^Pass title or text\.$"):
        sw.forum.update(ANNA, post["id"])
    with pytest.raises(ToolError, match="The title is one line"):
        sw.forum.update(ANNA, post["id"], title="a\nb")
    same = sw.forum.update(ANNA, post["id"], title="Plan", text=" v1 ")
    assert same["changes"] == [] and same["edited_at"] is None and events(sw, "post.updated") == []
    delivered.clear()
    changed = sw.forum.update(ANNA, post["id"], title="Plan", text="v2")
    assert changed["changes"] == ["text"] and changed["text"] == "v2"
    assert changed["edited_at"] is not None and changed["edited_at"] == changed["active_at"]
    [event] = events(sw, "post.updated")
    assert event["actor"] == ANNA and event["data"]["changes"] == ["text"] and event["data"]["post"]["text"] == "v2"
    assert "changes" not in event["data"]["post"]
    assert sw.forum.update(USER, post["id"], title="Plan (user)")["changes"] == ["title"]
    assert [c[0] for c in delivered] == ["post.updated", "post.updated"]
    with pytest.raises(ToolError, match=r"^No post #99$"):
        sw.forum.update(ANNA, 99, text="x")


# --- comments ----------------------------------------------------------------------------------------------

def test_comments(sw):
    add(sw, ANNA), add(sw, BOB)
    set_goal(sw)
    post = sw.forum.create(ANNA, "Topic", "")
    other = sw.forum.create(ANNA, "Other", "")
    with pytest.raises(ToolError, match=r"^Comment text must not be empty\.$"):
        sw.forum.comment(BOB, post["id"], "  ")
    with pytest.raises(ToolError) as err:
        sw.forum.comment(BOB, post["id"], "x" * 20001)
    assert str(err.value) == ("Comment is too long (20001 chars, max 20000). Put long content into a workspace file "
                              "and comment its path.")
    first = sw.forum.comment(BOB, post["id"], " first ")
    assert set(first) == {"id", "post_id", "author", "text", "reply_to", "ts", "likes", "dislikes"}
    assert (first["post_id"], first["author"], first["text"], first["reply_to"]) == (post["id"], BOB, "first", None)
    assert sw.forum.get(post["id"])["active_at"] == first["ts"] and sw.forum.get(post["id"])["comments"] == 1
    elsewhere = sw.forum.comment(ANNA, other["id"], "there")
    with pytest.raises(ToolError) as err:
        sw.forum.comment(ANNA, post["id"], "reply", reply_to=elsewhere["id"])
    assert str(err.value) == f"No comment c{elsewhere['id']} on #{post['id']}"
    reply = sw.forum.comment(ANNA, post["id"], "reply", reply_to=first["id"])
    assert reply["reply_to"] == first["id"]
    [_, _, event] = events(sw, "post.comment")
    assert event["actor"] == ANNA and event["data"] == {"post_id": post["id"], "comment": reply}
    assert [c["id"] for c in sw.forum.comments(post["id"])] == [first["id"], reply["id"]]
    assert [c["id"] for c in sw.forum.comments(post["id"], since_id=first["id"])] == [reply["id"]]
    assert [c["id"] for c in sw.forum.comments(post["id"], limit=1)] == [first["id"]]
    with pytest.raises(ToolError, match=f"No comment c{elsewhere['id']} on #{post['id']}"):
        sw.forum.comments(post["id"], since_id=elsewhere["id"])
    # the system posts take comments, also while the goal is done
    sw.db.set("goal", {**sw.goal(), "status": "done"})
    assert sw.forum.comment(BOB, 1, "late")["post_id"] == 1


# --- votes -------------------------------------------------------------------------------------------------

def test_votes_on_posts_and_comments(sw, delivered):
    add(sw, ANNA), add(sw, BOB), add(sw, CARL)
    post = sw.forum.create(ANNA, "Topic", "")
    comment = sw.forum.comment(BOB, post["id"], "hm")
    with pytest.raises(ToolError, match=r"^You cannot vote on your own post\.$"):
        sw.forum.vote(ANNA, f"#{post['id']}", "like")
    with pytest.raises(ToolError, match=r"^You cannot vote on your own comment\.$"):
        sw.forum.vote(BOB, f"c{comment['id']}", "dislike")
    with pytest.raises(ToolError, match="target must be '#5' for a post or 'c31' for a comment"):
        sw.forum.vote(BOB, "5", "like")
    with pytest.raises(ToolError, match=r"^No post #77$"):
        sw.forum.vote(BOB, "p77", "like")
    with pytest.raises(ToolError, match=r"^No comment c77$"):
        sw.forum.vote(BOB, "c77", "like")
    with pytest.raises(ToolError, match="value must be like, dislike or none"):
        sw.forum.vote(BOB, "p3", "love")
    delivered.clear()
    assert sw.forum.vote(BOB, "P3", "like") == {"target": "p3", "value": "like", "likes": 1, "dislikes": 0,
                                                "changed": True}
    assert sw.forum.vote(BOB, "#3", "like")["changed"] is False          # the same value again
    assert sw.forum.vote(CARL, "p3", "dislike")["dislikes"] == 1
    assert sw.forum.vote(CARL, "p3", "like") == {"target": "p3", "value": "like", "likes": 2, "dislikes": 0,
                                                 "changed": True}       # a second vote replaces the first
    assert sw.forum.vote(CARL, "p3", "none")["likes"] == 1
    assert sw.forum.vote(CARL, "p3", "none")["changed"] is False         # none without a vote
    assert sw.forum.vote(USER, f"c{comment['id']}", "like")["likes"] == 1
    assert sw.forum.get(3)["likes"] == 1 and sw.forum.comments(3)[0]["likes"] == 1
    votes = events(sw, "post.vote")
    assert [(e["actor"], e["data"]["value"]) for e in votes] == [(BOB, "like"), (CARL, "dislike"), (CARL, "like"),
                                                                 (CARL, "none"), (USER, "like")]
    assert votes[0]["data"] == {"target": "p3", "voter": BOB, "value": "like", "likes": 1, "dislikes": 0}
    assert len(delivered) == 5 and sw.forum.my_vote(BOB, "p3") == "like" and sw.forum.my_vote(CARL, "p3") is None
    assert sw.forum.votes(f"c{comment['id']}") == [{"voter": USER, "value": "like", "counted": False}]
    assert sw.forum.get(3)["active_at"] == comment["ts"]  # a vote does not touch active_at


def test_votes_on_1_are_closed_while_the_goal_is_not_active(sw, completions):
    add(sw, ANNA)
    with pytest.raises(ToolError, match=r"^No post #1$"):
        sw.forum.vote(ANNA, "#1", "like")
    set_goal(sw)
    sw.db.set("goal", {**sw.goal(), "status": "done"})
    with pytest.raises(ToolError) as err:
        sw.forum.vote(ANNA, "#1", "like")
    assert str(err.value) == "The goal is already reached: #1 is closed for votes until the goal is reopened."
    assert sw.forum.vote(ANNA, "#2", "like")["changed"] is True  # #2 stays open


# --- the goal tally and its re-evaluation -------------------------------------------------------------------

def _voters(sw: Swarm) -> None:
    for name in (ANNA, BOB, CARL, DORA):
        add(sw, name)
    add(sw, EVE, kind=AgentKind.EXTERNAL)
    add(sw, "Old Timer")


def test_goal_tally_counts_only_the_active_ai_agents(sw, completions):
    _voters(sw)
    set_goal(sw)
    sw.forum.vote("Old Timer", "p1", "like")
    retire(sw, "Old Timer")
    sw.forum.vote(EVE, "p1", "like")
    sw.forum.vote(USER, "p1", "like")
    sw.forum.vote(CARL, "p1", "like")
    sw.forum.vote(ANNA, "p1", "dislike")
    tally = goal_tally(sw.db, "democracy")
    assert tally == {"rule": "democracy", "counted": 4, "for": [CARL], "against": [ANNA], "needed": 3, "met": False}
    assert goal_tally(sw.db, "veto") == {**tally, "rule": "veto", "needed": 4}
    sw.forum.vote(DORA, "p1", "like")
    assert goal_tally(sw.db.conn, "democracy")["for"] == [CARL, DORA]  # members order, not vote order
    with sqlite3.connect(sw.db.path) as conn:                         # pure: a plain connection to the file
        assert goal_tally(conn, "democracy")["for"] == [CARL, DORA]
    assert completions == []
    assert [v["counted"] for v in sw.forum.votes("p1")] == [False, False, False, True, True, True]
    with pytest.raises(ValueError):
        goal_tally(sw.db, "user")


def test_no_vacuous_decision(sw, completions):
    add(sw, EVE, kind=AgentKind.EXTERNAL)
    set_goal(sw)
    for rule in ("democracy", "veto"):
        tally = goal_tally(sw.db, rule)
        assert (tally["counted"], tally["needed"], tally["met"]) == (0, 0 if rule == "veto" else 1, False)
    add(sw, ANNA)
    sw.forum.vote(ANNA, "p1", "dislike")
    assert goal_tally(sw.db, "veto")["met"] is False and completions == []


def test_a_like_on_1_completes_the_goal_under_democracy(sw, completions):
    for name in (ANNA, BOB, CARL):
        add(sw, name)
    add(sw, EVE, kind=AgentKind.EXTERNAL)
    set_goal(sw)
    first = sw.forum.vote(ANNA, "p1", "like")
    assert first["decided"] is False and first["goal_vote"]["for"] == [ANNA] and completions == []
    assert sw.forum.vote(BOB, "p1", "dislike")["decided"] is False
    assert sw.forum.vote(BOB, "p1", "none")["decided"] is False   # a withdrawal never meets the rule
    assert sw.forum.vote(EVE, "p1", "like")["decided"] is False   # shown, never counted
    result = sw.forum.vote(CARL, "p1", "like")
    assert result["decided"] is True and result["goal_vote"]["met"] is True
    [call] = completions
    assert call["actor"] == CARL
    assert call["decision"] == {"rule": "democracy", "for": [ANNA, CARL], "against": [], "counted": 3,
                                "wrap_up": False}
    assert call["summary"] == ("The goal is reached by vote (democracy): 2 of 3 for — Anna Smith, Carl Brown; "
                               "against: —. The artifacts are the shared workspace.")
    assert sw.forum.recheck_goal(None) is False  # the goal is done now


def test_veto_and_the_wrap_up_summary(make_swarm, monkeypatch):
    sw = make_swarm(goal_rule="veto")
    calls: list[dict] = []
    monkeypatch.setattr(sw, "complete_goal", lambda actor, summary, *, decision=None: calls.append(
        {"actor": actor, "summary": summary, "decision": decision}))
    add(sw, ANNA), add(sw, BOB)
    set_goal(sw)
    sw.db.set("wrap_up", {"on": True, "by": USER, "since": time.time()})
    assert sw.forum.vote(ANNA, "p1", "like")["decided"] is False
    assert sw.forum.vote(BOB, "p1", "like")["decided"] is True
    assert calls[0]["decision"] == {"rule": "veto", "for": [ANNA, BOB], "against": [], "counted": 2, "wrap_up": True}
    assert calls[0]["summary"].startswith("The team finished the work by vote in wrap-up mode (veto): 2 of 2 for")


def test_a_retire_can_meet_the_rule_and_a_hire_cannot(sw, completions):
    for name in (ANNA, BOB, CARL):
        add(sw, name)
    set_goal(sw)
    sw.forum.vote(ANNA, "p1", "like")
    add(sw, DORA)                                       # a hire adds a counted voter without a vote
    assert sw.forum.vote(CARL, "p1", "like")["decided"] is False   # 2 of 4
    retire(sw, DORA)
    assert sw.forum.recheck_goal(None) is True                      # 2 of 3
    assert completions[0]["actor"] is None and completions[0]["decision"]["counted"] == 3


def test_goal_vote_applies_the_session_rule(make_swarm):
    sw = make_swarm(goal_rule="veto")
    add(sw, ANNA), add(sw, BOB), add(sw, CARL)
    assert sw.forum.goal_vote() is None
    set_goal(sw)
    assert sw.forum.goal_vote() == {"rule": "veto", "counted": 3, "for": [], "against": [], "needed": 3,
                                    "met": False}


def test_the_vote_lines_and_the_summaries():
    tally = {"rule": "democracy", "counted": 5, "for": ["A", "B", "C"], "against": ["D"], "needed": 3, "met": True}
    assert forummod.vote_line("en", tally) == "Goal vote (#1, democracy): 3 of 5 for, 1 against; needs 3."
    assert forummod.vote_line("ru", tally) == "Голосование по цели (#1, демократия): за 3 из 5, против 1; нужно 3."
    veto = {**tally, "rule": "veto", "needed": 5}
    assert forummod.vote_line("en", veto).endswith("1 against; needs all 5 and none against.")
    assert forummod.vote_line("ru", veto).endswith("против 1; нужны все 5 и никого против.")
    decision = forummod.decision_of(tally, False)
    assert decision == {"rule": "democracy", "for": ["A", "B", "C"], "against": ["D"], "counted": 5, "wrap_up": False}
    assert forummod.decision_summary("ru", decision) == (
        "Цель достигнута голосованием (демократия): за 3 из 5 — A, B, C; против: D. Артефакты — общая рабочая папка.")
    assert forummod.decision_of(tally, True, rule="user")["rule"] == "user"
    assert forummod.rule_name("ru", "user") == "решение пользователя"
    assert forummod.user_summary("en", tally) == ("The user marked the goal reached. Goal vote (#1, democracy): 3 of 5 "
                                                  "for, 1 against; needs 3. The artifacts are the shared workspace.")


# --- reset and reopen --------------------------------------------------------------------------------------

def test_reset_goal_votes(sw):
    add(sw, ANNA)
    sw.forum.reset_goal_votes(None, "reopened")  # no #1: nothing
    assert events(sw, "post.updated") == []
    set_goal(sw)
    sw.forum.reset_goal_votes(USER, "wrap_up_end")
    sw.forum.reset_goal_votes(None, "new_goal")
    assert events(sw, "post.updated") == []      # nothing deleted
    sw.forum.vote(ANNA, "p1", "dislike")
    sw.forum.reset_goal_votes(USER, "wrap_up_end")
    sw.forum.reset_goal_votes(USER, "reopened")  # always
    assert [(e["actor"], e["data"]["changes"], e["data"]["post"]["dislikes"])
            for e in events(sw, "post.updated")] == [(USER, ["votes"], 0), (USER, ["votes"], 0)]
    with pytest.raises(ValueError):
        sw.forum.reset_goal_votes(None, "other")


@pytest.mark.parametrize("comment, posted", [("", None), ("-", None), ("  -  ", None),
                                             (" Missing tests ", "Missing tests"), ("x" * 20500, "x" * 19999 + "…")])
def test_reopen(sw, delivered, comment, posted):
    add(sw, ANNA)
    set_goal(sw)
    sw.forum.vote(ANNA, "p1", "dislike")
    delivered.clear()
    sw.forum.reopen(USER, comment)
    assert sw.forum.votes("p1") == []
    [reset] = events(sw, "post.updated")
    assert reset["actor"] == USER and reset["data"]["changes"] == ["votes"]
    comments = sw.forum.comments(1)
    if posted is None:
        assert comments == [] and delivered == []
    else:
        assert [(c["author"], c["text"]) for c in comments] == [(USER, posted)]
        assert len(posted) <= forummod.MAX_POST
        assert events(sw, "post.comment")[0]["actor"] == USER
        assert delivered == [("post.comment", [], True)]


# --- feed ----------------------------------------------------------------------------------------------------

def test_the_feed_order_and_filters(sw):
    add(sw, ANNA), add(sw, BOB)
    set_goal(sw)
    a = sw.forum.create(ANNA, "Parser notes", "Про ПАРСЕР")
    b = sw.forum.create(BOB, "Lexer", "tokens")
    c = sw.forum.create(USER, "From the user", "")
    sw.forum.comment(BOB, a["id"], "bump")
    for pid, at in ((1, 1000), (2, 1000), (a["id"], 1004), (b["id"], 1002), (c["id"], 1003)):  # a is the most active
        sw.db.execute("UPDATE posts SET active_at = ? WHERE id = ?", (at, pid))
    assert [p["id"] for p in sw.forum.feed()] == [1, 2, a["id"], c["id"], b["id"]]
    assert [p["id"] for p in sw.forum.feed(limit=3)] == [1, 2, a["id"]]   # the system posts count
    assert [p["id"] for p in sw.forum.feed(limit=0)] == [1]               # clamped to 1..100
    assert len(sw.forum.feed(limit=1000)) == 5
    assert [p["id"] for p in sw.forum.feed(by="anna")] == [a["id"]]       # the system posts drop out
    assert [p["id"] for p in sw.forum.feed(by="user")] == [c["id"]]
    assert [p["id"] for p in sw.forum.feed(since=1002.5)] == [a["id"], c["id"]]
    assert [p["id"] for p in sw.forum.feed(query="парсер")] == [a["id"]]  # casefold, Cyrillic too
    assert [p["id"] for p in sw.forum.feed(query="PARSER")] == [1, 2, a["id"]]  # the goal "Build the parser"
    assert sw.forum.feed_total() == 5 and sw.forum.feed_total(query="парсер") == 1
    assert sw.forum.feed_total(by=BOB) == 1
    assert sw.forum.counts() == {"posts": 3, "comments": 1}
    retire(sw, BOB)
    assert [p["id"] for p in sw.forum.feed(by=BOB)] == [b["id"]]          # a retired author too
    with pytest.raises(ToolError, match="Unknown member"):
        sw.forum.feed(by="Nobody Here")


# --- subscriptions -----------------------------------------------------------------------------------------

def test_auto_subscribe(sw):
    sw.forum.auto_subscribe(ANNA)
    sw.forum.auto_subscribe(ANNA)  # repeated: no duplicate, no event
    subs = sw.forum.subscriptions(ANNA)
    assert [(s["kind"], s["target"], s["by"], s["mode"], s["auto"]) for s in subs] == [
        ("feed", "", "", "digest", True), ("reactions", "", "", "digest", True), ("post", "1", "", "digest", True),
        ("post", "2", "", "digest", True)]
    assert set(subs[0]) == {"id", "member", "kind", "target", "by", "mode", "auto", "created_at", "by_former"}
    changed = events(sw, "subscription.changed")
    assert [(e["actor"], e["data"]["op"], e["data"]["subscription"]["id"]) for e in changed] == [
        (None, "created", s["id"]) for s in subs]


def test_subscribe_and_its_checks(sw, tmp_path):
    add(sw, ANNA), add(sw, BOB), add(sw, "Old Timer")
    set_goal(sw)
    post = sw.forum.create(BOB, "Topic", "")
    with pytest.raises(ToolError, match=r"^No post #9$"):
        sw.forum.subscribe(ANNA, "post", "#9")
    with pytest.raises(ToolError, match="what=post needs target"):
        sw.forum.subscribe(ANNA, "post", None)
    with pytest.raises(ToolError, match=r"^target is only for what=post or what=file$"):
        sw.forum.subscribe(ANNA, "feed", "3")
    with pytest.raises(ToolError, match=r"^You never get your own events: by cannot be yourself\.$"):
        sw.forum.subscribe(ANNA, "feed", by="anna smith")
    with pytest.raises(ToolError, match="what must be"):
        sw.forum.subscribe(ANNA, "everything")
    with pytest.raises(ToolError, match="mode must be"):
        sw.forum.subscribe(ANNA, "feed", mode="loud")
    with pytest.raises(ToolError, match="outside the workspace"):
        sw.forum.subscribe(ANNA, "file", "../elsewhere")
    with pytest.raises(ToolError, match="inside .git"):
        sw.forum.subscribe(ANNA, "file", ".git/config")
    with pytest.raises(ToolError, match="The user has no subscriptions"):
        sw.forum.subscribe(USER, "feed")
    created = sw.forum.subscribe(ANNA, "post", f"p{post['id']}")
    assert (created["op"], created["kind"], created["target"], created["by"], created["mode"], created["auto"]) == (
        "created", "post", str(post["id"]), "", "wake", False)
    assert sw.forum.subscribe(ANNA, "post", str(post["id"]), mode="wake")["op"] == "unchanged"
    changed = sw.forum.subscribe(ANNA, "post", post["id"], mode="digest")
    assert (changed["op"], changed["id"], changed["mode"]) == ("changed", created["id"], "digest")
    assert [sw.forum.subscribe(ANNA, "file", raw)["target"] for raw in ("", ".", "/", "src/parser/", "./docs")] == [
        "", "", "", "src/parser", "docs"]
    assert sw.forum.subscribe(ANNA, "feed", by="user")["by"] == USER
    old = sw.forum.subscribe(ANNA, "feed", by="Old Timer")
    retire(sw, "Old Timer")
    with pytest.raises(ToolError, match="Unknown member"):
        sw.forum.subscribe(ANNA, "reactions", by="Old Timer")
    marks = {s["id"]: s["by_former"] for s in sw.forum.subscriptions(ANNA)}
    assert marks[old["id"]] is True and not any(v for k, v in marks.items() if k != old["id"])
    # (the automatic rows of the members' creation, Swarm._create_member, have no actor)
    ops = [(e["actor"], e["data"]["op"]) for e in events(sw, "subscription.changed") if e["actor"] is not None]
    assert ops == [(ANNA, "created"), (ANNA, "changed")] + [(ANNA, "created")] * 5  # "", "." and "/": one row


def test_unsubscribe(make_swarm):
    sw = make_swarm()
    add(sw, ANNA), add(sw, BOB), add(sw, CARL, language="ru")
    for name in (ANNA, BOB, CARL):
        sw.forum.auto_subscribe(name)
    feed, reactions = sw.forum.subscriptions(ANNA)[:2]
    bob_feed = sw.forum.subscriptions(BOB)[0]
    with pytest.raises(ToolError) as err:
        sw.forum.unsubscribe(ANNA, feed["id"])
    assert str(err.value) == ("The feed cannot be unsubscribed: change its mode (subscribe what=feed "
                              "mode=digest|wake), or add narrower subscriptions.")
    with pytest.raises(ToolError) as err:
        sw.forum.unsubscribe(CARL, sw.forum.subscriptions(CARL)[0]["id"])
    assert str(err.value).startswith("Подписку на ленту нельзя отменить: смени ее режим")
    with pytest.raises(ToolError, match=rf"^No subscription s{bob_feed['id']} of yours$"):
        sw.forum.unsubscribe(ANNA, bob_feed["id"])
    with pytest.raises(ToolError, match=r"^No subscription s999 of yours$"):
        sw.forum.unsubscribe(ANNA, 999)
    sw.forum.unsubscribe(ANNA, reactions["id"])          # an automatic row other than the feed goes
    by_bob = sw.forum.subscribe(ANNA, "feed", by=BOB)
    sw.forum.unsubscribe(ANNA, by_bob["id"])             # a feed row with by goes too
    assert [s["kind"] for s in sw.forum.subscriptions(ANNA)] == ["feed", "post", "post"]
    removed = [e for e in events(sw, "subscription.changed") if e["data"]["op"] == "removed"]
    assert [(e["actor"], e["data"]["subscription"]["id"]) for e in removed] == [
        (ANNA, reactions["id"]), (ANNA, by_bob["id"])]
    assert sw.forum.subscriptions(ANNA)[0]["mode"] == "digest"
    assert sw.forum.subscribe(ANNA, "feed", mode="wake")["op"] == "changed"


def test_drop_subscriptions(sw):
    add(sw, ANNA), add(sw, BOB)
    sw.forum.auto_subscribe(ANNA), sw.forum.auto_subscribe(BOB)
    follow = sw.forum.subscribe(BOB, "feed", by=ANNA)
    assert sw.forum.drop_subscriptions(ANNA) == 4
    assert sw.forum.subscriptions(ANNA) == []
    last = events(sw, "subscription.changed")[-1]
    assert last["actor"] is None and last["data"] == {"member": ANNA, "op": "removed", "count": 4}
    retire(sw, ANNA)
    assert {s["id"]: s["by_former"] for s in sw.forum.subscriptions(BOB)}[follow["id"]] is True


# --- mentions and similar posts ----------------------------------------------------------------------------

def test_mentions(sw, delivered):
    for name in (ANNA, BOB, CARL):
        add(sw, name)
    add(sw, "Old Timer")
    add(sw, "Old Stone")
    retire(sw, "Old Timer")
    post = sw.forum.create(ANNA, "Ask @Bob Stone", "and @user, @Anna Smith, @Old Timer, @Nobody")
    assert delivered[-1] == ("post.created", [BOB, USER], False)
    sw.forum.update(ANNA, post["id"], text="and @user, @Carl Brown")
    assert delivered[-1][1] == [CARL]                     # only what the edit adds
    sw.forum.comment(USER, post["id"], "@пользователь @Old Stone look")
    assert delivered[-1][1] == ["Old Stone"]


def test_similar_open(sw):
    add(sw, ANNA), add(sw, BOB)
    set_goal(sw, "Parser benchmark results", "")
    other = sw.forum.create(BOB, "Parser benchmark results", "Numbers for the parser.\n\nDetails later.")
    own = sw.forum.create(ANNA, "Benchmark parser results", "")
    sw.forum.create(BOB, "Weather report", "")
    new = sw.forum.create(ANNA, "Results of the parser benchmark", "We ran it.")
    assert [p["id"] for p in sw.forum.similar_open(new)] == [other["id"]]  # not its own, not #1 / #2
    assert own["id"] not in [p["id"] for p in sw.forum.similar_open(new)]
    many = [sw.forum.create(BOB, f"Parser benchmark results {word}", "") for word in ("one", "two", "three", "four")]
    assert len(sw.forum.similar_open(new)) == 3 and many
    assert forummod.post_goal("First paragraph\n  \nsecond") == "First paragraph"
    assert len(forummod.post_goal("x" * 500)) == forummod.SIMILAR_GOAL_CHARS


def test_the_forum_is_the_swarm_s_in_a_thread_session_only(sw, make_swarm):
    assert isinstance(sw.forum, Forum) and sw.forum.swarm is sw
    assert make_swarm(mode="board").forum is None
    assert (GOAL_POST, PLAN_POST) == (1, 2)


# --- the clarify guard, the hint and the switch-off in a thread session (docs/SPEC.md §22.4 "Clarify") -------------
def _clarify_swarm(make_swarm) -> Swarm:
    """A clarify session with the LLM members Anna (the kickoff asker) and Bob, and a goal."""
    sw = make_swarm(clarify=True)
    add(sw, ANNA), add(sw, BOB)
    set_goal(sw)
    assert sw.forum.plan_data() == {"clarify": True, "asker": ANNA}
    return sw


def _kinds_to(sw: Swarm, recipient: str, kind: str) -> int:
    return sw.db.scalar("SELECT COUNT(*) FROM notifications WHERE recipient = ? AND kind = ?", (recipient, kind))


def _age(sw: Swarm, seconds: float = 10.0) -> None:
    """What happened so far (messages, comments, notices, #2) happened `seconds` earlier: the next rewrite of #2 comes
    strictly later, whatever the clock's resolution."""
    sw.db.execute("UPDATE messages SET ts = ts - ?", (seconds,))
    sw.db.execute("UPDATE post_comments SET ts = ts - ?", (seconds,))
    sw.db.execute("UPDATE notifications SET ts = ts - ?", (seconds,))
    sw.db.execute("UPDATE posts SET created_at = created_at - ?, edited_at = edited_at - ?", (seconds, seconds))


def test_asks_user_holds_for_the_asker_until_it_comments_on_2(make_swarm):
    sw = _clarify_swarm(make_swarm)
    assert not sw.asks_user(ANNA) and sw._in_clarify_phase(ANNA) and not sw._in_clarify_phase(BOB)
    sw.mail.send(ANNA, ["user"], "1. Which format? (a) JSON (b) CSV; default JSON")
    assert sw.asks_user(ANNA) and sw._awaits_user_answer(ANNA) and not sw._in_clarify_phase(ANNA)
    assert not sw.asks_user(BOB)                                   # only the asker of #2
    assert sw.snapshot()["swarm"]["clarify_wait"] == {"name": ANNA, "post_id": PLAN_POST}
    sw.forum.comment(ANNA, PLAN_POST, "Decisions: JSON.")
    assert not sw.asks_user(ANNA) and not sw._awaits_user_answer(ANNA)
    assert sw.snapshot()["swarm"]["clarify_wait"] is None
    # the setting off: the guard stands down, the wait itself is still read (_awaits_user_answer)
    sw2 = _clarify_swarm(make_swarm)
    sw2.mail.send(ANNA, ["user"], "Questions")
    sw2.config = dataclasses.replace(sw2.config, swarm=dataclasses.replace(sw2.config.swarm, clarify=False))
    assert not sw2.asks_user(ANNA) and sw2._awaits_user_answer(ANNA)


def test_an_old_message_or_comment_does_not_end_a_new_clarify_phase(make_swarm):
    sw = _clarify_swarm(make_swarm)
    sw.mail.send(ANNA, ["user"], "Questions about the first goal")
    sw.forum.comment(ANNA, PLAN_POST, "Decisions for the first goal.")
    assert not sw._in_clarify_phase(ANNA)
    _age(sw)
    set_goal(sw, "Build the lexer")                                # #2 is written again
    assert sw._in_clarify_phase(ANNA) and not sw.asks_user(ANNA)
    sw.mail.send(ANNA, ["user"], "Questions about the lexer")
    assert sw.asks_user(ANNA)                                      # the old comment does not count as planning


def test_planner_question_keeps_the_board_call():
    calls: list[tuple[str, tuple]] = []

    def scalar(sql: str, params: tuple) -> Any:
        calls.append((sql, params))
        return None

    assert autoreply.planner_question(scalar, ANNA, 7, 1.5) is None
    assert calls[0] == (autoreply.PLANS, (ANNA, 7))
    calls.clear()
    autoreply.planner_question(scalar, ANNA, PLAN_POST, 1.5, plans=autoreply.PLANS_THREAD)
    assert calls[0] == (autoreply.PLANS_THREAD, (ANNA, PLAN_POST, 1.5))
    assert "PLANS_THREAD" in autoreply.__all__


def test_the_clarify_hint_after_n_steps_then_2n_and_never_a_third(make_swarm):
    sw = _clarify_swarm(make_swarm)
    steps = sw.config.agents.planning_hint_steps
    runtime = SimpleNamespace(llm_steps=0)
    sw.runtimes[ANNA] = sw.runtimes[BOB] = runtime  # (only llm_steps is read)
    sw.hint_planner(ANNA)                           # the watch starts
    sw.hint_planner(None)                           # the scheduler's call is a no-op in a thread session
    runtime.llm_steps = steps - 1
    sw.hint_planner(ANNA)
    assert _kinds_to(sw, ANNA, "n_clarify_hint_thread") == 0
    runtime.llm_steps = steps
    sw.hint_planner(BOB)                            # another member's step hints nobody
    sw.hint_planner(ANNA)
    assert _kinds_to(sw, ANNA, "n_clarify_hint_thread") == 1
    sw.hint_planner(ANNA)                           # no step since the hint
    runtime.llm_steps = 2 * steps
    sw.hint_planner(ANNA)
    assert _kinds_to(sw, ANNA, "n_clarify_hint_thread") == 2
    runtime.llm_steps = 5 * steps
    sw.hint_planner(ANNA)
    assert _kinds_to(sw, ANNA, "n_clarify_hint_thread") == 2
    assert not _kinds_to(sw, ANNA, "n_clarify_hint") and not _kinds_to(sw, ANNA, "n_planning_hint")
    assert not _kinds_to(sw, BOB, "n_clarify_hint_thread")
    # a restart (a fresh watch) counts the hints already sent for this goal
    sw._planning_watch.clear()
    sw.hint_planner(ANNA)
    runtime.llm_steps = 9 * steps
    sw.hint_planner(ANNA)
    assert _kinds_to(sw, ANNA, "n_clarify_hint_thread") == 2


def test_no_clarify_hint_once_the_asker_asked_or_commented(make_swarm):
    sw = _clarify_swarm(make_swarm)
    runtime = SimpleNamespace(llm_steps=0)
    sw.runtimes[ANNA] = runtime
    sw.hint_planner(ANNA)
    sw.mail.send(ANNA, ["user"], "Questions")
    runtime.llm_steps = 10 * sw.config.agents.planning_hint_steps
    sw.hint_planner(ANNA)
    assert "p2" not in sw._planning_watch and not _kinds_to(sw, ANNA, "n_clarify_hint_thread")
    other = _clarify_swarm(make_swarm)
    other.runtimes[ANNA] = runtime
    other.forum.comment(ANNA, PLAN_POST, "No questions.")
    other.hint_planner(ANNA)
    assert not _kinds_to(other, ANNA, "n_clarify_hint_thread")
    paused = _clarify_swarm(make_swarm)  # paused or in wrap-up: no hint, the watch stays
    paused.runtimes[ANNA] = SimpleNamespace(llm_steps=0)
    paused.hint_planner(ANNA)
    paused.db.set("wrap_up", {"on": True})
    paused.runtimes[ANNA].llm_steps = 100
    paused.hint_planner(ANNA)
    assert not _kinds_to(paused, ANNA, "n_clarify_hint_thread") and "p2" in paused._planning_watch


def test_clarify_switched_off_tells_the_asker_once(make_swarm):
    sw = _clarify_swarm(make_swarm)
    sw._clarify_switched_off()
    assert _kinds_to(sw, ANNA, "n_clarify_off_thread") == 1 and not _kinds_to(sw, BOB, "n_clarify_off_thread")
    sw._clarify_switched_off(once=True)             # a start: not again since #2's last write
    assert _kinds_to(sw, ANNA, "n_clarify_off_thread") == 1
    assert not _kinds_to(sw, ANNA, "n_clarify_off")
    _age(sw)
    set_goal(sw, "Build the lexer")                 # a new write of #2: told again at a start
    sw._clarify_switched_off(once=True)
    assert _kinds_to(sw, ANNA, "n_clarify_off_thread") == 2
    # the asker that asked and waits is told too; one that commented on #2 is not
    asked = _clarify_swarm(make_swarm)
    asked.mail.send(ANNA, ["user"], "Questions")
    asked._clarify_switched_off(once=True)
    assert _kinds_to(asked, ANNA, "n_clarify_off_thread") == 1
    done = _clarify_swarm(make_swarm)
    done.forum.comment(ANNA, PLAN_POST, "No questions.")
    done._clarify_switched_off()
    assert not _kinds_to(done, ANNA, "n_clarify_off_thread")


def test_the_thread_workspace_emits_files_committed(sw, make_swarm):
    assert sw.workspace.commit_events and not make_swarm(mode="board").workspace.commit_events
