"""Crew tasks at the swarm level (docs/SPEC.md §3 items 1-3, §4.2 "Offers" and Liveness, §6.2): offers keyed by the
pair (task id, finish state), the liveness key (stalled ids, marks) of the scheduler's items 2 and 3, the
in-activation nudge of a crew task's owner, the user's review reminder, and the seams other parts read (snapshot,
events, settings, session statistics, export). Crew tasks are built with tests/helpers/crew.py, straight into the
rows; the members are `IdleRuntime`s of a real Swarm (test_review.py), except for the nudge, which runs a scripted
agent."""

from __future__ import annotations

import dataclasses
import sqlite3
import time

import pytest

from dreamteam import prompts
from dreamteam import settings as st
from dreamteam.db import Database
from dreamteam.models import USER, AgentStatus, Status
from dreamteam.sessions import read_stats
from dreamteam.tools import frozen
from dreamteam.transfer import export_home
from helpers.crew import finish_raw, make_crew_raw, set_raw
from test_review import WAIT, IdleRuntime, deliver, live_team
from test_runtime import kinds_for, last_text, make_swarm, run_until, shutdown
from test_sessions import _home, _manager

ANNA, BOB, CARL = "Anna Smith", "Bob Brown", "Carl Clark"
DONE, FREE = "subtasks done", "free directions"


def texts_for(swarm, name: str, kind: str) -> list[str]:
    return [r["text"] for r in swarm.db.all("SELECT text FROM notifications WHERE recipient = ? AND kind = ?"
                                             " ORDER BY id", (name, kind))]


def offer_rounds(swarm, rounds: int) -> None:
    """`rounds` passes of _nudge_idle, each after the members read their news, with no wait between offers."""
    for _ in range(rounds):
        deliver(swarm)
        swarm._last_nudge.clear()
        swarm._nudge_idle()


def offered(swarm, name: str, task) -> int:
    """How many n_ready offers to `name` named `task`."""
    return sum(f"#{task.id} {task.title}" in text for text in texts_for(swarm, name, "n_ready"))


def stall(swarm, name: str, *ids: int) -> None:
    """The member's last activation ended with plain text holding `ids` after the nudge, long enough ago."""
    runtime = swarm.runtimes[name]
    runtime.stalled = frozenset(ids)
    runtime.stalled_at = 0.0


def idle_for_a_while(swarm) -> None:
    """Item 3 once agents.idle_nudge_seconds have passed since its clocks started (and since the last activity)."""
    swarm.db.execute("UPDATE agents SET last_active = ?", (time.time() - 10 * WAIT,))
    swarm._idle_holding = {n: (key, since - 2 * WAIT) for n, (key, since) in swarm._idle_holding.items()}
    swarm._wake_idle_holders()


def wakes(swarm, name: str) -> list[str]:
    return [k for k in kinds_for(swarm, name) if k in ("n_stalled", "n_marks_stalled")]


def parent_with_finished_subtasks(swarm, holder: str, status: str = Status.IN_PROGRESS, title: str = "Report"):
    board = swarm.board
    parent = board.create(USER, title, goal="g")
    subs = [board.create(USER, f"part {i}", goal="g", parent_id=parent.id) for i in (1, 2)]
    for sub in subs:
        finish_raw(board, sub.id, Status.DONE, "ok", assignee=BOB)
    return set_raw(board, parent.id, status=status, assignee=holder), subs


# ================================================================== offers (§3 item 1, §4.2 "Offers")

async def test_offers_are_keyed_by_task_and_finish_state(tmp_path):
    """A parent without an assignee is ready while its subtasks are worked on, so everyone was offered it; when its
    subtasks have all finished it is offered afresh to every idle member, those that took its subtasks included."""
    swarm = await live_team(tmp_path, ANNA, BOB, CARL)
    try:
        board = swarm.board
        parent = board.create(USER, "Report", goal="g")
        first = board.create(USER, "part 1", goal="g", parent_id=parent.id)
        second = board.create(USER, "part 2", goal="g", parent_id=parent.id)
        swarm.set_goal(USER, "Ship it")                     # the board is not empty: no planning card
        offer_rounds(swarm, 4)
        assert [offered(swarm, n, parent) for n in (ANNA, BOB, CARL)] == [1, 1, 1]
        assert set(swarm._nudged) == {(parent.id, None), (first.id, None), (second.id, None)}
        finish_raw(board, first.id, Status.DONE, "one", assignee=ANNA)
        offer_rounds(swarm, 4)                               # one subtask still open: the same key, no new offer
        assert [offered(swarm, n, parent) for n in (ANNA, BOB, CARL)] == [1, 1, 1]
        finish_raw(board, second.id, Status.DONE, "two", assignee=BOB)
        offer_rounds(swarm, 4)
        assert [offered(swarm, n, parent) for n in (ANNA, BOB, CARL)] == [2, 2, 2]
        assert swarm._nudged == {(parent.id, "finished"): {ANNA, BOB, CARL}}   # the old keys are dropped
        offer_rounds(swarm, 2)
        assert [offered(swarm, n, parent) for n in (ANNA, BOB, CARL)] == [2, 2, 2]
    finally:
        await shutdown(swarm)


async def test_a_new_finish_state_of_a_crew_task_is_offered_again(tmp_path):
    """An ownerless crew task whose last open direction fails is offered (key "failed"); once the failed direction is
    cancelled its finish state changes ("finished"), and it is offered once more."""
    swarm = await live_team(tmp_path, ANNA, BOB)
    try:
        board = swarm.board
        crew, (parser, cli) = make_crew_raw(board, USER, 2, ["parser", "cli"], title="Tool", goal="g")
        swarm.set_goal(USER, "Ship it")
        finish_raw(board, parser.id, Status.DONE, "parsed", assignee=ANNA)
        finish_raw(board, cli.id, Status.FAILED, "broken", assignee=BOB)
        offer_rounds(swarm, 3)
        assert set(swarm._nudged) == {(crew.id, "failed")}
        finish_raw(board, cli.id, Status.CANCELLED, "broken", assignee=BOB)
        offer_rounds(swarm, 3)
        assert set(swarm._nudged) == {(crew.id, "finished")}
        assert [offered(swarm, n, crew) for n in (ANNA, BOB)] == [2, 2]
    finally:
        await shutdown(swarm)


async def test_the_kickoff_is_everyones_offer_of_the_card(tmp_path):
    swarm = await live_team(tmp_path, ANNA, BOB)
    try:
        swarm.set_goal(USER, "Ship it")
        card = swarm.board.get(1)
        assert "planning" in card.tags
        assert swarm._nudged == {(card.id, None): {ANNA, BOB}}
        offer_rounds(swarm, 3)
        assert "n_ready" not in kinds_for(swarm, ANNA) + kinds_for(swarm, BOB)
    finally:
        await shutdown(swarm)


async def test_free_directions_are_offered_only_while_their_crew_task_takes_them(tmp_path):
    swarm = await live_team(tmp_path, ANNA, BOB, CARL)
    try:
        board = swarm.board
        crew, (parser, cli) = make_crew_raw(board, USER, 2, ["parser", "cli"], title="Tool", goal="g")
        swarm.set_goal(USER, "Ship it")
        set_raw(board, crew.id, status=Status.IN_PROGRESS, assignee=ANNA)
        offer_rounds(swarm, 3)
        assert offered(swarm, BOB, parser) == 1 and offered(swarm, BOB, cli) == 1
        assert offered(swarm, ANNA, parser) == 1    # the owner is offered them like anyone
        # full: the owner and Bob work on it with crew 2, so cli is no longer ready
        set_raw(board, parser.id, status=Status.IN_PROGRESS, assignee=BOB)
        assert [t.id for t in board.ready_unassigned()] == []
        # blocked: a free place again (crew 3), but the crew task waits for another task
        set_raw(board, crew.id, crew=3)
        assert [t.id for t in board.ready_unassigned()] == [cli.id]
        blocker = board.create(USER, "Schema", goal="g", assignee=CARL)
        board.update(USER, crew.id, depends_on=[blocker.id])
        assert [t.id for t in board.ready_unassigned()] == []
        # closed: the crew task is done (raw), its directions are no longer offered
        board.update(USER, crew.id, depends_on=[])
        set_raw(board, crew.id, status=Status.DONE)
        assert [t.id for t in board.ready_unassigned()] == []
        offer_rounds(swarm, 3)
        assert [offered(swarm, n, cli) for n in (ANNA, BOB, CARL)] == [1, 1, 1]   # the first offers only
    finally:
        await shutdown(swarm)


# ================================================================== liveness (§3 items 2-3)

async def test_a_parent_holder_that_stalls_three_times_is_woken_once(tmp_path):
    """Spec test: the holder of a parent whose subtasks are all terminal stalls three times in a row and gets one
    wake for that key, the mark sentence alone (n_marks_stalled); item 3 computes the same key and stays quiet."""
    swarm = await live_team(tmp_path, ANNA, BOB)
    try:
        parent, _ = parent_with_finished_subtasks(swarm, ANNA)
        for _ in range(3):
            stall(swarm, ANNA, parent.id)
            swarm._wake_stalled()
            deliver(swarm)
        assert wakes(swarm, ANNA) == ["n_marks_stalled"]
        [text] = texts_for(swarm, ANNA, "n_marks_stalled")
        ending = prompts.t("en", "owner_prefix_plain") + prompts.t("en", "ending_plain")
        assert text == prompts.t("en", "mark_subtasks_done", id=parent.id, title="Report", ending=ending).lstrip()
        assert swarm._stall_woken[ANNA] == (frozenset({parent.id}), frozenset({(parent.id, DONE)}))
        swarm._wake_idle_holders()
        idle_for_a_while(swarm)
        assert wakes(swarm, ANNA) == ["n_marks_stalled"]
    finally:
        await shutdown(swarm)


async def test_a_waiting_task_without_subtasks_is_never_woken(tmp_path):
    swarm = await live_team(tmp_path, ANNA)
    try:
        task = swarm.board.create(USER, "Docs", goal="g")
        set_raw(swarm.board, task.id, status=Status.WAITING, assignee=ANNA)
        for _ in range(3):
            stall(swarm, ANNA, task.id)
            swarm._wake_stalled()
            swarm._wake_idle_holders()
            idle_for_a_while(swarm)
        assert wakes(swarm, ANNA) == [] and ANNA not in swarm._idle_holding
        # a parent waiting while its subtask is open has no mark either
        parent = swarm.board.create(USER, "Report", goal="g")
        swarm.board.create(USER, "part", goal="g", parent_id=parent.id)
        set_raw(swarm.board, parent.id, status=Status.WAITING, assignee=ANNA)
        swarm._wake_idle_holders()
        idle_for_a_while(swarm)
        assert wakes(swarm, ANNA) == []
    finally:
        await shutdown(swarm)


async def test_a_holder_woken_before_its_subtasks_finished_is_woken_once_more(tmp_path):
    """({N}, {}) and then ({N}, {(N, "subtasks done")}): two keys, two wakes; the mark that appears on the unchanged
    id set waits the full idle clock like a new set."""
    swarm = await live_team(tmp_path, ANNA, BOB)
    try:
        board = swarm.board
        parent = board.create(USER, "Report", goal="g")
        part = board.create(USER, "part", goal="g", parent_id=parent.id)
        set_raw(board, parent.id, status=Status.IN_PROGRESS, assignee=ANNA)
        set_raw(board, part.id, status=Status.IN_PROGRESS, assignee=BOB)
        swarm._wake_idle_holders()
        idle_for_a_while(swarm)
        assert wakes(swarm, ANNA) == ["n_stalled"]
        assert texts_for(swarm, ANNA, "n_stalled") == [prompts.t("en", "n_stalled", tasks=f"#{parent.id}", marks="")]
        deliver(swarm)
        finish_raw(board, part.id, Status.DONE, "written", assignee=BOB)
        swarm._wake_idle_holders()                          # the key changed: the clock starts again
        assert wakes(swarm, ANNA) == ["n_stalled"]
        swarm._wake_idle_holders()
        assert wakes(swarm, ANNA) == ["n_stalled"]
        idle_for_a_while(swarm)
        assert wakes(swarm, ANNA) == ["n_stalled", "n_marks_stalled"]
        deliver(swarm)
        for _ in range(2):                                  # it keeps stalling: the same pair, no more wakes
            stall(swarm, ANNA, parent.id)
            swarm._wake_stalled()
            idle_for_a_while(swarm)
            deliver(swarm)
        assert wakes(swarm, ANNA) == ["n_stalled", "n_marks_stalled"]
    finally:
        await shutdown(swarm)


async def test_a_waiting_parent_is_woken_by_its_mark_after_the_idle_clock(tmp_path):
    """Item 3 wakes also with no stalled id when there is a mark (a `waiting` parent whose subtasks finished), but
    only after agents.idle_nudge_seconds; item 2 needs an in_progress id."""
    swarm = await live_team(tmp_path, ANNA, BOB)
    try:
        parent, _ = parent_with_finished_subtasks(swarm, ANNA, Status.WAITING)
        stall(swarm, ANNA, parent.id)
        swarm._wake_stalled()
        swarm._wake_idle_holders()
        assert wakes(swarm, ANNA) == []
        assert swarm._idle_holding[ANNA][0] == (frozenset(), frozenset({(parent.id, DONE)}))
        idle_for_a_while(swarm)
        assert wakes(swarm, ANNA) == ["n_marks_stalled"]
        assert texts_for(swarm, ANNA, "n_marks_stalled")[0].startswith(f'#{parent.id} "Report": All its subtasks')
    finally:
        await shutdown(swarm)


async def test_unmarked_ids_get_n_stalled_followed_by_the_mark_sentences(tmp_path):
    swarm = await live_team(tmp_path, ANNA, BOB)
    try:
        board = swarm.board
        parent, _ = parent_with_finished_subtasks(swarm, ANNA, Status.WAITING)
        other = board.create(USER, "Other", goal="g")
        set_raw(board, other.id, status=Status.IN_PROGRESS, assignee=ANNA)
        swarm._wake_idle_holders()
        idle_for_a_while(swarm)
        marks = board.mark_text(ANNA, {(parent.id, DONE)})
        assert marks.startswith(f' #{parent.id} "Report": ')
        assert texts_for(swarm, ANNA, "n_stalled") == [prompts.t("en", "n_stalled", tasks=f"#{other.id}", marks=marks)]
        assert "n_marks_stalled" not in kinds_for(swarm, ANNA)
        row = swarm.db.one("SELECT data FROM notifications WHERE recipient = ? AND kind = 'n_stalled'", (ANNA,))
        assert f"#{other.id}" in row["data"] and "All its subtasks" in row["data"]
    finally:
        await shutdown(swarm)


async def test_a_crew_task_with_open_directions_is_not_stalled_and_its_free_direction_is_a_mark(tmp_path):
    """The owner's crew task waits for its directions: not among the ids. While all of them are taken nothing wakes
    the owner; a free one, while it works on none of them, is the "free directions" mark (the sentence alone)."""
    swarm = await live_team(tmp_path, ANNA, BOB, CARL)
    try:
        board = swarm.board
        crew, (parser, cli) = make_crew_raw(board, USER, 3, ["parser", "cli"], title="Tool", goal="g")
        set_raw(board, crew.id, status=Status.IN_PROGRESS, assignee=ANNA)
        set_raw(board, parser.id, status=Status.IN_PROGRESS, assignee=BOB)
        set_raw(board, cli.id, status=Status.IN_PROGRESS, assignee=CARL)
        stall(swarm, ANNA, crew.id)
        swarm._wake_stalled()
        swarm._wake_idle_holders()
        idle_for_a_while(swarm)
        assert wakes(swarm, ANNA) == [] and ANNA not in swarm._idle_holding
        set_raw(board, cli.id, status=Status.TODO, assignee=None)          # Carl let it go
        swarm._wake_idle_holders()
        assert swarm._idle_holding[ANNA][0] == (frozenset(), frozenset({(crew.id, FREE)}))
        idle_for_a_while(swarm)
        assert wakes(swarm, ANNA) == ["n_marks_stalled"]
        expected = prompts.t("en", "mark_free_directions", id=crew.id, directions='#%d "cli"' % cli.id).lstrip()
        assert texts_for(swarm, ANNA, "n_marks_stalled") == [expected]
        # the owner works on a direction of its own: no mark (it takes no free one meanwhile)
        deliver(swarm)
        mine = board.create(USER, "docs", goal="g", parent_id=crew.id)
        set_raw(board, mine.id, status=Status.IN_PROGRESS, assignee=ANNA)
        assert swarm.board.stall_marks(ANNA) == frozenset()
        swarm._wake_idle_holders()
        idle_for_a_while(swarm)
        assert wakes(swarm, ANNA) == ["n_marks_stalled", "n_stalled"]      # its direction itself has stalled
        assert texts_for(swarm, ANNA, "n_stalled") == [prompts.t("en", "n_stalled", tasks=f"#{mine.id}", marks="")]
    finally:
        await shutdown(swarm)


async def test_two_drafts_in_one_wake_share_the_budget(tmp_path):
    swarm = await live_team(tmp_path, ANNA, BOB)
    try:
        board = swarm.board
        swarm.config.agents.tool_output_chars = 4000       # B = 2000: each of the two drafts gets 1000
        crews = []
        for name in ("Alpha", "Beta"):
            crew, subs = make_crew_raw(board, USER, 2, ["one", "two"], title=name, goal="g")
            for sub in subs:
                finish_raw(board, sub.id, Status.DONE, name.lower() + " " + "x" * 2000, assignee=BOB)
            crews.append(set_raw(board, crew.id, status=Status.IN_PROGRESS, assignee=ANNA))
        swarm._wake_idle_holders()
        idle_for_a_while(swarm)
        [text] = texts_for(swarm, ANNA, "n_marks_stalled")
        for crew in crews:
            draft = board._crew_draft(crew, "en", budget=1000)
            assert 500 < len(draft) <= 1000 and draft in text
            assert board._crew_draft(crew, "en") not in text            # not the draft of the whole budget
        assert text.index(f'#{crews[0].id} "Alpha"') < text.index(f'#{crews[1].id} "Beta"')
    finally:
        await shutdown(swarm)


async def test_a_failed_render_leaves_the_key_for_the_next_pass(tmp_path, monkeypatch):
    swarm = await live_team(tmp_path, ANNA, BOB)
    try:
        parent, _ = parent_with_finished_subtasks(swarm, ANNA)
        real = swarm.board.mark_text

        def broken(name, marks):
            raise RuntimeError("render failed")

        monkeypatch.setattr(swarm.board, "mark_text", broken)
        stall(swarm, ANNA, parent.id)
        swarm._wake_stalled()
        swarm._wake_idle_holders()
        idle_for_a_while(swarm)
        assert wakes(swarm, ANNA) == [] and ANNA not in swarm._stall_woken
        monkeypatch.setattr(swarm.board, "mark_text", real)
        idle_for_a_while(swarm)
        assert wakes(swarm, ANNA) == ["n_marks_stalled"] and ANNA in swarm._stall_woken
    finally:
        await shutdown(swarm)


async def test_a_member_in_russian_gets_the_mark_in_russian(tmp_path):
    swarm = await live_team(tmp_path, BOB)
    try:
        await swarm.hire(USER, "Developer", name="Дарья Петрова", language="ru")
        swarm.runtimes["Дарья Петрова"] = IdleRuntime()
        parent, _ = parent_with_finished_subtasks(swarm, "Дарья Петрова", Status.WAITING, title="Отчёт")
        deliver(swarm)
        swarm._wake_idle_holders()
        idle_for_a_while(swarm)
        [text] = texts_for(swarm, "Дарья Петрова", "n_marks_stalled")
        assert text.startswith(f"#{parent.id} «Отчёт»: " + prompts.t("ru", "owner_prefix_plain"))
    finally:
        await shutdown(swarm)


# ================================================================== the user's review reminder (§3 item 7)

async def test_user_is_told_when_a_crew_task_waits_for_a_task_in_their_review(tmp_path):
    """The directions of a crew task blocked by a task in the user's review are unassigned todos without blockers of
    their own, but nobody can take them: the user is told; a free direction of another crew task keeps it back."""
    swarm = await live_team(tmp_path, BOB)
    try:
        board = swarm.board
        impl = board.create(USER, "impl", goal="g")
        swarm.set_goal(USER, "Ship it")
        board.claim(BOB, impl.id)
        assert board.complete(BOB, impl.id, "done").reviewer == USER
        crew, (parser, cli) = make_crew_raw(board, USER, 2, ["parser", "cli"], title="Tool", goal="g",
                                            depends_on=[impl.id])
        set_raw(board, parser.id, assignee=BOB)             # the own part of a blocked crew task waits too
        other, (free,) = make_crew_raw(board, USER, 2, ["free one"], title="Other", goal="g")
        deliver(swarm)
        swarm._remind_user_reviews()
        assert "u_reviews_waiting" not in kinds_for(swarm, USER)      # Bob can take the free direction
        for task in (other, free):
            set_raw(board, task.id, status=Status.CANCELLED)
        swarm._remind_user_reviews()
        swarm._remind_user_reviews()
        assert kinds_for(swarm, USER).count("u_reviews_waiting") == 1
        assert texts_for(swarm, USER, "u_reviews_waiting") == [
            prompts.t("en", "u_reviews_waiting", tasks=f"#{impl.id}")]
    finally:
        await shutdown(swarm)


async def test_a_held_todo_of_an_ordinary_parent_keeps_the_reminder_back(tmp_path):
    swarm = await live_team(tmp_path, BOB)
    try:
        board = swarm.board
        impl = board.create(USER, "impl", goal="g")
        swarm.set_goal(USER, "Ship it")
        board.claim(BOB, impl.id)
        board.complete(BOB, impl.id, "done")
        parent = board.create(USER, "Report", goal="g", depends_on=[impl.id])
        board.create(USER, "part", goal="g", parent_id=parent.id, assignee=BOB)
        deliver(swarm)
        swarm._remind_user_reviews()
        assert "u_reviews_waiting" not in kinds_for(swarm, USER)      # Bob holds a todo nothing blocks
    finally:
        await shutdown(swarm)


async def test_a_held_top_level_todo_keeps_the_reminder_back(tmp_path):
    swarm = await live_team(tmp_path, BOB)
    try:
        board = swarm.board
        impl = board.create(USER, "impl", goal="g")
        swarm.set_goal(USER, "Ship it")
        board.claim(BOB, impl.id)
        board.complete(BOB, impl.id, "done")
        board.create(USER, "tests", goal="g", depends_on=[impl.id])
        side = board.create(USER, "side", goal="g", assignee=BOB)
        deliver(swarm)
        swarm._remind_user_reviews()
        assert "u_reviews_waiting" not in kinds_for(swarm, USER)      # Bob holds work it can go on with
        board.update(USER, side.id, status="cancelled")
        deliver(swarm)
        swarm._remind_user_reviews()
        swarm._remind_user_reviews()
        assert kinds_for(swarm, USER).count("u_reviews_waiting") == 1
    finally:
        await shutdown(swarm)


@pytest.mark.parametrize("status", [Status.TODO, Status.IN_PROGRESS])
async def test_user_is_told_when_a_crew_task_waits_only_for_their_review_of_a_direction(tmp_path, status):
    """The owner holds the crew task (todo, as task_create with assignee leaves it, or claimed): it waits for its
    directions, so neither way does it keep the reminder back while the last one waits for the user's review."""
    swarm = await live_team(tmp_path, ANNA, BOB)
    try:
        board = swarm.board
        crew, (parser, cli) = make_crew_raw(board, ANNA, 2, ["parser", "cli"], title="Tool", goal="g")
        swarm.set_goal(USER, "Ship it")
        set_raw(board, crew.id, status=status, assignee=ANNA)
        finish_raw(board, parser.id, Status.DONE, "parsed", assignee=ANNA)
        finish_raw(board, cli.id, Status.REVIEW, "cli done", assignee=BOB, reviewer=USER)
        deliver(swarm)
        for _ in range(3):
            swarm._wake_stalled()
            idle_for_a_while(swarm)
            swarm._remind_user_reviews()
        assert texts_for(swarm, USER, "u_reviews_waiting") == [prompts.t("en", "u_reviews_waiting",
                                                                         tasks=f"#{cli.id}")]
    finally:
        await shutdown(swarm)


@pytest.mark.parametrize("held", [True, False])
async def test_user_is_told_when_an_ordinary_parent_waits_for_their_review_of_its_subtasks(tmp_path, held):
    """An agent cannot complete a parent with an open subtask: an epic whose subtasks wait for the user's review is
    no work that keeps the swarm going, held in progress or released to todo."""
    swarm = await live_team(tmp_path, BOB)
    try:
        board = swarm.board
        epic = board.create(USER, "epic", goal="g")
        swarm.set_goal(USER, "Ship it")
        board.claim(BOB, epic.id)
        parts = [board.create(BOB, f"part {i}", goal="g", parent_id=epic.id) for i in (1, 2)]
        for part in parts:
            board.claim(BOB, part.id)
            assert board.complete(BOB, part.id, "done").reviewer == USER   # a one-agent team: the user reviews
        assert "has open subtasks" in str(pytest.raises(Exception, board.complete, BOB, epic.id, "all").value)
        if not held:
            board.update(BOB, epic.id, status="todo")
        deliver(swarm)
        swarm._remind_user_reviews()
        swarm._remind_user_reviews()
        assert texts_for(swarm, USER, "u_reviews_waiting") == [
            prompts.t("en", "u_reviews_waiting", tasks=", ".join(f"#{p.id}" for p in parts))]
    finally:
        await shutdown(swarm)


async def test_a_held_direction_its_holder_cannot_take_keeps_no_reminder_back(tmp_path):
    """A direction assigned while its crew task is full waits like the full crew task's free ones."""
    swarm = await live_team(tmp_path, ANNA, BOB, CARL)
    try:
        board = swarm.board
        impl = board.create(USER, "impl", goal="g")
        swarm.set_goal(USER, "Ship it")
        crew, (parser, cli) = make_crew_raw(board, ANNA, 2, ["parser", "cli"], title="Tool", goal="g")
        set_raw(board, crew.id, status=Status.WAITING, assignee=ANNA)
        set_raw(board, parser.id, status=Status.WAITING, assignee=BOB)
        set_raw(board, cli.id, assignee=CARL)                           # todo, assigned: the crew task is full
        finish_raw(board, impl.id, Status.REVIEW, "done", assignee=BOB, reviewer=USER)
        deliver(swarm)
        assert swarm._has_actionable_todo() is False
        swarm._remind_user_reviews()
        assert kinds_for(swarm, USER).count("u_reviews_waiting") == 1
        set_raw(board, parser.id, status=Status.REVIEW, reviewer=USER)  # handed in: Carl can take cli now
        assert swarm._has_actionable_todo() is True
    finally:
        await shutdown(swarm)


async def test_an_owner_that_can_take_a_free_direction_of_its_full_crew_task_keeps_the_reminder_back(tmp_path):
    """The owner's claim takes no place: a free direction of its full crew task is work for it, so the user is not
    told that only their reviews keep the swarm going while the owner has a "free directions" mark."""
    swarm = await live_team(tmp_path, ANNA, BOB, CARL)
    try:
        board = swarm.board
        impl = board.create(USER, "impl", goal="g")
        swarm.set_goal(USER, "Ship it")
        crew, (d1, d2) = make_crew_raw(board, USER, 2, ["d1", "d2"], title="Tool", goal="g")
        set_raw(board, crew.id, status=Status.IN_PROGRESS, assignee=ANNA)
        set_raw(board, d1.id, status=Status.WAITING, assignee=BOB)     # the crew task is full
        finish_raw(board, impl.id, Status.REVIEW, "done", assignee=CARL, reviewer=USER)
        deliver(swarm)
        assert board.stall_marks(ANNA) == frozenset({(crew.id, FREE)})
        assert swarm._has_task_in_progress() is True
        swarm._remind_user_reviews()
        assert "u_reviews_waiting" not in kinds_for(swarm, USER)
    finally:
        await shutdown(swarm)


@pytest.mark.parametrize("status", [Status.CANCELLED, Status.FAILED])
async def test_an_owner_whose_directions_wait_for_a_closed_one_is_woken(tmp_path, status):
    """A crew task whose only open direction waits for a cancelled or failed one is no longer left out of its
    owner's stalled ids: n_stalled wakes the owner once for that key."""
    swarm = await live_team(tmp_path, ANNA, BOB)
    try:
        board = swarm.board
        crew, (p, q, r) = make_crew_raw(board, USER, 3, ["p", "q", "r"], title="Tool", goal="g")
        board.update(USER, r.id, depends_on=[q.id])
        set_raw(board, crew.id, status=Status.IN_PROGRESS, assignee=ANNA)
        set_raw(board, q.id, status=Status.IN_PROGRESS, assignee=BOB)
        finish_raw(board, p.id, Status.DONE, "ok", assignee=BOB)
        assert swarm._stall_ids(ANNA) == frozenset()                       # r waits for q, which is open
        assert swarm.crew_nudge(ANNA, [board.get(crew.id)])[0] == {crew.id}
        finish_raw(board, q.id, status, assignee=BOB)
        assert swarm._stall_ids(ANNA) == frozenset({crew.id})
        assert swarm.crew_nudge(ANNA, [board.get(crew.id)]) == (set(), "")
        swarm._wake_idle_holders()
        idle_for_a_while(swarm)
        assert wakes(swarm, ANNA) == ["n_stalled"]
        assert texts_for(swarm, ANNA, "n_stalled") == [prompts.t("en", "n_stalled", tasks=f"#{crew.id}", marks="")]
        deliver(swarm)
        stall(swarm, ANNA, crew.id)
        swarm._wake_stalled()
        idle_for_a_while(swarm)
        assert wakes(swarm, ANNA) == ["n_stalled"]                         # once per key
    finally:
        await shutdown(swarm)


# ================================================================== liveness of crew owners and assignees (§4.2)

async def test_an_owner_holding_its_crew_task_todo_is_woken_when_its_directions_finished(tmp_path):
    """task_create with assignee=me leaves the crew task todo: the owner that answered n_subtasks_done with plain
    text is woken once by the "subtasks done" mark after the idle clock."""
    swarm = await live_team(tmp_path, ANNA, BOB)
    try:
        board = swarm.board
        swarm.set_goal(USER, "Ship it")
        crew = board.create(ANNA, "Tool", goal="g", crew=2, directions=["parser", "cli"], assignee=ANNA)
        for sub in board.subtasks_of(crew.id):
            finish_raw(board, sub.id, Status.DONE, "ok", assignee=BOB)
        assert board.get(crew.id).status == Status.TODO
        deliver(swarm)
        swarm._wake_idle_holders()
        idle_for_a_while(swarm)
        assert wakes(swarm, ANNA) == ["n_marks_stalled"]
        [text] = texts_for(swarm, ANNA, "n_marks_stalled")
        assert text.startswith(f'#{crew.id} "Tool": ' + prompts.t("en", "owner_prefix_finished"))
        deliver(swarm)
        idle_for_a_while(swarm)
        assert wakes(swarm, ANNA) == ["n_marks_stalled"]                # once per key
    finally:
        await shutdown(swarm)


async def test_a_member_refused_a_direction_of_a_full_crew_task_is_woken_once_a_place_frees(tmp_path):
    swarm = await live_team(tmp_path, ANNA, BOB, CARL)
    try:
        board = swarm.board
        swarm.set_goal(USER, "Ship it")
        crew, (parser, cli) = make_crew_raw(board, ANNA, 2, ["parser", "cli"], title="Tool", goal="g")
        set_raw(board, crew.id, status=Status.IN_PROGRESS, assignee=ANNA)
        set_raw(board, parser.id, status=Status.IN_PROGRESS, assignee=BOB)
        board.update(ANNA, cli.id, assignee=CARL)
        deliver(swarm)
        swarm._wake_idle_holders()
        idle_for_a_while(swarm)
        assert wakes(swarm, CARL) == []                                 # full: nothing it can do yet
        set_raw(board, parser.id, status=Status.REVIEW, reviewer=ANNA)  # Bob handed it in
        swarm._wake_idle_holders()
        assert wakes(swarm, CARL) == []                                 # the idle clock first
        idle_for_a_while(swarm)
        assert texts_for(swarm, CARL, "n_marks_stalled") == [
            prompts.t("en", "mark_direction_to_take", sub=cli.id, title="cli", id=crew.id).lstrip()]
    finally:
        await shutdown(swarm)


# ================================================================== the in-activation nudge (§6.2)

async def test_the_owner_is_nudged_about_free_directions_only(tmp_path):
    """A crew task with open directions is left out of the nudge; while one is free and its owner works on none of
    them, the nudge is the "free directions" sentence, once per activation; with another task in progress the
    sentence follows the usual nudge. `stalled` holds only the other task."""
    def policy(swarm, name, system, messages, tools):
        return "Noted."

    swarm, rec = make_swarm(tmp_path, policy, agents={"idle_nudge_seconds": 60})
    board = swarm.board
    await swarm.start()
    try:
        anna = swarm.members()[0]["name"]
        for name in (BOB, CARL):
            await swarm.hire(USER, "Developer", name=name, kind="external")
        await run_until(lambda: swarm.member(anna)["status"] == AgentStatus.IDLE and not swarm.has_pending(anna))
        crew, (parser, cli) = make_crew_raw(board, USER, 3, ["parser", "cli"], title="Tool", goal="g")
        set_raw(board, crew.id, status=Status.IN_PROGRESS, assignee=anna)
        set_raw(board, parser.id, status=Status.IN_PROGRESS, assignee=BOB)
        set_raw(board, cli.id, status=Status.IN_PROGRESS, assignee=CARL)
        runtime = swarm.runtimes[anna]

        async def activation(text: str) -> list[list[dict]]:
            before = rec.count(anna)
            swarm.mail.send(USER, anna, text)
            await run_until(lambda: rec.count(anna) > before and swarm.member(anna)["status"] == AgentStatus.IDLE
                            and not swarm.has_pending(anna))
            return rec.calls[anna][before:]

        calls = await activation("status?")
        assert len(calls) == 1 and runtime.stalled == frozenset()      # all directions taken: no nudge
        # a crew task the owner parked in `waiting`, with a free direction: never in the nudge
        parked, _ = make_crew_raw(board, USER, 3, ["later"], title="Parked", goal="g")
        set_raw(board, parked.id, status=Status.WAITING, assignee=anna)
        set_raw(board, cli.id, status=Status.TODO, assignee=None)
        sentence = board.mark_text(anna, {(crew.id, FREE)})
        assert sentence.startswith(f" #{crew.id} is a crew task with free directions")
        calls = await activation("and now?")
        assert len(calls) == 2 and last_text(calls[1]) == sentence.lstrip()
        assert runtime.stalled == frozenset()
        other = board.create(USER, "Other", goal="g")
        set_raw(board, other.id, status=Status.IN_PROGRESS, assignee=anna)
        calls = await activation("and now?")
        assert len(calls) == 2
        assert last_text(calls[1]) == prompts.t("en", "nudge", tasks=f"#{other.id}") + sentence
        assert runtime.stalled == frozenset({other.id})
        assert not rec.violations, rec.violations
    finally:
        await shutdown(swarm)


# ================================================================== seams (settings, snapshot, stats, export)

async def test_reconfigure_applies_crew_review_at_once(tmp_path):
    swarm = await live_team(tmp_path)
    try:
        assert swarm.board.crew_review is True
        config = dataclasses.replace(swarm.config, swarm=dataclasses.replace(swarm.config.swarm, crew_review=False))
        swarm.reconfigure(config)
        assert swarm.board.crew_review is False
    finally:
        await shutdown(swarm)


async def test_snapshot_and_events_carry_crew_and_its_working_set(tmp_path):
    swarm = await live_team(tmp_path, ANNA, BOB)
    try:
        board = swarm.board
        crew, (parser, cli) = make_crew_raw(board, USER, 3, ["parser", "cli"], title="Tool", goal="g")
        set_raw(board, crew.id, status=Status.IN_PROGRESS, assignee=ANNA)
        set_raw(board, parser.id, status=Status.IN_PROGRESS, assignee=BOB)
        tasks = {t["id"]: t for t in swarm.snapshot()["tasks"]}
        assert (tasks[crew.id]["crew"], tasks[crew.id]["crew_working"]) == (3, [ANNA, BOB])
        assert (tasks[parser.id]["crew"], tasks[parser.id]["crew_working"]) == (0, [])
        board.update(USER, cli.id, title="cli tool")
        event = swarm.bus.recent(types=("task.updated",), limit=1)[-1]
        assert {"crew", "crew_working"} <= set(event["data"]["task"])
        created = swarm.bus.recent(types=("task.created",), limit=1)[-1]
        assert {"crew", "crew_working"} <= set(created["data"]["task"])
        assert "crew 2/3" in board.get(crew.id).short()
    finally:
        await shutdown(swarm)


def test_settings_view_carries_crew_review(tmp_path):
    view = st.public_view({})
    assert view["swarm"]["crew_review"] is True
    assert st.public_view({"swarm": {"crew_review": False}})["swarm"]["crew_review"] is False


async def test_a_session_patch_sets_crew_review(tmp_path):
    manager = _manager(_home(tmp_path))
    try:
        sid = (await manager.create("Crew", start=False))["id"]
        assert manager.config_view(sid)["swarm"]["crew_review"] is True
        summary = manager.update(sid, settings={"swarm": {"crew_review": False}})
        assert summary["overrides"]["swarm"] == {"crew_review": False}
        assert manager.config_view(sid)["swarm"]["crew_review"] is False
    finally:
        await manager.shutdown()


async def test_session_statistics_count_directions_as_tasks(tmp_path):
    swarm = await live_team(tmp_path, ANNA)
    path = swarm.db.path
    board = swarm.board
    crew, (parser, _cli) = make_crew_raw(board, USER, 2, ["parser", "cli"], title="Tool", goal="g")
    finish_raw(board, parser.id, Status.DONE, "parsed", assignee=ANNA)
    await shutdown(swarm)
    stats = read_stats(path)
    assert stats["tasks"]["total"] == 3 and stats["tasks"]["done"] == 1 and stats["earlier_version"] is False


async def test_an_export_keeps_the_crew_column(tmp_path):
    home = _home(tmp_path)
    manager = _manager(home)
    try:
        sid = (await manager.create("Crew", start=False))["id"]
    finally:
        await manager.shutdown()
    db = Database(home / "sessions" / sid / ".dreamteam" / "swarm.db")
    try:
        db.execute("INSERT INTO tasks(title, status, created_by, created_at, updated_at, crew)"
                   " VALUES ('Tool', 'todo', 'user', 1.0, 1.0, 3)")
        frozen.write(db, "alice", frozen.current_record("en"))
    finally:
        db.close()
    dest = tmp_path / "out"
    assert export_home(home, dest).ok
    copy = sqlite3.connect(dest / "sessions" / sid / ".dreamteam" / "swarm.db")
    try:
        assert copy.execute("SELECT title, crew FROM tasks").fetchall() == [("Tool", 3)]
    finally:
        copy.close()
