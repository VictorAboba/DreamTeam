"""Independent review (docs/SPEC.md §4 "Independent review"): with [swarm] independent_review on (the default)
an agent never approves work it executed. Board level against `FakeSwarm` with the default config; then the swarm
level (wake-up text, reminders, hand-over, the user's notice, the scheduler) against a real Swarm."""

from __future__ import annotations

import pytest

from dreamteam import prompts
from dreamteam import swarm as swarm_module
from dreamteam.board import Board
from dreamteam.config import Config, SwarmSettings, render_template
from dreamteam.db import loads
from dreamteam.models import USER, AgentKind, AgentStatus, Status, Task, ToolError
from dreamteam.swarm import QUIET_KEY
from test_runtime import call, kinds_for, make_swarm, run_until, shutdown

ANNA, BOB, CARL, DINA = "Anna Smith", "Bob Brown", "Carl Clark", "Dina Evans"
WAIT = 0.5  # agents.idle_nudge_seconds of the swarm-level tests


@pytest.fixture
def swarm(fake_swarm):
    fake_swarm.board = Board(fake_swarm)
    fake_swarm.add_member(ANNA, language="en")
    fake_swarm.add_member(BOB, language="en")
    fake_swarm.add_member(CARL, language="ru")
    fake_swarm.add_member(DINA, kind=AgentKind.EXTERNAL, language="en")
    return fake_swarm


@pytest.fixture
def board(swarm) -> Board:
    return swarm.board


def in_review(board: Board, creator: str, executor: str, title: str = "t", **kw) -> Task:
    task = board.create(creator, title, **kw)
    board.claim(executor, task.id)
    return board.complete(executor, task.id, "res")


def updates(swarm) -> list[dict]:
    return [e for e in swarm.bus.recent(limit=1000) if e["type"] == "task.updated"]


def set_status(swarm, name: str, status: str) -> None:
    swarm.db.execute("UPDATE agents SET status = ? WHERE name = ?", (status, name))


def stored(swarm, name: str) -> list[tuple[str, bool]]:
    """(kind, sent with wake=False) of `name`'s stored notifications, oldest first."""
    rows = swarm.db.all("SELECT kind, data FROM notifications WHERE recipient = ? ORDER BY id", (name,))
    return [(r["kind"], bool(loads(r["data"], {}).get(QUIET_KEY))) for r in rows]


def test_the_switch_defaults_to_on(swarm, board):
    assert board.independent_review is True


def test_completion_picks_a_member_who_neither_created_nor_did_it(swarm, board):
    t = board.create(ANNA, "t")
    board.claim(BOB, t.id)
    swarm.notified.clear()
    done = board.complete(BOB, t.id, "built it")
    # ANNA created it, BOB did it, DINA is external: CARL
    assert done.status == Status.REVIEW and done.reviewer == CARL and done.assignee == BOB
    assert swarm.notified == [(CARL, "n_review_requested", {"id": t.id, "title": "t", "by": BOB})]
    assert updates(swarm)[-1]["data"]["changes"] == {
        "result": [None, "built it"], "status": ["in_progress", "review"], "reviewer": [None, CARL]}


def test_pick_order_idle_then_load_then_name(swarm, board):
    t = board.create(USER, "t", assignee=BOB)
    assert board.pick_reviewer(t) == ANNA                           # both idle and free: by name
    set_status(swarm, ANNA, AgentStatus.WORKING)
    assert board.pick_reviewer(t) == CARL                           # idle before working
    set_status(swarm, CARL, AgentStatus.ERROR)
    assert board.pick_reviewer(t) == ANNA                           # working before any other status
    set_status(swarm, ANNA, AgentStatus.IDLE)
    set_status(swarm, CARL, AgentStatus.IDLE)
    board.create(USER, "held", assignee=ANNA)
    assert board.pick_reviewer(t) == CARL                           # fewer open tasks + pending reviews
    in_review(board, USER, BOB, "pending", reviewer=CARL)
    in_review(board, USER, BOB, "pending too", reviewer=CARL)
    assert board.pick_reviewer(t) == ANNA                           # CARL now has 2 reviews waiting
    assert board.pick_reviewer(t, exclude=[ANNA]) == CARL
    # the creator comes last among the agents, whatever its status or load
    by_carl = board.create(CARL, "by carl", assignee=BOB)
    set_status(swarm, ANNA, AgentStatus.PAUSED)
    assert board.pick_reviewer(by_carl) == ANNA
    assert board.pick_reviewer(by_carl, exclude={ANNA}) == CARL
    # retired and external members are never picked: nobody left means the user
    set_status(swarm, ANNA, AgentStatus.RETIRED)
    set_status(swarm, CARL, AgentStatus.RETIRED)
    assert board.pick_reviewer(t) == USER


def test_creator_reviews_when_nobody_else_can(swarm, board):
    set_status(swarm, CARL, AgentStatus.RETIRED)
    t = in_review(board, ANNA, BOB)
    assert t.reviewer == ANNA
    assert swarm.notifications_for(ANNA)[-1] == ("n_review_requested", {"id": t.id, "title": "t", "by": BOB})


def test_single_agent_sends_the_review_to_the_user(swarm, board):
    set_status(swarm, ANNA, AgentStatus.RETIRED)
    set_status(swarm, CARL, AgentStatus.RETIRED)
    t = in_review(board, USER, BOB)
    assert t.status == Status.REVIEW and t.reviewer == USER
    assert (USER, "u_review_fallback", {"by": BOB, "id": t.id, "title": "t"}) in swarm.notified
    for lang in ("en", "ru"):
        assert f"/approve {t.id}" in prompts.t(lang, "u_review_fallback", by=BOB, id=t.id, title="t")


def test_explicit_reviewer_is_kept_and_user_reviewer_is_notified(swarm, board):
    # an external member is never picked, but can be designated
    t = in_review(board, ANNA, BOB, reviewer=DINA)
    assert t.reviewer == DINA
    assert swarm.notifications_for(DINA) == [("n_review_requested", {"id": t.id, "title": "t", "by": BOB})]
    assert "reviewer" not in updates(swarm)[-1]["data"]["changes"]
    # so can the creator and the user
    t2 = in_review(board, ANNA, BOB, "t2", reviewer=ANNA)
    assert t2.reviewer == ANNA
    swarm.notified.clear()
    t3 = in_review(board, ANNA, BOB, "t3", reviewer=USER)
    assert t3.reviewer == USER
    assert swarm.notifications_for(USER) == [("u_review_requested", {"by": BOB, "id": t3.id, "title": "t3"})]
    # a designated reviewer that left the swarm is replaced on completion
    t4 = board.create(ANNA, "t4", reviewer=CARL)
    board.claim(BOB, t4.id)
    set_status(swarm, CARL, AgentStatus.RETIRED)
    assert board.complete(BOB, t4.id, "res").reviewer == ANNA


def test_executor_cannot_be_the_reviewer(swarm, board):
    with pytest.raises(ToolError, match="Bob Brown is the assignee of the task: a task is reviewed by someone other "
                                        "than its assignee"):
        board.create(ANNA, "t", assignee=BOB, reviewer=BOB)
    assert board.create(ANNA, "for the user", assignee=USER, reviewer=USER).reviewer == USER
    t = board.create(ANNA, "t", assignee=BOB)
    for actor in (ANNA, USER):
        with pytest.raises(ToolError, match="Bob Brown is the assignee of task #2: .*leave the reviewer empty"):
            board.update(actor, t.id, reviewer=BOB)
    with pytest.raises(ToolError, match="is the assignee of task #2"):
        board.update(ANNA, t.id, assignee=CARL, reviewer=CARL)
    # handing a task to its designated reviewer clears the reviewer
    t2 = board.create(ANNA, "t2", reviewer=CARL)
    upd = board.update(ANNA, t2.id, assignee=CARL)
    assert upd.assignee == CARL and upd.reviewer is None
    assert updates(swarm)[-1]["data"]["changes"] == {"assignee": [None, CARL], "reviewer": [CARL, None]}
    # so does a claim by the designated reviewer; another member reviews it then
    t3 = board.create(ANNA, "t3", reviewer=CARL)
    claimed = board.claim(CARL, t3.id)
    assert claimed.assignee == CARL and claimed.reviewer is None
    assert updates(swarm)[-1]["data"]["changes"] == {
        "assignee": [None, CARL], "status": ["todo", "in_progress"], "reviewer": [CARL, None]}
    assert board.complete(CARL, t3.id, "res").reviewer == BOB      # ANNA created it


def test_assignee_never_approves_even_with_legacy_rows(swarm, board):
    t = in_review(board, ANNA, BOB)
    with pytest.raises(ToolError, match=r"You are the assignee of task #1: your own work is reviewed by someone else "
                                        r"\(Carl Clark\)\. Wait for the verdict"):
        board.review(BOB, t.id, True)
    # a row written while the switch was off: the executor is its own reviewer
    legacy = board.create(ANNA, "legacy")
    swarm.db.execute("UPDATE tasks SET assignee = ?, reviewer = ?, status = 'review' WHERE id = ?",
                     (BOB, BOB, legacy.id))
    with pytest.raises(ToolError, match=r"reviewed by someone else \(the user\)"):
        board.review(BOB, legacy.id, True)
    assert board.review(USER, legacy.id, True).status == Status.DONE


def test_in_review_guards(swarm, board):
    t = in_review(board, ANNA, BOB)
    assert t.reviewer == CARL
    for actor in (BOB, ANNA):      # the executor and the creator
        with pytest.raises(ToolError, match="Task #1 is waiting for review, so its assignee cannot change now: the "
                                            "reviewer approves it or returns it to Bob Brown"):
            board.update(actor, t.id, assignee=DINA)
    with pytest.raises(ToolError, match="needs a reviewer: pass another member or 'user' instead of clearing it"):
        board.update(ANNA, t.id, reviewer="")
    # another reviewer: the new one is asked, the old one gets quiet news
    swarm.notified.clear()
    upd = board.update(USER, t.id, reviewer=DINA)
    assert upd.reviewer == DINA and upd.status == Status.REVIEW
    assert swarm.notifications_for(DINA) == [("n_review_requested", {"id": t.id, "title": "t", "by": USER})]
    assert swarm.notifications_for(CARL) == [("n_review_cancelled", {"id": t.id, "title": "t", "by": USER})]
    assert stored(swarm, CARL)[-1] == ("n_review_cancelled", True)
    # the user hands the task to its reviewer: a new pick excludes both the new and the old assignee
    swarm.notified.clear()
    upd = board.update(USER, t.id, assignee=DINA)
    assert (upd.assignee, upd.reviewer, upd.status) == (DINA, CARL, Status.REVIEW)     # ANNA created it
    assert updates(swarm)[-1]["data"]["changes"] == {"assignee": [BOB, DINA], "reviewer": [DINA, CARL]}
    assert swarm.notifications_for(CARL) == [("n_review_requested", {"id": t.id, "title": "t", "by": USER})]
    # the executor withdraws its work: the reviewer no longer needs to review it
    t2 = in_review(board, ANNA, BOB, "t2")
    swarm.notified.clear()
    assert board.update(BOB, t2.id, status="in_progress").status == Status.IN_PROGRESS
    assert swarm.notifications_for(t2.reviewer) == [("n_review_cancelled", {"id": t2.id, "title": "t2", "by": BOB})]


def test_reviewer_leaving_repicks_and_executor_leaving_keeps_the_review(swarm, board):
    t = in_review(board, ANNA, BOB)
    assert t.reviewer == CARL
    swarm.notified.clear()
    assert board.release_all(CARL, USER) == []
    task = board.get(t.id)
    assert (task.status, task.assignee, task.reviewer) == (Status.REVIEW, BOB, ANNA)
    assert updates(swarm)[-1]["data"]["changes"] == {"reviewer": [CARL, ANNA]}
    assert swarm.notified == [(ANNA, "n_review_requested", {"id": t.id, "title": "t", "by": BOB})]
    # the executor leaves: done work stays in review without an assignee and is not released
    other = board.create(ANNA, "other")
    board.claim(BOB, other.id)
    assert board.release_all(BOB, USER) == [other.id]
    task = board.get(t.id)
    assert (task.status, task.assignee, task.reviewer) == (Status.REVIEW, None, ANNA)
    assert board.get(other.id).status == Status.TODO
    assert board.review(ANNA, t.id, False, "redo").status == Status.TODO
    # a review left by the only other agent goes to the user
    set_status(swarm, CARL, AgentStatus.RETIRED)
    t2 = in_review(board, USER, BOB, "t2")
    assert t2.reviewer == ANNA
    swarm.notified.clear()
    board.release_all(ANNA, USER)
    assert board.get(t2.id).reviewer == USER
    assert swarm.notified == [(USER, "u_review_fallback", {"by": BOB, "id": t2.id, "title": "t2"})]


def test_user_settles_reviews(swarm, board):
    t = in_review(board, ANNA, BOB)
    swarm.notified.clear()
    assert board.review(USER, t.id, True).status == Status.DONE
    assert swarm.notifications_for(BOB) == [("n_review_approved", {"id": t.id, "title": "t", "by": USER,
                                                                     "comment": ""})]
    assert swarm.notifications_for(CARL) == [("n_review_cancelled", {"id": t.id, "title": "t", "by": USER})]
    assert stored(swarm, CARL)[-1] == ("n_review_cancelled", True)
    # done set directly on a task in review is an approval too
    t2 = in_review(board, ANNA, BOB, "t2")
    swarm.notified.clear()
    assert board.update(USER, t2.id, status="done").status == Status.DONE
    assert swarm.notifications_for(BOB) == [("n_review_approved", {"id": t2.id, "title": "t2", "by": USER,
                                                                     "comment": ""})]
    assert stored(swarm, BOB)[-1] == ("n_review_approved", False)
    assert swarm.notifications_for(t2.reviewer) == [("n_review_cancelled", {"id": t2.id, "title": "t2",
                                                                             "by": USER})]
    # the user's own completion follows the old rule: done unless an agent reviewer is set
    t3 = board.create(ANNA, "t3")
    assert board.complete(USER, t3.id, "did it").status == Status.DONE
    t4 = board.create(ANNA, "t4", reviewer=CARL)
    assert board.complete(USER, t4.id, "did it").status == Status.REVIEW


def test_rejection_returns_to_the_same_reviewer(swarm, board):
    t = in_review(board, ANNA, BOB)
    assert board.review(CARL, t.id, False, "add tests").status == Status.IN_PROGRESS
    swarm.notified.clear()
    again = board.complete(BOB, t.id, "with tests")
    assert again.status == Status.REVIEW and again.reviewer == CARL
    assert swarm.notified == [(CARL, "n_review_requested", {"id": t.id, "title": "t", "by": BOB})]
    assert "reviewer" not in updates(swarm)[-1]["data"]["changes"]


def test_switch_off_is_legacy_and_hot(swarm, board):
    swarm.config.swarm.independent_review = False
    assert board.independent_review is False
    t = board.create(ANNA, "t", assignee=BOB, reviewer=BOB)         # allowed without the rule
    board.claim(BOB, t.id)
    assert board.complete(BOB, t.id, "res").status == Status.DONE   # its own reviewer: done
    t2 = board.create(ANNA, "t2")
    board.claim(BOB, t2.id)
    assert board.complete(BOB, t2.id, "res").status == Status.DONE  # no reviewer: done
    swarm.config.swarm.independent_review = True
    t3 = board.create(ANNA, "t3")
    board.claim(BOB, t3.id)
    assert board.complete(BOB, t3.id, "res").status == Status.REVIEW


def test_reviews_of_and_short_line(swarm, board):
    t = in_review(board, ANNA, BOB, priority=2)
    t2 = in_review(board, ANNA, BOB, "t2")
    board.create(ANNA, "not yet", reviewer=CARL)
    in_review(board, ANNA, BOB, "for dina", reviewer=DINA)
    assert [x.id for x in board.reviews_of(CARL)] == [t.id, t2.id]
    assert board.reviews_of(BOB) == []
    assert board.get(t.id).short() == "#1 [review] t @Bob Brown review:Carl Clark (high)"
    assert "review:" not in board.get(3).short()                   # a reviewer is shown only in review


def test_approval_wakes_the_executor(swarm, board):
    t = in_review(board, ANNA, BOB)
    board.review(CARL, t.id, False, "redo")
    assert stored(swarm, BOB)[-1] == ("n_review_rejected", False)   # a rejection wakes the assignee
    board.complete(BOB, t.id, "fixed")
    swarm.db.execute("UPDATE notifications SET delivered_at = 1 WHERE recipient = ?", (BOB,))
    board.review(CARL, t.id, True, "good")
    # an approval wakes the executor too: after task_complete it may have ended its turn with `wait` for the verdict
    assert stored(swarm, BOB)[-1] == ("n_review_approved", False)
    assert swarm.has_pending(BOB)


def test_hand_over_review(swarm, board):
    t = in_review(board, ANNA, BOB)
    assert t.reviewer == CARL
    swarm.notified.clear()
    moved = board.hand_over_review(t.id, exclude={CARL})
    assert moved is not None and moved.reviewer == ANNA            # the creator: nobody else qualifies
    event = updates(swarm)[-1]
    assert event["actor"] is None and event["data"]["changes"] == {"reviewer": [CARL, ANNA]}
    assert swarm.notified == [
        (ANNA, "n_review_requested", {"id": t.id, "title": "t", "by": BOB}),
        (CARL, "n_review_reassigned", {"id": t.id, "title": "t", "reviewer": ANNA}),
    ]
    assert stored(swarm, CARL)[-1] == ("n_review_reassigned", True)
    # nobody else qualifies: the user, who is told that no agent took the review over (not "Bob finished it, and
    # no other member can review it": two other agents exist)
    swarm.notified.clear()
    assert board.hand_over_review(t.id, exclude={CARL}).reviewer == USER
    assert swarm.notified == [
        (USER, "u_review_handed_over", {"id": t.id, "title": "t", "reviewer": ANNA}),
        (ANNA, "n_review_reassigned", {"id": t.id, "title": "t", "reviewer": USER}),
    ]
    # a task no longer in review is left alone
    board.review(USER, t.id, True)
    before = len(updates(swarm))
    assert board.hand_over_review(t.id, exclude=()) is None and len(updates(swarm)) == before


def test_review_of_a_task_whose_executor_left(swarm, board):
    t = in_review(board, ANNA, BOB)
    assert t.reviewer == CARL
    board.release_all(BOB, USER)                                    # the executor leaves: the review goes on
    set_status(swarm, BOB, AgentStatus.RETIRED)
    # handed over: the review comes from the reviewer that let it lapse, not from "user"
    swarm.notified.clear()
    assert board.hand_over_review(t.id, exclude={CARL}).reviewer == ANNA
    assert swarm.notified[0] == (ANNA, "n_review_requested", {"id": t.id, "title": "t", "by": CARL})
    # the user gets it when no agent can take it over, and is told so (it is never handed to the user silently)
    swarm.notified.clear()
    assert board.hand_over_review(t.id, exclude={CARL}).reviewer == USER
    assert swarm.notified[0] == (USER, "u_review_handed_over", {"id": t.id, "title": "t", "reviewer": ANNA})
    # the reviewer leaves: the same, with the member that left as the sender
    t2 = in_review(board, ANNA, CARL, "t2")
    assert t2.reviewer == ANNA                                      # the creator: nobody else qualifies
    board.release_all(CARL, USER)
    set_status(swarm, CARL, AgentStatus.RETIRED)
    swarm.notified.clear()
    board.release_all(ANNA, USER)
    assert board.get(t2.id).reviewer == USER
    assert swarm.notified == [(USER, "u_review_handed_over", {"id": t2.id, "title": "t2", "reviewer": ANNA})]


def test_review_notifications_render_in_both_languages(swarm, board):
    t = in_review(board, ANNA, BOB)
    board.update(USER, t.id, reviewer=DINA)
    board.hand_over_review(t.id, exclude=())
    set_status(swarm, ANNA, AgentStatus.RETIRED)
    set_status(swarm, CARL, AgentStatus.RETIRED)
    board.hand_over_review(t.id, exclude=())
    in_review(board, USER, BOB, "t2")
    in_review(board, USER, BOB, "t3", reviewer=USER)
    kinds = {k for _, k, _ in swarm.notified}
    assert {"n_review_requested", "n_review_cancelled", "n_review_reassigned", "u_review_requested",
            "u_review_fallback", "u_review_handed_over"} <= kinds
    for _, key, kw in swarm.notified:
        for lang in ("en", "ru"):
            assert prompts.t(lang, key, **kw)


def test_config_flag(make_fake_swarm, tmp_path):
    assert SwarmSettings().independent_review is True
    off = make_fake_swarm({"swarm": {"independent_review": False}}, subdir="off")
    off.board = Board(off)
    assert off.board.independent_review is False
    with pytest.raises(ValueError, match=r"\[swarm\] independent_review"):
        Config.from_dict(tmp_path / "bad", {"swarm": {"independent_review": "yes"}})
    assert "\nindependent_review = true" in render_template("X", "ru", "anthropic")


def test_pick_prefers_the_reviewer_role(swarm, board):
    """The catalog's reviewer role («Ревьюер»/Reviewer, any language) comes right after the creator rule; a tester
    is not preferred (its own test tasks would starve)."""
    t = board.create(USER, "t", assignee=BOB)
    swarm.db.execute("UPDATE agents SET role = 'Tester' WHERE name = ?", (ANNA,))
    assert board.pick_reviewer(t) == ANNA                           # a tester is just a member: by name
    swarm.db.execute("UPDATE agents SET role = 'Ревьюер' WHERE name = ?", (CARL,))
    assert board.pick_reviewer(t) == CARL
    set_status(swarm, CARL, AgentStatus.WORKING)
    board.create(USER, "held", assignee=CARL)
    assert board.pick_reviewer(t) == CARL                           # the role before status and load
    assert board.pick_reviewer(board.create(CARL, "by carl", assignee=BOB)) == ANNA   # the creator rule first
    swarm.db.execute("UPDATE agents SET role = 'reviewer' WHERE name = ?", (ANNA,))
    set_status(swarm, CARL, AgentStatus.IDLE)
    assert board.pick_reviewer(t) == ANNA                           # two reviewers: the usual order between them


# ================================================================== swarm level
# A real Swarm without running agents: `IdleRuntime` stands in for an agent that is idle and never reviews, so the
# scheduler steps (_remind_reviewers, _nudge_idle, _remind_user_reviews) can be called deterministically. The last
# test runs real scripted agents through the scheduler.

class IdleRuntime:
    """A running agent that stays idle: the scheduler sees a runtime; waking it does nothing."""
    stalled: frozenset = frozenset()
    interrupted = False
    _task = None

    def wake(self) -> None:
        pass

    def reset_llm(self) -> None:
        pass

    async def stop(self) -> None:
        pass


async def live_team(tmp_path, *names: str, **swarm_overrides):
    swarm, _ = make_swarm(tmp_path, lambda *a: call("wait"), swarm={"start_agents": 0, **swarm_overrides},
                          agents={"idle_nudge_seconds": WAIT})
    await swarm.start(run_agents=False)
    for name in names:
        await swarm.hire(USER, "Developer", name=name)
        swarm.runtimes[name] = IdleRuntime()
    deliver(swarm)
    return swarm


def deliver(swarm, *names: str) -> None:
    """The members read their pending notifications (as at a wake-up): they are idle with nothing pending."""
    for name in names or [m["name"] for m in swarm.members()]:
        swarm.render_wake(name)


def age(swarm, task_id: int, seconds: float = 2 * WAIT) -> None:
    swarm.db.execute("UPDATE tasks SET updated_at = updated_at - ? WHERE id = ?", (seconds, task_id))


def review_for(swarm, reviewer: str, executor: str, title: str = "t") -> Task:
    """A task the user created for `executor`, now waiting for `reviewer` (designated), aged past the wait."""
    task = swarm.board.create(USER, title, goal="g", reviewer=reviewer)
    swarm.board.claim(executor, task.id)
    task = swarm.board.complete(executor, task.id, "done")
    age(swarm, task.id)
    deliver(swarm)
    return task


async def test_wake_lists_reviews_waiting(tmp_path):
    swarm = await live_team(tmp_path, ANNA, BOB, CARL)
    try:
        t = review_for(swarm, CARL, BOB)
        text = swarm.render_wake(CARL)
        assert prompts.t("en", "my_reviews", tasks=f"#{t.id} t ({BOB})") in text.splitlines()
        assert "Waiting for your review" not in swarm.render_wake(ANNA)
        # the reviewer of a task whose executor left sees "-" as its assignee
        swarm.board.release_all(BOB, USER)
        assert f"#{t.id} t (-)" in swarm.render_wake(CARL)
    finally:
        await shutdown(swarm)


async def test_idle_reviewer_is_reminded_once_per_set(tmp_path):
    swarm = await live_team(tmp_path, ANNA, BOB, CARL)
    try:
        t = swarm.board.create(USER, "t", goal="g", reviewer=CARL)
        swarm.board.claim(BOB, t.id)
        swarm.board.complete(BOB, t.id, "done")
        deliver(swarm)
        swarm._remind_reviewers()
        assert "n_review_reminder" not in kinds_for(swarm, CARL)     # not due yet (agents.idle_nudge_seconds)
        age(swarm, t.id)
        swarm._remind_reviewers()
        assert kinds_for(swarm, CARL).count("n_review_reminder") == 1
        assert swarm.db.one("SELECT text FROM notifications WHERE recipient = ? ORDER BY id DESC LIMIT 1",
                            (CARL,))["text"] == prompts.t("en", "n_review_reminder", tasks=f"#{t.id} t")
        for _ in range(3):                                           # woken, it calls wait again: no repeat
            deliver(swarm, CARL)
            swarm._remind_reviewers()
        assert kinds_for(swarm, CARL).count("n_review_reminder") == 1
        t2 = review_for(swarm, CARL, BOB, "t2")                      # a new set: exactly one more reminder
        swarm._remind_reviewers()
        swarm._remind_reviewers()
        assert kinds_for(swarm, CARL).count("n_review_reminder") == 2
        assert prompts.t("en", "n_review_reminder", tasks=f"#{t.id} t; #{t2.id} t2") in swarm.render_wake(CARL)
        assert swarm.board.get(t.id).reviewer == CARL                # not handed over yet
    finally:
        await shutdown(swarm)


async def test_ignored_review_is_handed_to_another_member(tmp_path):
    swarm = await live_team(tmp_path, ANNA, BOB, CARL)
    try:
        t = review_for(swarm, CARL, BOB)
        swarm._remind_reviewers()                                    # reminded; CARL calls wait again
        deliver(swarm, CARL)
        swarm._remind_reviewers()
        assert swarm.board.get(t.id).reviewer == CARL                # the reminder gets its time
        key, since = swarm._review_reminded[CARL]
        swarm._review_reminded[CARL] = (key, since - WAIT)            # agents.idle_nudge_seconds later
        swarm._remind_reviewers()
        assert swarm.board.get(t.id).reviewer == ANNA
        event = swarm.bus.recent(types=("task.updated",), limit=1)[-1]
        assert event["actor"] is None and event["data"]["changes"] == {"reviewer": [CARL, ANNA]}
        assert stored(swarm, CARL)[-1] == ("n_review_reassigned", True)   # quiet news
        row = swarm.db.one("SELECT data FROM notifications WHERE recipient = ? AND kind = 'n_review_requested'",
                           (ANNA,))
        assert loads(row["data"]) == {"id": t.id, "title": "t", "by": BOB}
        assert CARL not in swarm._review_reminded
        # ANNA ignores it too: it never goes back to CARL, so it is the user's
        age(swarm, t.id)
        deliver(swarm, ANNA)
        swarm._remind_reviewers()
        deliver(swarm, ANNA)
        key, since = swarm._review_reminded[ANNA]
        swarm._review_reminded[ANNA] = (key, since - WAIT)
        swarm._remind_reviewers()
        assert swarm.board.get(t.id).reviewer == USER
        row = swarm.db.one("SELECT data FROM notifications WHERE recipient = ? AND kind = 'u_review_handed_over'",
                           (USER,))
        assert loads(row["data"]) == {"id": t.id, "title": "t", "reviewer": ANNA}
    finally:
        await shutdown(swarm)


async def test_review_goes_to_the_user_when_no_other_member_can_take_it(tmp_path):
    swarm = await live_team(tmp_path, BOB, CARL)
    try:
        t = review_for(swarm, CARL, BOB)
        swarm._remind_reviewers()
        deliver(swarm, CARL)
        key, since = swarm._review_reminded[CARL]
        swarm._review_reminded[CARL] = (key, since - WAIT)
        swarm._remind_reviewers()
        assert swarm.board.get(t.id).reviewer == USER
        row = swarm.db.one("SELECT data FROM notifications WHERE recipient = ? AND kind = 'u_review_handed_over'",
                           (USER,))
        assert loads(row["data"]) == {"id": t.id, "title": "t", "reviewer": CARL}
    finally:
        await shutdown(swarm)


async def test_reviewer_in_error_is_handed_over_without_a_reminder(tmp_path):
    swarm = await live_team(tmp_path, ANNA, BOB, CARL)
    try:
        t = review_for(swarm, CARL, BOB)
        swarm.db.execute("UPDATE agents SET status = ? WHERE name = ?", (AgentStatus.ERROR, CARL))
        swarm._remind_reviewers()                                    # it cannot act: only the clock starts
        key, since = swarm._review_reminded[CARL]
        swarm._review_reminded[CARL] = (key, since - 1.5 * WAIT)      # 1.5 x agents.idle_nudge_seconds
        swarm._remind_reviewers()
        assert swarm.board.get(t.id).reviewer == CARL
        swarm._review_reminded[CARL] = (key, since - 2 * WAIT)        # 2 x agents.idle_nudge_seconds
        swarm._remind_reviewers()
        assert swarm.board.get(t.id).reviewer == ANNA
        assert "n_review_reminder" not in kinds_for(swarm, CARL)
    finally:
        await shutdown(swarm)


async def test_reminders_and_hand_overs_need_independent_review(tmp_path):
    swarm = await live_team(tmp_path, ANNA, BOB, CARL, independent_review=False)
    try:
        t = review_for(swarm, CARL, BOB)
        swarm._remind_reviewers()
        assert "n_review_reminder" not in kinds_for(swarm, CARL) and swarm.board.get(t.id).reviewer == CARL
    finally:
        await shutdown(swarm)


async def test_nudge_skips_members_with_reviews(tmp_path):
    swarm = await live_team(tmp_path, ANNA, BOB, CARL)
    try:
        review_for(swarm, CARL, BOB)
        swarm.set_goal(USER, "Ship it")                              # active goal; the board is not empty
        swarm.board.create(USER, "ready one", goal="g")
        swarm.board.create(USER, "ready two", goal="g")
        for _ in range(4):
            deliver(swarm)
            for m in swarm.members():                                # no wait between offers in this test
                swarm._last_nudge.pop(m["name"], None)
            swarm._nudge_idle()
        assert "n_ready" not in kinds_for(swarm, CARL)               # it is reminded of its review instead
        assert "n_ready" in kinds_for(swarm, ANNA) and "n_ready" in kinds_for(swarm, BOB)
    finally:
        await shutdown(swarm)


async def test_user_is_told_when_only_their_reviews_remain(tmp_path):
    """One agent and a chain impl -> tests: impl waits for the user's review, tests is blocked by it. The user is
    told once that the team cannot go on; not while a member works or has work it can take."""
    swarm = await live_team(tmp_path, BOB)
    try:
        board = swarm.board
        impl = board.create(USER, "impl", goal="g")
        board.create(USER, "tests", goal="g", depends_on=[impl.id])
        swarm.set_goal(USER, "Ship it")
        board.claim(BOB, impl.id)
        assert board.complete(BOB, impl.id, "done").reviewer == USER
        deliver(swarm)
        swarm.db.execute("UPDATE agents SET status = ? WHERE name = ?", (AgentStatus.WORKING, BOB))
        swarm._remind_user_reviews()
        assert "u_reviews_waiting" not in kinds_for(swarm, USER)     # a member is working
        swarm.db.execute("UPDATE agents SET status = ? WHERE name = ?", (AgentStatus.IDLE, BOB))
        extra = board.create(USER, "extra", goal="g")
        swarm._remind_user_reviews()
        assert "u_reviews_waiting" not in kinds_for(swarm, USER)     # there is a ready task
        board.update(USER, extra.id, status="cancelled")
        deliver(swarm)
        swarm._remind_user_reviews()
        swarm._remind_user_reviews()
        assert kinds_for(swarm, USER).count("u_reviews_waiting") == 1
        row = swarm.db.one("SELECT text FROM notifications WHERE recipient = ? AND kind = 'u_reviews_waiting'",
                           (USER,))
        assert row["text"] == prompts.t("en", "u_reviews_waiting", tasks=f"#{impl.id}")
    finally:
        await shutdown(swarm)


async def test_retire_reviewer_repicks(tmp_path):
    swarm = await live_team(tmp_path, ANNA, BOB, CARL)
    try:
        t = swarm.board.create(USER, "Build", goal="built", assignee=ANNA, reviewer=BOB)
        swarm.board.claim(ANNA, t.id)
        swarm.board.complete(ANNA, t.id, "built it")
        assert await swarm.retire(USER, BOB) == []
        task = swarm.board.get(t.id)
        assert (task.status, task.assignee, task.reviewer) == (Status.REVIEW, ANNA, CARL)
        assert "n_review_requested" in kinds_for(swarm, CARL)
        assert "n_reviewer_left" not in kinds_for(swarm, ANNA)
    finally:
        await shutdown(swarm)


async def test_snapshot_exposes_the_switch(tmp_path):
    swarm = await live_team(tmp_path)
    try:
        assert swarm.snapshot()["swarm"]["independent_review"] is True
        swarm.config.swarm.independent_review = False                # hot: read on every call
        assert swarm.snapshot()["swarm"]["independent_review"] is False
    finally:
        await shutdown(swarm)
    swarm = await live_team(tmp_path / "off", independent_review=False)
    try:
        assert swarm.snapshot()["swarm"]["independent_review"] is False
    finally:
        await shutdown(swarm)


async def test_restart_wakes_a_member_whose_review_waits(tmp_path):
    swarm = await live_team(tmp_path, ANNA, BOB, CARL)
    swarm.set_goal(USER, "Ship it")
    t = review_for(swarm, CARL, BOB)
    swarm.runtimes.clear()
    await shutdown(swarm)
    swarm, _ = make_swarm(tmp_path, lambda *a: call("wait"), swarm={"start_agents": 0})
    await swarm.start(run_agents=False)
    try:
        assert swarm.board.get(t.id).reviewer == CARL
        assert kinds_for(swarm, CARL)[-1] == "n_restart" and swarm.has_pending(CARL)
    finally:
        await shutdown(swarm)


async def test_scripted_reviewers_that_ignore_a_review_pass_it_on(tmp_path, monkeypatch):
    """The scheduler end to end, with agents that always call wait: the reviewer is reminded, the review is handed
    to the other member, then to the user, who is told."""
    monkeypatch.setattr(swarm_module, "SCHEDULER_SECONDS", 0.05)
    swarm, _ = make_swarm(tmp_path, lambda *a: call("wait"), swarm={"start_agents": 0},
                          agents={"idle_nudge_seconds": 0.2})
    await swarm.start()
    try:
        for name in (ANNA, BOB, CARL):
            await swarm.hire(USER, "Developer", name=name)
        t = swarm.board.create(USER, "t", goal="g", reviewer=CARL)
        swarm.board.claim(BOB, t.id)
        swarm.board.complete(BOB, t.id, "done")
        await run_until(lambda: swarm.board.get(t.id).reviewer == USER, 15)
        assert "n_review_reminder" in kinds_for(swarm, CARL) and "n_review_reminder" in kinds_for(swarm, ANNA)
        assert kinds_for(swarm, CARL).count("n_review_reassigned") == 1
        assert kinds_for(swarm, ANNA).count("n_review_reassigned") == 1
        assert "u_review_handed_over" in kinds_for(swarm, USER)
    finally:
        await shutdown(swarm)
